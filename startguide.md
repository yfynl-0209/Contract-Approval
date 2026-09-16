# 本地启动指南（Mock 网关 → API → 两个 Worker → 前端）

> 手动启动看本文；一键启停直接跑 `scripts/start_all.ps1` / `scripts/stop_all.ps1`。
> 所有命令都在**项目根目录**（`contract-approval-system/`）执行。

## 0. 前置条件（只需做一次）

| 依赖 | 版本 | 检查命令 |
| --- | --- | --- |
| Python | 3.11+ | `python --version` |
| Node.js | 20+ | `node --version` |

```powershell
# ① Python 虚拟环境 + 依赖
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt

# ② 前端依赖
cd frontend; npm install; cd ..

# ③ 配置文件：首次从模板复制（本仓库已有 .env 则跳过）
Copy-Item .env.example .env
```

`.env` 关键项（开发默认值即可跑通）：`DB_URL=sqlite:///./data/app.db`、
`MOCK_APPROVAL_BASE_URL=http://127.0.0.1:8001`、`AUTH_MODE=dev`。

## 1. 初始化数据库（首次 / schema 变更后）

```powershell
.\.venv\Scripts\python.exe scripts\init_db.py
```

建表 `data/app.db`，并灌入规则种子数据。**数据库已存在时跳过此步**。

## 2.按顺序启动 5 个服务

每条命令开一个**独立的终端窗口**（Ctrl+C 可停掉该窗口的服务）。

```powershell
# ① Mock 审批网关（端口 8001）—— 必须最先起，API 启动时会校验它可达
.\.venv\Scripts\python.exe -m uvicorn mock_approval.main:app --host 127.0.0.1 --port 8001

# ② API 后端（端口 8000）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ③ 规则/解析 Worker（默认领 PARSE + RULE 两类作业）
.\.venv\Scripts\python.exe scripts\run_worker.py

# ④ 回写派发器（轮询 Outbox，把审查结论送回审批系统）
.\.venv\Scripts\python.exe scripts\run_outbox_dispatcher.py

# ⑤ 前端开发服务器（端口 5173，/api 自动代理到 8000）
cd frontend; npm run dev
```

## 3. 验证都起来了

| 检查 | 方式 | 期望 |
| --- | --- | --- |
| Mock 网关 | 浏览器开 `http://127.0.0.1:8001/docs` | Swagger 页面能打开 |
| API 后端 | 浏览器开 `http://127.0.0.1:8000/docs` | Swagger 页面能打开 |
| 前端 | 浏览器开 `http://127.0.0.1:5173` | 出现「合同审查控制台」待办列表 |
| Worker | 看窗口 ③④ 输出 | 持续轮询日志，无红字报错 |

## 4. 使用身份（AUTH_MODE=dev）

开发期身份由请求头声明，前端页面右上角的**身份面板**可随时切换
（对应 `X-Actor-Id` / `X-Actor-Roles` / `X-Tenant-Id` 三个头，
角色只认 `system_admin` / `approver` / `auditor`，其余角色不授予权限）。

## 5. 停止

- 各窗口直接 Ctrl+C；
- 或一键停全部：`.\scripts\stop_all.ps1`（按端口 8000/8001/5173 + 脚本名定位进程）。

## 6. 一键验收（可选）

```powershell
.\scripts\verify_m8.ps1   # 后端全量 pytest + 五模块走查 + 前端四条门禁
```

## 常见问题

- **API 起不来、报网关不可达**：先起 Mock 网关（①），API 启动校验依赖它。
- **页面 401「缺少身份」**：AUTH_MODE=dev 下需要请求头；在页面右上角身份面板选一个身份（沉浸式翻译等浏览器扩展可能剥掉自定义头，换无痕窗口验证）。
- **解析一直 pending**：窗口 ③ 的 Worker 没起，或它的 `.env` 里 `ATTACHMENT_ALLOWED_TYPES` 与附件类型不符。
- **回写一直 not_written**：确认窗口 ④ 的派发器在跑；门禁拒绝时看任务详情的 `latest_reason_code`。
