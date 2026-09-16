"""应用服务层 —— 全部业务逻辑所在，与传输协议无关（不 import fastapi）。

依赖方向：`api/` → `services/` → `ports/` ← `adapters/`。
这保证 REST 与 MCP 两种形态**共用同一套业务逻辑**。

服务方法名与 7 个工具逐字对齐（如 `list_pending_contract_approvals`），
M7 的工具门面才能是零逻辑的协议适配器。
"""
