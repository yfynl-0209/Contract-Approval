#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M7 验收证据（逐条实测值 + 退出码）。

沿用 `verify_m3..m6.py` 的形态：**每条验收单独起一次 pytest**，
采集它的实测结果，最后打印一张"逐条证据表"并用退出码表达结论。
五条沿用约定（第 5 轮评审补齐，M7 继续遵守）：

1. **pytest 的退出码必须参与判定**：`--tb=no` 下收集错误不会产生任何 `PASSED` 行，
   退出码却是 1 —— 只数行会让验收**假绿**；
2. **节点解析容忍参数化里的空格**（本脚本的引用都不带参数，但仍沿用同一正则）；
3. **`SKIPPED` 也算未通过**；
4. **引用必须是精确节点**，不是整个文件 —— 删掉专属断言后该验收项仍会显示绿色；
5. **`UNMET` 计入退出码 1**。

## M7 特有的三条

6. **"两种协议共用一套业务实现"必须被两侧的证据夹住**：
   REST↔门面（`test_m7_contracts.py`）与 MCP↔门面（`test_mcp_tools.py`）各有一组 parity。
   只测一侧时，另一侧可以完整地"再实现一遍"而无人察觉。
7. **权限验收要逐条路由**，不是"抽查一条"。只读角色被拒的**方式**如果在某一条路由上
   漏了，从"某条路由被拒"的抽查里完全看不出来。
8. **"没做到的事"必须显式写进报告**（`unmet`），而不是从表里消失。

## ⚠️ 本脚本的输出只用可打印字符

`verify_m5` 的初版在打印 `⚠️`（U+26A0）时于 Windows 默认 GBK 终端抛
`UnicodeEncodeError`，**脚本当场中止**。因此标记一律 ASCII，
且入口把 stdout 的错误处理设为 `replace`：**验收脚本不允许因为输出不出去而中止**。

用法：
    python scripts/verify_m7.py [--verbose]

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

_RESULT_LINE = re.compile(
    r"^(?P<node>.+?)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|\[|$)"
)

_EXIT_NO_TESTS = 5

#: 测试文件路径（引用集中在顶部，避免每条 criteria 里重复长字符串）
_CONTRACTS = "tests/test_m7_contracts.py"
_QUERIES = "tests/test_task_queries.py"
_CONTENT = "tests/test_attachment_content_api.py"
_RETRY = "tests/test_retry_api.py"
_RULES = "tests/test_rule_admin_api.py"
_MCP = "tests/test_mcp_tools.py"
_RBAC = "tests/test_auth_rbac.py"
_SOURCE = "tests/test_source_invariants.py"
_SCHEMA = "tests/test_m6_schema.py"


