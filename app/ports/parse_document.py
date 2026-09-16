"""标准文档 DTO —— 解析结果的**可信载体**（设计文档 §4.2 / §3.4）。

它是落进对象存储的工件格式，也是 M8 画证据框、M5 做规则评价的输入。

## 为什么用 Pydantic 而不是 `@dataclass`

这份结构既要**落库**又要**读回**。普通 `dataclass` 的注解只是类型提示、
不在运行时检查 —— 实测 `DocumentPage(page_status="illegal")` 能创建成功，
于是"非法状态写不进"这类验收**根本无法成立**。

Pydantic 的校验在构造时就生效，**读回工件时同样生效**：一份带着
`rotation=45` 或倒序 bbox 的工件不会流到 M8 才暴露。

## 与端口层其他 DTO 的不对称是有意的

`app/ports/` 里的传递型 DTO（`PendingApprovalDTO` 等）仍是 `@dataclass(frozen=True)`：
它们只在进程内传递、不落库，加校验没有收益。
**不要为了"统一"把两侧改成一样** —— 一边会丢掉校验，另一边会白付构造开销。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import BboxPrecision, PageStatus, TextPrecision

#: 归一化包围盒：`(x0, y0, x1, y1)`，约定 `x0 <= x1`、`y0 <= y1`
BBox = tuple[float, float, float, float]


class _Frozen(BaseModel):
    """工件内的全部结构：冻结 + 拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # check_fields=False：`bbox` 只在**子类**上存在，基类本身没有这个字段。
    # 不加这个参数 pydantic 会在建模阶段直接抛错（要求校验器字段必须存在）。
    @field_validator("bbox", check_fields=False)
    @classmethod
    def _bbox_normalized(cls, value: BBox) -> BBox:
        """bbox 必须已归一化。

        ⚠️ 倒序的框**看起来仍然像个框**（四个数、量级也对），
        只是画出来是零面积或负面积 —— 因此必须在入口拒绝，而不是留给 M8 去猜。
        """
        x0, y0, x1, y1 = value
        if x0 > x1 or y0 > y1:
            raise ValueError(f"bbox 必须归一化（x0<=x1、y0<=y1），收到 {value}")
        return value


class SourceSpan(_Frozen):
    """规范化后的**一个**字符，对应原始文本的半开区间 `[raw_start, raw_end)`。

    ⚠️ 不能用一个整数表示"规范化字符 → 原始下标"：NFC 是**多对一**的 ——
    `e` + 组合重音（2 码位）→ `é`（1 码位）。一个整数指不到两个原始字符，
    也算不出那个字符应有的**联合 bbox**，而证据框恰恰需要覆盖两个原始字符。
    """

    raw_start: int = Field(ge=0)
    raw_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _span_non_empty(self) -> SourceSpan:
        if self.raw_end <= self.raw_start:
            raise ValueError(
                f"原始区间必须非空（raw_end > raw_start），收到 "
                f"[{self.raw_start}, {self.raw_end})"
            )
        return self


class DocumentChar(_Frozen):
    """一个字符 —— 存在它就说明几何精度**真的**是 `char`。"""

    text: str = Field(min_length=1)
    bbox: BBox
    char_start: int = Field(ge=0)  # 区间为 **[start, end)**
    char_end: int = Field(ge=0)


class DocumentBlock(_Frozen):
    """文本块。`chars` 为空元组即"没有逐字符几何"。"""

    block_id: str = Field(min_length=1)  # "p3-b12"
    text: str
    bbox: BBox
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text_precision: TextPrecision
    bbox_precision: BboxPrecision
    chars: tuple[DocumentChar, ...] = ()

    @model_validator(mode="after")
    def _char_precision_requires_chars(self) -> DocumentBlock:
        """`bbox_precision == char` 时 **`chars` 必须非空**。

        ⚠️ 这是整个文件最要紧的一条校验。只有声明没有 `chars` 就是**空头承诺**：
        M8 拿到 `char` 却只能画出块框，而验收 6 断言的正是"能画字符框"这个能力。
        能力声明与数据结构必须一起改 —— 只改声明不会有人发现。
        """
        if self.bbox_precision is BboxPrecision.CHAR and not self.chars:
            raise ValueError(
                f"块 {self.block_id} 声明 bbox_precision=char，但 chars 为空 —— "
                "没有逐字符几何就不能声称字符级精度"
            )
        return self


