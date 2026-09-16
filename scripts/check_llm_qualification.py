#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型合格性实测（M11 Task 5）：把"这个模型能不能用"变成**退出码**。

退出码：
    0 = 合格（全部判据满足）
    1 = 有不合格项（逐项打印实测值与阈值）
    2 = 配置缺失（LLM 三项未填全）—— 不是"模型不合格"，是"没得测"

## ⚠️ 本脚本测的是什么、不是什么

**直接调判定钩子**（`make_llm_judge` 产出的 `judge(spec, text)`），而不是跑整条
管线。`judge` 已内含**证据反向核验**（`_quote_is_in_text`），因此它单独就能
回答"这个模型的逐字摘录能力够不够" —— 那是模型选型唯一要回答的问题。
整条管线还要 PDF、OCR、字段提取，那些变量会掩盖模型本身的质量。

**代价**：绕过了 `applies_when` 的适用性判断（那是评价引擎的职责）。
本报告**不是**全景回归，不要当"业务正确性验收"来读。

## 输出纪律

报告全部用 ASCII 标记（`OK` / `NG` / `--`），**不用 emoji** ——
`verify_m5.py` 实测过：Windows GBK 终端打印 `⚠️` 会让脚本当场中止。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 直接以 `python scripts/check_llm_qualification.py` 运行时 sys.path[0] 是
# scripts/，因此显式加入项目根（与 run_worker / run_migrate 同一约定）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.enums import ReasonCode  # noqa: E402
from app.rules.evaluator import RuleSpec  # noqa: E402
from app.rules.matching import MatchResult  # noqa: E402


@dataclass(frozen=True)
class Sample:
    name: str
    text: str
    #: 这份正文里**确实存在**的风险 → 必须判成 matched。空的表示"干净合同"。
    must_match: tuple[str, ...] = ()
    #: 对这些规则判 `undecidable` 属于**设计内行为**，不计入可判定比例：
    #: - 主题缺失（正文没有知识产权/验收/管辖条款）—— 模型对"没提"诚实地说
    #:   判不了，结论转 `needs_review` 交人工，正是系统的设计输出；
    #: - 信息不可得（管辖地是否不利取决于合同签订地在哪，文本不含此信息）。
    #: ⚠️ 只有这两类可以豁免；"正文里明明写着却判不了"仍算不合格。
    may_be_undecidable: tuple[str, ...] = ()


SAMPLES: tuple[Sample, ...] = (
    Sample(
        name="一方独担违约责任",
        text=(
            "甲方与乙方就软件开发事宜达成如下协议。"
            "第三条 违约责任：本合同履行过程中产生的一切违约责任均由甲方承担，"
            "甲方应向乙方支付合同总价百分之三十的违约金；乙方在任何情形下均不承担违约责任。"
            "第四条 保密：双方对在合作中知悉的对方商业秘密承担保密义务，"
            "保密期限为本合同终止后三年。"
            "第五条 争议解决：双方协商不成的，提交合同签订地人民法院诉讼解决。"
        ),
        must_match=("LIAB_UNEQUAL_AGAINST_PARTY_A",),
        may_be_undecidable=(
            "JURIS_UNFAVOR_FOR_PARTY_A",   # 管辖地是否不利取决于签订地（文本无此信息）
            "JURIS_UNFAVOR_FOR_PARTY_B",
            "IP_TRANSFER_AWAY_FROM_PARTY_A",  # 样本无知识产权条款（主题缺失）
            "IP_TRANSFER_AWAY_FROM_PARTY_B",
            "ACC_VAGUE",                      # 样本无验收条款（主题缺失）
        ),
    ),
    Sample(
        name="保密义务单方承担",
        text=(
            "甲方与乙方就数据处理服务事宜达成如下协议。"
            "第三条 保密：乙方应就本合同项下全部信息承担无限期保密义务，"
            "未经甲方书面同意不得向任何第三方披露；甲方对乙方提供的资料不承担任何保密义务。"
            "第四条 违约责任：双方按各自过错程度承担相应责任。"
        ),
        must_match=("CONF_UNILATERAL_AGAINST_PARTY_B",),
        may_be_undecidable=(
            "JURIS_UNFAVOR_FOR_PARTY_A",   # 样本无争议解决条款（主题缺失）
            "JURIS_UNFAVOR_FOR_PARTY_B",
            "IP_TRANSFER_AWAY_FROM_PARTY_A",
            "IP_TRANSFER_AWAY_FROM_PARTY_B",
            "ACC_VAGUE",                   # 样本无验收条款（主题缺失）
        ),
    ),
    Sample(
        name="条款均衡（假阳性探测）",
        text=(
            "甲方与乙方就办公用品采购事宜达成如下协议。"
            "第三条 违约责任：任何一方违反本合同约定的，应按实际损失向对方承担赔偿责任。"
            "第四条 保密：双方对在合作中知悉的对方商业秘密承担保密义务。"
            "第五条 验收：交付后三十日内完成验收，验收标准以双方确认的技术参数表为准。"
            "第六条 争议解决：双方协商不成的，提交合同签订地人民法院诉讼解决。"
        ),
        must_match=(),
        may_be_undecidable=(
            "JURIS_UNFAVOR_FOR_PARTY_A",   # 签订地未知（信息不可得）
            "IP_TRANSFER_AWAY_FROM_PARTY_A",  # 样本无知识产权条款（主题缺失）
            "IP_TRANSFER_AWAY_FROM_PARTY_B",
        ),
    ),
)

