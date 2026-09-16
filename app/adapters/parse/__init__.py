"""解析适配器 —— **运行期唯一**允许使用 PyMuPDF 的目录（§7 风险表的承诺）。

这条纪律由两处源码级测试共同守住，而不是靠自觉：

- `tests/test_ports_and_errors.py` 的 `_FORBIDDEN_IN_PORTS`：端口层禁止 `fitz`；
- `tests/test_make_fixtures.py::test_mock_does_not_import_pymupdf`：
  `mock_approval/`（外部对接方）禁止 `fitz`。

开发期的 `scripts/make_fixtures.py` 也用 PyMuPDF，但**不进入运行链路**。

> PyMuPDF 是 **AGPL-3.0**（§7 已知约束）。替换成本之所以可控，
> 正是因为它的**运行期**用法只在这里 —— 不散落到业务层。
"""
