"""受控的规则配置 DTO 与解析函数。

规则配置（`applies_when_json` / `match_text` / `fallback_match_json`）在数据库里是
JSON 文本。如果只当作字符串存取、等到运行时才 `json.loads`，配置写错要到
"某条规则执行时"才暴露，而且失败方式取决于实现细节（静默跳过还是抛异常）。

这里为每一类 JSON 建立 Pydantic 模型并集中解析：

- **`extra="forbid"`：未知键直接报错。**
  规则配置由人手写，最常见的错误就是键名拼错。若把 `contract_types` 写成
  `contract_type`，Pydantic 默认会把多余字段**静默丢弃**，规则于是退化成
  "全局适用"——而这种错误几乎无法通过观察输出发现。

- **枚举字段引用 `app/enums.py`**：非法取值直接报错（如 `our_business_roles`
  里写了 `purchaser` 而不是 `buyer`）。

- **`match_text` 按 `match_mode` 分派**到不同模型校验，正则模式还会实际尝试编译。

任何一处校验失败 → 抛出 `RuleConfigError` → 规则加载失败并写日志，
**不允许带病运行**（spec §4.3）。

注：面向接口层的 DTO（M2 起）也会放在本模块。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.enums import (
    BusinessRole,
    ContractLabel,
    ContractType,
    MatchMode,
    RiskLevel,
    RuleStatus,
)
from app.rules.fields import is_known

# ============================================================
# 基类与异常
# ============================================================


class StrictModel(BaseModel):
    """拒绝未知字段的基类（`extra="forbid"`）。

    规则配置是人工手写的 JSON，键名拼错是最常见的错误，
    因此全项目统一禁止多余字段，而不是默认忽略。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleConfigError(ValueError):
    """规则配置非法。加载遇到此异常必须让规则加载失败并写日志。"""


class ParseOptions(StrictModel):
    """解析参数。每个字段都有**默认值与取值范围**。

    ⚠️ 没有范围的数值参数，等于把校验推迟到最难排查的时刻 ——
    `dpi=1` 或 `ocr_min_confidence=5.0` 都能进库，问题只在渲染/判定时才浮现，
    而那时离"参数写错"这个真正的原因已经很远了。

    ⚠️ 本类**必须定义在这里**而不是 `app/workflow/job_inputs.py`：
    两者都要用它（工具 4 的请求体、解析作业的输入），而 `job_inputs`
    已经依赖 `app.schemas.StrictModel` —— 定义在 `job_inputs` 就构成循环导入。
    依赖方向是单向的 `workflow → schemas`。
    """

    dpi: int = Field(default=200, ge=72, le=600)
    ocr_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    #: 文本层判定阈值（设计文档 §4.2 的 OCR 路由判据）
    min_chars: int = Field(default=20, ge=1)
    #: ⚠️ **不要按"文字应该占页面的多少"来设这个值。** 真实文本件的覆盖率很低：
    #: A4 上 30 行正文，文字面积只占页面的 **0.09 ~ 0.13**（实测见下）。
    #: 初版取 0.3 的直接后果是：**每一份干净的文本 PDF 都被判"要走 OCR"** ——
    #: 慢、精度从 char 掉到 line，而没有 OCR 引擎时直接 `FAILED(OCR_MODEL_UNAVAILABLE)`。
    #:
    #: | 夹具 | 字符数 | 覆盖率 |
    #: | --- | --- | --- |
    #: | `contract_01_clean.pdf` | 336 | 0.1159 |
    #: | `contract_03_dev_no_ip.pdf` | 375 | 0.1293 |
    #: | `contract_06_conflict.pdf` | 256 | 0.0883 |
    #:
    #: 取 0.02 是为了**只抓真的"几乎没有文字"**（水印/页眉页脚场景覆盖率约 0.001，
    #: 低两个数量级）。主力判据其实是 `min_chars`，本项是它的补充。
    min_coverage: float = Field(default=0.02, ge=0.0, le=1.0)
    max_garbage_ratio: float = Field(default=0.2, ge=0.0, le=1.0)


