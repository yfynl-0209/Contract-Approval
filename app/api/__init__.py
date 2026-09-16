"""接口层 —— 只做协议转换，不写业务判断（企业化设计 §6.1）。

依赖方向：`api/` → `services/` → `ports/` ← `adapters/`。
只负责四件事：参数校验（`schemas.py`）、调用服务、结果序列化、异常翻译（`api/errors.py`）。

一旦这里出现 `if task_status == ...` 这类分支，业务规则就会散落到 REST 与 MCP
两套门面里，两边迟早给出不同结论。
"""
