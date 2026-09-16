"""文本归一化的**唯一入口**（叶子模块，不依赖项目内任何模块）。

## 为什么需要它

"哪些差异算排版、哪些算内容"是**一个**决定，却在两处要用：

- M4 的字段解析（金额阈值 `800，000．0０` → `800,000.00`）；
- M5 LLM 规则的**证据反向核验**（模型引用的原文是否真的出自正文）。

各自实现一次的话，两份**只在同时被改对时才一致** —— 而它们分处两个模块、
由不同时间的人维护。M4 已经因为"同一个判断各写一遍"栽过一次
（`resolve_span` 之前的 `locate` 与字段提取器），这里不再重来。

⚠️ 也不能让 `app/rules/` 直接 import `app/services/field_extractor`：
那是 `rules → services` 的反向依赖，而 `field_extractor` 已经 import 了 `app.rules`
—— 会形成**循环**。
"""

from __future__ import annotations

import unicodedata


def fold_numeric(raw: str) -> str:
    """NFKC 折叠：全角数字与标点 → ASCII。

    ⚠️ **只能作用在捕获到的短片段上，不能作用在整篇文本上**：
    NFKC 会**改变字符串长度**（`㍿` 展开成 4 个字符），施加到全文会让所有
    `char_start/char_end` 与 `bbox` 一起错位。捕获值的偏移不参与坐标计算，
    因此在那一层折叠是安全的。
    """
    return unicodedata.normalize("NFKC", raw)


def squash_whitespace(value: str) -> str:
    """去掉**全部**空白（含全角空格）。

    用途是"比较两段文字是否为同一段"，不是"展示"或"定位"：
    模型复述原文时常把换行与空格吞掉，而 `char_start/char_end` 依赖原始空白 ❌
    因此**绝不能**用它改写要落库的 `value_text`。
    """
    return "".join(value.split())


#: 中文数字字符与单位。范围**零~千**（没有 `万`）。
_CN_DIGITS: dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_CN_UNITS: dict[str, int] = {"十": 10, "百": 100, "千": 1000}


def chinese_number(text: str) -> int | None:
    """中文数字 → 整数，范围零~千。**解析不了就返回 `None`（不猜）**。

    覆盖合同里的常见写法：`十` / `十五` / `三十` / `四十五` / `九十` / `一百二十`
    / `三百零五` / `一千零二十` —— 它们都能正确还原。

    超出范围（`一万`）或含无法识别的字（`若干`）返回 `None`，调用方据此判
    `uncertain` 而**不猜**：猜错的表现是"条款里的 300 天变成了 3 天"，
    而那个数字会被拿去和阈值比较。

    ## 为什么放在这里（而不是各模块各写一份）

    它同时被两处需要：M4 的字段提取（`付款期限九十日`）与 M5 的事实解析
    （`百分之六十`）。各写一份的话，两份**只在同时被改对时才一致** ——
    而它们分处两个模块、由不同时间的人维护。

    ⚠️ 也不能让 `app/rules/` 直接 import `app/services/field_extractor`：
    那是 `rules → services` 的反向依赖，而 `field_extractor` 已经 import 了
    `app.rules` —— 会形成**循环**。本模块是叶子模块，谁都可以依赖它。

    > 写这段时我一度以为 `三百零五` 超出了实现能力，测了一下发现能正确解析为 305 ——
    > **不要把"看起来复杂"当成"不支持"**，那会平白丢掉一个能用的能力。
    """
    if not text:
        return None
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            total += (current or 1) * _CN_UNITS[char]
            current = 0
        else:
            return None
    return total + current
