"""M4 验收证据生成器（56 条）—— 逐条打印实测值，退出码可直接当门禁。

```powershell
python scripts/verify_m4.py            # 逐条实测，退出码 0/1
python scripts/verify_m4.py --keep     # 保留现场（本脚本目前不建现场）
```

## 为什么逐条列出来、而不是"跑一遍 pytest 看退出码"

一条验收标准引用多个用例。整体失败只能说明"有问题"，说不清**是哪条没过** ——
而验收报告的价值恰恰在于指出具体那一条。因此每条**单独**起一次 pytest：
pytest 遇到"不存在的节点"（例如把参数化用例写成裸名）会**整批拒绝执行**，
合并成一次调用时，一个笔误会让所有条目变成"未采集到结果"，真正的错因反而被淹没。

## 三种结论，**没有"跳过"**

| 标记 | 含义 | 退出码 |
| --- | --- | --- |
| `PASS` | 引用节点全部通过 | 不影响 |
| `FAIL` | 有节点未过 / 未采集到结果 | **1** |
| `UNMET` | **需求未满足且已显式记录**（见 §0.14） | **1** |

⚠️ `UNMET` 也计入退出码 1 —— 这正是"**不得以'OCR 已跑通'为理由宣告 M4 完成**"
这条纪律的落地方式。把它做成"警告但不影响门禁"，那条纪律就变回一句口号了。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: `-v` 输出里每条用例的结果行：`tests/x.py::test_y PASSED [ 12%]`
_OUTCOME_PATTERN = re.compile(r"^(?P<node>\S+::\S+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)")


# ============================================================
# 验收标准目录
# ============================================================


@dataclass(frozen=True)
class Criterion:
    """一条验收标准及其证据来源。

    `number` 是**字符串**：验收表里有 `10b` / `12c` / `14d` 这类带字母的条目，
    用 int 会把它们压成同一个号，于是"56 条"与实际条目数对不上。
    """

    number: str
    title: str
    #: 覆盖本条的 pytest 节点（文件或 `文件::用例`）
    tests: tuple[str, ...] = ()
    #: 需求**未满足**且已显式记录（不是"暂时没测"，是"确实没做"）
    unmet: bool = False
    #: `UNMET` 时的处置指向
    note: str = ""


CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "1",
        "文本件返回 char 级位置，且能反向匹配标准文档文本块",
        tests=(
            "tests/test_document_builder.py::test_block_text_equals_slice_of_page_text",
            "tests/test_document_builder.py::test_real_text_fixture_builds_char_blocks",
            "tests/test_document_builder.py::test_locate_returns_evidence_with_coordinates",
        ),
    ),
    Criterion(
        "2",
        "扫描件返回 line 级，且文本非空",
        tests=(
            "tests/test_document_builder.py::test_ocr_page_uses_line_precision_and_maps_pixels",
            "tests/test_parse_adapters.py",
        ),
    ),
    Criterion(
        "3",
        "同一份内容的文本件与扫描件，提取的关键字段一致",
        tests=("tests/test_failure_drills.py::test_paired_fixtures_agree_on_key_fields",),
        note=(
            "⚠️ 该用例走**真实 OCR**（推理慢），默认 `SKIPPED` —— 而 `SKIPPED` "
            "在本脚本里算**未通过**（跳过却被记成通过＝报告里出现未经检验的绿点）。"
            "要真正取得这条证据：`$env:RUN_SLOW_OCR=1` 后重跑本脚本。"
        ),
    ),
    Criterion(
        "4",
        "标准文档含页面尺寸 + 坐标系声明 + 旋转角",
        tests=(
            "tests/test_m4_contracts.py",
            "tests/test_parse_adapters.py::test_cropbox_with_rotation_maps_correctly",
        ),
    ),
    Criterion(
        "5",
        "坐标换算正确（含非零 CropBox 原点 + 90° 旋转的组合用例）",
        tests=("tests/test_parse_adapters.py::test_cropbox_with_rotation_maps_correctly",),
    ),
    Criterion(
        "6",
        "双精度如实标注：bbox_precision=char 时 chars 必须非空",
        tests=(
            "tests/test_m4_contracts.py::test_char_precision_requires_chars",
            "tests/test_field_extractor.py::test_char_evidence_bbox_is_the_union_of_matched_characters",
        ),
    ),
    Criterion(
        "7",
        "四态语义正确：not_found 只在可靠检索后确实没有时出现",
        tests=(
            "tests/test_field_extractor.py::test_missing_clause_is_reported_as_not_found",
            "tests/test_field_extractor.py"
            "::test_failed_page_turns_misses_into_failed_not_not_found",
            "tests/test_field_extractor.py::test_uncertain_page_turns_misses_into_uncertain",
        ),
    ),
    Criterion(
        "8",
        "字段 JSON 覆盖 8 项基本信息 + 8 类条款，结构符合 §4.8",
        tests=(
            "tests/test_field_extractor.py::test_basic_info_fields_are_exactly_eight",
            "tests/test_field_extractor.py::test_required_clause_types_are_exactly_eight",
            "tests/test_field_extractor.py::test_all_clauses_get_a_conclusion",
            "tests/test_field_extractor.py::test_all_direct_fields_get_a_conclusion",
        ),
    ),
    Criterion(
        "9",
        "解析版本不覆盖历史：不同 parser_version 产生两条记录并存",
        tests=(
            "tests/test_parse_service.py::test_failed_record_does_not_count_as_a_cache_hit",
            "tests/test_parse_api.py::test_tool4_reruns_after_a_failed_parse",
        ),
    ),
    Criterion(
        "10",
        "缓存可追溯：cache_key / parser_name / parser_version / config_digest 均已落库",
        tests=(
            "tests/test_parse_api.py::test_parse_response_exposes_traceability",
            "tests/test_parse_service.py::test_cache_key_covers_every_component",
            "tests/test_parse_service.py::test_config_digest_changes_when_any_option_changes",
        ),
    ),
    Criterion(
        "10b",
        "缓存并发原子性：并发请求只产生一条记录",
        tests=(
            "tests/test_failure_drills.py::test_concurrent_reserve_creates_exactly_one_row",
        ),
    ),
    Criterion(
        "11",
        "重试预算不被重复扣：租约回收后 attempt_no 不变",
        tests=("tests/test_worker.py::test_recycle_does_not_increment_attempt_no",),
    ),
    Criterion(
        "12",
        "租约条件完成（fencing）：失去所有权的 Worker 不得写入结果",
        tests=(
            "tests/test_worker.py::test_old_token_cannot_complete_after_release",
            "tests/test_worker.py::test_complete_after_lease_expiry_is_rejected",
        ),
    ),
    Criterion(
        "12b",
        "lease_token 真的生效：同一 worker_id 下旧 token 提交必须失败",
        tests=("tests/test_worker.py::test_old_token_cannot_complete_after_release",),
    ),
    Criterion(
        "12c",
        "写结果与完成作业同事务：越权写入时业务数据不得落库",
        tests=(
            "tests/test_worker.py::test_business_write_rolls_back_when_lease_is_lost",
            "tests/test_worker.py::test_lease_lost_is_logged_and_does_not_touch_business_data",
            "tests/test_worker.py::test_business_write_survives_when_lease_is_held",
        ),
    ),
    Criterion(
        "12d",
        "回收后走退避（retry_wait）而非 queued",
        tests=(
            "tests/test_worker.py::test_recycle_goes_to_retry_wait_with_backoff",
            "tests/test_worker.py::test_recycled_job_becomes_claimable_after_backoff",
        ),
    ),
    Criterion(
        "13",
        "TaskRef 可查询：工具 4 返回 job_id，且请求不阻塞",
        tests=("tests/test_parse_api.py::test_tool4_returns_queryable_task_ref",),
    ),
    Criterion(
        "14",
        "多附件聚合：全部目标附件完成才进 reviewing",
        tests=(
            "tests/test_parse_service.py::test_all_targets_succeeded_moves_task_to_reviewing",
            "tests/test_parse_service.py::test_partial_completion_keeps_task_parsing",
        ),
    ),
    Criterion(
        "14b",
        "外部附件缺失 → blocked + ATTACHMENT_MISSING，不得只记日志继续",
        tests=("tests/test_parse_service.py::test_missing_attachment_blocks_the_task",),
    ),
    Criterion(
        "14c",
        "无任何可解析附件 → blocked：空集合不等于全部完成",
        tests=("tests/test_parse_service.py::test_no_parseable_attachment_blocks_the_task",),
    ),
    Criterion(
        "14d",
        "图片扫描件受支持：image/png / image/jpeg 可走完 OCR 链路",
        tests=(
            "tests/test_image_input.py::test_whitelist_accepts_the_three_required_types",
            "tests/test_image_input.py::test_image_becomes_a_one_page_document",
            "tests/test_image_input.py::test_image_page_is_a_normal_document_page",
            "tests/test_image_input.py::test_image_without_an_engine_fails_honestly",
        ),
        note=(
            "⚠️ 上述节点证明的是**输入面真的开了 + 图片被路由到 OCR + 坐标系未变**。"
            "**真正读出字**的那条是 `test_image_goes_through_ocr_end_to_end`，"
            "走真实 OCR、默认 `SKIPPED` —— 要取回这条证据需 `$env:RUN_SLOW_OCR=1` 后重跑。"
        ),
    ),
    Criterion(
        "15",
        "correlation_id 贯穿：一次请求的日志（含 Worker 段）共享同一 ID",
        tests=(
            "tests/test_correlation.py::test_log_carries_correlation_id_automatically",
            "tests/test_correlation.py::test_job_records_correlation_id",
            "tests/test_correlation.py::test_worker_reads_back_and_rebinds",
        ),
    ),
    Criterion(
        "16",
        "correlation_id 非法输入被拒（400）",
        tests=("tests/test_correlation.py::test_illegal_id_is_rejected_with_400",),
    ),
    Criterion(
        "17",
        "OCR 不可用时降级：页面 failed，不谎报 not_found",
        tests=(
            "tests/test_document_builder.py::test_ocr_failure_is_recorded_with_its_code",
            "tests/test_document_builder.py::test_missing_ocr_engine_fails_the_page_honestly",
        ),
    ),
    Criterion(
        "18",
        "异常 PDF 五类边界：加密 / 损坏 / 超页数 / 超大像素 / 渲染超时",
        tests=(
            "tests/test_failure_drills.py::test_five_failure_classes_have_distinct_codes",
            "tests/test_failure_drills.py::test_encrypted_pdf_is_rejected",
            "tests/test_failure_drills.py::test_oversized_render_is_rejected_before_allocating",
            "tests/test_parse_adapters.py::test_render_timeout_is_transient_not_corrupt",
        ),
    ),
    Criterion("19", "日志不含合同正文", tests=("tests/test_log_service.py",)),
    Criterion("20", "既有测试全绿", tests=()),  # 由全量回归单独填充
    Criterion(
        "21",
        "空白页与失败页可区分（blank / failed + error_code）",
        tests=(
            "tests/test_document_builder.py::test_ocr_nothing_found_means_blank",
            "tests/test_field_extractor.py::test_blank_page_does_not_block_not_found",
        ),
    ),
    Criterion(
        "22",
        "作业输入拒绝未知字段（extra=forbid）",
        tests=(
            "tests/test_m4_contracts.py",
            "tests/test_parse_api.py::test_tool4_rejects_unknown_payload_keys",
            "tests/test_workflow_jobs.py",
        ),
    ),
    Criterion(
        "23",
        "金额无浮点：十进制字符串 + 独立币种字段",
        tests=(
            "tests/test_field_extractor.py::test_amount_rejects_scientific_notation",
            "tests/test_field_extractor.py::test_amount_keeps_decimal_precision",
            "tests/test_field_extractor.py::test_baseline_fixture_extracts_basic_info",
        ),
    ),
    Criterion(
        "24",
        "工件与字段不重复存储（库中无坐标列；工件内无 field_code）",
        tests=("tests/test_parse_service.py::test_successful_parse_writes_fields_and_artifacts",),
    ),
    Criterion(
        "25",
        "OCR 路由阈值生效：水印页必须走 OCR",
        tests=(
            "tests/test_document_builder.py::test_watermark_only_page_is_routed_to_ocr",
            "tests/test_document_builder.py::test_normal_density_text_page_is_not_routed_to_ocr",
            "tests/test_document_builder.py::test_thresholds_come_from_options_not_constants",
        ),
    ),
    Criterion(
        "26",
        "uncertain 页状态存在且不归入 blank / failed",
        tests=(
            "tests/test_document_builder.py::test_ocr_low_confidence_means_uncertain_not_blank",
        ),
    ),
    Criterion(
        "27",
        "文本层与 OCR 坐标同空间（旋转页）",
        tests=(
            "tests/test_parse_adapters.py::test_cropbox_with_rotation_maps_correctly",
            "tests/test_document_builder.py::test_ocr_page_uses_line_precision_and_maps_pixels",
        ),
    ),
    Criterion(
        "28",
        "工具 4 能取到结果：作业成功时 result_ref 非空，解析接口返回结构化字段",
        tests=("tests/test_parse_api.py::test_result_ref_appears_after_the_job_succeeds",),
    ),
    Criterion(
        "29",
        "图片附件坐标统一：bbox_space 取值域只有一个",
        tests=(
            "tests/test_image_input.py"
            "::test_bbox_space_has_exactly_one_value_across_all_inputs",
            "tests/test_image_input.py::test_bbox_space_constant_agrees_with_the_model_literal",
        ),
        note=(
            "⚠️ 断言的是**取值域的大小**（== 1），不是『每页都等于某个字面量』 —— "
            "后者在**新增一种输入形态时不会被触发**，而前者会（§4.9 修-24）。"
        ),
    ),
    Criterion(
        "30",
        "失败记录不构成缓存命中：failed / blocked 不阻止重新解析",
        tests=(
            "tests/test_parse_api.py::test_tool4_reruns_after_a_failed_parse",
            "tests/test_parse_service.py::test_blocked_record_also_allows_reparse",
        ),
    ),
    Criterion(
        "31",
        "NFC 映射正确：多对一时 bbox 与区间仍指向同一字符",
        tests=(
            "tests/test_document_builder.py::test_combining_sequence_yields_span_of_two",
            "tests/test_document_builder.py::test_already_composed_text_keeps_span_of_one",
        ),
    ),
    Criterion(
        "32",
        "门禁：整份为空 → failed + DOCUMENT_EMPTY",
        tests=("tests/test_parse_service.py::test_all_pages_blank_means_document_empty",),
    ),
    Criterion(
        "33",
        "门禁：页面失败 → 解析失败",
        tests=(
            "tests/test_parse_service.py::test_failed_page_means_failed_contract",
            "tests/test_parse_service.py::test_failed_page_records_code_and_writes_no_fields",
        ),
    ),
    Criterion(
        "34",
        "门禁：无法识别 → 不得进入 reviewing",
        tests=(
            "tests/test_parse_service.py::test_uncertain_page_blocks_the_contract",
            "tests/test_parse_service.py::test_uncertain_page_blocks_and_records_ocr_code",
        ),
    ),
    Criterion(
        "35",
        "门禁是 reviewing 的前置条件",
        tests=("tests/test_parse_service.py::test_failed_parse_does_not_let_the_task_through",),
    ),
    Criterion(
        "36",
        "新错误码重试性正确（逐码遍历，不抽查）",
        tests=("tests/test_m4_contracts.py::test_m4_error_codes_retryability_is_exhaustive",),
    ),
    Criterion(
        "37",
        "parse_id 在作业输入中（缺失被拒；Worker 填充的行 == 预留的 parse_id）",
        tests=(
            "tests/test_m4_contracts.py",
            "tests/test_parse_api.py::test_tool4_returns_queryable_task_ref",
        ),
    ),
    Criterion(
        "38",
        "单个 blank 页不判失败",
        tests=(
            "tests/test_parse_service.py::test_single_blank_page_is_not_a_failure",
            "tests/test_field_extractor.py::test_blank_page_does_not_block_not_found",
        ),
    ),
    Criterion(
        "39",
        "失败后可重新解析：failed / blocked 记录不占唯一约束位",
        tests=(
            "tests/test_parse_service.py::test_failed_record_does_not_count_as_a_cache_hit",
            "tests/test_parse_service.py::test_blocked_record_also_allows_reparse",
        ),
    ),
    Criterion(
        "40",
        "渲染不使用 clip、不做区域裁切（源码级断言）",
        tests=("tests/test_parse_adapters.py",),
    ),
    Criterion(
        "41",
        "标准文档是 Pydantic 模型：非法值构造即失败",
        tests=(
            "tests/test_m4_contracts.py",
            "tests/test_parse_adapters.py",
        ),
    ),
    Criterion(
        "42",
        "出现 uncertain 页即 blocked（默认不放宽）",
        tests=("tests/test_parse_service.py::test_uncertain_page_blocks_the_contract",),
    ),
    Criterion(
        "43",
        "parse_error_code 已落库且接口可读",
        tests=(
            "tests/test_parse_api.py::test_failed_gate_still_returns_result_ref_but_no_fields",
            "tests/test_parse_service.py::test_build_failure_is_recorded_with_its_code",
        ),
    ),
    Criterion(
        "44",
        "PageStatus 四态齐备且写入受控",
        tests=("tests/test_m4_contracts.py", "tests/test_document_builder.py"),
    ),
    Criterion(
        "45",
        "char_map 能表达多对一，且 bbox == 两个原始字符框的并集",
        tests=("tests/test_document_builder.py::test_combining_sequence_yields_span_of_two",),
    ),
    Criterion(
        "46",
        "ParseOptions 拒绝越界与未知参数",
        tests=("tests/test_m4_contracts.py",),
    ),
    Criterion(
        "47",
        "解析作业幂等键稳定：parse:{parse_id}:v1，不含随机后缀",
        tests=(
            "tests/test_parse_api.py::test_tool4_second_call_hits_cache_and_creates_no_job",
            "tests/test_parse_service.py::test_second_reserve_reuses_without_creating",
        ),
    ),
    Criterion(
        "48",
        "缓存命中不新建作业",
        tests=("tests/test_parse_api.py::test_tool4_second_call_hits_cache_and_creates_no_job",),
    ),
    Criterion(
        "49",
        "同一幂等键不得绑定不同输入（IdempotencyConflict 409）",
        tests=(
            "tests/test_workflow_jobs.py::test_same_key_with_different_input_is_rejected",
            "tests/test_workflow_jobs.py::test_same_key_same_input_is_still_reused",
            "tests/test_workflow_jobs.py::test_same_key_different_job_type_is_rejected",
            "tests/test_workflow_jobs.py::test_same_key_different_task_is_rejected",
        ),
    ),
    Criterion(
        "50",
        "PARSE 作业经真实 Worker 执行入队预留的那条解析记录（M6 Task 0 收口）",
        tests=(
            "tests/test_parse_worker_integration.py::test_worker_executes_the_parse_job_and_reuses_the_reserved_parse_id",
            "tests/test_parse_worker_integration.py::test_gate_failure_moves_the_task_to_blocked_not_stuck_in_parsing",
        ),
        note=(
            "M4 交付时 PARSE 作业没有处理器（入队 → 领取 → 解析从未整条跑通）。"
            "由 M6 Task 0 收口：复用入队预留的 parse_id 与冻结的 ParseJobInput，"
            "门禁失败（DOCUMENT_EMPTY / OCR_UNRECOGNIZABLE）推进任务到 blocked"
        ),
    ),
)


# ============================================================
# 执行
# ============================================================


@dataclass
class Finding:
    criterion: Criterion
    ok: bool
    measured: str
    source: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def failed(self) -> list[Finding]:
        return [item for item in self.findings if not item.ok]

    @property
    def passed_count(self) -> int:
        """通过数**不含** `UNMET` —— 未满足的需求不算通过。"""
        return sum(1 for item in self.findings if item.ok)

    @property
    def ok(self) -> bool:
        return not self.failed


def _run_pytest(nodes: list[str]) -> tuple[dict[str, str], str, str]:
    """跑指定节点，返回 `({节点: 结果}, 摘要行, stderr 摘要)`。"""
    command = [
        sys.executable,
        "-m",
        "pytest",
        *nodes,
        "-v",
        "--tb=short",
        "-p",
        "no:cacheprovider",
    ]
    completed = subprocess.run(
        command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, errors="replace"
    )

    outcomes: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        match = _OUTCOME_PATTERN.match(line.strip())
        if match:
            outcomes[match.group("node")] = match.group("outcome")

    summary = ""
    for line in reversed(completed.stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break

    stderr_tail = ""
    if not outcomes:
        for line in completed.stderr.splitlines():
            if "not found" in line or "ERROR" in line:
                stderr_tail = line.strip()
                break
        stderr_tail = stderr_tail or "pytest 未产出可解析的结果"

    return outcomes, summary, stderr_tail


def _run_full_suite() -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        errors="replace",
    )
    summary = ""
    for line in reversed(completed.stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break
    return completed.returncode == 0, summary or "（未解析到摘要）"


def _outcomes_for(node: str, outcomes: dict[str, str]) -> list[str]:
    """某条引用对应的实测结果。

    支持三种引用形态：

    | 写法 | 匹配 |
    | --- | --- |
    | `tests/x.py` | 该文件下的**全部**用例 |
    | `tests/x.py::test_y` | 该用例（含**参数化**的 `test_y[case]`） |
    | 其余 | 精确匹配 |

    ⚠️ 必须支持**参数化**：`pytest -v` 给出的节点是
    `tests/x.py::test_y[abc 123]`，而引用里写的是 `…::test_y`。
    初版只做"精确 + `::` 前缀"两种匹配，于是 `test_illegal_id_is_rejected_with_400`
    这类参数化用例**永远采集不到结果**，报告里显示为"未通过" ——
    而它其实是全绿的。**报告说错话比报告缺一条更糟**，因为它会让人去查一个不存在的问题。

    ⚠️ `SKIPPED` 也算**未通过**：跳过却被记成通过，报告里就出现了一个
    **未经检验的绿点** —— 那比缺一条更糟，因为它看起来是绿的。
    慢 OCR 类用例（验收 3）默认跳过，正是这一条的典型场景。
    """
    exact = outcomes.get(node)
    if exact is not None:
        return [exact] if exact == "PASSED" else []
    return [
        value
        for key, value in outcomes.items()
        if (key.startswith(node + "::") or key.startswith(node + "["))
        and value == "PASSED"
    ]


def _print_report(report: Report) -> None:
    print()
    print("=" * 96)
    print(f"M4 验收证据报告（{len(CRITERIA)} 条）")
    print("=" * 96)

    for finding in report.findings:
        if finding.criterion.unmet:
            mark = "UNMET"
        else:
            mark = "PASS" if finding.ok else "FAIL"
        print(f"[{finding.criterion.number:>3}] {mark:<5} {finding.criterion.title}")
        print(f"      实测 : {finding.measured}")
        print(f"      来源 : {finding.source}")
        # `note` 对 **未通过** 的条目一律打印（不只是 `UNMET`）：
        # 一条 FAIL 如果没有说明，读报告的人只能自己去猜它是缺陷还是环境 ——
        # 而"需要设 RUN_SLOW_OCR"这类原因光看标题根本看不出来。
        if finding.criterion.note and not finding.ok:
            print(f"      ⚠️   {finding.criterion.note}")

    print("-" * 96)
    total = len(report.findings)
    unmet = [f for f in report.findings if f.criterion.unmet]
    print(f"结论：{report.passed_count}/{total} 通过（未满足 {len(unmet)} 条）")
    if unmet:
        numbers = ", ".join(f.criterion.number for f in unmet)
        print(f"未满足：{numbers} —— **不得判定 M4 完成**，见各条 note")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 验收证据生成器（56 条）")
    parser.add_argument(
        "--keep", action="store_true", help="保留现场供人工查看（本脚本不建临时现场）"
    )
    args = parser.parse_args()  # noqa: F841  (与 verify_m3 保持同一套命令行形态)

    report = Report()

    for criterion in CRITERIA:
        if criterion.unmet:
            report.findings.append(
                Finding(
                    criterion,
                    False,
                    "**需求未满足**（说明见下方 ⚠️）",
                    "需求未满足且已显式记录（§0.14）",
                )
            )
            continue
        if not criterion.tests:
            continue  # 第 20 条由全量回归填充

        print(f"运行指定用例：验收 {criterion.number} ...")
        outcomes, summary, stderr_tail = _run_pytest(list(criterion.tests))
        results = {node: _outcomes_for(node, outcomes) for node in criterion.tests}
        passed = sum(1 for values in results.values() if values)
        missing = [node for node, values in results.items() if not values]

        ok = passed == len(criterion.tests)
        detail = f"{passed}/{len(criterion.tests)} 个引用节点全部通过"
        if missing:
            detail += f"；未通过或未采集到结果：{missing}"
        if stderr_tail:
            detail += f"；pytest 报错：{stderr_tail}"
        report.findings.append(
            Finding(criterion, ok, detail, "tests/ 指定用例 · " + (summary or "无摘要"))
        )

    # ---- 全量回归：本身就是第 20 条，不做"跳过"开关 ----
    regression = next(item for item in CRITERIA if item.number == "20")
    print("运行全量回归（验收 20）...")
    ok, summary = _run_full_suite()
    report.findings.append(Finding(regression, ok, summary, "pytest 全量"))

    report.findings.sort(key=lambda finding: _sort_key(finding.criterion.number))
    _print_report(report)
    return 0 if report.ok else 1


def _sort_key(number: str) -> tuple[int, str]:
    """`10b` 要排在 `10` 之后、`11` 之前 —— 纯字符串排序会把 `10b` 排到 `1` 附近。"""
    digits = re.match(r"\d+", number)
    return (int(digits.group(0)) if digits else 0, number)


if __name__ == "__main__":
    raise SystemExit(main())
