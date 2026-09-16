"""合同审批审查系统 —— 工具服务主入口（里程碑 M0 骨架）。

启动：
    python -m uvicorn app.main:app --reload

当前仅包含健康检查接口；后续里程碑会陆续挂载：
  - /tools/*       7 个工具接口（REST 形态，与 MCP 形态共用业务逻辑）
  - /api/tasks/*   任务查询与人工重试
  - /api/rules/*   规则 CRUD 与热更新
  - 控制台页面路由
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from app import __version__
from app.api import admin as admin_api
from app.api import attachments as attachments_api
from app.api import deps as api_deps
from app.api import identity as identity_api
from app.api import jobs as jobs_api
from app.api import results as results_api
from app.api import rules as rules_api
from app.api import tasks as tasks_api
from app.api import tools as tools_api
from app.api.errors import (
    error_body,
    error_headers,
    http_status_for,
    http_status_for_result_error,
    invalid_argument_body,
    result_error_body,
)
from app.auth import assert_auth_configuration
from app.config import settings
from app.context import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    is_valid_correlation_id,
    new_correlation_id,
    reset_correlation_id,
)
from app.db import engine
from app.errors import AppError
from app.services.result_service import ResultInputError


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """进程生命周期钩子。

    启动时校验**身份配置**：生产环境选了开发期身份来源、或 JWT 配置不完整，
    就在这里抛错让进程**起不来**（M7）。放在启动期而不是"第一次请求时"，
    是因为后者会让一个没有任何身份体系的服务**先跑起来并对外服务**，
    等有人发现时，它已经用"信任请求头"的方式处理过真实流量了。

    退出时关闭**已经构造过**的适配器（`httpx` 连接池）——
    惰性构造意味着进程可能从未用过网关，此时不该凭空建一个再关掉。
    """
    assert_auth_configuration(
        env=settings.env,
        auth_mode=settings.auth_mode,
        jwt_public_key=settings.jwt_public_key,
        jwt_jwks_url=settings.jwt_jwks_url,
        jwt_issuer=settings.jwt_issuer,
        jwt_audience=settings.jwt_audience,
    )
    yield
    api_deps.close_adapters(application)


app = FastAPI(
    title="合同审批审查系统",
    version=__version__,
    lifespan=lifespan,
    description=(
        "面向企业合同审批场景的自动审查工具服务。\n\n"
        "**定位**：AI 出证据，人做决定 —— 在不直接代替人工审批的前提下，"
        "生成风险审查意见并写回审批评论区。\n\n"
        "**降级说明**：未配置大模型时，系统自动切换为纯规则模式，功能仍完整可用。"
    ),
)

app.include_router(tools_api.router)
app.include_router(jobs_api.router)
# ⚠️ 注册顺序只看**路径是否重复**：`jobs` 与 `results` 两组路径互不重叠
# （`/api/results/{id}` 只在 `results` 里定义一次，见 `app/api/jobs.py` 的说明）。
# 重复注册时后一条**永远不生效且不报错**，因此 `tests/test_m7_routes.py`
# 有一条"路由路径不得重复"的守卫。
app.include_router(tasks_api.router)
app.include_router(results_api.router)
app.include_router(attachments_api.router)
# M7 Task 5：人工重试、日志与审计查询；规则管理（版本化修改 + 激活前校验）。
app.include_router(admin_api.router)
app.include_router(rules_api.router)
# M8 Task 2：前端要按权限渲染导航，而权限只能由服务端算（理由见模块 docstring）。
app.include_router(identity_api.router)


# ============================================================
# 关联 ID 贯穿（设计文档 §4.7 / 决策④）
# ============================================================


@app.middleware("http")
async def _correlation_middleware(request: Request, call_next: Any) -> Response:
    """绑定请求的关联 ID、回传响应头，并在**任何路径下**都重置。

    | 项 | 行为 |
    | --- | --- |
    | 来源 | 请求头 `X-Correlation-ID`；缺省由服务端生成 UUID4 |
    | 校验 | 不合法 → **400**（不静默替换） |
    | 传递 | 存 contextvar → `LogService` 自动带上 → `create_job` 写入作业 |
    | 清理 | `finally` 里重置，避免线程池复用导致**日志串号** |

    ⚠️ 重置必须放在 `finally`。异常路径上不重置时，那个线程的下一个请求
    会**继承本次的关联 ID** —— 于是 A 的日志记到 B 的 ID 下，
    而两边各自看起来都很正常。排障时最怕的就是证据本身是错的。

    **为什么回传响应头**：调用方没传 ID 时，他需要知道我们用了哪一个，
    否则拿不到任何可以拿来查日志的东西 —— 生成一个不告诉别人的 ID 等于没生成。
    """
    raw = request.headers.get(CORRELATION_ID_HEADER)

    if raw is None:
        identifier = new_correlation_id()
    else:
        identifier = raw.strip()
        if not is_valid_correlation_id(identifier):
            # 不静默替换：换了之后调用方手里的 ID 与我们库里的对不上，
            # 他以为能查到，实际永远查不到，而且**没有任何提示**。
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=invalid_argument_body(
                    f"请求头 {CORRELATION_ID_HEADER} 非法："
                    f"需匹配 ^[A-Za-z0-9._:-]{{1,128}}$，收到 {raw!r}"
                ),
            )

    token = bind_correlation_id(identifier)
    try:
        response = await call_next(request)
    finally:
        reset_correlation_id(token)

    response.headers[CORRELATION_ID_HEADER] = identifier
    return response


# ============================================================
# 异常翻译：业务异常 → HTTP 状态码
# ============================================================
# 集中在这里而不是散在各端点：三个端点共用一套映射，
# 且 M7 新增端点时自动继承，不会漏掉某一处。
# 映射表与选择原则见 `app/api/errors.py`。


@app.exception_handler(AppError)
async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    """把端口异常翻译成 HTTP 响应。

    ⚠️ 本处理器只对端点**未捕获**的异常生效。
    工具 3 的"业务事实"（附件缺失 / 空文件 / 超限 / 类型不符）已在端点内
    被捕获并返回 **200** —— 那是业务结论，不是错误。理由见 `app/api/errors.py`。
    """
    return JSONResponse(
        status_code=http_status_for(exc),
        content=error_body(exc),
        headers=error_headers(exc),
    )


@app.exception_handler(ResultInputError)
async def _handle_result_input_error(
    _request: Request, exc: ResultInputError
) -> JSONResponse:
    """结果保存 / 查询的稳定业务错误 → 404 / 409 / 400（**保留原因码**）。

    ⚠️ **必须单独登记，且必须排在 `ValueError` 之前**：`ResultInputError`
    继承 `ValueError`，不登记就会落进下面那个"参数非法 → 400"的兜底分支，
    于是：

    | 实际情况 | 兜底后的响应 |
    | --- | --- |
    | 批次还没跑完（可恢复，等等再来） | 400 `INVALID_ARGUMENT` |
    | 风险等级传错了（调用方得改） | 400 `INVALID_ARGUMENT` |

    两者**无法区分**，而它们的正确处置完全不同。原因码也被泛化掉，
    调用方（含 MCP 形态与模型侧）赖以分支的机器判据就此丢失。

    Starlette 按异常的 MRO 选取处理器，因此这条比 `ValueError` 那条更具体、
    优先命中；顺序上把它写在前面，是为了让"更具体的处理器在前"这件事
    在源码里也一眼可见。
    """
    return JSONResponse(
        status_code=http_status_for_result_error(exc),
        content=result_error_body(exc),
    )


@app.exception_handler(ValueError)
async def _handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
    """参数非法 → **400**。

    服务层对**调用方传入的标识**（空串、纯空白）抛 `ValueError`，
    它的消息本身就是写给调用方看的。映射成 500 会让"调用方传错了参数"
    看起来像"服务端有 bug"，把排查方向带偏。

    这里只兜 `ValueError`：真正的代码缺陷（`TypeError`、`AttributeError`）
    仍会以 500 暴露出来 —— 那才是它们该有的样子。
    """
    return JSONResponse(status_code=400, content=invalid_argument_body(str(exc)))


def _probe_db() -> tuple[bool, list[str]]:
    """探测数据库连通性，返回 `(是否可用, 表清单)`。

    最轻量的探测：`SELECT 1`，不读任何业务表，避免健康检查本身造成负载。
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        # 顺带回传表清单，便于确认 db/schema.sql 是否已执行
        return True, sorted(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001
        # 健康检查必须吞掉所有异常：连不上库是"要报告的状态"，不是"要抛出的错误"
        return False, []


@app.get("/health/live", tags=["系统"], summary="存活检查")
def health_live() -> dict:
    """进程是否**存活**（liveness）。

    **刻意不检查任何依赖**：数据库断开时它也必须返回 200。
    否则编排系统会判定"进程死了"而反复重启容器——
    而真正的问题在数据库，重启应用进程永远修不好，只会放大故障。
    """
    return {"status": "alive", "version": __version__}


@app.get("/health/ready", tags=["系统"], summary="就绪检查")
def health_ready(response: Response) -> dict:
    """是否**可以接收流量**（readiness）。

    关键依赖（数据库）不可用时返回 **503**，
    这样负载均衡器会把流量切走，而不是继续把请求送给一个注定失败的实例。

    与 `/health` 的分工必须分清：

    | 接口 | 给谁看 | 失败时 |
    | --- | --- | --- |
    | `/health/ready` | 编排系统 / 负载均衡 | **503** |
    | `/health` | 人（诊断） | 仍 200 + 详细字段 |
    """
    db_ok, tables = _probe_db()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "version": __version__,
            "reason": "DATABASE_UNAVAILABLE",
            "checks": {"database": False},
        }
    return {
        "status": "ready",
        "version": __version__,
        "checks": {"database": True, "table_count": len(tables)},
    }


@app.get("/health", tags=["系统"], summary="健康检查（人用诊断接口）")
def health() -> dict:
    """完整诊断信息，用于验证 M0 / M1 是否就绪。

    设计取舍：**即使数据库不可用也返回 HTTP 200**，
    而是通过响应体里的 `db_ok` 字段表达真实状态——
    这样人能看到完整诊断信息，而不是一个没有细节的 500。

    ⚠️ 正因为"永远 200"，它**不适合作为生产环境的就绪判据**：
    数据库断开时它照样返回 200，负载均衡器可能继续把流量送来。
    编排与负载均衡请使用 `/health/ready`（依赖不可用时返回 503）。
    """
    db_ok, tables = _probe_db()

    return {
        "status": "ok",
        "version": __version__,
        "db_ok": db_ok,
        "table_count": len(tables),
        "tables": tables,
        # 暴露降级状态，便于确认当前跑的是"模型增强"还是"纯规则"模式
        "llm_enabled": settings.llm_enabled,
    }
