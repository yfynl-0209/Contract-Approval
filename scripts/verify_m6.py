#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6 验收证据（逐条实测值 + 退出码）。

沿用 `verify_m3.py` / `verify_m4.py` / `verify_m5.py` 的形态：**每条验收单独起一次
pytest**，采集它的实测结果，最后打印一张"逐条证据表"并用退出码表达结论。

## 五条沿用约定（第 5 轮评审补齐，M6 继续遵守）

1. **pytest 的退出码必须参与判定**。`--tb=no` 下收集错误（ImportError 等）
   不会产生任何 `PASSED` 行，退出码却是 1 —— 若只数行，验收会**假绿**；
2. **节点解析必须容忍参数化里的空格**：`test_x[abc 123]` 的节点含空格，
   用 `\\S+` 抓节点会让这类用例永远匹配不上；
3. **`SKIPPED` 也算未通过** —— 跳过却记成通过，报告里就出现一个
   **未经检验的绿点**；
4. **引用必须是精确节点，不是整个文件** —— 文件通过不能证明
   "该验收项有专属断言"，删掉专属断言后该验收项仍会显示绿色；
5. **`UNMET` 计入退出码 1**。

## M6 特有的两条

6. **M6 的核心保证是"事务 + 幂等"，不是"某段代码存在"**。因此每一个核心保证
   都配了**反面证据**：重复投递（幂等键去重）、提交后强杀（租约回收重派）、
   超时（结果未知 → 先查询再重发）、确认失效（正文变更打断摘要绑定）。
   只证明"顺利路径能跑通"的验收，在这三件事上等于没验 —— 它们全部
   只在**故障路径**上才表现出来。
7. **"回写重试不重跑 M4/M5" 是独立验收项**，不是"顺带"的断言。
   一次写入失败若顺带重跑了 OCR 与规则，会凭空多出一个批次与一套评价，
   而"多出来的批次"在统计上看起来只是"又审了一次" —— 没有任何地方会报错。

## ⚠️ 本脚本的输出只用可打印字符

`verify_m5` 的初版在打印 `⚠️`（U+26A0）时于 Windows 默认 GBK 终端抛
`UnicodeEncodeError`，**脚本当场中止** —— 后 29 条一条都没跑。
因此标记一律 ASCII，且入口把 stdout 的错误处理设为 `replace`：
**验收脚本不允许因为输出不出去而中止**。

用法：
    python scripts/verify_m6.py [--verbose]

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

