"""缺失类规则的适用范围政策。

`absent=true` 的缺失类规则最容易误报：一份标准商品采购合同没有知识产权条款是**正常**的，
若规则全局适用就会报"知识产权条款缺失"——纯粹的噪音。

原则：**缺失类规则都应当限定适用范围**。唯一例外是"任何合同都应当具备的条款"——
违约责任与争议解决，这两类缺失在任何合同里都是真实缺陷。

白名单必须显式维护：新增允许全局适用的缺失类规则时要登记并写明理由，
避免"忘记限定范围"被悄悄放过。
"""

from __future__ import annotations

from typing import Final

#: 允许不限定适用范围（全局适用）的缺失类规则
UNIVERSALLY_REQUIRED_MISSING_RULES: Final[frozenset[str]] = frozenset(
    {
        "LIAB_MISSING",  # 违约责任：任何合同都应约定违约情形与赔偿方式
        "JURIS_MISSING",  # 争议解决：任何合同都应约定管辖或仲裁
    }
)


def needs_scope(rule_code: str) -> bool:
    """该缺失类规则是否必须限定适用范围（否则规则加载校验判为配置问题）。"""
    return rule_code not in UNIVERSALLY_REQUIRED_MISSING_RULES
