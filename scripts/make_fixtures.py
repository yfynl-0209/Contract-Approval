"""生成中文合成合同 PDF 夹具（**开发期脚本**，产出物提交进仓库）。

    python scripts/make_fixtures.py           # 生成（覆盖）全部夹具
    python scripts/make_fixtures.py --check   # 只校验：磁盘上的夹具是否与定义一致

## 为什么生成与运行分开

夹具必须是**静态的实物**，而不是运行时现算的。理由不是"合规"，是可复现性：

- 运行时生成意味着"同一份夹具"会随 PyMuPDF 升级而**悄悄改变**，
  于是 OCR / 字段提取的回归结果也跟着变 ——
  那时很难判断是**代码退化**还是**夹具变了**；
- 静态文件可以提交、可以 diff、可以在没有 PyMuPDF 的环境里被读取。

因此：**运行时的 `mock_approval` 不 import PyMuPDF**，只读这些文件；
本脚本是**唯一**在开发期使用 PyMuPDF 生成夹具的地方
（运行期使用 PyMuPDF 的地方只有 `app/adapters/parse/`）。

## 三类渲染

| render | 产出 | 用途 |
| --- | --- | --- |
| `text` | 带文本层的 PDF | 走文本抽取路径 |
| `scan` | 只有页面图像、**无文本层** | 走 OCR 路径 |
| `scan_unreadable` | 有墨迹但**不可识别**的图像 | OCR 失败 / 无法识别 |

`scan_unreadable` 用"先正常渲染、再降到极低 DPI"制造：
页面上确实有深色笔迹（不是空白页），但字已经糊成色块。
这样它区分得开两种失败——**空白页**（确实没内容）与**不可识别**（不知道有没有）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import fitz

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mock_approval.contract_texts import (  # noqa: E402
    FIXTURES,
    PAGE_HEIGHT_PT,
    PAGE_WIDTH_PT,
    SCAN_DPI,
    TEXTS,
    FixtureSpec,
    line_baseline,
)

FIXTURES_DIR = PROJECT_ROOT / "mock_approval" / "fixtures"

#: 内置简体中文字体。用内置字体是为了**不引入字体文件依赖** ——
#: 否则夹具能否生成就取决于运行环境里装了什么字体。
CHINESE_FONT = "china-s"

#: "不可识别"的渲染 DPI。取这个值的依据：
#: 低到字迹糊成色块（OCR 无法给出可用结果），又高到页面上确实有内容
#: （不是空白页）—— 两者必须区分得开，否则测的是"空文档"而不是"读不出来"。
UNREADABLE_DPI = 25


# ============================================================
# 渲染
# ============================================================


def _new_page(doc: fitz.Document) -> fitz.Page:
    return doc.new_page(width=PAGE_WIDTH_PT, height=PAGE_HEIGHT_PT)


def _write_lines(page: fitz.Page, lines: tuple[str, ...]) -> None:
    """按 `line_baseline()` 给出的坐标逐行写入。

    坐标由 `contract_texts.line_baseline()` 统一给出，**生成与断言共用** ——
    验收 5 要"用已知坐标写入文本，再反查 OCR 结果落在容差内"，
    "已知"只能有一个来源，否则两边各算一次就会静默错开。
    """
    from mock_approval.contract_texts import MARGIN_LEFT_PT, FONT_SIZE_PT

    for index, line in enumerate(lines):
        if not line:
            continue
        # PyMuPDF 的 insert_text 用的是**基线**坐标，与 line_baseline 同义
        page.insert_text(
            fitz.Point(MARGIN_LEFT_PT, line_baseline(index)),
            line,
            fontname=CHINESE_FONT,
            fontsize=FONT_SIZE_PT,
        )


def render_text(lines: tuple[str, ...]) -> fitz.Document:
    """文本件：带文本层，可直接抽取。"""
    doc = fitz.open()
    _write_lines(_new_page(doc), lines)
    return doc


def _image_only_doc(source: fitz.Document, *, dpi: int) -> fitz.Document:
    """把**每一页**渲染成图像，再包成只有图像的多页 PDF（无文本层）。

    ⚠️ 必须遍历全部页。只取 `source[0]` 时，多页夹具会**静默丢掉第 2 页起的内容**，
    产出一份 1 页的扫描件；而配对断言比的是 `text_key`（**定义**）而不是渲染结果，
    于是照样通过 —— "同一份内容的文本件与扫描件"在两者内容已经不同的情况下仍然成立。
    """
    doc = fitz.open()
    for source_page in source:
        pixmap = source_page.get_pixmap(dpi=dpi)
        page = _new_page(doc)
        page.insert_image(page.rect, pixmap=pixmap)
    return doc


def render_scan(lines: tuple[str, ...]) -> fitz.Document:
    """扫描件：正文渲染成图像，因此**没有文本层**，必须走 OCR。"""
    return _image_only_doc(render_text(lines), dpi=SCAN_DPI)


def render_scan_unreadable(lines: tuple[str, ...]) -> fitz.Document:
    """不可识别的扫描件：有墨迹、但糊到读不出字。

    刻意不是空白页 —— 空白会被判"确实没有内容"，
    而这里要验证的是"有内容但读不出来"，两者在页状态上是**不同**的结论。
    """
    return _image_only_doc(render_text(lines), dpi=UNREADABLE_DPI)


_RENDERERS = {
    "text": render_text,
    "scan": render_scan,
    "scan_unreadable": render_scan_unreadable,
}


# ============================================================
# 生成 / 校验
# ============================================================


def build_document(spec: FixtureSpec) -> fitz.Document:
    lines = TEXTS[spec.text_key]
    try:
        renderer = _RENDERERS[spec.render]
    except KeyError:  # pragma: no cover - 定义写错时立刻暴露
        raise SystemExit(
            f"[错误] 夹具 {spec.file_name} 的 render 取值未知：{spec.render!r}"
            f"（允许：{sorted(_RENDERERS)}）"
        ) from None
    return renderer(lines)


def _save(doc: fitz.Document, path: Path) -> None:
    """保存并**固定元数据**，减少两次生成之间的无谓差异。"""
    doc.set_metadata(
        {
            "title": path.name,
            "author": "mock_approval fixture generator",
            "creator": "scripts/make_fixtures.py",
            "producer": "scripts/make_fixtures.py",
            "creationDate": "D:20260914000000Z",
            "modDate": "D:20260914000000Z",
        }
    )
    doc.save(path, garbage=4, deflate=True)


def _text_geometry_digest(page: fitz.Page) -> str:
    """文本层的**行坐标**摘要。

    ⚠️ 没有这一项时，只要 `get_text("text")` 的内容相同就判"无漂移" ——
    而坐标是烘在 PDF 里的**第二份真相**：`FIRST_BASELINE_PT` 或 `LINE_HEIGHT_PT`
    一改，磁盘上的夹具仍保留旧坐标，`--check` 却报"一致"。

    后果直接落在验收 5：它要"用**已知**坐标写入的文本反查 OCR 结果落在容差内"，
    那时"已知坐标"就有两份（代码里的 `line_baseline()` 与 PDF 里的实际位置），
    容差够大时两边不一致**也不会失败** —— 这条验收在验一个不存在的东西。
    """
    lines: list[tuple[float, float, float, float]] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            lines.append(tuple(round(value, 1) for value in line["bbox"]))
    return hashlib.sha256(repr(lines).encode("utf-8")).hexdigest()[:16]


def _image_content_digest(doc: fitz.Document, page: fitz.Page) -> tuple[str, ...]:
    """内嵌图像的**像素内容**摘要。

    ⚠️ 必须有这一项：扫描件没有文本层，`get_text()` 那一段恒为空，
    而页面上通常只有一张图 —— 于是"尺寸 + 图像数量"对**任意一份同尺寸的单图 PDF**
    都相同。实测两份扫描件（可识别件与不可识别件）的指纹**完全一致**，
    互换后 `--check` 依然通过，而它们的语义正好相反。
    没有内容摘要，扫描件的漂移校验等于不存在。

    摘的是**解码后的像素**而不是存储流：后者会因编码参数变化产生假漂移，
    与"不做字节比对"的初衷冲突。
    """
    digests: list[str] = []
    for info in page.get_images(full=True):
        pixmap = fitz.Pixmap(doc, info[0])
        try:
            header = f"{pixmap.width}x{pixmap.height}x{pixmap.n}:".encode("utf-8")
            digests.append(hashlib.sha256(header + bytes(pixmap.samples)).hexdigest()[:16])
        finally:
            del pixmap
    return tuple(digests)


def fingerprint(doc: fitz.Document) -> tuple:
    """夹具的**内容指纹**。

    刻意不用字节比对：PDF 里含对象编号、流压缩等与内容无关的差异，
    字节级比对会让"升级一次 PyMuPDF"就报一堆假漂移，最后没人再看它。

    守住的是**内容**，且必须覆盖三类渲染各自的"内容"：

    | 项 | 管什么 |
    | --- | --- |
    | 页面尺寸 | 版式 |
    | 文本层文本 | 文本件的**文字**（扫描件此项恒空） |
    | 文本层行坐标 | 文字**写在哪** —— 少了它，改行距/边距不会被发现 |
    | 图像像素摘要 | 扫描件的**全部内容** —— 少了它，扫描件可被任意替换 |
    """
    pages = []
    for page in doc:
        pages.append(
            (
                round(page.rect.width, 3),
                round(page.rect.height, 3),
                page.get_text("text").strip(),
                _text_geometry_digest(page),
                _image_content_digest(doc, page),
            )
        )
    return tuple(pages)


def cmd_generate() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"生成目录：{FIXTURES_DIR.relative_to(PROJECT_ROOT)}")

    for spec in FIXTURES:
        doc = build_document(spec)
        target = FIXTURES_DIR / spec.file_name
        _save(doc, target)
        doc.close()
        print(f"  [生成] {spec.file_name:34s} {spec.render:16s} {target.stat().st_size:>7d} 字节")
    return 0


def cmd_check() -> int:
    problems: list[str] = []
    missing: list[str] = []

    for spec in FIXTURES:
        target = FIXTURES_DIR / spec.file_name
        if not target.exists():
            missing.append(spec.file_name)
            continue

        expected = fingerprint(build_document(spec))
        with fitz.open(target) as actual_doc:
            actual = fingerprint(actual_doc)

        if expected != actual:
            problems.append(
                f"{spec.file_name}：与定义不一致\n"
                f"    期望 {expected}\n"
                f"    实际 {actual}"
            )

    if missing:
        print("[缺失] 以下夹具不存在，请运行 python scripts/make_fixtures.py：")
        for name in missing:
            print(f"  - {name}")

    if problems:
        print("[漂移] 以下夹具内容与定义不一致：")
        for problem in problems:
            print(f"  - {problem}")

    if missing or problems:
        return 1

    print(f"[通过] {len(FIXTURES)} 份夹具与定义一致（{FIXTURES_DIR.relative_to(PROJECT_ROOT)}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验磁盘上的夹具是否与 contract_texts.py 的定义一致，不写入",
    )
    args = parser.parse_args()
    return cmd_check() if args.check else cmd_generate()


if __name__ == "__main__":
    raise SystemExit(main())
