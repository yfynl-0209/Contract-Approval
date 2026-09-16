"""解析字段白名单 —— 解析模块与规则库之间的契约。

`expr` 规则用**字符串**字段名引用解析结果，而数据库不校验它是否存在。没有白名单时：

    把 `prepay_ratio` 拼成 `prepay_ratios` → 运行时只得到"字段为空"，
    而"字段为空"在本项目里意味着"未约定" → **一个拼写错误变成
    "合同缺少预付款约定"的错误结论**，且完全没有报错。

## 三组字段，**语义不同，不要混在一个桶里**

初版只有一个 `DIRECT_FIELDS`，结果两件不同的事被合并了：

| 组 | 回答的问题 | 谁消费 |
| --- | --- | --- |
| `BASIC_INFO_FIELDS` | **合同本身是什么**（标题、编号、双方、金额、日期） | 人看的审查意见、M8 展示 |
| `DIRECT_RULE_FIELDS` | **规则要比较的量**（付款天数、续约通知期…） | `expr` 规则的输入 |
| `DERIVED_RULE_FIELDS` | 由上面两组**算出**的量 | `expr` 规则的输入 |

混在一起的具体后果：验收要求"覆盖需求规定的 **8 项基本信息**"，
而混完之后的清单里**连 `contract_title` / `effective_date` 都没有** ——
也就是说，按当时那份白名单实现，"覆盖 8 项"这条验收**根本无法成立**，
而清单本身就是"覆盖"的定义。

> 与 `clauses.py` 的拆分理由同源：**"有多少项"不是问题，"是什么项"才是。**

## 两类字段对解析模块的要求不同

- **直接抽取**：可在正文中定位到原文片段，能给出证据与精度；
- **派生计算**：由其他字段算出（如预付款比例）。**它没有独立原文片段**，
  证据必须回指到参与计算的原始字段，否则审批人无法核验。
"""

from __future__ import annotations

from typing import Final

# ------------------------------------------------------------
# ① 合同基本信息（需求规定的 8 项）
# ------------------------------------------------------------
# ⚠️ 这 8 项**必须**在 `basic_info_json` 里出现 —— 验收标准里
# "覆盖需求规定的 8 项基本信息"指的就是这一组。
#
# 注意 `effective_date`（生效时间）与"签订日期"**不是同一个业务概念**：
# 签了不一定立刻生效（常见"自双方签字盖章之日起生效"或约定未来某日生效）。
# 用签订日期顶替生效时间，会让"生效时间"这一栏永远显示一个语义不同的值。
BASIC_INFO_FIELDS: Final[dict[str, str]] = {
    "contract_title": "合同标题",
    "contract_number": "合同编号",
    "party_a": "甲方名称",
    "party_b": "乙方名称",
    "amount": "合同总金额",
    "currency": "结算币种",
    "effective_date": "生效时间",
    "expiry_date": "到期时间",
}

# ------------------------------------------------------------
# ② 规则直接输入（可在正文中定位）
# ------------------------------------------------------------
DIRECT_RULE_FIELDS: Final[dict[str, str]] = {
    "credit_code": "统一社会信用代码",
    "pay_days": "付款周期（天）",
    "renew_term_months": "单次自动续约期（月）",
    "renew_notice_days": "续约异议的提前通知期（天）",
    "confidentiality_years": "保密期限（年）",
    "acceptance_days": "验收期限（天）",
}

#: 所有可直接抽取的字段（不论它属于基本信息还是规则输入）。
DIRECT_FIELDS: Final[dict[str, str]] = {**BASIC_INFO_FIELDS, **DIRECT_RULE_FIELDS}

# ------------------------------------------------------------
# ③ 派生字段：由其他字段计算，无独立原文片段
# ------------------------------------------------------------
DERIVED_RULE_FIELDS: Final[dict[str, str]] = {
    "prepay_ratio": "预付款比例（0–1）＝ 预付款金额 / 合同金额",
    "liability_party_a_ratio": "甲方违约金比例（0–1）＝ 甲方违约金 / 合同金额",
    "liability_party_b_ratio": "乙方违约金比例（0–1）＝ 乙方违约金 / 合同金额",
}

#: 兼容旧名（M3 起就在用）。语义等同 `DERIVED_RULE_FIELDS`。
DERIVED_FIELDS: Final[dict[str, str]] = DERIVED_RULE_FIELDS