#: 合格线（**刻意分开三条**，因为它们的失败原因完全不同）
MAX_UNAVAILABLE = 0        # 模型连合法 JSON 都给不出 → 端点/连通性/格式约束问题
MAX_FABRICATED = 1         # 引用在正文里找不到 → **模型的逐字摘录能力**不足
MIN_DECIDABLE_RATIO = 0.85  # 判出明确结论的比例


@dataclass(frozen=True)
class Row:
    sample: str
    rule: str
    result: MatchResult
    seconds: float


def judgeable_specs(session) -> tuple[RuleSpec, ...]:
    """9 条 `llm` 规则的已解析规格。

    `is_llm` 用 `RuleSpec` 自身那个属性，而不是在这里比 `match_mode` 字符串
    —— 后者在 `MatchMode` 取值变化时会静默变小（少判几条规则而看不出来）。
    """
    from app.services.rule_service import load_active_rules, specs_by_code

    specs = specs_by_code(load_active_rules(session))
    return tuple(spec for spec in specs.values() if spec.is_llm)


def run_checks(
    judge: Callable[[RuleSpec, str], MatchResult],
    specs: tuple[RuleSpec, ...],
) -> tuple[Row, ...]:
    rows: list[Row] = []
    for sample in SAMPLES:
        for spec in specs:
            started = time.perf_counter()
            result = judge(spec, sample.text)
            rows.append(
                Row(sample.name, spec.rule_code, result, time.perf_counter() - started)
            )
    return tuple(rows)


def counts(rows: tuple[Row, ...]) -> dict[str, int]:
    """三条判据的**原始计数** —— 阈值在 `decide()` 里比，测试直接断言计数。"""
    return {
        "total": len(rows),
        "unavailable": sum(
            r.result.reason_code is ReasonCode.MODEL_UNAVAILABLE for r in rows
        ),
        "fabricated": sum(
            r.result.detail.get("verdict_discarded")
            in {"evidence_not_found", "matched_without_evidence"}
            for r in rows
        ),
        "decidable": sum(r.result.decidable for r in rows),
    }


def missed_must_match(rows: tuple[Row, ...]) -> list[tuple[str, str]]:
    """正文里**确实存在**却没判出来的风险 —— 假阴性。"""
    return [
        (sample.name, code)
        for sample in SAMPLES
        for code in sample.must_match
        if not any(
            row.sample == sample.name and row.rule == code and row.result.matched
            for row in rows
        )
    ]


def _allowed_undecidable() -> set[tuple[str, str]]:
    """各样本声明"判不了属设计内"的 (样本, 规则) 对。"""
    return {
        (sample.name, code)
        for sample in SAMPLES
        for code in sample.may_be_undecidable
    }


def decidability_ratio(rows: tuple[Row, ...]) -> tuple[float, int, int]:
    """可判定比例，**只统计文本可判的行**。

    主题缺失 / 信息不可得的行（样本显式声明豁免）不计入分母 ——
    对它们判 `needs_review` 转人工，是系统的设计输出而不是模型缺陷。
    返回 `(比例, 明确结论数, 计入分母的行数)`。
    """
    allowed = _allowed_undecidable()
    counted = [
        row for row in rows if (row.sample, row.rule) not in allowed
    ]
    decided = sum(row.result.decidable for row in counted)
    ratio = decided / len(counted) if counted else 0.0
    return ratio, decided, len(counted)


