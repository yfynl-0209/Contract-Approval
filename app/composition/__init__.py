"""组合根（composition root）。

**整个项目里唯一允许实例化具体适配器来装配运行时管线的地方。**

服务层依赖端口（`ports/`），适配器依赖端口（`adapters/`），
而"哪个适配器接哪个端口、用什么参数"这件事必须**恰好有一个地方**知道 ——
分散在调用点的话，换实现（M9 换 MinIO/真实 OCR）就会变成全库检索。
"""

from app.composition import parse_pipeline  # noqa: F401  (显式转出)
