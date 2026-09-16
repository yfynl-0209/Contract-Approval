#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M5 验收证据（逐条实测值 + 退出码）。

沿用 `verify_m3.py` / `verify_m4.py` 的形态：**每条验收单独起一次 pytest**，
采集它的实测结果，最后打印一张"逐条证据表"并用退出码表达结论。

## 五条约定（第 5 轮评审补齐前两条）

1. **pytest 的退出码必须参与判定**。`--tb=no` 下收集错误（ImportError 等）
   不会产生任何 `PASSED` 行，退出码却是 1 —— 若只数行，验收会**假绿**；
2. **节点解析必须容忍参数化里的空格**：`test_x[abc 123]` 的节点含空格，
   用 `\S+` 抓节点会让这类用例永远匹配不上（"991 passed" 与逐条计数对不上
   的根因就是这个）；
3. **`SKIPPED` 也算未通过** —— 跳过却记成通过，报告里就出现一个
   **未经检验的绿点**；
4. **引用必须是精确节点，不是整个文件** —— 文件通过不能证明
   "该验收项有专属断言"，删掉专属断言后该验收项仍会显示绿色；
5. **`UNMET` 计入退出码 1**。

## ⚠️ 本脚本的输出只用可打印字符

初版在打印 `⚠️`（U+26A0）时于 Windows 默认 GBK 终端抛 `UnicodeEncodeError`，
**脚本当场中止** —— 后 29 条一条都没跑。因此标记一律 ASCII，
且入口把 stdout 的错误处理设为 `replace`：**验收脚本不允许因为输出不出去而中止**。

用法：
    python scripts/verify_m5.py [--verbose]

退出码：0 全部通过；1 存在未通过或未满足的条目。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

EXIT_OK = 0
EXIT_PROBLEM = 1

#: `pytest -v` 的行。⚠️ 节点部分用**非贪婪** `.+?` 而不是 `\S+`：
#: 参数化节点里可以有**空格**（`test_x[abc 123]`），
#: `\S+` 会让这类行匹配不上 —— 于是它们从逐条证据里凭空消失。
_RESULT_LINE = re.compile(
    r"^(?P<node>.+?)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|\[|$)"
)

#: pytest 的"什么都没收集到"退出码
_EXIT_NO_TESTS = 5


@dataclass(frozen=True)
class Criterion:
    number: str
    title: str
    tests: tuple[str, ...]
    note: str | None = None
    #: 整目录引用（"既有测试全绿"）：只关心**有没有失败**，
    #: 已存在的跳过项由逐条验收各自计数，不在这里重复判罪
    allow_skipped: bool = False
    #: 需求尚未落地（不是缺陷）—— 显式记录，且**计入退出码 1**
    unmet: bool = False


@dataclass
class Finding:
    criterion: Criterion
    outcome: str  # PASSED / FAILED / SKIPPED / MISSING
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome == "PASSED" and not self.criterion.unmet


CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "1",
        "40 条规则各产生且仅产生一条评价（四态计数之和 == 规则总数）",
        tests=(
            "tests/test_rule_service.py::test_run_batch_evaluates_every_active_rule_once",
            "tests/test_rule_aggregator.py::test_four_counts_sum_to_the_total",
        ),
    ),
    Criterion(
        "2",
        "高风险合同总风险 = 高（四条同时断言）",
        tests=(
            "tests/test_rule_service.py::test_full_batch_on_the_prepay_contract_is_high_risk",
        ),
        note=(
            "批次级：真实 40 条规则跑 HT-2026-0002。证据按语义断言"
            "（预付款语境 + 比例事实 + 可定位区间），不绑定 60%/百分之六十 这类具体写法——"
            "写法取决于合同怎么印，不取决于契约。主证据优先取完整句子（条款），"
            "摘要行仍保留在 evidence_json 里"
        ),
    ),
    Criterion(
        "3",
        "无模型时按 fallback 降级",
        tests=(
            "tests/test_rule_evaluator.py::test_llm_without_a_model_uses_the_rule_fallback",
            "tests/test_rule_evaluator.py::test_llm_without_a_model_and_without_fallback_is_model_unavailable",
        ),
    ),
    Criterion(
        "4",
        "IP_MISSING 适用性（命中 / not_applicable）",
        tests=(
            "tests/test_rule_matching.py::test_absent_rule_does_not_hit_when_the_clause_exists",
            "tests/test_rule_evaluator.py::test_not_applicable_is_not_upgraded_to_needs_review",
        ),
    ),
    Criterion(
        "5",
        "立场冲突只影响依赖立场的规则",
        tests=(
            "tests/test_rule_evaluator.py::test_conflicting_party_context_makes_dependent_rules_undecidable",
            "tests/test_rule_config.py::test_contract_type_conflict_only_affects_type_dependent_rules",
        ),
    ),
    Criterion(
        "6",
        "阈值类规则遇 not_found 必须 needs_review",
        tests=(
            "tests/test_rule_evaluator.py::test_expr_not_found_is_needs_review_not_not_hit",
            "tests/test_rule_matching.py::test_not_found_is_undecidable_for_every_numeric_op",
        ),
    ),
    Criterion(
        "7",
        "缺失类规则遇 uncertain/failed 不得报缺失",
        tests=(
            "tests/test_rule_matching.py::test_existence_ops_are_decided_by_status",
            "tests/test_rule_evaluator.py::test_field_absent_from_the_contract_is_needs_review",
        ),
    ),
    Criterion(
        "8",
        "币种不可比不参与比较",
        tests=(
            "tests/test_rule_matching.py::test_money_comparison_requires_a_known_currency",
            "tests/test_rule_matching.py::test_money_comparison_rejects_a_foreign_currency",
        ),
    ),
    Criterion(
        "9",
        "hit_detail_json 记录计算过程",
        tests=(
            "tests/test_rule_matching.py::test_numeric_detail_records_the_comparison_as_strings",
        ),
    ),
    Criterion(
        "10",
        "证据反向匹配（命中片段能定位）",
        tests=(
            "tests/test_rule_evidence.py::test_keyword_hit_gets_locatable_evidence",
            "tests/test_rule_evidence.py::test_unlocatable_hit_is_downgraded_to_needs_review",
        ),
    ),
    Criterion(
        "11",
        "批次绑定六项版本",
        tests=("tests/test_rule_service.py::test_get_run_returns_the_recomputed_aggregate",),
    ),
    Criterion(
        "12",
        "ruleset_version 是确定性摘要（改 rule_version 必变值）",
        tests=(
            "tests/test_rule_service.py::test_ruleset_version_is_stable_and_order_independent",
            "tests/test_rule_service.py::test_ruleset_version_changes_when_a_rule_version_changes",
        ),
    ),
    Criterion(
        "13",
        "批次幂等：六项全同才复用（逐项遍历）",
        tests=(
            "tests/test_rule_service.py::test_six_identical_inputs_reuse_the_same_run",
            "tests/test_rule_service.py::test_changing_any_single_input_creates_a_new_run",
            "tests/test_rule_service.py::test_changing_the_context_creates_a_new_run",
            "tests/test_rule_service.py::test_changing_the_ruleset_creates_a_new_run",
        ),
    ),
    Criterion(
        "14",
        "needs_review 不提高总风险，但使结论不完整",
        tests=(
            "tests/test_rule_aggregator.py::test_needs_review_does_not_raise_the_risk_but_marks_incomplete",
        ),
    ),
    Criterion(
        "15",
        "未命中不加风险且可见",
        tests=("tests/test_rule_service.py::test_all_four_states_are_persisted",),
    ),
    Criterion(
        "16",
        "工具 5 可查询且不阻塞",
        tests=("tests/test_rule_service.py::test_request_rule_run_enqueues_a_pollable_job",),
    ),
    Criterion(
        "17",
        "regex 模式真的被执行",
        tests=(
            "tests/test_rule_matching.py::test_regex_matches_a_fragment_not_the_whole_string",
            "tests/test_rule_matching.py::test_regex_miss_and_missing_text",
        ),
    ),
    Criterion(
        "18",
        "关键词否定词表生效",
        tests=(
            "tests/test_rule_matching.py::test_exclude_phrase_suppresses_a_hit",
            "tests/test_rule_evaluator.py::test_exclude_text_is_applied_by_the_engine",
        ),
    ),
    Criterion(
        "19",
        "无 GPU 全链路可跑（基线合同总风险 = low）",
        tests=(
            "tests/test_rule_service.py::test_full_batch_on_a_baseline_contract_is_low_risk",
            "tests/test_rule_service.py::test_the_worker_claims_and_executes_a_rule_job",
        ),
        note=(
            "原先只映射到 worker 链路测试 —— 那条测的是链路通不通，与基线的总风险是两件事。"
            "映射错了不会报错，只会让报告写着已覆盖而实际没有"
        ),
    ),
    Criterion(
        "20",
        "新原因码是 ReasonCode 而非 ErrorCode",
        tests=(
            "tests/test_m5_groundwork.py::test_new_reason_codes_are_not_error_codes",
        ),
    ),
    Criterion(
        "21",
        "既有测试全绿（整个 tests/ 目录）",
        tests=("tests/",),
        allow_skipped=True,
        note=(
            "原先只跑两个文件 —— 那不能代表全项目。跳过项（慢 OCR 等）由逐条验收各自计数，"
            "本项只关心有没有失败"
        ),
    ),
    Criterion(
        "22",
        "M5 不写 review_results（边界断言）",
        tests=(
            "tests/test_rule_service.py::test_complete_run_marks_completed_and_writes_no_results",
        ),
    ),
    Criterion(
        "23",
        "强制重跑可用（force）",
        tests=("tests/test_rule_service.py::test_force_creates_a_new_run_even_when_identical",),
    ),
    Criterion(
        "24",
        "甲方违约金比例命中",
        tests=("tests/test_rule_fact_resolver.py::test_party_a_only_wording_does_not_leak_to_party_b",),
    ),
    Criterion(
        "25",
        "乙方违约金比例命中",
        tests=("tests/test_rule_fact_resolver.py::test_party_b_only_wording_does_not_leak_to_party_a",),
    ),
    Criterion(
        "26",
        "普通付款进度不得误判为预付款",
        tests=("tests/test_rule_fact_resolver.py::test_payment_milestones_are_not_prepay",),
    ),
    Criterion(
        "27",
        "多个冲突比例必须 uncertain",
        tests=("tests/test_rule_fact_resolver.py::test_conflicting_ratios_yield_uncertain_with_both_evidences",),
    ),
    Criterion(
        "28",
        "每个启用的 expr.field 必须有运行期事实生产者",
        tests=(
            "tests/test_rule_fact_resolver.py::test_every_expr_field_has_a_runtime_producer",
            "tests/test_rule_fact_resolver.py::test_the_real_chain_produces_every_expr_field",
        ),
    ),
    Criterion(
        "29",
        "入队到执行之间规则变了，仍评价原批次",
        tests=("tests/test_rule_service.py::test_execute_existing_run_uses_the_frozen_ruleset",),
    ),
    Criterion(
        "30",
        "重复请求命中运行中批次时状态取自作业",
        tests=("tests/test_rule_service.py::test_repeat_enqueue_reports_the_real_job_status",),
    ),
    Criterion(
        "31",
        "工具 5 的响应可轮询到结论（全链路）",
        tests=("tests/test_rule_service.py::test_the_full_chain_keeps_the_same_run_id",),
    ),
)


