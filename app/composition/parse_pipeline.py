"""解析管线的组合：字节 → 标准文档。

## 为什么它必须是唯一实例化适配器的地方

`PyMuPdfExtractor` / `RapidOcrAdapter` / `DocumentBuilder` 都是**实现选择**：
M9 换 OCR、或引入新的渲染实现时，应当只改这里 ——
服务层（`parse_service`）只认 `document_factory: bytes -> StandardDocument` 这个可调用对象，
连"有没有 OCR"都不需要知道。

`content_type` 参数参与签名是为了一处对称：**入队时冻结了它**（`ParseJobInput`），
执行时把它交给组合根 —— 未来若需要按类型选择不同管线（例如图片走独立预处理），
改的仍然只有这里。
"""

from __future__ import annotations

from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
from app.adapters.parse.rapidocr_adapter import RapidOcrAdapter
from app.ports.parse_document import StandardDocument
from app.services.document_builder import DocumentBuilder
from app.workflow.job_inputs import ParseOptions


def build_standard_document(
    data: bytes, *, options: ParseOptions, content_type: str
) -> StandardDocument:
    """附件字节 → 标准文档。

    ⚠️ OCR 适配器**总是传入**：是否真的走 OCR 由 `DocumentBuilder` 逐页路由
    （有文本层的页不会碰 OCR），而不是在这里猜测内容类型 ——
    猜错的表现是"文本页被送去做 OCR"或"扫描页拿文本层冒充成功"。

    ⚠️ 抽取器用 `with` 管理：`DocumentBuilder.build()` 读完页面后，
    底层的 PDF 句柄必须释放 —— 解析是长任务，泄漏会累积到把 Worker 拖垮。
    """
    with PyMuPdfExtractor.open(data) as extractor:
        return DocumentBuilder(extractor, ocr=RapidOcrAdapter(), options=options).build()
