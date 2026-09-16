"""LLM 适配器 —— **运行期唯一允许 import 模型 SDK 的目录**。

与 `app/adapters/parse/` 同一条纪律（那里只管 PyMuPDF，这里只管模型 SDK）：

- `app/ports/` / `app/services/` / `app/rules/` **不得** import `openai` 之类的 SDK；
- 换厂商（或换成 M11 的 GPU 推理 API）只改这里的实现，业务代码不动。

守卫在 `tests/test_adapter_llm.py`（源码级断言：`openai` 只允许出现在本目录下）——
只写在文档里的纪律会随着开发自然腐蚀，这与 M4 给 PyMuPDF 加守卫是同一条理由。
"""
