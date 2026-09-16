"""中文合成合同夹具测试（M4 / T2）。

本文件守住四件事，它们分别对应四类**会静默发生**的失败：

1. **夹具必须真的在仓库里**
   生成脚本能跑通 ≠ 夹具已提交。`.gitignore` 一改、或忘了 `git add`，
   表现是"解析器什么都抽不出来"，而错误信息里没有任何线索指向夹具。

2. **夹具内容必须与定义一致**（`--check`）
   手工改过 PDF、或升级 PyMuPDF 后没重新生成，回归基线就变了 ——
   那时"字段提取退化了"与"夹具变了"分不清。

3. **三类渲染的前提必须成立**
   文本件要有文本层与逐字符 bbox；扫描件要**没有**文本层、**有**图像。
   任一条不成立，M4 后续的验收测的就不是它声称要测的东西。

4. **正文里的立场必须与审批单的声明一致**
   我方在正文里是甲方还是乙方，决定了方向敏感规则的方向。
   夹具写反了，规则结论会全线反转，而**没有任何断言会失败**。
   唯一的例外是 HT-2026-0006 —— 它**故意**写反，用于演示立场冲突。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

import mock_approval.contract_texts as contract_texts
from app.config import PROJECT_ROOT
from mock_approval.contract_texts import (
    FIXTURES,
    PAGE_HEIGHT_PT,
    PAGE_WIDTH_PT,
    TEXTS,
    FixtureSpec,
    fixture_by_name,
)
from mock_approval.main import app
from scripts.make_fixtures import build_document, fingerprint

FIXTURES_DIR = PROJECT_ROOT / "mock_approval" / "fixtures"
MOCK_DIR = PROJECT_ROOT / "mock_approval"
FIXTURES_JSON = MOCK_DIR / "fixtures.json"

#: 我方企业名 —— 正文里的立场就是靠它出现在甲方还是乙方来判定的
OUR_PARTY = "示例科技有限公司"

#: **故意写反**的实例：正文把我方写成甲方，而审批单声明 party_b。
#: 这是 HT-2026-0006 存在的全部意义（演示立场冲突），因此它是例外而非缺陷。
DELIBERATE_LABEL_CONFLICT = "contract_06_conflict.pdf"

CLIENT = TestClient(app)
AUTH = {"Authorization": "Bearer demo-token"}


# ============================================================
# 辅助
# ============================================================


def _demo_attachments() -> list[tuple[str, str, str]]:
    """从 fixtures.json 取出 `(instance_code, file_name, our_party_contract_label)`。

    刻意读**真实数据**而不是在测试里抄一份：抄一份的话，
    夹具与 fixtures.json 漂移时两边都"自洽"，测试全绿。
    """
    raw = json.loads(FIXTURES_JSON.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    for item in raw["instances"]:
        for attachment in item.get("attachments", []):
            if attachment.get("content_kind") == "missing":
                continue  # 该附件在审批系统中已被删除，没有内容可测
            rows.append(
                (
                    item["approval_code"],
                    attachment["file_name"],
                    item["our_party_contract_label"],
                )
            )
    return rows


def _char_bbox_count(page: fitz.Page) -> int:
    """逐字符 bbox 的数量 —— `DocumentChar` 的来源能力。"""
    return sum(
        len(span["chars"])
        for block in page.get_text("rawdict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
    )


# ============================================================
# 1. 夹具存在且与定义一致
# ============================================================


@pytest.mark.parametrize("spec", FIXTURES, ids=lambda s: s.file_name)
def test_fixture_file_is_committed(spec: FixtureSpec) -> None:
    """每份定义过的夹具都必须真的存在于仓库中。"""
    assert (FIXTURES_DIR / spec.file_name).exists(), (
        f"夹具 {spec.file_name} 不存在 —— 运行 python scripts/make_fixtures.py"
    )


def test_fixtures_have_no_drift() -> None:
    """磁盘上的夹具必须与 `contract_texts.py` 的定义一致（走 `--check` 入口）。

    用子进程调真实 CLI，而不是在测试里复刻比对逻辑：
    复刻的话，CLI 与测试可以各自演化，而"漂移检测"本身就成了没人验的东西。
    """
    completed = subprocess.run(
        [sys.executable, "scripts/make_fixtures.py", "--check"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert completed.returncode == 0, (
        f"夹具与定义漂移：\n{completed.stdout}\n{completed.stderr}"
    )


def test_demo_attachments_all_have_fixtures() -> None:
    """演示实例用到的每份附件都必须有夹具 —— 否则会静默回落到占位 PDF。"""
    missing = [
        file_name
        for _, file_name, _ in _demo_attachments()
        if not (FIXTURES_DIR / file_name).exists()
    ]
    assert not missing, f"以下附件缺夹具：{missing}"


# ============================================================
# 2. 三类渲染的前提
# ============================================================


@pytest.mark.parametrize(
    "spec", [s for s in FIXTURES if s.render == "text"], ids=lambda s: s.file_name
)
def test_text_fixture_has_text_layer_and_char_bboxes(spec: FixtureSpec) -> None:
    """文本件必须有文本层，且 `rawdict` 能给出**逐字符** bbox。

    后者是 M4 声明"几何精度可以是 `char`"的前提（设计文档 §4.2）。
    取不到逐字符 bbox 时，`bbox_precision` 只能如实降为 `line` / `block`，
    而那会让验收 6 变成一条无法通过的断言。
    """
    with fitz.open(FIXTURES_DIR / spec.file_name) as doc:
        page = doc[0]
        assert page.get_text("text").strip(), "文本件没有文本层"
        assert _char_bbox_count(page) > 0, "文本件取不到逐字符 bbox"
        assert not page.get_images(full=True), "文本件不应包含整页图像"


@pytest.mark.parametrize(
    "spec",
    [s for s in FIXTURES if s.render in {"scan", "scan_unreadable"}],
    ids=lambda s: s.file_name,
)
def test_scan_fixture_has_image_and_no_text_layer(spec: FixtureSpec) -> None:
    """扫描件必须是**结构正常但没有文本层**的 PDF，而不是损坏文件。

    有图像 + 无文本 → 解析路由才会判"需要 OCR"。
    若它同时带文本层，就会走文本抽取路径 —— 于是"扫描件验收"实际测的是文本件。
    """
    with fitz.open(FIXTURES_DIR / spec.file_name) as doc:
        page = doc[0]
        assert page.get_images(full=True), "扫描件必须是图像页"
        assert not page.get_text("text").strip(), (
            "扫描件不得带文本层，否则不会走 OCR 路径"
        )


def _dark_ratio(doc: fitz.Document, dpi: int = 50) -> float:
    """页面中明显偏离纸白（<200）的像素占比（灰度图）。"""
    samples = bytes(doc[0].get_pixmap(dpi=dpi).samples)
    return sum(1 for value in samples if value < 200) / max(len(samples), 1)


def test_unreadable_fixture_is_not_a_blank_page() -> None:
    """"不可识别"必须与"空白页"区分得开。

    空白页的结论是"确实没有内容"，而不可识别的结论是"不知道有没有内容"——
    两者在页状态与后续门禁上完全不同。因此这份夹具必须**有墨迹**。

    ## 判据是**与空白页对照**，而不是某个绝对比例

    实测（50 dpi，同一套夹具）：

    | 页面 | 深色像素占比 |
    | --- | --- |
    | 可识别扫描件 `contract_05_scan.pdf` | 0.0498 |
    | 不可识别件 `contract_07_scan_unreadable.pdf` | 0.0490 |
    | **空白页（对照）** | **0.0000** |

    由此可见**墨迹多少根本分不开这两份扫描件** —— 它们的差别是**清晰度**，
    不是内容量。所以拿一个绝对阈值（当初写的 `> 0.05`）去判"是不是空白页"
    既不经不起这份数据、也说不清在判什么。

    真正要判的只有一件事：**它不是空白页**。基准由测试现场生成，
    不依赖任何魔数 —— 空白页的占比还必须是 0，否则这条断言失去意义。
    """
    with fitz.open(FIXTURES_DIR / "contract_07_scan_unreadable.pdf") as doc:
        actual = _dark_ratio(doc)

    blank = fitz.open()
    try:
        blank.new_page(width=PAGE_WIDTH_PT, height=PAGE_HEIGHT_PT)
        control = _dark_ratio(blank)
    finally:
        blank.close()

    assert control == 0.0, "对照页必须是纯白，否则这条断言没有基准可比"
    assert actual > 0.01, (
        f"页面上几乎没有墨迹（{actual:.4f}，空白页 {control:.4f}）—— "
        "那会被判为空白页，而不是不可识别"
    )


def test_scan_and_text_fixture_share_the_same_text() -> None:
    """**验收 3 的配对前提**：同一份内容的文本件与扫描件。

    `contract_03_dev_no_ip.pdf`（文本件）与 `contract_05_scan.pdf`（扫描件）
    必须引用**同一个 text_key**。否则"两种 kind 提取的关键字段一致"这条验收
    根本无从比较 —— 而它看起来仍然会通过（各自都提取出了一堆字段）。
    """
    text_spec = next(s for s in FIXTURES if s.file_name == "contract_03_dev_no_ip.pdf")
    scan_spec = next(s for s in FIXTURES if s.file_name == "contract_05_scan.pdf")

    assert text_spec.text_key == scan_spec.text_key

    # 扫描件没有文本层，无法直接比文本；比的是**生成它的那份定义**
    assert TEXTS[scan_spec.text_key] == TEXTS[text_spec.text_key]
    assert "知识产权" not in "\n".join(TEXTS[text_spec.text_key]), (
        "HT-2026-0003 用于验证『缺 IP 条款 → 命中』，正文不得含知识产权条款"
    )


# ============================================================
# 3. 正文立场与审批单声明一致
# ============================================================


@pytest.mark.parametrize(
    ("instance_code", "file_name", "label"),
    _demo_attachments(),
    ids=lambda value: str(value),
)
def test_fixture_party_label_matches_declaration(
    instance_code: str, file_name: str, label: str
) -> None:
    """正文里我方出现在甲方还是乙方，必须与审批单的声明**对得上**。

    写反的后果：方向敏感规则（"我方是买方还是卖方"）全线反转，
    而**没有任何断言会失败** —— 结论看起来仍然完整、仍然有证据。

    唯一的例外是 `contract_06_conflict.pdf`：它故意写反，用于演示立场冲突。
    """
    spec = next(s for s in FIXTURES if s.file_name == file_name)
    text = "\n".join(TEXTS[spec.text_key])

    as_party_a = f"甲方（采购方）：{OUR_PARTY}" in text or f"甲方（委托方）：{OUR_PARTY}" in text
    as_party_b = f"乙方（供货方）：{OUR_PARTY}" in text or f"乙方（服务方）：{OUR_PARTY}" in text

    if file_name == DELIBERATE_LABEL_CONFLICT:
        assert label != "party_a", "这条夹具的意义就是把我方写成甲方，与声明相反"
        assert as_party_a, "冲突夹具必须把我方写在甲方一侧"
        return

    if label == "party_a":
        assert as_party_a and not as_party_b
    else:
        assert as_party_b and not as_party_a


# ============================================================
# 4. 运行时必须从夹具读取，且不得依赖 PyMuPDF
# ============================================================


def test_demo_attachments_are_served_from_fixtures() -> None:
    """演示实例的附件必须来自**夹具**，不能静默回落到运行时生成的占位 PDF。

    回落的表现是"解析器什么都抽不出来"，与病因（夹具缺失）相距很远。
    因此把来源做成响应头，让这条回落路径在正常流程里不可能被走到。
    """
    for instance_code, _, _ in _demo_attachments():
        detail = CLIENT.get(f"/api/instances/{instance_code}", headers=AUTH).json()
        for attachment in detail["attachments"]:
            if not attachment["available"]:
                continue
            response = CLIENT.get(
                f"/api/instances/{instance_code}"
                f"/attachments/{attachment['attachment_id']}/download",
                headers=AUTH,
            )
            assert response.status_code == 200
            assert response.headers["x-fixture-source"] == "fixture", (
                f"{instance_code}/{attachment['attachment_id']} 的附件不是来自夹具："
                f"{response.headers['x-fixture-source']}"
            )


def test_mock_does_not_import_pymupdf() -> None:
    """`mock_approval` 运行期**不得**依赖 PyMuPDF（源码级约束）。

    为什么这条必须由测试守：`mock_approval` 的定位是**外部对接方**，
    它依赖主项目的第三方库会让"换一个审批系统"变成"换一套依赖"。

    而夹具生成**需要** PyMuPDF —— 那一步被放在 `scripts/make_fixtures.py`（开发期）。
    两者只差一个位置，很容易被"顺手 import 一下"破坏，
    且破坏后功能完全正常，只是分层没了。
    """
    offenders: list[str] = []
    for path in sorted(MOCK_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"fitz", "pymupdf"}:
                    offenders.append(f"{path.name}: import {name}")

    assert not offenders, (
        "mock_approval 运行期不得依赖 PyMuPDF（生成夹具请用 scripts/make_fixtures.py）："
        + "；".join(offenders)
    )


# ============================================================
# 5. 正文纵向顺序
# ============================================================


@pytest.mark.parametrize(
    "spec", [s for s in FIXTURES if s.render == "text"], ids=lambda s: s.file_name
)
def test_text_fixture_reading_order_is_top_to_bottom(spec: FixtureSpec) -> None:
    """正文必须**自上而下**排布：第一行在最上、末行在最下。

    ⚠️ 这条守的是一次真实发生过的颠倒。`FIRST_BASELINE_PT` 曾按"PDF 左下原点"
    写成 770，而 **PyMuPDF 的页面坐标是左上原点** —— 实测生成的 PDF 里
    `采购合同` 落在 y≈758（页高 842），末条落在 y≈294，整份文档上下翻转。

    它最隐蔽的后果不是"看着别扭"，而是**文本件与扫描件不一致**：
    文本抽取按内容流读（看起来完全正常），而 OCR 读的是**图像**、
    按 y 从小到大读（读到反序）—— 验收 3 要的恰恰是同一份内容的两种渲染
    给出一致的结论，而这条验收在两边顺序相反时仍然"通过"。
    """
    with fitz.open(FIXTURES_DIR / spec.file_name) as doc:
        lines = [
            (
                line["bbox"][1],
                "".join(c["c"] for span in line["spans"] for c in span["chars"]),
            )
            for block in doc[0].get_text("rawdict")["blocks"]
            for line in block.get("lines", [])
        ]

    assert lines, f"{spec.file_name} 没有文本行"

    first_y, first_text = min(lines)
    last_y, last_text = max(lines)
    expected = [line for line in TEXTS[spec.text_key] if line]

    assert first_y < last_y, f"正文上下颠倒：最上行 y={first_y} 不小于最下行 y={last_y}"
    assert first_text == expected[0], (
        f"页面上最上方的行应当是正文第一行，实际是 {first_text!r}"
    )
    assert last_text == expected[-1], (
        f"页面上最下方的行应当是正文末行，实际是 {last_text!r}"
    )


# ============================================================
# 6. 漂移指纹必须覆盖"内容"，而不只是"形状"
# ============================================================


def test_scan_fixtures_have_distinct_fingerprints() -> None:
    """两份扫描件的指纹**必须不同**，否则漂移校验形同虚设。

    ⚠️ 这条守的是一次真实发生过的失效：`fingerprint` 原先只含
    "尺寸 + 文本层 + 图像数量"。扫描件没有文本层（此项恒为空），又都只有一张图 ——
    实测可识别件与不可识别件的指纹**完全相同**：

    ```text
    contract_05_scan.pdf           -> ((595.0, 842.0, '', 1),)
    contract_07_scan_unreadable.pdf -> ((595.0, 842.0, '', 1),)
    ```

    于是**互换这两份文件后 `--check` 依然通过**，而两者语义正好相反
    （`ok` vs `uncertain` / `failed`），互换会让验收 17/34 被静默反转。
    """

    def content_fingerprint(name: str) -> tuple:
        with fitz.open(FIXTURES_DIR / name) as doc:
            return fingerprint(doc)

    assert content_fingerprint("contract_05_scan.pdf") != content_fingerprint(
        "contract_07_scan_unreadable.pdf"
    )


def test_fingerprint_covers_text_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    """指纹必须覆盖**行坐标**，否则改行距/边距不会被 `--check` 发现。

    ⚠️ 实测：行距 16 / 17 / 20 三种的旧指纹**完全相同**（文本长度都是 358）。
    于是磁盘上的夹具保留旧坐标、而验收 5 用 `line_baseline()` 算新坐标 ——
    "已知坐标"变成两份，容差够大时两边不一致也不会失败，
    那条验收就在验一个**不存在**的东西。
    """
    spec = next(s for s in FIXTURES if s.file_name == "contract_01_clean.pdf")
    baseline = fingerprint(build_document(spec))

    monkeypatch.setattr(contract_texts, "LINE_HEIGHT_PT", 17.0)
    shifted = fingerprint(build_document(spec))

    assert baseline != shifted, "行距变化没有反映在指纹里"


# ============================================================
# 7. 表单与正文的一致性
# ============================================================


def _text_amount(text: str) -> str | None:
    match = re.search(r"合同总金额：人民币 ([\d,]+)(?:\.\d+)? 元", text)
    return match.group(1).replace(",", "") if match else None


def test_form_amount_matches_contract_text() -> None:
    """审批单表单里的金额必须与**它所渲染的正文**一致。

    ⚠️ 这条守的是一次真实存在过的矛盾：HT-2026-0005（扫描件）表单填 `450000`，
    而它与 HT-2026-0003 共用同一份正文，正文写的是 `800,000.00` 元。

    单看 OCR 验收察觉不到（OCR 只读正文，不读表单）。但一旦做
    "表单 - 正文一致性检查"，就会冒出一个金额冲突 —— 而那个冲突**不是**
    这份夹具想演示的场景，它会把真正要验的 OCR 链路搅浑，
    排障的人会先怀疑 OCR 读错了数。
    """
    raw = json.loads(FIXTURES_JSON.read_text(encoding="utf-8"))
    mismatches: list[tuple[str, str, str]] = []

    for item in raw["instances"]:
        declared = item.get("form_data", {}).get("合同金额")
        if not declared:
            continue
        for attachment in item.get("attachments", []):
            spec = fixture_by_name(attachment.get("file_name", ""))
            if spec is None:
                continue
            in_text = _text_amount("\n".join(TEXTS[spec.text_key]))
            if in_text and in_text.lstrip("0") != str(declared).lstrip("0"):
                mismatches.append((item["approval_code"], str(declared), in_text))

    assert not mismatches, (
        "表单金额与正文金额不一致（实例, 表单, 正文）："
        + "；".join(f"{code}: {a} vs {b}" for code, a, b in mismatches)
    )
