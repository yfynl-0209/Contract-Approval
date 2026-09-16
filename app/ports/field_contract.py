"""字段 JSON 契约（设计文档 §4.8 修-8 / 修-16）—— M5 与 M8 消费。

`contract_parses.basic_info_json` 与 `clause_info_json` 用的是**同一个结构**：
两者都是"一组带证据的结论"，只是一个装基本信息、一个装条款。

## 四条硬约束

**① 根结构必须带 `schema_version`**（修-16）。
这两个字段是**长期存放**的。没有版本号时，读到一个缺字段的旧记录，
消费方无法区分"旧格式"与"数据损坏"，只能加防御性判断 ——
那种判断一旦写错就变成静默的默认值。

**② 金额绝不用 JSON 浮点数**（修-16）。
`value_decimal` 是**十进制字符串**，币种是**独立字段**。
合成 `"人民币 1,200,000.00 元"` 的话，M5 的金额阈值规则只能对中文串做匹配 ——
既无法比较大小，也无法处理"USD 200,000"与"CNY 1,200,000"这类**不可比**的情况。

**③ `reason_code` 必须是稳定枚举**（修-16）。
检索范围、说明文字一律放 `reason_text`。把自然语言塞进 `reason_code`
会让它无法统计、无法看板、无法断言 —— 这三件事正是它存在的理由。

**④ 字段码必须**在**白名单里**（本稿新增）。
初版只查重、不查合法性，于是 `intellectual_propertys` 与 `totally_unknown`
都能构造成功 —— 而"防止这类拼写错误"**正是白名单存在的唯一理由**。
一份不校验白名单的契约，等于把白名单降级成文档。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums import BboxPrecision, FieldStatus, ReasonCode, TextPrecision
from app.ports.parse_document import BBox
from app.rules.clauses import ALL_CLAUSE_TYPES, REQUIRED_CLAUSE_TYPES
from app.rules.fields import ALL_EXPR_FIELDS, BASIC_INFO_FIELDS, DIRECT_FIELDS

SCHEMA_VERSION = 1

#: 需要**十进制定点**表达的字段码 —— 它们绝不允许出现浮点数表示
DECIMAL_FIELD_CODES = frozenset({"amount", "prepay_ratio"})


class EvidenceSpan(BaseModel):
    """一处证据：指向标准文档里的一个块与字符区间。

    `char_start` / `char_end` 是**页内**半开区间，与 `DocumentBlock` 一致 ——
    共用同一个坐标系，"从证据反推块"才是查表而不是换算。

    ⚠️ 区间与 bbox 都必须**归一化**：`char_end <= char_start` 的空区间、
    倒序 bbox（`x0 > x1`）都能构造出一个"看起来像个证据"的对象 ——
    而画出来是零面积或负面积，下游只会画出一个看不见的框。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(ge=1)
    block_id: str = Field(min_length=1)
    text: str
    bbox: BBox
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text_precision: TextPrecision
    bbox_precision: BboxPrecision

    @model_validator(mode="after")
    def _span_is_non_empty(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError(
                f"证据区间必须非空（char_end > char_start），"
                f"收到 [{self.char_start}, {self.char_end})"
            )
        return self

    @model_validator(mode="after")
    def _bbox_is_normalized(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        if x0 > x1 or y0 > y1:
            raise ValueError(f"证据 bbox 必须归一化（x0<=x1、y0<=y1），收到 {self.bbox}")
        return self

    @model_validator(mode="after")
    def _char_precision_needs_a_char_bbox_hint(self) -> Self:
        """`bbox_precision=char` 与"整块 bbox"不能同时成立。

        这条**不是**在数据里查得到的东西（这里没有 `chars`），
        因此它只能靠**构造方的契约**保证 —— 由
        `app/services/document_builder.py::resolve_span` 统一产出。
        真正能在这里查的是反向的一致性：区间长度与文本长度必须相等，
        否则"声明 char、框住整行"这种错配必然成立。
        """
        if self.bbox_precision is BboxPrecision.CHAR and len(self.text) != (
            self.char_end - self.char_start
        ):
            raise ValueError(
                f"声明 char 精度的证据，文本长度（{len(self.text)}）必须等于区间长度"
                f"（{self.char_end - self.char_start}）—— 否则框与文字对不上"
            )
        return self


class ExtractedField(BaseModel):
    """一个字段（或一类条款）的结论。

    ## 四态各自的不变量

    | `status` | 证据 | `reason_code` |
    | --- | --- | --- |
    | `extracted` | **必须有**（没有证据的结论不可核验） | 可空 |
    | `not_found` | **必须为空**（"没找到"却带着证据是自相矛盾） | 可空 |
    | `uncertain` | 可空 | **必须给**（M5 靠它判 `needs_review`） |
    | `failed` | 可空 | **必须给** |

    `not_found` 与 `failed` 的差别是本项目最要紧的一处：
    **`not_found` 只在"可靠检索过、确实没有"时才成立**。
    把读取失败报成缺失，会让"我们没读到"变成"合同没约定" ——
    于是缺失类规则误报，而报告看起来完全正常。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_code: str = Field(min_length=1)
    #: 原文片段（人看的）。证据不足以给值时为空串。
    value_text: str = ""
    #: 十进制**字符串**，仅数值类字段有值。绝不用 float。
    value_decimal: str | None = None
    currency: str | None = None
    status: FieldStatus
    evidence: tuple[EvidenceSpan, ...] = ()
    reason_code: ReasonCode | None = None
    reason_text: str | None = None

    @model_validator(mode="after")
    def _status_implies_evidence_and_reason(self) -> Self:
        if self.status is FieldStatus.EXTRACTED and not self.evidence:
            raise ValueError(
                f"字段 {self.field_code} 标为 extracted 却没有证据 —— "
                "没有证据的结论不可核验"
            )
        if self.status is FieldStatus.NOT_FOUND and self.evidence:
            raise ValueError(
                f"字段 {self.field_code} 标为 not_found 却带着证据 —— 自相矛盾"
            )
        if (
            self.status in {FieldStatus.FAILED, FieldStatus.UNCERTAIN}
            and self.reason_code is None
        ):
            raise ValueError(
                f"字段 {self.field_code} 标为 {self.status} 却没有 reason_code —— "
                "M5 无法据此判 needs_review"
            )
        return self

    @model_validator(mode="after")
    def _decimal_is_not_a_float(self) -> Self:
        """金额类字段**必须**是十进制字符串。

        调用方可能自己 `str(金额)` 一下，而那会引入 `1.2e+06` 这类科学计数法，
        或者更糟：`str(0.1+0.2)` = `'0.30000000000000004'`。
        这里在入口把它挡住，并要求**字符串本身就是合法十进制**。
        """
        if self.value_decimal is None:
            return self
        if self.field_code in DECIMAL_FIELD_CODES:
            try:
                Decimal(self.value_decimal)
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(
                    f"字段 {self.field_code} 的 value_decimal 不是合法十进制："
                    f"{self.value_decimal!r}"
                ) from exc
            if "e" in self.value_decimal.lower():
                raise ValueError(
                    f"字段 {self.field_code} 的金额不得用科学计数法：{self.value_decimal!r}"
                )
        return self


class _FieldSet(BaseModel):
    """两份 JSON 共有的结构。**不要直接用它** —— 它不校验字段码的合法性。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    fields: tuple[ExtractedField, ...] = ()

    @model_validator(mode="after")
    def _field_codes_are_unique(self) -> Self:
        codes = [item.field_code for item in self.fields]
        if len(codes) != len(set(codes)):
            duplicates = {code for code in codes if codes.count(code) > 1}
            raise ValueError(f"字段码重复：{sorted(duplicates)}")
        return self

    def get(self, field_code: str) -> ExtractedField | None:
        for item in self.fields:
            if item.field_code == field_code:
                return item
        return None


class BasicInfoFieldSet(_FieldSet):
    """`basic_info_json` 的根结构。

    **构造即保证两条**：

    1. 出现的字段码**全部**在白名单里 —— 拼错一个就构造不出来；
    2. 需求规定的 **8 项基本信息全部在**。

    第 2 条是"覆盖需求规定的 8 项基本信息"这条验收的落地形式。
    靠测试去断言覆盖度是不够的：那份 JSON 是**长期存放**的，
    而缺项的记录在被消费时才暴露，那时已经离写入很远了。
    """

    @model_validator(mode="after")
    def _codes_are_known(self) -> Self:
        unknown = sorted(
            {item.field_code for item in self.fields} - ALL_EXPR_FIELDS
        )
        if unknown:
            raise ValueError(f"basic_info_json 含未登记字段码：{unknown}")
        return self

    @model_validator(mode="after")
    def _covers_required_basic_info(self) -> Self:
        present = {item.field_code for item in self.fields}
        missing = sorted(set(BASIC_INFO_FIELDS) - present)
        if missing:
            raise ValueError(
                f"basic_info_json 缺少需求规定的信息项：{missing} "
                f"（需求要求覆盖 {sorted(BASIC_INFO_FIELDS)}）"
            )
        return self


class ClauseFieldSet(_FieldSet):
    """`clause_info_json` 的根结构。同样**构造即保证**码合法 + 覆盖需求 8 类。"""

    @model_validator(mode="after")
    def _codes_are_known(self) -> Self:
        unknown = sorted({item.field_code for item in self.fields} - ALL_CLAUSE_TYPES)
        if unknown:
            raise ValueError(f"clause_info_json 含未登记条款码：{unknown}")
        return self

    @model_validator(mode="after")
    def _covers_required_clauses(self) -> Self:
        present = {item.field_code for item in self.fields}
        missing = sorted(set(REQUIRED_CLAUSE_TYPES) - present)
        if missing:
            raise ValueError(
                f"clause_info_json 缺少需求规定的条款类别：{missing} "
                f"（需求要求 {len(REQUIRED_CLAUSE_TYPES)} 类）"
            )
        return self


__all__ = [
    "SCHEMA_VERSION",
    "BasicInfoFieldSet",
    "ClauseFieldSet",
    "EvidenceSpan",
    "ExtractedField",
    "DIRECT_FIELDS",
]