@dataclass(frozen=True)
class Criterion:
    number: str
    title: str
    tests: tuple[str, ...]
    note: str | None = None
    allow_skipped: bool = False
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
    # ---------- 兼容门面：七个工具的名字与最低参数 ----------
    Criterion(
        "1",
        "七个工具名与需求 2.4.10 逐字一致（恰好七个）",
        tests=(f"{_CONTRACTS}::test_facade_exposes_exactly_the_seven_requirement_names",),
        note="多一个或少一个都会让这条直接失败——名字是需求承诺出去的接口",
    ),
    Criterion(
        "2",
        "最低参数逐字：名字、**顺序**、有无默认值",
        tests=(
            f"{_CONTRACTS}::test_positional_parameters_match_the_requirement_verbatim",
            f"{_CONTRACTS}::test_enterprise_context_is_keyword_only",
        ),
        note=(
            "顺序也断言：只比名字集合时，download 的 instance_id / attachment_id 对调仍通过，"
            "而两者都是字符串——传反了会拿到「附件不存在」，排查方向直接跑偏"
        ),
    ),
    Criterion(
        "3",
        "权限映射恰好覆盖七个工具；门面在每个函数**第一行**判定",
        tests=(
            f"{_CONTRACTS}::test_required_permissions_covers_exactly_the_seven_tools",
            f"{_CONTRACTS}::test_single_gate_rejects_before_any_side_effect",
        ),
        note="判定被挪到副作用之后时，越权请求已经写下了数据，而响应看起来只是「被拒」",
    ),
    Criterion(
        "4",
        "旧参数形态只在门面存在（REST 侧拒绝）",
        tests=(
            f"{_CONTRACTS}::test_legacy_id_accepts_only_the_decimal_string_from_the_requirement",
            f"{_CONTRACTS}::test_legacy_id_rejects_an_int_with_a_message_naming_the_canonical_target",
            f"{_CONTRACTS}::test_focus_points_json_must_be_a_json_string_array",
        ),
        note="两种写法都收时，「该传哪个」变成一个没有正确答案的问题，而两种都「看起来能用」",
    ),
    # ---------- 依赖方向 ----------
    Criterion(
        "5",
        "门面不依赖协议层；业务层不 import HTTP 框架；MCP 不依赖协议层与适配器",
        tests=(
            f"{_CONTRACTS}::test_facade_never_imports_the_http_layer",
            f"{_SOURCE}::test_business_layers_do_not_import_the_http_layer",
            f"{_SOURCE}::test_mcp_adapter_imports_neither_http_layer_nor_adapters",
        ),
        note=(
            "三者都是「违反了照样能跑」的规则：业务层 import 了 fastapi 之后，"
            "MCP / Worker 形态要么复制一份逻辑，要么构造假请求对象"
        ),
    ),
    # ---------- REST ↔ 门面同源 ----------
    Criterion(
        "6",
        "REST 与门面同源：七个工具各自逐字相等（两套独立装配）",
        tests=(
            f"{_CONTRACTS}::test_tool1_rest_and_facade_agree",
            f"{_CONTRACTS}::test_tool2_rest_and_facade_agree",
            f"{_CONTRACTS}::test_tool3_rest_and_facade_agree",
            f"{_CONTRACTS}::test_tool4_rest_and_facade_agree",
            f"{_CONTRACTS}::test_tool5_rest_and_facade_agree",
            f"{_CONTRACTS}::test_tool6_rest_and_facade_agree",
            f"{_CONTRACTS}::test_tool7_rest_and_facade_agree",
        ),
        note=(
            "用「包含关系」比较时，端点**多下发**了 object_key 或文件系统路径照样通过——"
            "因此比较的是逐字相等"
        ),
    ),
    Criterion(
        "7",
        "REST 转发它接受的企业上下文（`parse_options` / `force`）",
        tests=(f"{_CONTRACTS}::test_rest_forwards_the_enterprise_context_it_accepts",),
        note="两者都有默认值，端点忘了转发时接口照样 200——「强制重跑」会静默变成「复用」",
    ),
    # ---------- 任务 / 作业 / 评价 / 结果查询 ----------
    Criterion(
        "8",
        "分页与总数由后端给；排序稳定（同秒创建也不重不漏）",
        tests=(
            f"{_QUERIES}::test_totals_come_from_the_backend_not_from_the_page_length",
            f"{_QUERIES}::test_the_two_pages_do_not_overlap_and_cover_everything",
            f"{_QUERIES}::test_identical_created_at_still_gives_a_total_order",
        ),
        note="总数只有一页时永远等于页长，这类缺陷会一直活到数据量上来",
    ),
    Criterion(
        "9",
        "回写状态的两个层级（任务级 + 最近一次尝试级，带 `attempt_no`）",
        tests=(
            f"{_QUERIES}::test_task_detail_reports_both_writeback_levels",
            f"{_QUERIES}::test_latest_attempt_is_the_highest_attempt_no",
        ),
        note="缺口 G-4 的关闭点：任务级说「写成了没有」，尝试级说「最近一次为什么没成」",
    ),
    Criterion(
        "10",
        "`confirmation_valid` 由**后端**计算（缺口 G-3）",
        tests=(
            f"{_QUERIES}::test_confirmation_valid_is_computed_by_the_backend",
            f"{_QUERIES}::test_result_list_rows_have_the_same_shape_as_the_detail",
        ),
        note="前端自行比对摘要时，算错的方向恰恰是把失效的确认显示成有效，且不会报错",
    ),
    Criterion(
        "11",
        "四态评价一条不少，需关注的排前（排序而非过滤）",
        tests=(
            f"{_QUERIES}::test_evaluations_keep_all_four_states_and_order_attention_first",
            f"{_QUERIES}::test_evaluation_ordering_is_stable_within_the_same_weight",
        ),
        note="只返回 hit 时，「这条规则为什么没报警」永远答不出来",
    ),
    Criterion(
        "12",
        "非法过滤值 → 400（不是空列表）",
        tests=(
            f"{_QUERIES}::test_an_unknown_filter_value_is_rejected_instead_of_returning_nothing",
            f"{_RULES}::test_unknown_filter_value_is_rejected_instead_of_returning_nothing",
        ),
        note="空列表会被读成「没有阻塞的任务」，而真相是这个过滤值从来不存在",
    ),
    # ---------- 租户可见性（每一次查询，含嵌套 id）----------
    Criterion(
        "13",
        "租户门：嵌套 id 与整段 id 区间枚举都只得到 404，且响应无对方数据",
        tests=(
            f"{_QUERIES}::test_another_tenant_cannot_read_a_nested_resource_by_guessing_its_id",
            f"{_QUERIES}::test_enumeration_over_an_id_range_yields_no_data",
            f"{_QUERIES}::test_lists_never_mix_tenants",
            f"{_QUERIES}::test_a_cross_tenant_result_cannot_be_confirmed",
        ),
        note=(
            "403 会确认「这个 id 存在」，于是状态码本身成了逐位试出别人 id 的探针；"
            "写路由同样要过门（否则可以替对方确认一份结果）"
        ),
    ),
    Criterion(
        "14",
        "人工确认**权威审查上下文**：只有 complete 可确认、幂等、审计一次",
        tests=(
            f"{_QUERIES}::test_context_confirmation_requires_a_complete_context",
            f"{_QUERIES}::test_context_confirmation_is_idempotent_and_audited_once",
            f"{_QUERIES}::test_read_only_auditor_cannot_confirm_anything",
        ),
        note="missing / conflict 下放行，等于让人确认一个我们说不清是什么的东西，而门禁随后会把它当可信立场",
    ),
    # ---------- 附件内容与标准文档下发（缺口 G-1）----------
    Criterion(
        "15",
        "附件内容支持单区间 Range（含后缀区间、越界 416、不支持的 Range 忽略）",
        tests=(
            f"{_CONTENT}::test_a_valid_byte_range_returns_206_with_content_range",
            f"{_CONTENT}::test_an_open_ended_range_runs_to_the_end",
            f"{_CONTENT}::test_a_suffix_range_returns_the_last_bytes",
            f"{_CONTENT}::test_a_range_past_the_end_is_416",
            f"{_CONTENT}::test_a_zero_length_suffix_is_416",
            f"{_CONTENT}::test_a_header_we_do_not_support_is_ignored_not_416",
        ),
        note="`bytes=-100` 写成「从 100 开始」同样返回 206 与一段数据，只有逐字节比对才看得出",
    ),
    Criterion(
        "16",
        "响应里**永不出现**对象键与服务器路径（正文与全部响应头）",
        tests=(
            f"{_CONTENT}::test_the_object_key_never_appears_anywhere_in_the_response",
            f"{_CONTENT}::test_the_document_response_does_not_leak_the_object_key",
            f"{_CONTENT}::test_a_filename_cannot_inject_response_headers",
        ),
        note="文件名来自外部审批系统，是本模块唯一一处把外部字符串放进响应头的地方（响应头注入）",
    ),
    Criterion(
        "17",
        "下发接口的鉴权：无身份 401、只读可读、跨租户 404 且无字节泄漏",
        tests=(
            f"{_CONTENT}::test_reading_content_requires_an_identity",
            f"{_CONTENT}::test_a_read_only_auditor_may_read_content",
            f"{_CONTENT}::test_another_tenant_cannot_read_the_bytes",
        ),
        note="挡掉只读角色时，审计的人除了任务列表什么都看不到——「只读」名不副实也是缺陷",
    ),
    Criterion(
        "18",
        "两个 404 机器码不同：`RESOURCE_NOT_FOUND`（核对 id） vs `OBJECT_NOT_FOUND`（先跑工具 3）",
        tests=(
            f"{_CONTENT}::test_a_wrong_id_and_a_missing_object_are_different_machine_codes",
            f"{_CONTENT}::test_a_parse_without_an_artifact_is_404_object_not_found",
            f"{_CONTENT}::test_a_checksum_mismatch_is_refused_instead_of_delivered",
        ),
        note="合成一个码时，调用方会去改一个本来就对的 id",
    ),
    Criterion(
        "19",
        "标准文档原样下发（不重算坐标）+ 契约校验",
        tests=(f"{_CONTENT}::test_the_standard_document_is_delivered_verbatim",),
        note="下发时重算坐标会让消费方再应用一次 rotation，而画偏的框最难被发现",
    ),
    Criterion(
        "20",
        "校验和：区间请求也核验**整份**对象",
        tests=(f"{_CONTENT}::test_a_range_not_starting_at_zero_still_verifies_the_whole_object",),
        note="只核验被请求那一段时，「文件被换成另一份等长内容」在这条路径上完全看不出来",
    ),
    # ---------- 人工重试（Task 5）----------
    Criterion(
        "21",
        "重试矩阵：parse→parse / rule→rule / result→result / 任务回到对应状态",
        tests=(
            f"{_RETRY}::test_retry_matrix_reruns_exactly_the_failed_step",
            f"{_RETRY}::test_only_blocked_tasks_can_be_retried",
            f"{_RETRY}::test_a_running_job_is_not_reset",
            f"{_RETRY}::test_retry_without_a_matching_job_is_rejected",
            f"{_RETRY}::test_unresumable_stage_is_rejected_instead_of_queuing_a_dead_job",
        ),
        note=(
            "只断言状态时，一个「把状态改回去但没排作业」的实现照样通过——"
            "而那正是最坏的一种：界面显示已重试，Worker 那边什么都没发生"
        ),
    ),
    Criterion(
        "22",
        "回写失败只**重新武装投递**：不新建任何作业、归还完整重试预算",
        tests=(f"{_RETRY}::test_writeback_retry_rearms_delivery_and_does_not_rerun_anything",),
        note="不归还预算时，重武装出来的事件会立刻再次耗尽——人工重试看起来生效了，实际什么都没变",
    ),
    Criterion(
        "23",
        "重试必须带操作原因，且留痕（审计 `TASK_RETRIED` + 日志）",
        tests=(
            f"{_RETRY}::test_retry_requires_an_operator_reason",
            f"{_RETRY}::test_retry_is_audited_with_operator_and_reason",
        ),
        note="审计账只记「有人点了重试」时，没人回答得了「当时为什么要重试」",
    ),
    Criterion(
        "24",
        "重试与审计的权限：只读**与法务**都被拒；审计只对管理员/审计员开放",
        tests=(
            f"{_RETRY}::test_retry_requires_ops_permission",
            f"{_RETRY}::test_only_audit_readers_can_read_the_audit_trail",
            f"{_RETRY}::test_another_tenant_cannot_retry_a_task_by_guessing_its_id",
        ),
        note="只断言「只读被拒」时，一个把任意写权限当通行证的实现照样通过",
    ),
    Criterion(
        "25",
        "日志与审计查询：按关联 ID 可过滤；系统级事件**默认不可见**",
        tests=(
            f"{_RETRY}::test_logs_endpoint_returns_the_retry_trail",
            f"{_RETRY}::test_audit_endpoint_hides_system_events_by_default",
            f"{_RETRY}::test_logs_and_audit_stay_inside_the_tenant",
        ),
        note="默认带上系统级事件时，每个租户都能读到全局配置变更——多租户下是越权",
    ),
    # ---------- 规则管理（Task 5）----------
    Criterion(
        "26",
        "激活前校验：11 类齐、一次报全部问题、**覆盖停用规则**",
        tests=(
            f"{_RULES}::test_reload_passes_for_a_complete_valid_ruleset",
            f"{_RULES}::test_reload_reports_every_problem_at_once",
            f"{_RULES}::test_reload_also_checks_inactive_rules",
        ),
        note="停用期间配置是坏的不会被任何人发现，直到启用那一刻才炸——而那时它已经进了一个批次",
    ),
    Criterion(
        "27",
        "规则版本化：使用中的版本不得就地改写；提升版本允许；不得倒退；无变化不算变更",
        tests=(
            f"{_RULES}::test_updating_a_used_version_in_place_is_refused",
            f"{_RULES}::test_bumping_the_version_allows_the_change",
            f"{_RULES}::test_in_place_edit_is_allowed_before_the_first_use",
            f"{_RULES}::test_version_cannot_go_backwards",
            f"{_RULES}::test_a_noop_patch_is_not_a_change",
        ),
        note="判据是 `rule_hits.rule_version`（评价当时的快照）——就地改内容会让「版本 N 的含义」被静默改写",
    ),
    Criterion(
        "28",
        "规则写入前校验（非法配置一个字段都不写）；未提及字段不被清空",
        tests=(
            f"{_RULES}::test_create_rejects_an_invalid_config_without_writing_anything",
            f"{_RULES}::test_create_applies_the_same_semantic_judge_as_the_cli_check",
            f"{_RULES}::test_create_rejects_a_duplicate_rule_code",
            f"{_RULES}::test_update_requires_a_valid_config",
            f"{_RULES}::test_partial_update_does_not_clear_unmentioned_fields",
            f"{_RULES}::test_update_rejects_an_unknown_field",
        ),
        note=(
            "`transactional_session` 对业务异常**也提交**：先写一半再报错会留下"
            "「报错了但数据已改」的记录；而「缺省即清空」会把 applies_when 悄悄清成 NULL"
        ),
    ),
    Criterion(
        "29",
        "规则变更的审计事件**不属于任何任务**（`task_id` 为空）",
        tests=(
            f"{_RULES}::test_create_writes_a_system_level_audit_event",
            f"{_RULES}::test_rule_audit_events_are_visible_through_the_audit_endpoint",
        ),
        note="挂到某条任务上，审计里就会出现一条「看起来在说那条任务」的规则变更记录",
    ),
    Criterion(
        "30",
        "规则管理仅管理员：5 条路由逐条 403 + 无身份 401",
        tests=(
            f"{_RULES}::test_rule_management_is_admin_only",
            f"{_RULES}::test_rule_management_requires_authentication",
        ),
        note="规则是「系统怎么判」的输入，改一条会改变**所有**合同的结论",
    ),
    # ---------- MCP（Task 6）----------
    Criterion(
        "31",
        "MCP：恰好七个工具，名称与**必填参数**逐项一致，且都有描述",
        tests=(
            f"{_MCP}::test_exactly_seven_tools_with_the_required_inputs",
            f"{_MCP}::test_tools_describe_themselves_for_model_side_use",
        ),
        note="必填变可选会让模型侧构造出一个缺字段的调用，错误推迟到运行时才出现",
    ),
    Criterion(
        "32",
        "MCP 身份 fail-closed：无来源拒绝构造、两种来源拒绝、逐请求解、无凭据 401",
        tests=(
            f"{_MCP}::test_server_refuses_to_start_without_an_identity_source",
            f"{_MCP}::test_two_identity_sources_are_rejected",
            f"{_MCP}::test_http_transport_resolves_identity_per_request",
            f"{_MCP}::test_a_request_without_credentials_is_rejected",
        ),
        note="启动时解一次 = 所有人共用第一个请求的身份（越权），而响应上看不出异常",
    ),
    Criterion(
        "33",
        "MCP 与门面同源（工具 1/2/3 逐字相等）",
        tests=(
            f"{_MCP}::test_tool_1_pulls_and_reports_the_same_shape",
            f"{_MCP}::test_tool_2_matches_the_facade_byte_for_byte",
            f"{_MCP}::test_tool_3_success_matches_the_facade",
        ),
        note="比较的是**同一次**调用（第一次同步会创建附件记录，is_new 不同——那差异是测试自己造的）",
    ),
    Criterion(
        "34",
        "MCP 业务结论不是错误；代码缺陷**不**伪装成业务结论",
        tests=(
            f"{_MCP}::test_business_denial_comes_back_as_a_normal_result",
            f"{_MCP}::test_a_code_defect_surfaces_as_an_error_not_a_business_conclusion",
        ),
        note="把 blocked 报成 isError，会让每个调用方各写一遍「哪些错误其实不是错误」的判断",
    ),
    Criterion(
        "35",
        "MCP 长任务返回可查询的 `task_ref`",
        tests=(
            f"{_MCP}::test_tool_4_returns_a_task_ref_instead_of_blocking",
            f"{_MCP}::test_tool_5_returns_a_task_ref_pointing_at_the_run",
        ),
        note="长任务同步等待会让调用方一直挂着连接",
    ),
    Criterion(
        "36",
        "MCP 错误载荷保留稳定机器码（含 `ResultInputError` 的原因码）",
        tests=(
            f"{_MCP}::test_missing_resource_keeps_its_machine_code",
            f"{_MCP}::test_an_unknown_case_id_is_a_structured_error",
            f"{_MCP}::test_a_malformed_id_is_reported_as_an_argument_error",
            f"{_MCP}::test_permission_denial_becomes_a_structured_error",
        ),
        note="`ResultInputError` 落进 `ValueError` 兜底分支时，REST 与 MCP 会对同一次调用给出相反的处置方向",
    ),
    Criterion(
        "37",
        "MCP stdio 身份从环境变量解出；缺失则**拒绝启动**",
        tests=(f"{_MCP}::test_stdio_identity_is_required_and_resolved_from_the_environment",),
        note="抛 AuthConfigurationError 而不是 AuthenticationError：缺配置是启动问题，调用方补头解决不了",
    ),
    # ---------- 身份与授权（Task 1 的复核）----------
    Criterion(
        "38",
        "生产环境 fail-closed：ENV=production 下拒绝启动，绝不降级为匿名",
        tests=(
            f"{_RBAC}::test_production_with_dev_mode_is_refused",
            f"{_RBAC}::test_production_without_any_verification_key_is_refused",
            f"{_RBAC}::test_production_without_issuer_is_refused",
            f"{_RBAC}::test_production_without_audience_is_refused",
            f"{_RBAC}::test_unknown_auth_mode_is_refused_not_silently_downgraded",
            f"{_RBAC}::test_jwt_without_a_key_is_a_configuration_error_not_401",
        ),
        note="JWT 缺密钥原本抛 AuthenticationError（→401，让调用方去重新登录），实为运维配错，已改为配置错误（→500）",
    ),
    Criterion(
        "39",
        "401 与 403 可区分；错误消息里不出现令牌原文",
        tests=(
            f"{_RBAC}::test_authentication_and_authorization_errors_are_distinguishable",
            f"{_RBAC}::test_request_without_identity_is_401",
            f"{_RBAC}::test_request_with_read_only_role_is_403_on_mutation",
            f"{_RBAC}::test_request_with_unrecognised_role_is_403_not_500",
            f"{_RBAC}::test_request_with_mismatched_tenant_is_401",
            f"{_RBAC}::test_rejected_token_text_never_appears_in_the_error",
        ),
        note="合并成一个码时，「我没登录」与「我登录了但没权限」在客户端看起来一样，处置只能靠猜",
    ),
    Criterion(
        "40",
        "角色→权限映射不可运行时改写；未识别角色不给任何权限",
        tests=(
            f"{_RBAC}::test_role_permission_map_cannot_be_mutated_at_runtime",
            f"{_RBAC}::test_unknown_role_grants_nothing_but_stays_visible",
            f"{_RBAC}::test_read_only_auditor_has_no_mutation_permissions",
        ),
        note="运行时改映射表等于在不重启的情况下放开权限，且不留任何审计痕迹",
    ),
    # ---------- 结构 ----------
    Criterion(
        "41",
        "审计动作取值域含 M7 三个新动作；系统级事件（task_id 为空）允许入库",
        tests=(
            f"{_SCHEMA}::test_outbox_and_audit_enums",
            f"{_SCHEMA}::test_audit_event_may_belong_to_no_task",
        ),
        note="需求 §12 要求「规则修改」也进不可变审计账，而改一条规则影响的是所有任务",
    ),
    Criterion(
        "42",
        "路由不得重复注册（后注册的那一条永远不生效且不报错）",
        tests=(f"{_QUERIES}::test_no_route_path_is_registered_twice",),
        note="M7 把 /api/results/{id} 从 jobs 挪到 results 时正是靠这条守住的",
    ),
    Criterion(
        "43",
        "既有测试全绿（整个 tests/ 目录）",
        tests=("tests/",),
        allow_skipped=True,
        note="跳过项（慢 OCR 等）由逐条验收各自计数，本项只关心有没有失败",
    ),
)