def decide(rows: tuple[Row, ...]) -> tuple[bool, list[str]]:
    """按三条阈值 + 假阴性清单判定。返回 `(是否合格, 不合格原因列表)`。"""
    counts_ = counts(rows)
    reasons: list[str] = []

    if counts_["unavailable"] > MAX_UNAVAILABLE:
        reasons.append(
            f"MODEL_UNAVAILABLE {counts_['unavailable']} 次（阈值 = {MAX_UNAVAILABLE}）"
            "—— 端点/连通性/JSON 格式约束问题"
        )
    if counts_["fabricated"] > MAX_FABRICATED:
        reasons.append(
            f"证据引用被作废 {counts_['fabricated']} 次（阈值 <= {MAX_FABRICATED}）"
            "—— 模型的逐字摘录能力不足"
        )
    ratio, decided, denominator = decidability_ratio(rows)
    if ratio < MIN_DECIDABLE_RATIO:
        reasons.append(
            f"明确结论比例 {decided}/{denominator} = {ratio:.0%}"
            f"（阈值 >= {MIN_DECIDABLE_RATIO:.0%}）"
        )
    for sample_name, code in missed_must_match(rows):
        reasons.append(f"判不出：样本「{sample_name}」规则 {code}")
    return not reasons, reasons


def _report(
    model_version: str, rows: tuple[Row, ...], reasons: list[str]
) -> None:
    counts_ = counts(rows)
    seconds = [row.seconds for row in rows]
    must_total = sum(len(sample.must_match) for sample in SAMPLES)
    must_hit = must_total - len(missed_must_match(rows))
    ratio, decided, denominator = decidability_ratio(rows)
    print("-" * 68)
    print(f"model_version         : {model_version}")
    print(
        f"调用总数              : {counts_['total']}"
        f"        ({len(SAMPLES)} 份样本 x {counts_['total'] // len(SAMPLES)} 条 llm 规则)"
    )
    print(
        f"明确结论              : {decided}/{denominator}"
        f"     (阈值 >= {MIN_DECIDABLE_RATIO:.0%}；"
        f"主题缺失/信息不可得的 {counts_['total'] - denominator} 次不计入)"
    )
    print(f"MODEL_UNAVAILABLE     : {counts_['unavailable']}         (阈值 = {MAX_UNAVAILABLE})")
    print(f"证据引用被作废        : {counts_['fabricated']}         (阈值 <= {MAX_FABRICATED})")
    print(f"必判命中项            : {must_hit}/{must_total}")
    if seconds:
        print(
            f"单次耗时              : p50 {statistics.median(seconds):.1f}s"
            f" / max {max(seconds):.1f}s / 合计 {sum(seconds):.1f}s"
        )
    print("-" * 68)
    for reason in reasons:
        print(f"[NG] {reason}")
    if not reasons:
        print("[OK] 全部判据满足 —— 该模型可用于 9 条 llm 规则")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模型合格性实测（退出码判合格）")
    args = parser.parse_args(argv)

    if not settings.llm_enabled:
        print(
            "[配置缺失] LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三项未填全 ——"
            "没有可实测的模型（这不是'不合格'，是'没得测'）",
            file=sys.stderr,
        )
        return 2

    from app.composition.llm_pipeline import build_llm_gateway, build_llm_judge
    from app.db import SessionLocal
    from app.ports.llm_gateway import model_version_of

    gateway = build_llm_gateway(settings)
    judge = build_llm_judge(settings)
    model_version = model_version_of(gateway)
    if gateway is None or judge is None:  # pragma: no cover - 与 llm_enabled 矛盾
        print("[配置缺失] 网关构造失败", file=sys.stderr)
        return 2

    session = SessionLocal()
    try:
        specs = judgeable_specs(session)
    finally:
        session.close()
    if not specs:
        print(
            "[数据缺失] 库里没有 `llm` 模式的规则 —— 先执行 "
            "`python scripts/init_db.py --reset` 灌入 40 条种子规则",
            file=sys.stderr,
        )
        return 2

    rows = run_checks(judge, specs)
    ok, reasons = decide(rows)
    _report(model_version, rows, reasons)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