#: 测试文件路径（引用集中在顶部，避免每条 criteria 里重复长字符串）
_SCHEMA = "tests/test_m6_schema.py"
_RESULT = "tests/test_result_service.py"
_WRITEBACK = "tests/test_writeback_service.py"
_OUTBOX = "tests/test_outbox.py"
_API = "tests/test_m6_api.py"


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
    # ---------- 结果持久化与版本链（决策 ①②）----------
    Criterion(
        "1",
        "结果落库：聚合口径与内容一次写成一行",
        tests=(f"{_RESULT}::test_persists_aggregate_and_content_in_one_row",),
        note=(
            "口径（等级 / 完整性 / 三计数）取自 M5 聚合，正文摘要取自正文本身 ——"
            "调用方说了不算"
        ),
    ),
    Criterion(
        "2",
        "口径不可由调用方改写（三项拒绝各有稳定原因码）",
        tests=(
            f"{_RESULT}::test_supplied_risk_level_must_equal_the_aggregate",
            f"{_RESULT}::test_save_rejects_a_run_that_is_not_completed",
            f"{_RESULT}::test_save_rejects_an_unknown_run_with_a_stable_code",
        ),
        note=(
            "未完成批次被拒尤其重要：在残缺输入上保存，等于把半截结论焊成正式版，"
            "而它看起来和一份正常结果完全一样"
        ),
    ),
    Criterion(
        "3",
        "同一载荷只产出一个不可变版本；内容变更产生新版本而非覆盖",
        tests=(
            f"{_RESULT}::test_replaying_identical_content_reuses_the_result",
            f"{_RESULT}::test_changed_content_creates_a_new_version_and_keeps_history",
        ),
        note="决策 ②：首次唯一载荷建版；相同重放返回该版本；变更即新版本，历史保留",
    ),
    Criterion(
        "4",
        "版本链与指纹的唯一性由**数据库约束**保证（不是应用层先查后写）",
        tests=(
            f"{_SCHEMA}::test_result_version_unique_per_task",
            f"{_SCHEMA}::test_result_fingerprint_unique_per_run",
            f"{_SCHEMA}::test_supersession_cannot_cross_tasks",
            f"{_SCHEMA}::test_result_version_and_fingerprint_are_valid",
            f"{_SCHEMA}::test_review_results_has_m6_version_columns",
        ),
        note=(
            "'先查后写'在并发下会穿透；约束才是最后防线。跨任务接替由复合外键挡住 ——"
            "否则一条结果可以'接替'另一个任务的历史，而它看起来只是一次正常改版"
        ),
    ),
    # ---------- 确认与失效（决策 ③）----------
    Criterion(
        "5",
        "确认摘要由**后端**绑定（调用方没有传摘要的入口）",
        tests=(
            f"{_RESULT}::test_confirm_result_binds_the_backend_digest_and_retains_the_actor",
            f"{_RESULT}::test_repeated_confirmation_is_idempotent",
            f"{_RESULT}::test_confirming_a_missing_result_is_a_stable_error",
        ),
        note=(
            "决策 ③：confirmation_valid 由后端算。重复确认是幂等 no-op ——"
            "同一人对同一版本确认两次是**一次**业务事实，不是两次"
        ),
    ),
    Criterion(
        "6",
        "**确认失效**：新版本接替、或正文变更打断摘要绑定",
        tests=(
            f"{_RESULT}::test_new_version_invalidates_the_old_confirmation",
            f"{_RESULT}::test_changed_comment_body_breaks_the_digest_binding",
            f"{_RESULT}::test_get_result_view_reports_version_and_confirmation_state",
            f"{_RESULT}::test_get_result_view_rejects_unknown_result",
        ),
        note=(
            "这是 M6 需求里的验收项之一。失效的方向必须是**严格**的："
            "把失效显示成有效，等于让一份没人看过的正文带着'已确认'的回写出去"
        ),
    ),
    Criterion(
        "7",
        "审计事件**只追加、不可变**，且动作取值受控",
        tests=(
            f"{_RESULT}::test_confirmation_writes_an_immutable_audit_event",
            f"{_SCHEMA}::test_audit_events_table_exists_with_required_columns",
            f"{_SCHEMA}::test_audit_action_is_constrained",
            f"{_SCHEMA}::test_audit_actor_and_target_must_be_present",
            f"{_SCHEMA}::test_audit_events_belong_to_a_task_with_cascade",
        ),
        note=(
            "审计动作无 update / delete 服务接口 —— 审计的价值在事后不可改；"
            "缺少操作人或目标的行则说明这条审计**无法归因**"
        ),
    ),
    # ---------- 回写门禁（决策 ④）----------
    Criterion(
        "8",
        "**门禁语义**：九类拒绝各有稳定原因码，三类允许面成立",
        tests=(
            f"{_WRITEBACK}::test_gate_denies_each_case_with_a_stable_reason_code",
            f"{_WRITEBACK}::test_gate_allows_confirmed_or_automated_results",
        ),
        note=(
            "决策 ④：高风险 / needs_review **永远**要有效确认；"
            "低/中风险完整结果只在 AUTO_WRITEBACK_ENABLED=true 时可绕过"
            "（默认 false）。参数化节点一次覆盖全部用例"
        ),
    ),
    Criterion(
        "9",
        "拒绝只留拒绝证据：`not_written` + 原因码，**不创建 Outbox 事件**",
        tests=(
            f"{_WRITEBACK}::test_denial_is_recorded_as_not_written_without_an_outbox_event",
            f"{_WRITEBACK}::test_instance_mismatch_is_a_recorded_denial",
            f"{_WRITEBACK}::test_missing_result_raises_a_stable_error",
        ),
        note=(
            "拒绝若也发一个 Outbox 事件，'被拒绝'就会在投递队列里表现为"
            "'排队中' —— 一个等人确认的任务会看起来只是慢"
        ),
    ),
    # ---------- 事务性意图（决策 ⑤ / M6 核心保证）----------
    Criterion(
        "10",
        "**意图 + Outbox + 审计同生共死**（同一事务；注入失败则一并回滚）",
        tests=(
            f"{_WRITEBACK}::test_allowed_request_creates_intent_outbox_and_audit_together",
            f"{_WRITEBACK}::test_injected_failure_rolls_back_intent_and_outbox_together",
        ),
        note=(
            "M6 的核心保证。只有'顺利时三者都在'是不够的 ——"
            "必须有**注入失败**的反面证据：中途出错时三者必须一起消失，"
            "否则会出现'意图提交了但事件没提交'（回写永久丢失且无人知晓）"
        ),
    ),
    Criterion(
        "11",
        "幂等键绑定身份与正文摘要；重放返回同一次尝试；拒绝行可复用转正",
        tests=(
            f"{_WRITEBACK}::test_idempotency_key_binds_identity_and_digest",
            f"{_WRITEBACK}::test_identical_replay_returns_the_same_attempt",
            f"{_WRITEBACK}::test_denied_attempt_transitions_the_same_row_once_conditions_are_fixed",
        ),
        note=(
            "键含 content_digest ⇒ 正文变了就是**另一次**回写（不是重放）；"
            "不含 approval_code ⇒ 重新拉取后 code 变化不会变成第二次回写。"
            "拒绝行复用是必需的：拒绝行占着唯一键，另建新行必然撞键，"
            "于是'先拒绝后修复'的任务会**永远写不出去**"
        ),
    ),
    Criterion(
        "12",
        "Outbox 表约束齐备（状态 / 次数 / 键唯一 / 时间自洽 / 领取索引）",
        tests=(
            f"{_SCHEMA}::test_outbox_events_table_exists_with_required_columns",
            f"{_SCHEMA}::test_outbox_event_status_is_constrained",
            f"{_SCHEMA}::test_outbox_attempts_are_bounded_and_non_negative",
            f"{_SCHEMA}::test_outbox_idempotency_key_non_empty_and_unique",
            f"{_SCHEMA}::test_outbox_event_type_and_aggregate_type_non_empty",
            f"{_SCHEMA}::test_outbox_delivered_at_matches_status",
            f"{_SCHEMA}::test_outbox_has_claim_and_retry_indexes",
            f"{_SCHEMA}::test_outbox_payload_is_required",
            f"{_SCHEMA}::test_outbox_and_audit_enums",
        ),
        note=(
            "`delivered_at` 与状态必须自洽：'已送达但没有送达时间'与"
            "'没送达却有送达时间'都会让对账得出相反结论"
        ),
    ),
    Criterion(
        "13",
        "作业输入严格：未知键被拒（声明什么就执行什么）",
        tests=(
            f"{_SCHEMA}::test_result_and_writeback_job_inputs_are_strict",
            f"{_SCHEMA}::test_result_job_input_rejects_unknown_fields",
            f"{_SCHEMA}::test_writeback_job_input_rejects_unknown_fields",
        ),
    ),
    # ---------- 派发：至少一次 → 恰好一次（需求验收项）----------
    Criterion(
        "14",
        "**重复投递不产生重复评论**（幂等键去重，三方状态一次提交）",
        tests=(
            f"{_OUTBOX}::test_delivery_marks_outbox_attempt_and_task_together",
            f"{_OUTBOX}::test_kill_window_leaves_exactly_one_comment",
        ),
        note=(
            "M6 需求验收项之一。「强杀窗口只留一条评论」是关键："
            "至少一次投递必然产生重复调用，去重必须发生在**外部系统那侧**"
            "（按幂等键），而不是靠'我们只调一次'"
        ),
    ),
    Criterion(
        "15",
        "**提交后进程被强杀**：事件不丢，租约过期后可被重新领取",
        tests=(
            f"{_OUTBOX}::test_claim_lease_is_exclusive",
            f"{_OUTBOX}::test_expired_lease_is_recycled_and_reclaimable",
        ),
        note=(
            "M6 需求验收项之一。领取**不改状态**（仍是 pending），互斥靠租约 ——"
            "若引入 dispatching 之类的中间态，'已领取但进程死亡'就必须额外对账"
        ),
    ),
    Criterion(
        "16",
        "**超时是「结果未知」，不是失败**：重发前先按幂等键查询",
        tests=(f"{_OUTBOX}::test_timeout_retry_queries_write_result_before_resending",),
        note=(
            "决策 ⑥。直接重发会让'其实已经写成功'的那次变成两条评论 ——"
            "而调用方看到的只是'重试了一次'"
        ),
    ),
    Criterion(
        "17",
        "重试有界：瞬时错误退避后耗尽 → `failed` + 任务 `blocked`（恢复点=回写）",
        tests=(
            f"{_OUTBOX}::test_retry_exhaustion_fails_and_blocks_at_writeback",
            f"{_OUTBOX}::test_permanent_error_fails_immediately_without_retry",
        ),
        note=(
            "确定性错误不浪费重试预算；恢复点必须是**回写** ——"
            "标成解析/审查会让人工重试去重跑一遍已经正确的 OCR 与规则"
        ),
    ),
    Criterion(
        "18",
        "未知事件类型必须被拒（不得标成已送达）；事件按 FIFO 派发",
        tests=(
            f"{_OUTBOX}::test_unknown_event_type_is_rejected_not_delivered",
            f"{_OUTBOX}::test_events_are_dispatched_in_fifo_order",
        ),
        note=(
            "'跳过'会让事件静默丢失，且丢失方式在统计上看不出来 ——"
            "队列长度看起来正常，因为没有东西留在队列里"
        ),
    ),
    # ---------- 工具 6-7（需求 2.4.10）----------
    Criterion(
        "19",
        "7 个工具名**逐字不变**（需求 2.4.10）",
        tests=(f"{_API}::test_seven_required_tool_names_are_exposed_verbatim",),
        note=(
            "REST 与 MCP 共用同一套服务，因此**改名不会有任何测试失败** ——"
            "兼容面只能靠这条断言守住"
        ),
    ),
    Criterion(
        "20",
        "工具 6 同步返回保存结果；工具 6/7 的最低参数集可用",
        tests=(
            f"{_API}::test_save_returns_the_saved_result_synchronously",
            f"{_API}::test_tool_six_accepts_the_required_minimum_fields",
            f"{_API}::test_tool_seven_accepts_the_required_minimum_fields",
        ),
        note=(
            "需求 §6.5 的参数名（`case_id` / `review_id`）由 **M7 的兼容门面**翻译，"
            "本层用规范名（`run_id` / `result_id`）—— 与工具 5 先例一致"
            "（需求写 `case_id`，M5 用 `parse_id`）"
        ),
    ),
    Criterion(
        "21",
        "工具请求仍然严格：未知字段与歧义别名一律 422",
        tests=(
            f"{_API}::test_tool_requests_still_reject_unknown_fields",
            f"{_API}::test_legacy_parameter_names_are_rejected_at_this_layer",
        ),
        note=(
            "`case_id` 是 `str`、`review_runs.id` 是 `int`：两个名字并存会让"
            "'传 \"12\" 还是 12'变成没有正确答案的问题"
        ),
    ),
    Criterion(
        "22",
        "保存的三种失败语义**在接口上可分**（404 / 409 / 400，保留原因码）",
        tests=(
            f"{_API}::test_save_of_an_unfinished_run_is_a_conflict_not_a_bad_request",
            f"{_API}::test_save_with_a_mismatched_risk_level_is_a_bad_request",
            f"{_API}::test_save_of_a_missing_run_is_404",
        ),
        note=(
            "`ResultInputError` 继承 `ValueError`：不单独登记就全部塌成"
            "400 `INVALID_ARGUMENT`，'等等再来'与'你传错了'再也分不开"
        ),
    ),
    Criterion(
        "23",
        "`confirmation_valid` 由后端计算并在接口上可见（决策 ③）",
        tests=(
            f"{_API}::test_result_query_exposes_backend_computed_confirmation_validity",
            f"{_API}::test_missing_result_query_is_404",
        ),
        note=(
            "让浏览器自己比对两个摘要，等于把业务口径复制进前端（M8 设计 G-3）——"
            "前端一改版就会**静默**算错，且算错的方向最危险（失效显示成有效）"
        ),
    ),
    Criterion(
        "24",
        "门禁拒绝是**业务结论**（200 + `blocked` + 稳定原因码），不是 5xx",
        tests=(
            f"{_API}::test_unconfirmed_result_is_denied_as_a_business_conclusion",
            f"{_API}::test_high_risk_cannot_bypass_confirmation_even_with_auto_writeback",
            f"{_API}::test_writeback_of_a_missing_result_is_404",
        ),
        note=(
            "用 5xx 会让调用端当成抖动反复重试一个注定被拒的请求 ——"
            "而它等的那个**人**的确认，永远不会因为重试而出现"
        ),
    ),
    # ---------- 全链路闭环（需求验收项）----------
    Criterion(
        "25",
        "**全链路闭环**：已完成的 M5 批次 → 保存 → 确认 → 回写 → 派发 → 评论 → 任务 done",
        tests=(
            f"{_API}::test_end_to_end_closure_reaches_task_done",
            f"{_API}::test_denied_then_repaired_attempt_is_the_same_attempt",
            f"{_API}::test_missing_writeback_query_is_404",
        ),
        note=(
            "跨越 M4（解析）→ M5（规则）→ M6（保存/确认/回写/派发）四个环节。"
            "外部评论**恰好一条**、任务终态 `done`、`write_status=success` 三者同时成立"
        ),
    ),
    Criterion(
        "26",
        "**回写重试不重跑 M4/M5**（解析 / 批次 / 评价计数一律不变）",
        tests=(
            f"{_API}::test_transient_writeback_failure_never_reruns_parse_or_rules",
            f"{_API}::test_deterministic_writeback_failure_does_not_burn_retries",
        ),
        note=(
            "全局约束：M6 消费 M5 批次，不得重评规则。重跑会凭空多出一个批次"
            "与一套评价，而'多出来的批次'在统计上看起来只是'又审了一次'"
        ),
    ),
    Criterion(
        "27",
        "作业引用按 `job_type` 分派（绝不从输入里猜 id）；Worker 能执行两类新作业",
        tests=(
            f"{_API}::test_result_reference_dispatches_by_job_type_and_never_probes_the_payload",
            f"{_API}::test_worker_executes_result_and_writeback_jobs",
            f"{_API}::test_worker_still_fails_closed_for_unregistered_types",
        ),
        note=(
            "RESULT / WRITEBACK 的产物 id **不在冻结输入里**（作业执行时才产生，"
            "处理器的 JobRun 无权写检查点）—— 所以它们如实返回「没有结果引用」，"
            "而不是指向输入里那个 id；后者会指向一份完全正常的结果，"
            "而真正出问题的那次没有任何线索指向它"
        ),
    ),
    Criterion(
        "28",
        "既有测试全绿（整个 tests/ 目录）",
        tests=("tests/",),
        allow_skipped=True,
        note=(
            "跳过项（慢 OCR 等）由逐条验收各自计数，本项只关心有没有失败"
        ),
    ),
)