# ============================================================
# 适用条件
# ============================================================


class AppliesWhenConfig(StrictModel):
    """规则的适用条件（对应 `review_rules.applies_when_json`）。

    全部字段可省略；**全空或 NULL 等价于"不限制"**（全局适用）。

    两个轴必须分清：
      - `our_contract_labels` 回答"这条规则说的是我吗"（合同正文里的甲方/乙方）；
      - `our_business_roles`  回答"这对我是好是坏"（付钱还是收钱）。
    """

    contract_types: list[ContractType] | None = None
    our_contract_labels: list[ContractLabel] | None = None
    our_business_roles: list[BusinessRole] | None = None
    # 合同正文必须出现其中任意一个词，规则才适用。
    # 这是压制"缺失类规则"误报最有效的手段：一份与数据无关的合同，
    # 不该因为"没写数据处理条款"而被报高风险。
    requires_any_keyword: list[str] | None = None

    @field_validator(
        "contract_types", "our_contract_labels", "our_business_roles",
        "requires_any_keyword",
    )
    @classmethod
    def _reject_empty_list(cls, v: list[Any] | None) -> list[Any] | None:
        # 空数组是有歧义的写法：它既可能表示"不限制"，也可能表示"谁都不适用"。
        # 与其猜测，不如要求显式写成 null。
        if v is not None and len(v) == 0:
            raise ValueError("不得使用空数组，请省略该键或显式写 null")
        return v

    @field_validator("requires_any_keyword")
    @classmethod
    def _reject_blank_keyword(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(not k.strip() for k in v):
            raise ValueError("requires_any_keyword 不能包含空白字符串")
        return v

    @property
    def is_unrestricted(self) -> bool:
        """是否不限制适用范围。"""
        return not any(
            (
                self.contract_types,
                self.our_contract_labels,
                self.our_business_roles,
                self.requires_any_keyword,
            )
        )


# ============================================================
# 匹配条件（按 match_mode 分派）
# ============================================================


class KeywordMatchConfig(StrictModel):
    """关键词匹配。

    `absent=True` 表示"全文均未出现才命中"——即"条款缺失"类规则。
    这类规则最容易误报，因此通常需要配 `applies_when` 限定适用范围。
    """

    keywords: list[str] = Field(min_length=1)
    absent: bool = False

    @field_validator("keywords")
    @classmethod
    def _reject_blank(cls, v: list[str]) -> list[str]:
        if any(not k.strip() for k in v):
            raise ValueError("keywords 不能包含空白字符串")
        return v


class RegexMatchConfig(StrictModel):
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _must_compile(cls, v: str) -> str:
        # 提前编译：正则写错必须在加载阶段暴露，而不是等到匹配时才炸
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"正则表达式无法编译：{exc}") from exc
        return v


class LlmMatchConfig(StrictModel):
    instruction: str = Field(min_length=1)


ExprOp = Literal["gt", "gte", "lt", "lte", "eq", "contains", "is_null", "not_null"]


class ExprMatchConfig(StrictModel):
    """字段/数值比较。

    `is_null` / `not_null` 是**存在性判断**，不应提供 `value`；
    其余操作符必须提供 `value`。这个区分强制写出来，
    是为了避免 `{"op": "gt"}` 这种"忘了写阈值"的配置进入生产。
    """

    field: str = Field(min_length=1)
    op: ExprOp
    value: float | str | None = None

    @field_validator("field")
    @classmethod
    def _field_must_be_known(cls, v: str) -> str:
        # 字段名拼错会静默退化成"字段为空"，进而被解释成"合同未约定"——
        # 一个拼写错误会变成一条错误的风险结论，因此必须在此拦截
        if not is_known(v):
            raise ValueError(f"未知字段 {v!r}，请先在 app/rules/fields.py 中登记")
        return v

    @model_validator(mode="after")
    def _value_consistency(self) -> ExprMatchConfig:
        if self.op in {"is_null", "not_null"}:
            if self.value is not None:
                raise ValueError(f"op={self.op} 是存在性判断，不应提供 value")
        elif self.value is None:
            raise ValueError(f"op={self.op} 必须提供 value（阈值或比较值）")
        return self


