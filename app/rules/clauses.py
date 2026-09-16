"""条款类型白名单 —— 解析模块与规则库之间的契约（与 `fields.py` 同源）。

## 为什么必须有这份白名单

`clause_info_json` 的消费方是 M5 的规则。如果条款类型只是一串**自由文本**，
那么：

    把 `intellectual_property` 拼成 `intellectual_propertys`
    → 运行时只得到"该条款不存在"
    → **一个拼写错误变成"合同缺少知识产权条款"的错误结论**
    → 而且完全没有报错。

这与 `app/rules/fields.py` 存在的理由**逐字相同**。`fields.py` 立起来之后，
字段名不再会拼错；但条款类型此前**没有任何受控定义** —— 全仓库只有
`app/models.py` 里的一句注释。

## ⚠️ 一次修正：数量对了，语义不对

初版也是 8 类，但为了凑数做了两件错事：

- 把**交付**与**验收**合成 `delivery_acceptance`；
- 另外**新增**了 `auto_renewal`。

数量仍是 8，但需求规定的 8 类是
**付款 / 交付 / 验收 / 违约 / 保密 / 数据 / 知识产权 / 争议解决** ——
"自动续约"**不能替代**独立的"交付条款"或"验收条款"。

合并交付与验收的后果很具体：一条"缺交付条款"的规则与一条"缺验收条款"的规则
读到的是**同一个键**，于是两者的结论必然一致 —— 而真实合同完全可能
约定了交付却没约定验收。

**本稿按需求拆成 8 类**，`auto_renewal` 作为**第 9 类扩展**保留
（`db/seed.sql` 里有 4 条自动续约规则要读它，删掉会让那些规则永远判缺失）。
"""

from __future__ import annotations

from typing import Final

#: 需求规定的 8 类。**不要为了"看起来更全"而增删** ——
#: 每增一类都要有规则消费它，每删一类都会有规则判不到。
REQUIRED_CLAUSE_TYPES: Final[dict[str, str]] = {
    "payment": "付款",
    "delivery": "交付",
    "acceptance": "验收",
    "liability": "违约",
    "confidentiality": "保密",
    "data_processing": "数据",
    "intellectual_property": "知识产权",
    "dispute_resolution": "争议解决（含管辖地）",
}

#: 扩展类：需求没要求，但已有规则在消费它。
#: 与 `REQUIRED_CLAUSE_TYPES` **分开声明**，这样"8 类"这个数字始终可校验 ——
#: 混在一起时，"需求覆盖"与"规则需要"两件事会互相掩盖。
EXTENSION_CLAUSE_TYPES: Final[dict[str, str]] = {
    "auto_renewal": "自动续约",
}

#: 全部条款类型（需求 8 类 + 扩展）。
CLAUSE_TYPES: Final[dict[str, str]] = {
    **REQUIRED_CLAUSE_TYPES,
    **EXTENSION_CLAUSE_TYPES,
}

#: 每类条款的**检索词**（按优先级）。命中任一即判"该条款存在"。
#:
#: ⚠️ 这些词直接决定 `not_found` 的结论，因此**必须覆盖合同中真实出现的写法**。
#: 放宽（多列几个同义词）的代价只是多定位到几处；
#: 收紧的代价是**把存在的条款判成缺失** —— 而那是误报，会触发"条款缺失"告警。
CLAUSE_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "payment": ("付款方式", "付款", "支付"),
    # 交付与验收**分开检索**：合成一类会让两条规则读到同一个键
    "delivery": ("交付", "交货", "供货"),
    "acceptance": ("验收",),
    "liability": ("违约责任", "违约金"),
    "confidentiality": ("保密条款", "保密义务", "保密"),
    "data_processing": ("数据处理", "个人信息", "数据安全"),
    "intellectual_property": ("知识产权", "著作权", "专利"),
    "dispute_resolution": ("争议解决", "管辖", "仲裁", "诉讼"),
    "auto_renewal": ("自动续约", "自动续期", "续约"),
}

ALL_CLAUSE_TYPES: Final[frozenset[str]] = frozenset(CLAUSE_TYPES)


def is_known_clause(clause_code: str) -> bool:
    return clause_code in ALL_CLAUSE_TYPES


def is_required_clause(clause_code: str) -> bool:
    """是否属于需求规定的 8 类（扩展类返回 `False`）。"""
    return clause_code in REQUIRED_CLAUSE_TYPES


def describe_clause(clause_code: str) -> str:
    """条款类型的中文名，用于规则校验报错与界面展示。"""
    return CLAUSE_TYPES.get(clause_code, "未知条款类型")


def keywords_for(clause_code: str) -> tuple[str, ...]:
    """该条款的检索词。未登记的类型返回空元组（检索不到 → `not_found`）。"""
    return CLAUSE_KEYWORDS.get(clause_code, ())
