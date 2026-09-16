"""规则相关能力。

本阶段实现的是**规则配置的解析与校验**：
    schemas.py         受控的 JSON 配置 DTO
    rules/fields.py    expr 规则的字段白名单
    rules/applicability.py  适用性判断

M5 才实现 `evaluator.py`（四状态判定）与 `aggregator.py`（风险聚合）。
"""