def _subprocess_env() -> dict[str, str]:
    """子进程环境：**剥掉 PYTHONPATH**。

    IDE 会把自家的 `sitecustomize.py` 钩子经 PYTHONPATH 注入每个 Python 进程，
    而它在解释器退出时抛 `SystemExit(1)` —— pytest 于是把**最后一个用例**
    记成收尾 ERROR，尽管测试本体是绿的。这正是验收 31 那个"时红时绿"的根因：
    它永远排在最后，永远离解释器退出最近。

    验收子进程不需要继承它：这里跑的是本项目自己的测试，
    依赖由 venv 与项目根目录提供。
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def run_node(node: str) -> tuple[str, str]:
    """跑一个引用，返回 `(结论, 实测说明)`。**每条单独起一次 pytest**。"""
    completed = subprocess.run(
        [PYTHON, "-m", "pytest", node, "-v", "--tb=no", "-p", "no:cacheprovider"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_subprocess_env(),
    )
    outcomes: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        match = _RESULT_LINE.match(line.strip())
        if match:
            outcomes[match.group("node")] = match.group("outcome")

    if not outcomes:
        if completed.returncode == _EXIT_NO_TESTS:
            return "MISSING", "pytest 没有收集到任何用例（退出码 5）"
        return (
            "MISSING",
            f"没有采集到任何用例结果（pytest 退出码 {completed.returncode}）",
        )

    if node.endswith("/"):
        wanted = list(outcomes.values())  # 整目录引用：全部用例
    else:
        wanted = [
            value
            for node_id, value in outcomes.items()
            if node_id == node
            or node_id.startswith(node + "[")
            or node_id.startswith(node + "::")
        ]
    if not wanted:
        return "MISSING", f"引用 {node} 没有匹配到用例"

    failures = sum(1 for item in wanted if item in {"FAILED", "ERROR"})
    if failures:
        # 首跑用 `--tb=no`（快，且别的失败不受干扰）—— 但那也意味着看不到原因。
        # 因此失败时补跑一次带 `--tb=short` 的，把回溯收进报告并全文落盘。
        rerun = subprocess.run(
            [PYTHON, "-m", "pytest", node, "-v", "--tb=short", "-p", "no:cacheprovider"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_env(),
        )
        tail = [
            line.strip()
            for line in rerun.stdout.splitlines()
            if line.startswith("E ") or line.startswith(">")
        ][:8]
        (PROJECT_ROOT / "verify_rerun_last.log").write_text(
            rerun.stdout + "\n=== stderr ===\n" + rerun.stderr, encoding="utf-8"
        )
        reason = " | ".join(tail) if tail else "（补跑未捕获到回溯）"
        reason += "；完整回溯见 verify_rerun_last.log"
        return "FAILED", f"{failures} 条失败（共 {len(wanted)} 条）：{reason}"

    skipped = sum(1 for item in wanted if item == "SKIPPED")
    if skipped and not _criterion_of(node).allow_skipped:
        return "SKIPPED", f"{skipped} 条被跳过 —— 跳过不算通过"

    # ⚠️ **退出码必须参与判定**（评审 P1）：`--tb=no` 下收集错误、内部错误
    # 或"没被 `PASSED|FAILED|…` 词表覆盖"的失败，都可能一行都不产生、
    # 或只产生几条 PASSED 而退出码是 1 —— 只数行的话验收会**假绿**。
    if completed.returncode != 0:
        stderr_tail = [
            line.strip() for line in completed.stderr.splitlines() if line.strip()
        ][-3:]
        hint = " | ".join(stderr_tail) if stderr_tail else "（stderr 为空）"
        return (
            "FAILED",
            f"{len(wanted)} 条通过，但 pytest 退出码为 {completed.returncode}（0 才算干净）：{hint}",
        )

    return "PASSED", f"{len(wanted)} 条通过" + (f"（跳过 {skipped} 条）" if skipped else "")


def _criterion_of(node: str) -> Criterion:
    for criterion in CRITERIA:
        if node in criterion.tests:
            return criterion
    return Criterion("?", "?", ())


def collect() -> list[Finding]:
    findings: list[Finding] = []
    for criterion in CRITERIA:
        outcomes: list[str] = []
        details: list[str] = []
        for node in criterion.tests:
            outcome, detail = run_node(node)
            outcomes.append(outcome)
            details.append(f"{node} -> {outcome}（{detail}）")

        if "FAILED" in outcomes:
            overall = "FAILED"
        elif "MISSING" in outcomes:
            overall = "MISSING"
        elif "SKIPPED" in outcomes:
            overall = "SKIPPED"
        else:
            overall = "PASSED"

        findings.append(Finding(criterion, overall, "；".join(details)))
    return findings


def report(findings: list[Finding], *, verbose: bool) -> int:
    passed = sum(1 for item in findings if item.ok)
    total = len(findings)

    print("\n[M5 acceptance evidence]")
    for item in findings:
        mark = "[OK]  " if item.ok else "[FAIL]"
        print(f"  {mark} {item.criterion.number:>2}. {item.criterion.title}")
        if verbose or not item.ok:
            print(f"        measured: {item.outcome} - {item.detail}")
        if item.criterion.note:
            print(f"        [NOTE] {item.criterion.note}")

    unmet = [item for item in findings if item.criterion.unmet]
    failed = [item for item in findings if not item.ok and not item.criterion.unmet]

    print(f"\n结论：{passed}/{total} 通过（未满足 {len(unmet)} 条，未通过 {len(failed)} 条）")
    if not verbose and (unmet or failed):
        print("（用 --verbose 看每条的完整实测说明）")

    return EXIT_OK if passed == total else EXIT_PROBLEM


def main(argv: list[str] | None = None) -> int:
    # 验收脚本不允许因为输出不出去而中止（Windows GBK 终端下 U+26A0 会炸）
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
        pass

    parser = argparse.ArgumentParser(description="M5 验收证据生成器")
    parser.add_argument("--verbose", action="store_true", help="打印每条的完整实测说明")
    args = parser.parse_args(argv)

    return report(collect(), verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