#: **带币种**的字段 —— 对它们做数值比较必须先校验币种。
#:
#: ⚠️ 不校验的后果不是"算错一点点"，而是**把一个不可比的大小关系当成结论**：
#: `USD 200,000 > 1,000,000`（阈值显然按人民币写的）会得到 False，
#: 规则报"未命中" —— 而它其实**根本不可比**。
#: M4 §4.8 把金额拆成"十进制数值 + 独立币种字段"，为的就是让这件事**可判断**。
MONEY_FIELDS: Final[frozenset[str]] = frozenset({"amount"})

# expr 规则可引用的全部字段 —— 三组的并集。
# 基本信息也在内：`amount` / `party_a` 这类本身就是规则的输入
# （"金额缺失"规则要读 `amount`）。
ALL_EXPR_FIELDS: Final[frozenset[str]] = (
    frozenset(BASIC_INFO_FIELDS) | frozenset(DIRECT_RULE_FIELDS) | frozenset(DERIVED_RULE_FIELDS)
)


# ------------------------------------------------------------
# ④ 运行期事实生产者（谁真的能产出这个字段）
# ------------------------------------------------------------
#: 字段码 → **生产它的模块**。
#:
#: ⚠️ 白名单能拦住**拼错**（`prepay_ratios`），拦不住"**字段合法却没人生产**" ——
#: 而后者更隐蔽：字段取不到值 → `is_null` 类规则报"未约定"、阈值类规则报"判不了"，
#: 报告上**完全看不出这是实现缺口**。
#:
#: 这不是假想：`prepay_ratio` / `liability_party_a_ratio` / `liability_party_b_ratio`
#: 曾在白名单里待了很久而**没有任何生产者** —— 6 条 expr 规则在运行期永远拿不到输入，
#: 而白名单、`check_rules.py`、全部测试**都一声不吭**。补生产者的是 T3a
#: （`app/rules/fact_resolver.py`）。
FIELD_PRODUCERS: Final[dict[str, str]] = {
    **{name: "field_extractor" for name in DIRECT_FIELDS},
    "prepay_ratio": "fact_resolver",
    "liability_party_a_ratio": "fact_resolver",
    "liability_party_b_ratio": "fact_resolver",
}


def producer_of(field_name: str) -> str | None:
    """谁生产这个字段。`None` 表示**没有任何人** —— 引用它的规则必然拿不到输入。"""
    return FIELD_PRODUCERS.get(field_name)


# 白名单里的每个字段都必须有生产者 —— 在**导入时**校验，而不是靠测试守着。
#
# ⚠️ 写在模块级而不是测试里，是因为"手工维护的集合会漏"这件事本仓库已经发生过两次
# （M4 的 `M4_ERROR_CODES` 漏登过错误码；本表的三个派生字段漏登过生产者）。
# 测试会被人跳过、被标记 xfail、在改代码时被顺手改宽 —— 而导入时的断言不会。
# 新增白名单条目却忘了写生产者时，这里**立刻**失败，且报错直指该做什么。
_missing_producers = sorted(ALL_EXPR_FIELDS - set(FIELD_PRODUCERS))
if _missing_producers:  # pragma: no cover - 只有改白名单忘写生产者时才会走到
    raise RuntimeError(
        f"字段 {_missing_producers} 在白名单里但没有运行期生产者。"
        "请在 FIELD_PRODUCERS 里登记生产它的模块 —— "
        "否则引用它的 expr 规则在运行期永远拿不到输入，而报告上看不出这是实现缺口。"
    )


def describe(field_name: str) -> str:
    """返回字段的中文说明，用于规则校验报错信息与界面展示。"""
    return (
        BASIC_INFO_FIELDS.get(field_name)
        or DIRECT_RULE_FIELDS.get(field_name)
        or DERIVED_RULE_FIELDS.get(field_name)
        or "未知字段"
    )


def is_known(field_name: str) -> bool:
    return field_name in ALL_EXPR_FIELDS


def is_derived(field_name: str) -> bool:
    """派生字段的证据必须回指原始字段，不能只给计算值。"""
    return field_name in DERIVED_RULE_FIELDS


def is_basic_info(field_name: str) -> bool:
    """是否属于"需求规定的 8 项基本信息"。"""
    return field_name in BASIC_INFO_FIELDS
