"""生成占位用的最小 PDF —— **兜底路径**，正常流程不会走到。

## 现状（M4 / T2 之后）

中文合成合同已是**提交进仓库的静态夹具**，放在 `mock_approval/fixtures/`，
由 `scripts/make_fixtures.py`（开发期，用 PyMuPDF）生成。
运行时的 `mock_approval` **只读文件**，因此**仍然不依赖任何第三方库** ——
原约定没有被打破。

本模块保留的是第三条兜底：某个 `content_kind` 既没有夹具、
`data/contracts/` 下也没有同名文件时，生成一份 ASCII 占位 PDF，
让"下载"链路至少能拿到合法字节流。

> ⚠️ 但这条回落**必须可观测**：`main.py` 会把来源写进 `X-Fixture-Source`
> 响应头（`fixture` / `contracts` / `generated`），
> 并有测试断言"演示实例的附件全部来自夹具"。
> 否则夹具缺失的症状是"解析器什么都抽不出来"，与病因相距很远。

## 生成的是什么

一个**结构合法、可被 PyMuPDF 解析**的最小 PDF：

- `text_pdf`：带文本层（ASCII 占位文字），走文本抽取路径；
- `scan_pdf`：内容流不含文字绘制操作，没有文本层，会被逐页路由判为扫描页并走 OCR。

⚠️ 手写 PDF **无法嵌入中文字体**（需要字体子集，不具可行性）——
这正是夹具改用 PyMuPDF 在**开发期**生成的原因。
"""

from __future__ import annotations

from typing import Final

#: 一到两行 ASCII 占位内容，便于 M4 解析时肉眼确认"确实读到了东西"
DEFAULT_TEXT_LINES: Final[tuple[str, ...]] = (
    "PLACEHOLDER CONTRACT (mock data for M2)",
    "This page is generated at runtime by mock_approval.sample_pdf.",
)


def _escape_pdf_text(text: str) -> str:
    """转义 PDF 字符串字面量中的反斜杠与括号。"""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build_content_stream(lines: tuple[str, ...]) -> bytes:
    """构建页面内容流。

    无文字时仍然输出合法的空内容流——这样"扫描件"不是损坏文件，
    而是**结构正常但没有文本层**的 PDF，与真实扫描件的行为一致。
    """
    parts = ["BT /F1 12 Tf 72 770 Td 16 TL"]
    for line in lines:
        parts.append(f"({_escape_pdf_text(line)}) Tj T*")
    parts.append("ET")
    return "\n".join(parts).encode("ascii")


def build_minimal_pdf(lines: tuple[str, ...] = DEFAULT_TEXT_LINES) -> bytes:
    """生成单页 PDF 的字节内容。

    Args:
        lines: 页面上的 ASCII 文本行；传空元组即得到无文本层的"扫描件"。

    Returns:
        完整的 PDF 字节流（含正确的 xref 表，可被标准 PDF 库解析）。
    """
    stream = _build_content_stream(lines)

    # 5 个基础对象：Catalog / Pages / Page / Font / Contents
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []

    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    # xref 表：每行必须是固定的 20 字节格式，否则某些解析器会拒绝
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")

    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    return bytes(out)


def build_for_kind(content_kind: str) -> bytes:
    """按附件类型生成内容。

    `scan_pdf` 不写入文本，从而**没有文本层**，会被解析路由判为需要 OCR 的页面。
    """
    if content_kind == "scan_pdf":
        return build_minimal_pdf(lines=())
    return build_minimal_pdf()