def _subprocess_env() -> dict[str, str]:
    """子进程环境：**剥掉 PYTHONPATH**。

    IDE 会把自家的 `sitecustomize.py` 钩子经 PYTHONPATH 注入每个 Python 进程，
    而它会在解释器退出时（对批量删除）抛 `SystemExit(1)` —— pytest 于是把
    **teardown** 记成 ERROR，而测试本体是绿的（verify_m5 验收 31 那个
    "时红时绿"的根因；M7 期间在 `shutil.rmtree(.pytest_tmp/test-*)` 上又复现了一次）。

    验收子进程不需要继承它：这里跑的是本项目自己的测试。
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
            return "MISSING", "pytest 没有收集到任何用例（退出码 5 —— 引用可能已失效）"
        return (
            "MISSING",
            f"没有采集到任何用例结果（pytest 退出码 {completed.returncode}）",
        )

    if node.endswith("/"):
        wanted = list(outcomes.values())
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

    # ⚠️ `len(wanted)` 是**收集到的节点数**，不是通过数 —— 跳过项也在里面。
    # 初版写成"`N` 条通过（跳过 `M` 条）"，而这个数字被**当成通过数记录进了文档**
    # （M7 的"1339 passed"实为"收集 1339、其中 3 条跳过 → 1336 通过"）。
    # 一个会让人读错的统计口径，比没有统计更坏：它把偏差写进证据里。
    return "PASSED", (
        f"收集 {len(wanted)} 条，通过 {len(wanted) - skipped} 条"
        + (f"，跳过 {skipped} 条" if skipped else "")
    )


def _criterion_of(node: str) -> Criterion:
    for criterion in CRITERIA:
        if node in criterion.tests:
            return criterion
    return Criterion("?", "?", ())


def collect() -> list[Finding]:
    """逐条跑，**并逐条打印进度**。

    ⚠️ 进度输出不是"好看"：本脚本按 `verify_m6` 的形态**逐条引用各起一次 pytest**
    （43 条共约一百个子进程），全跑完要数分钟。若只在最后打印一张表，
    中途失败要等到全部跑完才看得见 —— 而验收是**迭代**着用的：
    先跑一遍、修、再跑，等待时间直接乘上迭代次数。
    因此每出一条就 flush 一行（`python -u` 下即时可见）。
    """
    findings: list[Finding] = []
    total = len(CRITERIA)

    for index, criterion in enumerate(CRITERIA, start=1):
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

        mark = "OK  " if overall == "PASSED" else overall
        print(
            f"  [{mark:>6}] {criterion.number:>2}/{total}. {criterion.title}",
            flush=True,
        )
        findings.append(Finding(criterion, overall, "；".join(details)))
    return findings


def report(findings: list[Finding], *, verbose: bool) -> int:
    passed = sum(1 for item in findings if item.ok)
    total = len(findings)

    print("\n[M7 acceptance evidence]")
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
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
        pass

    parser = argparse.ArgumentParser(description="M7 验收证据生成器")
    parser.add_argument("--verbose", action="store_true", help="打印每条的完整实测说明")
    args = parser.parse_args(argv)

    return report(collect(), verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