def _subprocess_env() -> dict[str, str]:
    """子进程环境：**剥掉 PYTHONPATH**。

    IDE 会把自家的 `sitecustomize.py` 钩子经 PYTHONPATH 注入每个 Python 进程，
    而它在解释器退出时抛 `SystemExit(1)` —— pytest 于是把**最后一个用例**
    记成收尾 ERROR，尽管测试本体是绿的（verify_m5 验收 31 那个"时红时绿"的根因）。

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

    # ⚠️ **先看退出码，再看显示输出**（评审约定 ①）。
    # "没有采集到结果"有两种完全不同的成因，退出码能把它们分开：
    #   5   → 引用写错了（节点不存在），验收项**从未被检验**
    #   1/2 → 收集期就炸了（ImportError / conftest 崩了），**测试根本没跑**
    # 只按"有没有 PASSED 行"判断的话，后者会以"没有实测值"混过去。
    if not outcomes:
        if completed.returncode == _EXIT_NO_TESTS:
            return "MISSING", "pytest 没有收集到任何用例（退出码 5 —— 引用可能已失效）"
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

    # ⚠️ **退出码必须参与判定**：`--tb=no` 下收集错误、内部错误
    # 或"没被词表覆盖"的失败，都可能只产生几条 PASSED 而退出码是 1 ——
    # 只数行的话验收会**假绿**。
    if completed.returncode != 0:
        stderr_tail = [
            line.strip() for line in completed.stderr.splitlines() if line.strip()
        ][-3:]
        hint = " | ".join(stderr_tail) if stderr_tail else "（stderr 为空）"
        return (
            "FAILED",
            f"{len(wanted)} 条通过，但 pytest 退出码为 {completed.returncode}"
            f"（0 才算干净）：{hint}",
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

    print("\n[M6 acceptance evidence]")
    for item in findings:
        mark = "[OK]  " if item.ok else "[FAIL]"
        print(f"  {mark} {item.criterion.number:>2}. {item.criterion.title}")
        if verbose or not item.ok:
            print(f"        measured: {item.outcome} - {item.detail}")
        if item.criterion.note:
            print(f"        [NOTE] {item.criterion.note}")

    unmet = [item for item in findings if item.criterion.unmet]
    failed = [item for item in findings if not item.ok and not item.criterion.unmet]

    print(
        f"\n结论：{passed}/{total} 通过"
        f"（未满足 {len(unmet)} 条，未通过 {len(failed)} 条）"
    )
    if not verbose and (unmet or failed):
        print("（用 --verbose 看每条的完整实测说明）")

    return EXIT_OK if passed == total else EXIT_PROBLEM


def main(argv: list[str] | None = None) -> int:
    # 验收脚本不允许因为输出不出去而中止（Windows GBK 终端下 U+26A0 会炸）
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
        pass

    parser = argparse.ArgumentParser(description="M6 验收证据生成器")
    parser.add_argument("--verbose", action="store_true", help="打印每条的完整实测说明")
    args = parser.parse_args(argv)

    return report(collect(), verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
