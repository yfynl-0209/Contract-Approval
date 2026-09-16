"""响应体的**形状**集中定义（M7 / Task 3）。

## 为什么要有这个模块

M7 新增的查询接口里，同一种东西会在**两个地方**出现：

| 对象 | 一处 | 另一处 |
| --- | --- | --- |
| 规则评价 | `GET /api/runs/{id}` 的 `evaluations[]` | `GET /api/evaluations` 的 `items[]` |
| 分页信封 | 任务列表 | 结果列表 |

两处各写一遍时的分叉方式是"某天给其中一处加了一个字段"，
而使用者看到的是**同一条评价在两张页面上字段不一样** ——
没有任何一处会报错。所以形状只有一份实现，两处都从它取。

⚠️ 本模块只做**形状**，不做业务判断：字段名与嵌套结构在这里，
"该不该返回"的决定在服务层。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def iso(value: datetime | None) -> str | None:
    """时间字段的对外形态。`None` 原样返回 —— 不要用空串顶替。"""
    return None if value is None else value.isoformat()


def json_or_none(raw: str | None) -> Any:
    """库里的 JSON 文本 → 对象。解析不了返回 `None` **而不是抛错**。

    一条字段 JSON 损坏的记录，其状态与错误码仍然是有用的信息 ——
    让整个接口 500 会把"这条记录坏了"变成"这个接口坏了"。
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def page_json(page: Any, serialize: Any) -> dict[str, Any]:
    """分页信封。

    ⚠️ `total` / `page_count` / `has_next` **都由后端算**。
    只给 `items` 时，前端要么把"这一页的长度"当总数（永远只有一页），
    要么自己再发一次计数请求（而那次请求与这次不是同一个快照）。
    """
    return {
        "items": [serialize(item) for item in page.items],
        "total": page.total,
        "page": page.page,
        "page_size": page.page_size,
        "page_count": page.page_count,
        "has_next": page.has_next,
    }


def evaluation_json(
    *,
    rule_code: str,
    evaluation_status: str,
    risk_level: str,
    reason_code: str | None,
    reason_text: str | None,
    evidence_json: str | None,
    hit_detail: Any,
    rule_name: str | None = None,
) -> dict[str, Any]:
    """一条规则评价的对外形状（§4.6 的 `evaluations[]`）。

    ⚠️ **四态都返回**，包括 `not_hit` 与 `not_applicable`：
    少了它们就答不出"这条规则为什么没报警"，而那正是这些记录存在的理由。

    参数刻意**逐个列出**而不是收一个 ORM 行或领域对象：两种调用方
    （读 `rule_hits` 行 / 读 `Evaluation` 领域对象）的字段来源不同，
    收一个具体类型会迫使其中一方先构造出另一方 —— 而那种"适配"
    正是下一次字段改动时忘记同步的地方。

    `rule_name`：规则的中文名（`ReviewRule.rule_name`）。调用方负责查表 ——
    评价行上只有 `rule_code`，而界面要给人看的是名字；
    查不到（规则被删）时为 `null`，前端回退显示编码。
    """
    return {
        "rule_code": rule_code,
        "rule_name": rule_name,
        "evaluation_status": evaluation_status,
        "risk_level": risk_level,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "evidence": json_or_none(evidence_json) or [],
        "hit_detail": hit_detail,
    }
