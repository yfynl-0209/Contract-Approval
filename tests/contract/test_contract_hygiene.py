"""合约目录自身的约束：**不得依赖任何具体实现**。

## 为什么需要这道守卫

合约测试只有在"任何实现都能跑"时才有价值。一旦它 import 了
`LocalFileStorage`（哪怕只是为了一句 `isinstance` 断言），
它就从"存储的合约"退化成"本地存储的测试"：

- M9 的 MinIO 要么无法复用这套测试；
- 要么被迫去模拟本地文件系统的行为，**而那是错的**——
  合约应当只约束外部语义，不约束实现手段。

这类退化是**静默**的：测试照样全绿，只是覆盖范围悄悄缩小了。
所以用机器来守，而不是靠评审时的记忆。
"""

from __future__ import annotations

import ast
from pathlib import Path

CONTRACT_DIR = Path(__file__).resolve().parent

#: 合约允许依赖的应用内模块前缀。
#:
#: 只放"定义"类模块：端口（被约束的契约）、枚举（取值域）、异常（失败分类）。
#: `app.services` / `app.api` 之类一律不允许 —— 合约描述的是存储/网关自身的语义，
#: 一旦它开始依赖业务逻辑，就不再是能独立复用的合约了。
ALLOWED_APP_PREFIXES = ("app.ports", "app.enums", "app.errors")


def _imported_modules(path: Path) -> set[str]:
    """收集一个模块里所有 import 的目标名（含 `from ... import` 的模块部分）。

    用 AST 而不是正则：正则会把字符串与注释里的 `import` 也算进来，
    而"禁止 import"这类规则最怕误报 —— 一次误报就没人再信任它了。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _contract_modules() -> list[Path]:
    """合约模块（排除测试文件本身）。"""
    return sorted(
        path
        for path in CONTRACT_DIR.glob("*.py")
        if not path.name.startswith("test_")
    )


def _findings(predicate) -> dict[str, list[str]]:  # type: ignore[no-untyped-def]
    found: dict[str, list[str]] = {}
    for path in _contract_modules():
        matched = sorted(name for name in _imported_modules(path) if predicate(name))
        if matched:
            found[path.name] = matched
    return found


def test_contract_modules_do_not_import_adapters() -> None:
    """合约模块不得 import 任何具体适配器。

    否则 M9 换实现时，这套测试要么跑不了，要么会**把新实现带偏**。
    """
    offenders = _findings(lambda name: "adapters" in name.split("."))

    assert offenders == {}, (
        "合约模块依赖了具体适配器，其他实现将无法复用："
        f"{offenders}。请把实现相关的断言移回各自的实现测试文件。"
    )


def test_contract_modules_only_depend_on_definitions() -> None:
    """合约只允许依赖端口 / 枚举 / 异常这三类"定义"模块。"""
    offenders = _findings(
        lambda name: name.startswith("app.")
        and not name.startswith(ALLOWED_APP_PREFIXES)
    )

    assert offenders == {}, (
        f"合约模块依赖了业务模块，不再是可独立复用的合约：{offenders}"
    )


def test_contract_suite_is_actually_present() -> None:
    """防止"守卫测试还在、被守卫的东西已经被删掉"。

    没有这条，删掉整个合约文件后上面两条会**静默通过** ——
    因为检查的是空集合。安全检查最常见的失效方式不是写错，
    而是它保护的对象已经不在了却没人发现。
    """
    modules = _contract_modules()

    assert modules, "合约目录下已经没有任何合约模块了"
    assert any(path.name == "storage_contract.py" for path in modules), (
        "存储合约不见了；若是有意删除，请同时删除本文件的守卫"
    )