#: **唯一**的坐标系取值（§4.2）。所有几何一律在这个空间里。
#:
#: ⚠️ 收成常量、而不是在模型、适配器、构建器里**各写一遍字面量**：
#: 出现第二个取值时（例如让图片走自己的像素坐标），必须改的只有这里 ——
#: 而下方 `DocumentPage.bbox_space` 的 `Literal` 会**拒绝**任何别的值。
#: 这正是 §4.9 修-24 要防的事：两套坐标系共存，且各自单独看都合法。
#: `tests/test_image_input.py` 里有一条断言让常量与 `Literal` 保持一致。
BBOX_SPACE: str = "pdf-point-top-left"


class DocumentPage(_Frozen):
    """一页的标准表示。

    `width` / `height` 对应**旋转后可见页面**；`bbox` 一律已换算到该空间
    （M8 直接用它画框，**不得再应用一次 `rotation`**）。

    ## 为什么这里要存 `text`（而不是只靠 blocks 拼回来）

    块的 `char_start/char_end` 是**页内**偏移，而页内行与行之间有分隔符 ——
    分隔符不属于任何块。只靠 blocks 拼不回明文：既不知道分隔符在哪，
    也不知道是不是该有。于是"证据文本"变成一件要猜的事。

    `text` 是**权威明文**，`blocks[].text` 是它的**派生视图**。
    重复是有意的，但它**不会漂移** —— 下面的校验器会逐块断言
    `text[char_start:char_end] == block.text`。这正是验收 1
    （"`char_start/char_end` 切出的子串 == 证据文本"）在**数据层**的落地：
    它不再是一条只能靠测试守住的约定，而是**构造即校验**的不变量。
    """

    page: int = Field(ge=1)  # 1-based
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    bbox_space: Literal["pdf-point-top-left"]
    rotation: Literal[0, 90, 180, 270]
    source: Literal["text", "ocr"]
    page_status: PageStatus
    #: 权威明文；块文本是它的切片（校验器强制）
    text: str = ""
    #: 走 OCR 的判据（哪一项阈值不满足），`source == "text"` 时为空
    route_reason: str | None = None
    #: `page_status == failed` 时的稳定错误码
    error_code: str | None = None
    #: 规范化后第 i 个字符 → 原始区间。承载 NFC 映射，缺了它字符框会逐字累积偏移
    char_map: tuple[SourceSpan, ...] = ()
    blocks: tuple[DocumentBlock, ...] = ()

    @model_validator(mode="after")
    def _blocks_are_slices_of_page_text(self) -> DocumentPage:
        """每个块的文本必须**恰好**等于它在页明文里的切片。

        ⚠️ 这条校验把"块文本与偏移一致"从**约定**变成**不变量**。
        没有它时，一处偏移算错的表现是：证据区间指向了正确的文字，
        但框画在别处 —— 两者单独看都正常，只有并排比对才看得出。
        """
        length = len(self.text)
        for block in self.blocks:
            if block.char_end > length or block.char_start > block.char_end:
                raise ValueError(
                    f"页 {self.page} 的块 {block.block_id} 区间 "
                    f"[{block.char_start}, {block.char_end}) 越出页明文长度 {length}"
                )
            actual = self.text[block.char_start : block.char_end]
            if actual != block.text:
                raise ValueError(
                    f"页 {self.page} 的块 {block.block_id} 文本与切片不一致："
                    f"切片={actual!r} 块文本={block.text!r}"
                )
        return self


class StandardDocument(_Frozen):
    """整份标准文档（落对象存储的工件）。

    `schema_version` 不可省：这份结构会**长期存放**，而契约会随 M5/M8 演化。
    没有版本号时，读到一份缺字段的旧记录，消费方无法区分"旧格式"与"数据损坏"，
    只能加防御性判断 —— 那种判断一旦写错就变成静默默认值。
    """

    schema_version: int = Field(default=1, ge=1)
    pages: tuple[DocumentPage, ...] = ()

    @model_validator(mode="after")
    def _pages_are_contiguous(self) -> StandardDocument:
        """页号必须是从 1 开始、连续无缺。

        缺页意味着"这一页没被处理"，而下游按页号索引时会**静默错位** ——
        第 3 页的证据被当成第 4 页的。
        """
        numbers = [page.page for page in self.pages]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"页号必须从 1 起连续，收到 {numbers}")
        return self
