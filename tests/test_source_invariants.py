"""跨模块的**源码级不变量**。

这里放的是"违反了也能跑通、只在特定条件下才出问题"的规则。
它们不是某个函数的行为，无法用普通单元测试表达，但可以用 AST 扫描来守。

## 为什么需要机器来守

下面两条规则都**写进过文档**，也都**真的被违反过**：

| 规则 | 被违反后的具体表现 |
| --- | --- |
| 幂等键不得被反解析 | 附件编号 `ATT:1` 被解析成 `1`：任务正确阻塞，而附件记录永留 `pending`，日志里的编号还是错的 |
| 业务层不得依赖适配器 | "换厂商只改一个文件"不再成立——替换实现会牵动 `services/` 的代码 |

**写进文档的规则会随时间被淡忘，写成测试的规则不会。**
而且这两条都属于"抽查发现不了"的形状：前者只在编号含特殊字符时触发，
后者只要有人图省事复制一行 import 就会破坏。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.config import PROJECT_ROOT

APP_DIR = PROJECT_ROOT / "app"

#: 会被误用为"反解析"的字符串方法
_SPLIT_METHODS = frozenset(
    {"split", "rsplit", "partition", "rpartition", "splitlines"}
)

#: 允许 import 适配器的位置（**组合根**）。
#:
#: `app/api/deps.py` 的职责就是"在这里把端口接上具体实现"，
#: 它 import 适配器不是违规，而是它的存在意义。
#: M4 的 `worker.py`、M6 的 `outbox_dispatcher.py` 同属此类，
#: 因此这里按**层**排除，而不是逐个文件白名单。
_COMPOSITION_ROOTS = frozenset({"api", "composition"})

#: 不得依赖适配器的层：业务逻辑必须只依赖 `ports/` 中的 Protocol
_BUSINESS_LAYERS = ("services", "workflow", "ports", "rules", "parsing")


def _iter_modules(root: Path) -> list[Path]:
    if not root.is_dir():  # 该层尚未建立（如 M4 的 parsing/）
        return []
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_modules(path: Path) -> set[str]:
    """收集模块里所有 import 的目标名（含 `from ... import` 的模块部分）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _relative(path: Path) -> str:
    return path.relative_to(APP_DIR).as_posix()


# ============================================================
# 1. 幂等键是不透明标识符
# ============================================================


