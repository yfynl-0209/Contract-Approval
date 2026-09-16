"""RapidOCR（CPU）适配器 —— 只负责"读字"，不做任何坐标换算。

坐标换算由解析管线用 `RenderTransform` 完成（§4.3）：本适配器返回的坐标
一律是**它看到的像素**，因此实现方不需要知道 PDF、DPI 或页面旋转。

## 两类失败必须分开登记

| 情形 | 错误码 | 分类 |
| --- | --- | --- |
| 引擎/模型不可用（未安装、模型文件缺失、版本不匹配） | `OCR_MODEL_UNAVAILABLE` | **确定性** |
| **真的超时**（超过执行时限） | `OCR_INFERENCE_TIMEOUT` | **瞬时** |
| 推理抛错但**不是**超时（资源抖动、图像异常） | `OCR_INFERENCE_FAILED` | **瞬时** |

三种情形的处置完全不同，因此**三个码不能合并**：

- 把"模型缺失"登记成可重试，会让一个**配置错误**重试到预算耗尽，
  把"模型路径写错"掩盖成"服务不稳定"；
- 把"非超时的推理抛错"记成"超时"，会让看板上"超时次数"这个数字失去意义 ——
  而它正是判断 Worker 是否健康的直接信号；
- 把"超时"判成确定性，会让一次卡顿把任务打成永久失败。

## 惰性加载

引擎在**首次构造适配器时**才 import 并加载模型（模型常驻内存，import 本身也慢）。
因此本模块可以在没有 RapidOCR 的环境里被导入 —— 测试用的是注入的假引擎。
"""

from __future__ import annotations

from typing import Any

from app.deadline import DeadlineExceeded, run_with_deadline
from app.enums import ErrorCode
from app.errors import PermanentError, TransientError
from app.ports.ocr_gateway import BBox, OcrLine, OcrPageResult

#: PyPI 包名（不是 import 名，两处刻意分开写，避免"改一个忘另一个"）
_PACKAGE = "rapidocr_onnxruntime"


class RapidOcrAdapter:
    """`OCRGateway` 的 RapidOCR 实现。

    Args:
        min_confidence: 行级置信度下限；低于它的行**不进入** `lines`，
            但仍计入 `detected_lines`（见 `OcrPageResult` 的说明）。
        engine: 可注入的引擎，便于测试。为 `None` 时惰性加载真实引擎。
    """

    ENGINE_NAME = _PACKAGE

    def __init__(
        self,
        *,
        min_confidence: float = 0.6,
        timeout_seconds: float = 60.0,
        engine: Any | None = None,
    ) -> None:
        self._min_confidence = min_confidence
        self._timeout = timeout_seconds
        self._engine = engine if engine is not None else self._load_engine()

    # ============================================================
    # 端口
    # ============================================================

    @property
    def engine(self) -> str:
        """引擎标识 —— 参与解析缓存键，换引擎必须让缓存失效。"""
        return self.ENGINE_NAME

    @property
    def version(self) -> str:
        """包版本 —— 同样参与缓存键，换模型不生效是最难发现的一类问题。"""
        try:
            from importlib.metadata import version

            return version(_PACKAGE)
        except Exception:  # pragma: no cover - 元数据缺失只在非常规安装下发生
            return "unknown"

    def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
        """识别**一页**整页图像（不做裁切，见 `PdfExtractor.render`）。"""
        try:
            # ⚠️ 推理是**同步 C 调用，无法从外部中断**。没有这道时限时，
            # 引擎卡死会让 Worker 永远阻塞，且不留任何错误码。
            detected, _elapsed = run_with_deadline(
                lambda: self._engine(image_png),
                timeout=self._timeout,
                label=f"ocr-page-{page}",
            )
        except DeadlineExceeded as exc:
            # 这里是**真的**超时，码名副其实。
            raise TransientError(
                f"第 {page} 页 OCR 推理超过 {self._timeout:g} 秒",
                code=ErrorCode.OCR_INFERENCE_TIMEOUT,
            ) from exc
        except Exception as exc:
            # 推理抛错但**不是**超时：按瞬时处理（重试有界、代价可控，
            # 归成确定性会让一次资源抖动把任务打成永久失败），
            # 但**码必须与超时分开** —— 合并会让看板上"超时次数"失去意义。
            raise TransientError(
                f"第 {page} 页 OCR 推理失败：{type(exc).__name__}",
                code=ErrorCode.OCR_INFERENCE_FAILED,
            ) from exc

        accepted: list[OcrLine] = []
        raw_scores: list[float] = []

        for item in detected or ():
            box, text, score = item
            confidence = float(score)
            raw_scores.append(confidence)
            if confidence < self._min_confidence:
                continue
            accepted.append(
                OcrLine(text=str(text), bbox=_box_to_bbox(box), confidence=confidence)
            )

        return OcrPageResult(
            page=page,
            lines=tuple(accepted),
            # 取**最小**：均值会让"大部分行清楚、少数行乱码"的页面显得合格，
            # 而乱码行恰恰是最需要人看的那一行。
            confidence=min((line.confidence for line in accepted), default=None),
            detected_lines=len(raw_scores),
            raw_confidence=min(raw_scores, default=None),
        )

    def close(self) -> None:
        """释放引擎占用的资源（模型常驻内存，长跑进程需要显式释放）。"""
        self._engine = None

    # ============================================================
    # 内部
    # ============================================================

    @staticmethod
    def _load_engine() -> Any:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:
            raise PermanentError(
                f"OCR 引擎不可用：{_PACKAGE} 未安装或无法导入",
                code=ErrorCode.OCR_MODEL_UNAVAILABLE,
            ) from exc

        try:
            return RapidOCR()
        except Exception as exc:
            raise PermanentError(
                f"OCR 引擎初始化失败（模型文件缺失或版本不匹配）：{_PACKAGE}",
                code=ErrorCode.OCR_MODEL_UNAVAILABLE,
            ) from exc


def _box_to_bbox(box: Any) -> BBox:
    """四点框 → 外接矩形。

    RapidOCR 返回的是**四边形**（可能因页面倾斜而不是轴对齐的）。
    这里取外接矩形而不是假设"四点就是矩形的四角"：后者在倾斜页面上
    会得到自相交的框，而它仍然"看起来像个框"。
    """
    points = list(box)
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return (min(xs), min(ys), max(xs), max(ys))