MatchCondition = (
    KeywordMatchConfig | RegexMatchConfig | LlmMatchConfig | ExprMatchConfig
)

MATCH_CONFIG_BY_MODE: Final[dict[MatchMode, type[StrictModel]]] = {
    MatchMode.KEYWORD: KeywordMatchConfig,
    MatchMode.REGEX: RegexMatchConfig,
    MatchMode.LLM: LlmMatchConfig,
    MatchMode.EXPR: ExprMatchConfig,
}

# fallback 只允许确定性条件。
# 不允许 fallback 再引用 llm —— 那等于"降级到需要再降级的东西"，会形成递归依赖。
FallbackCondition = KeywordMatchConfig | RegexMatchConfig


# ============================================================
# 内部工具
# ============================================================


def _format_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg')}")
    return "；".join(parts)


def _load_json(raw: str, *, field_name: str, rule_code: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuleConfigError(
            f"规则 {rule_code} 的 {field_name} 不是合法 JSON：{exc}"
        ) from exc


# ============================================================
# 解析函数
# ============================================================


def parse_applies_when(
    raw: str | None, *, rule_code: str
) -> AppliesWhenConfig | None:
    """解析 `applies_when_json`。

    `NULL` / 空串 / `{}` 一律视为"不限制"，返回 `None`。
    """
    if raw is None or not raw.strip():
        return None

    data = _load_json(raw, field_name="applies_when_json", rule_code=rule_code)
    if not isinstance(data, dict):
        raise RuleConfigError(
            f"规则 {rule_code} 的 applies_when_json 必须是 JSON 对象"
        )
    if not data:
        return None

    try:
        return AppliesWhenConfig.model_validate(data)
    except ValidationError as exc:
        raise RuleConfigError(
            f"规则 {rule_code} 的适用条件非法：{_format_errors(exc)}"
        ) from exc


def parse_match_config(
    match_mode: str, raw: str, *, rule_code: str
) -> MatchCondition:
    """按 `match_mode` 解析 `match_text`。"""
    try:
        mode = MatchMode(match_mode)
    except ValueError as exc:
        raise RuleConfigError(
            f"规则 {rule_code} 的 match_mode={match_mode!r} 不是合法取值"
        ) from exc

    data = _load_json(raw, field_name="match_text", rule_code=rule_code)
    if not isinstance(data, dict):
        raise RuleConfigError(f"规则 {rule_code} 的 match_text 必须是 JSON 对象")

    model = MATCH_CONFIG_BY_MODE[mode]
    try:
        return model.model_validate(data)  # type: ignore[return-value]
    except ValidationError as exc:
        raise RuleConfigError(
            f"规则 {rule_code} 的 match_text（{mode} 模式）非法：{_format_errors(exc)}"
        ) from exc


def parse_fallback_config(
    raw: str | None, *, rule_code: str
) -> FallbackCondition | None:
    """解析 `fallback_match_json`（仅 llm 规则需要）。

    按出现的键自动判定类型：有 `keywords` 视为关键词条件，有 `pattern` 视为正则条件。
    """
    if raw is None or not raw.strip():
        return None

    data = _load_json(raw, field_name="fallback_match_json", rule_code=rule_code)
    if not isinstance(data, dict):
        raise RuleConfigError(
            f"规则 {rule_code} 的 fallback_match_json 必须是 JSON 对象"
        )

    if "keywords" in data:
        model: type[StrictModel] = KeywordMatchConfig
    elif "pattern" in data:
        model = RegexMatchConfig
    else:
        raise RuleConfigError(
            f"规则 {rule_code} 的 fallback_match_json 必须包含 keywords 或 pattern"
        )

    try:
        return model.model_validate(data)  # type: ignore[return-value]
    except ValidationError as exc:
        raise RuleConfigError(
            f"规则 {rule_code} 的降级条件非法：{_format_errors(exc)}"
        ) from exc


# ============================================================
# 完整规则配置
# ============================================================


@dataclass(frozen=True)
class RuleConfig:
    """一条规则的完整受控配置。

    `from_row` 是**唯一的构造入口**，任何加载路径都走它，
    保证校验规则不会被绕过。
    """

    rule_code: str
    rule_name: str
    rule_category: str | None
    risk_level: str
    priority: int
    rule_version: int
    match_mode: MatchMode
    applies_when: AppliesWhenConfig | None
    match_condition: MatchCondition
    fallback_condition: FallbackCondition | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> RuleConfig:
        """从数据库行构造。

        Args:
            row: 支持 `[]` 取值的映射（sqlite3.Row、SQLAlchemy 对象转成的 dict 均可），
                 需包含 `rule_code` / `rule_name` / `match_mode` / `match_text`
                 及可选的 `applies_when_json` / `fallback_match_json` 等字段。

        Raises:
            RuleConfigError: 任一字段非法。**调用方必须让加载失败**，不能跳过该规则。
        """
        rule_code = str(row["rule_code"])
        match_mode = str(row["match_mode"])

        applies_when = parse_applies_when(row["applies_when_json"], rule_code=rule_code)
        match_condition = parse_match_config(
            match_mode, str(row["match_text"]), rule_code=rule_code
        )

        fallback_raw = row["fallback_match_json"]
        fallback_condition = parse_fallback_config(fallback_raw, rule_code=rule_code)

        # 降级条件只对 llm 规则有意义：
        # 其他模式本身就是确定性的，再配 fallback 属于配置冗余，
        # 而且会让人误以为"走的是降级路径"，必须显式报错。
        if fallback_condition is not None and match_mode != MatchMode.LLM.value:
            raise RuleConfigError(
                f"规则 {rule_code} 的 match_mode={match_mode} 不需要 fallback_match_json"
            )

        return cls(
            rule_code=rule_code,
            rule_name=str(row["rule_name"]),
            rule_category=row["rule_category"],
            risk_level=str(row["risk_level"]),
            priority=int(row["priority"]),
            rule_version=int(row["rule_version"]),
            match_mode=MatchMode(match_mode),
            applies_when=applies_when,
            match_condition=match_condition,
            fallback_condition=fallback_condition,
        )

    @property
    def is_direction_sensitive(self) -> bool:
        """规则是否依赖我方立场（用于统计与文档核对）。"""
        if self.applies_when is None:
            return False
        return bool(
            self.applies_when.our_contract_labels
            or self.applies_when.our_business_roles
        )


def risk_level_of(config: RuleConfig) -> RiskLevel | None:
    """把 `risk_level` 文本安全地转成枚举（非法值返回 None 而非抛异常）。"""
    try:
        return RiskLevel(config.risk_level)
    except ValueError:
        return None


# ============================================================
# 工具接口的请求 DTO（M3）
# ============================================================
# 与规则配置同属"输入校验"，因此放在同一模块（见模块 docstring）。
# 工具调用由外部系统或大模型生成，**键名拼错**同样是常见错误，
# 静默丢弃会让调用方以为参数生效了，所以一并 `extra="forbid"`。


class ToolRequest(StrictModel):
    """工具请求基类：拒绝未知字段，并去掉字符串两侧空白。

    `str_strip_whitespace=True` 与字段上的 `min_length=1` **必须配合使用**，
    否则拦不住 `"   "` 这种"看起来有值"的空白标识。
    放它进去的后果很具体：会拼出 `/api/instances//comments` 这类畸形路径，
    外部系统返回 404，而我们报"审批单不存在"—— 归因完全错位。
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True
    )


class ListPendingRequest(ToolRequest):
    """工具 1「拉取待审批合同」请求。"""

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="本次拉取的条数上限（1–100）",
    )


class GetApprovalRequest(ToolRequest):
    """工具 2「查询审批单详情」请求。"""

    instance_id: str = Field(min_length=1, description="审批单实例号")


class DownloadAttachmentRequest(ToolRequest):
    """工具 3「下载合同附件」请求。"""

    instance_id: str = Field(min_length=1, description="审批单实例号")
    attachment_id: str = Field(min_length=1, description="附件编号")
    file_name: str | None = Field(
        default=None,
        description=(
            "可选的文件名覆盖。不传则用外部系统返回的文件名；"
            "无论哪种来源都会做路径安全化处理"
        ),
    )


class RunContractRulesRequest(ToolRequest):
    """工具 5「执行合同规则审查」请求。

    ⚠️ **没有"我方立场"这类参数**：立场是审批记录里的既有事实
    （`approval_tasks.our_party_business_role` 等），由服务端读取并**冻结进批次快照**。
    让调用方传，等于让"我们认为我方是谁"变成一个可以随手改的入参 ——
    而它决定了每一条方向敏感规则**该不该判**。
    """

    parse_id: int = Field(
        ge=1, description="要审查的解析结果（必须是已通过质量门禁的 `succeeded`）"
    )
    force: bool = Field(
        default=False,
        description=(
            "强制新建批次。默认复用：六项输入全同则返回既有批次，不重跑。"
            "含 `needs_review` 的批次需要重跑时用它"
        ),
    )


class ParseContractDocumentRequest(ToolRequest):
    """工具 4「解析合同文档」请求。

    ⚠️ `document_id` 是**本系统主键** `approval_attachments.id`，
    不是外部审批系统那个附件编号（后者是 TEXT，见 §4.5 修-15）。
    两者都叫"attachment id"，用错的表现是"解析了另一个附件的内容"——
    或者更常见：查不到记录、报"附件不存在"，而附件明明在。
    """

    document_id: int = Field(
        ge=1,
        description=(
            "附件的**本系统主键**（`approval_attachments.id`），"
            "由工具 3 或任务查询接口给出；不是外部系统的附件编号"
        ),
    )
    parse_options: ParseOptions | None = Field(
        default=None,
        description="可选的解析参数覆盖。不传则用服务端默认值（§4.5）",
    )


class SaveReviewResultRequest(ToolRequest):
    """工具 6「保存审查结果」请求（需求 2.4.10）。

    ⚠️ `overall_risk_level` 由调用方传入但**必须**与批次聚合一致 ——
    服务层校验不等时返回稳定错误码 `RESULT_INPUT_MISMATCH`：
    拿旧批次的风险等级保存新结果，会把两份口径焊进同一条记录。

    ⚠️ 没有 `actor` 字段：保存者是**服务端事实**（来自 API 层的请求身份），
    不由调用方自称 —— 否则审计账里会出现"模型替人签字"。
    """

    run_id: int = Field(
        ge=1,
        description="审查批次（工具 5 返回的 `run_id`），必须是已完成批次",
    )
    overall_risk_level: str = Field(
        min_length=1,
        description="总体风险等级（low / medium / high），必须与批次聚合一致",
    )
    summary_text: str = Field(min_length=1, description="审查结论摘要")
    focus_points: list[str] = Field(
        default_factory=list, description="关注点列表（原文要点，非规则编号）"
    )
    comment_text: str = Field(
        min_length=1, description="回写到审批系统的意见正文"
    )


class WriteApprovalCommentRequest(ToolRequest):
    """工具 7「回写审批意见」请求（需求 2.4.10）。

    ⚠️ **没有 `comment_text` 字段**：回写的正文取自**已保存的结果**
    （`result_id` → `review_results.comment_text`）。让调用方在这里再传一份正文，
    会出现"人确认的是 A 版正文、实际写出去的是 B 版"——
    而那条审计记录（`RESULT_CONFIRMED`）指向的仍然是 A。
    正文的唯一入口是工具 6。

    ⚠️ **没有 `idempotency_key` 字段**：幂等键由服务端按
    `(provider, tenant, instance, result_id, content_digest)` 算出。
    让调用方传，等于把"这两次请求是不是同一件事"的判据交给外部 ——
    传一个随机值就能给同一个结果写出两条评论。

    ⚠️ 没有 `actor` 字段，理由同工具 6（见 `SaveReviewResultRequest`）。
    """

    instance_id: str = Field(
        min_length=1,
        description="审批单实例号（与结果所属任务必须一致）",
    )
    result_id: int = Field(
        ge=1,
        description="要回写的结果 id（工具 6 返回的 `result_id`）",
    )


# ============================================================
# 管理接口的请求 DTO（M7 / Task 5）
# ============================================================
# 与工具请求同一套约定（`extra="forbid"` + 去首尾空白）：请求体由前端或运维脚本
# 生成，**键名拼错**同样是常见错误，而静默丢弃会让调用方以为参数生效了。
#
# ⚠️ 规则配置（`match_text` / `applies_when_json` / `fallback_match_json`）
# 在本层**只做非空与长度**，内容校验一律交给 `app/schemas.py::RuleConfig`
# 与 `app/rules/validation.py` —— 那两处才是受控结构的唯一定义。
# 在这里再写一遍"什么样的 keywords 合法"，会多出一份会漂移的判据。


class RuleAdminRequest(StrictModel):
    """管理接口请求体的基类（与 `ToolRequest` 同一套严格性）。"""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True
    )


class CreateRuleRequest(RuleAdminRequest):
    """新建一条审查规则。

    ⚠️ `rule_code` 是**稳定标识**：它进历史评价、进批次快照、进界面，
    因此建好之后不提供改名入口（改名等于让历史记录指向一个不存在的规则）。
    """

    rule_code: str = Field(min_length=1, max_length=128)
    rule_name: str = Field(min_length=1, max_length=200)
    match_mode: MatchMode
    match_text: str = Field(min_length=1)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    rule_category: str | None = Field(default=None, max_length=64)
    priority: int = Field(default=100, ge=0)
    rule_version: int = Field(default=1, ge=1)
    rule_status: RuleStatus = RuleStatus.ACTIVE
    applies_when_json: str | None = None
    fallback_match_json: str | None = None
    exclude_text: str | None = None
    suggestion_text: str | None = None


class UpdateRuleRequest(RuleAdminRequest):
    """修改一条审查规则。

    ⚠️ **只有显式提供的字段才会被修改**（`exclude_unset`）：
    全字段可选的请求体若按"缺省即清空"处理，一次只想改名字的调用会把
    `applies_when_json` 悄悄清成 NULL —— 规则于是从"仅对软件合同适用"
    退化成"全局适用"，而响应里看不出任何异常。

    显式传 `null` 仍然是**有意义的操作**（清空该字段），因此判据是
    "字段有没有出现在请求体里"，不是"值是不是 `None`"。
    """

    rule_name: str | None = Field(default=None, min_length=1, max_length=200)
    match_mode: MatchMode | None = None
    match_text: str | None = Field(default=None, min_length=1)
    risk_level: RiskLevel | None = None
    rule_category: str | None = Field(default=None, max_length=64)
    priority: int | None = Field(default=None, ge=0)
    rule_version: int | None = Field(default=None, ge=1)
    rule_status: RuleStatus | None = None
    applies_when_json: str | None = None
    fallback_match_json: str | None = None
    exclude_text: str | None = None
    suggestion_text: str | None = None


class UpdateResultCommentRequest(RuleAdminRequest):
    """人工修改**回写正文**（M8 Task 7 的薄出口）。

    ## 为什么只有一个字段

    它给控制台用（控制台只调 `/api/*`，走不到工具 6 的 `/tools/*`），
    因此这里**只允许改正文**：风险等级 / 摘要 / 关注点一概不接受。

    理由不是"少做点"，而是工具 6 的硬约束：`overall_risk_level` 必须与批次聚合
    一致（不等 → `RESULT_INPUT_MISMATCH`）。把这个选择重新开放给控制台，
    等于把那个已经被关掉的口子再开一次 —— 而"拿旧批次的风险等级保存新结果"
    会把两份口径焊进同一条记录，那条记录看起来完全正常。

    ## 它没有开第二个"正文入口"

    `WriteApprovalCommentRequest` 的 docstring 写着"正文的唯一入口是工具 6"。
    这个请求体**不违反**那句话：它的实现把 `comment_text` 交给
    **工具 6 用的同一个服务函数**（版本化 / 版本链 / 摘要重算 / 审计都在那里发生）。
    第二条入口指的是第二份**实现**，不是第二个协议适配层。
    """

    comment_text: str = Field(
        min_length=1,
        description="修改后的回写意见正文（其余口径取自该结果自身，不由本接口修改）",
    )


class ConfirmContextRequest(RuleAdminRequest):
    """人工**修正**权威审查上下文（M8 补，设计 §4.2）。

    ## 为什么必须有"修正"，而不是只有"确认"

    报告确认接口只允许 `complete` 状态确认（那是正确的：`missing` / `conflict`
    下"确认"没有可确认的对象）。但由此产生一个**死锁**：

    ```text
    context_status = missing（我方立场没拿到）
      → 不允许确认（没有对象）
      → 也没有任何接口可以**填进去**
      → 任务永久停在此状态，而回写门禁要求可信立场
    ```

    而 `missing` 是**刚拉取完任务的正常状态**（M3 已定），不是异常。
    因此"修正"不是锦上添花，是让这条链路能走通的那一步。

    ## 四条**必须齐全**

    只给其中两条时，剩下两条还是旧值 —— 于是"我方是谁"与"这对我是好是坏"
    可能自相矛盾（例如把业务角色改成 `seller` 却留着原来的合同类型）。
    这类不一致**不会被任何校验发现**，它只会让规则方向判错。
    全量提交把"改一半"这件事变成不可表达。

    ## 为什么四个值都用枚举类型

    `ContractLabel` / `BusinessRole` / `ContractType` 是取值域的唯一定义处
    （`app/enums.py`）。在这里用 `str` 时，一个拼错的 `buyer` → `buyerr`
    会以 200 落库，而规则侧把它读成"未知角色" —— 于是方向敏感的规则
    全部不适用，报告上却看不出任何异常。
    """

    our_party_name: str = Field(
        min_length=1, max_length=200, description="我方在合同中的名称"
    )
    our_party_contract_label: ContractLabel = Field(
        description="我方在正文中的形式标签（party_a / party_b / other / unknown）"
    )
    our_party_business_role: BusinessRole = Field(
        description="我方在交易中的实际身份（buyer / seller / …），规则方向判断用它"
    )
    contract_type: ContractType = Field(
        description="合同业务分类（procurement / sales / …），决定规则是否适用"
    )


class RetryTaskRequest(RuleAdminRequest):
    """人工重试一条 `blocked` 任务。

    `reason` **必填**：它回答"为什么现在要重试"。一次网络抖动后的重试与一次
    排查后的人工重试在事后是两件事 —— 而审计账只记了"有人点了重试"时，
    没人回答得了"当时为什么要重试"。

    ⚠️ 这里**刻意不加 `min_length=1`**：那样缺字段会得到框架的 422，
    而空白串（`"   "`，被 `str_strip_whitespace` 清成 `""`）得到 400 ——
    **同一件事两种状态码**，而"原因必填"是一条**业务规则**（它要进审计账），
    因此只在服务层判一次（`retry_service._require_reason`），
    两种输入都得到同一个稳定的 400 `INVALID_ARGUMENT`。
    """

    reason: str = Field(
        default="",
        max_length=500,
        description="重试原因（进审计与日志，必填）",
    )