def test_idempotency_key_is_never_parsed() -> None:
    """**幂等键不得被反解析出业务字段。**

    它是"某个操作的唯一指纹"，不是结构化数据：各段由 `:` 连接只是可读性考虑，
    而**业务字段本身可以含冒号**（附件编号 `ATT:1` 合法）。

    实测过的后果：

    ```text
    key = download:mock:default:SAME:ATT:1:first
    job.idempotency_key.rsplit(":", 1)[0].rsplit(":", 1)[-1]
      → "1"        ← 期望 "ATT:1"
    ```

    于是按该编号去找附件记录找不到：任务被正确阻塞，
    而附件记录永远留在 `pending`（控制台显示"待下载"），
    日志里记的编号也是错的——排障时照着查会查到一份不存在的附件。

    需要业务字段时由**调用方显式传递**，而不是从键里猜。
    """
    offenders: dict[str, list[int]] = {}

    for path in _iter_modules(APP_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _SPLIT_METHODS:
                continue
            # 只看接收者里出现 idempotency_key 的调用，避免误伤普通字符串切分
            if "idempotency_key" in ast.unparse(func.value):
                lines.append(node.lineno)
        if lines:
            offenders[_relative(path)] = lines

    assert offenders == {}, (
        "幂等键被反解析了；业务字段请由调用方显式传入："
        f"{offenders}"
    )


# ============================================================
# 2. 依赖方向
# ============================================================


def test_business_layers_do_not_import_adapters() -> None:
    """**业务层只能依赖端口，不得依赖适配器。**

    这是"换厂商只改一个文件"能成立的唯一前提。
    一旦 `services/` 里出现 `from app.adapters...`，
    替换实现就会牵动业务代码，而这类破坏是**渐进**的：
    第一行 import 总是"先这样，回头再抽"，之后就再也没抽。

    > 这正是 M9 换 MinIO、M11 接真实 OCR 时最容易被卡住的地方。
    """
    offenders: dict[str, list[str]] = {}

    for layer in _BUSINESS_LAYERS:
        for path in _iter_modules(APP_DIR / layer):
            bad = sorted(
                name
                for name in _imported_modules(path)
                if "adapters" in name.split(".")
            )
            if bad:
                offenders[_relative(path)] = bad

    assert offenders == {}, (
        "业务层 import 了适配器，实现将无法替换："
        f"{offenders}。请改为依赖 ports/ 中的 Protocol。"
    )


def test_business_layers_do_not_import_the_http_layer() -> None:
    """**业务层不得 import `fastapi` / `starlette`。**

    与上一条同源（都是"依赖方向"），但方向相反且同样只在特定条件下显形：
    业务层 import 了 HTTP 框架之后，代码**照样能跑** —— 直到有人想在
    没有 HTTP 上下文的进程里复用它（MCP 形态、Worker、Scheduled 任务都算）。

    后果不是报错，而是"这段逻辑只能在请求里用"：MCP 形态于是要么复制一份，
    要么被迫构造一个假的请求对象。两条路都会让**两种协议的语义分叉**。

    > 门面（`app/tool_facade.py`）有它自己的守卫
    > （`tests/test_m7_contracts.py::test_facade_never_imports_the_http_layer`）；
    > 本条把同一约束扩到**整层**：只要有一处漏了，MCP 就无法复用整条链路。
    """
    offenders: dict[str, list[str]] = {}

    for layer in _BUSINESS_LAYERS:
        for path in _iter_modules(APP_DIR / layer):
            bad = sorted(
                name
                for name in _imported_modules(path)
                if name.split(".")[0] in {"fastapi", "starlette"}
            )
            if bad:
                offenders[_relative(path)] = bad

    assert offenders == {}, (
        f"业务层 import 了 HTTP 框架，逻辑将无法在非 HTTP 进程里复用：{offenders}"
    )


def test_mcp_adapter_imports_neither_http_layer_nor_adapters() -> None:
    """**MCP 适配器**不得 import `app.api` / HTTP 框架 / 任何适配器。

    | 违规 | 后果 |
    | --- | --- |
    | import `app.api` | MCP 形态被拖进 HTTP 框架（为调一个工具而必须构造请求、理解状态码） |
    | import 适配器 | "换厂商只改一个文件"不再成立；`mcp_server.py` 会变成**第二个组合根** |

    第二行尤其要机器来守：多一个组合根不会报错，只是**换实现时要改两处**，
    而漏掉的那一处要等到某个环境（比如 MCP 形态）起不来才发现。
    """
    path = APP_DIR / "mcp_server.py"
    assert path.is_file(), "app/mcp_server.py 不存在——守卫的对象可能被改名了"

    imports = _imported_modules(path)
    offenders = sorted(
        name
        for name in imports
        if name.split(".")[0] in {"fastapi", "starlette"}
        or name.startswith("app.api")
        or "adapters" in name.split(".")
    )

    assert offenders == [], f"app/mcp_server.py 不得依赖协议层或适配器：{offenders}"
    """防止"守卫还在、组合根已经被改名"。

    若 `app/api/deps.py` 被删或改名，上一条测试依然会通过（检查的是空集合）——
    安全检查最常见的失效方式不是写错，而是**它保护的对象已经换了位置**。
    这条把"适配器确实在某处被接线"这件事也固定下来。

    ## 适配器之间互相引用是允许的

    依赖方向约束的是**跨层**：业务层不得反向依赖实现。
    `adapters/auth/jwt_identity.py` 引用同包的 `adapters/auth/_headers.py`
    是**同一层内部**的复用，方向没有被反转 —— 把它也判为违规，
    唯一的结果是把公共小工具复制两份，而两份迟早会分叉。

    ⚠️ 这条豁免**只覆盖适配器自己的一层**：`services/`、`workflow/`、
    `rules/` 只要出现 `from app.adapters...` 依然会被上一条测试拦下，
    本条的判据也依然要求"至少有一个组合根真的在接线"。
    """
    def _is_composition_root(name: str) -> bool:
        parts = Path(name).parts
        return parts[0] in _COMPOSITION_ROOTS or parts[0] == "adapters"

    importers = sorted(
        _relative(path)
        for path in _iter_modules(APP_DIR)
        if any("adapters" in name.split(".") for name in _imported_modules(path))
    )

    assert importers, "没有任何模块 import 适配器——组合根可能被误删或改名了"
    assert all(
        _is_composition_root(name) for name in importers
    ), f"适配器只应在组合根被接线，实际出现在：{importers}"
