-- ============================================================
-- 合同审批审查系统 —— 规则种子数据
-- 版本：v2（数据模型与规则语义修订，2026-09-12）
-- 覆盖需求文档 2.4.6 要求的 11 类风险，共 40 条规则。
--
-- 相对 v1 的三处结构性变化：
--   1. 每条规则显式声明适用条件 applies_when_json（NULL = 全局适用），
--      解决"把不适用当成条款缺失"的误报问题；
--   2. 方向敏感规则按**业务角色 / 合同标签**拆成独立 rule_code，
--      而不是在一条规则里动态翻转结论；
--   3. llm 规则补充 fallback_match_json（显式降级条件），
--      无模型时执行确定性匹配，而不是静默跳过。
--
-- ---------- match_text 与 fallback 的结构（按 match_mode 解释）----------
--   keyword : {"keywords": ["..."], "absent": false}
--             absent=false → 出现任一关键词即命中
--             absent=true  → 全文均未出现才命中（"条款缺失"类规则）
--   regex   : {"pattern": "..."}
--   llm     : {"instruction": "..."}
--   expr    : {"field": "prepay_ratio", "op": "gt", "value": 0.3}
--             支持 op：gt / gte / lt / lte / eq / contains / is_null / not_null
--
-- ---------- applies_when_json 的受控结构（未知键将导致加载失败）----------
--   { "contract_types":      ["procurement", ...],
--     "our_contract_labels": ["party_a", ...],
--     "our_business_roles":  ["buyer", ...],
--     "requires_any_keyword": ["软件", "成果"] }
--
-- ⚠️ 两个轴必须分清：
--   our_contract_labels  —— 回答"这条规则说的是我吗"（合同正文里的甲方/乙方）
--   our_business_roles   —— 回答"这对我是好是坏"（付钱还是收钱）
--   销售合同的甲方通常是卖方，因此绝不能把"甲方"默认等同于"采购方"。
--
-- ---------- exclude_text ----------
--   仅对 keyword 且 absent=false 生效，逗号分隔的否定词；
--   命中处邻近窗口内出现否定词时不判为风险（防"不承担保密义务"误判）。
--
-- ---------- priority ----------
--   数值越小越先执行。
-- ============================================================

DELETE FROM review_rules;

INSERT INTO review_rules
    (rule_code, rule_name, rule_category, risk_level, rule_status, priority,
     rule_version, match_mode, applies_when_json, match_text, fallback_match_json,
     exclude_text, suggestion_text)
VALUES

-- ============================================================
-- 1. 预付款比例（方向敏感：按我方业务角色拆分）
-- ============================================================
('PAY_PREPAY_RATIO_HIGH_FOR_BUYER', '预付款比例过高（我方为付款方）', '预付款比例', 'high', 'active', 10, 1, 'expr',
 '{"our_business_roles": ["buyer", "customer", "licensee"]}',
 '{"field": "prepay_ratio", "op": "gt", "value": 0.3}',
 NULL, NULL,
 '预付款比例超过 30%，建议下调比例或要求对方提供对等履约担保'),

('PAY_PREPAY_RATIO_EXTREME_FOR_BUYER', '预付款比例严重偏高（我方为付款方）', '预付款比例', 'high', 'active', 11, 1, 'expr',
 '{"our_business_roles": ["buyer", "customer", "licensee"]}',
 '{"field": "prepay_ratio", "op": "gte", "value": 0.5}',
 NULL, NULL,
 '预付款比例达到 50% 及以上，存在重大资金风险，建议改为分期付款'),

-- 同一个"预付款比例"字段，在我方为收款方时方向相反：
-- 比例过低才是风险（收不到钱），因此必须是另一条 rule_code
('PAY_PREPAY_RATIO_LOW_FOR_SELLER', '预付款比例过低（我方为收款方）', '预付款比例', 'medium', 'active', 12, 1, 'expr',
 '{"our_business_roles": ["seller", "service_provider", "licensor"]}',
 '{"field": "prepay_ratio", "op": "lt", "value": 0.1}',
 NULL, NULL,
 '预付款比例不足 10%，我方垫资风险较高，建议提高预付款比例或要求预付材料款'),

-- ⚠️ **必须同时限定立场**：我方是**收款方**时，"没有预付款约定"才是风险（垫资）。
-- 我方是采购方时，全额验收后付款恰恰是**有利**的 —— 只按合同类型限定会让
-- `HT-2026-0001`（procurement + buyer、无预付款）命中 medium，
-- 把"低风险基线"推到 medium，而验收 19 断言的是 low。
-- 与上面 `PAY_PREPAY_RATIO_LOW_FOR_SELLER` 的立场集合保持一致。
('PAY_PREPAY_MISSING', '未约定预付款比例', '预付款比例', 'medium', 'active', 13, 1, 'expr',
 '{"contract_types": ["procurement", "sales", "software_service", "development", "outsourcing", "lease"], "our_business_roles": ["seller", "service_provider", "licensor"]}',
 '{"field": "prepay_ratio", "op": "is_null"}',
 NULL, NULL,
 '合同未明确预付款比例，建议补充约定以避免履约争议'),

-- ============================================================
-- 2. 付款周期（方向敏感）
-- ============================================================
('PAY_CYCLE_LONG_FOR_SELLER', '付款周期过长（我方为收款方）', '付款周期', 'high', 'active', 20, 1, 'expr',
 '{"our_business_roles": ["seller", "service_provider", "licensor"]}',
 '{"field": "pay_days", "op": "gt", "value": 60}',
 NULL, NULL,
 '付款周期超过 60 天，我方回款周期过长，建议缩短账期或补充逾期利息条款'),

('PAY_CYCLE_SHORT_FOR_BUYER', '付款周期过短（我方为付款方）', '付款周期', 'medium', 'active', 21, 1, 'expr',
 '{"our_business_roles": ["buyer", "customer", "licensee"]}',
 '{"field": "pay_days", "op": "lt", "value": 15}',
 NULL, NULL,
 '付款周期不足 15 天，付款早于验收将失去质量制衡，建议改为验收合格后再付款'),

('PAY_CYCLE_MISSING', '未约定付款周期', '付款周期', 'medium', 'active', 22, 1, 'expr',
 '{"contract_types": ["procurement", "sales", "software_service", "development", "outsourcing", "lease"]}',
 '{"field": "pay_days", "op": "is_null"}',
 NULL, NULL,
 '合同未约定付款周期，建议明确付款起算时点与到账期限'),

-- ============================================================
-- 3. 自动续约（方向敏感：锁定我方 vs 锁定对方）
-- ============================================================
('RENEW_AUTO_LOCKING_US', '自动续约锁定我方', '自动续约', 'high', 'active', 30, 1, 'keyword',
 '{"contract_types": ["lease", "software_service", "outsourcing"],
   "our_business_roles": ["buyer", "customer", "licensee"]}',
 '{"keywords": ["自动续约", "自动顺延", "自动延长", "期满自动"], "absent": false}',
 NULL,
 '不自动,不得自动,不再自动',
 '存在自动续约条款且我方为被锁定一方，建议改为到期另行签订或明确续约前的书面确认流程'),

('RENEW_AUTO_LOCKING_COUNTERPARTY', '自动续约锁定对方', '自动续约', 'low', 'active', 31, 1, 'keyword',
 '{"contract_types": ["lease", "software_service", "outsourcing"],
   "our_business_roles": ["seller", "service_provider", "licensor"]}',
 '{"keywords": ["自动续约", "自动顺延", "自动延长", "期满自动"], "absent": false}',
 NULL,
 '不自动,不得自动,不再自动',
 '自动续约条款对我方相对有利，但建议确认续约期的价格调整机制，避免长期锁定低价'),

('RENEW_AUTO_TERM_LONG', '自动续约期过长', '自动续约', 'medium', 'active', 32, 1, 'expr',
 '{"contract_types": ["lease", "software_service", "outsourcing"]}',
 '{"field": "renew_term_months", "op": "gte", "value": 12}',
 NULL, NULL,
 '单次自动续约期达到 12 个月及以上，建议缩短续约期以便定期复核'),

('RENEW_NOTICE_SHORT', '续约异议期过短', '自动续约', 'medium', 'active', 33, 1, 'expr',
 '{"contract_types": ["lease", "software_service", "outsourcing"]}',
 '{"field": "renew_notice_days", "op": "lt", "value": 30}',
 NULL, NULL,
 '提出不续约的提前通知期不足 30 天，建议延长至 30 天以上'),

-- ============================================================
-- 4. 违约责任（方向敏感：按我方合同标签拆分）
-- ============================================================
('LIAB_UNEQUAL_AGAINST_PARTY_A', '违约责任不对等（我方为甲方）', '违约责任', 'high', 'active', 40, 1, 'llm',
 '{"our_contract_labels": ["party_a"]}',
 '{"instruction": "判断违约责任条款是否主要由甲方承担（例如甲方承担违约金而乙方免责、或甲方赔偿责任明显重于乙方）。请引用原文片段说明理由。"}',
 '{"keywords": ["甲方承担全部违约责任", "甲方承担违约金", "由甲方承担赔偿责任", "乙方不承担"], "absent": false}',
 NULL,
 '违约责任存在不利于我方的对等性缺陷，建议调整为双方对等承担'),

('LIAB_UNEQUAL_AGAINST_PARTY_B', '违约责任不对等（我方为乙方）', '违约责任', 'high', 'active', 41, 1, 'llm',
 '{"our_contract_labels": ["party_b"]}',
 '{"instruction": "判断违约责任条款是否主要由乙方承担（例如乙方承担违约金而甲方免责、或乙方赔偿责任明显重于甲方）。请引用原文片段说明理由。"}',
 '{"keywords": ["乙方承担全部违约责任", "乙方承担违约金", "由乙方承担赔偿责任", "甲方不承担"], "absent": false}',
 NULL,
 '违约责任存在不利于我方的对等性缺陷，建议调整为双方对等承担'),

('LIAB_MISSING', '缺失违约责任条款', '违约责任', 'medium', 'active', 42, 1, 'keyword',
 NULL,
 '{"keywords": ["违约责任", "违约金", "赔偿责任"], "absent": true}',
 NULL, NULL,
 '合同缺少违约责任条款，建议补充违约情形、违约金计算方式与损失赔偿范围'),

('LIAB_RATIO_HIGH_FOR_PARTY_A', '违约金比例过高（我方为甲方）', '违约责任', 'high', 'active', 43, 1, 'expr',
 '{"our_contract_labels": ["party_a"]}',
 '{"field": "liability_party_a_ratio", "op": "gt", "value": 0.3}',
 NULL, NULL,
 '我方承担的违约金比例超过合同金额的 30%，可能被认定过高，建议下调至合理区间'),

('LIAB_RATIO_HIGH_FOR_PARTY_B', '违约金比例过高（我方为乙方）', '违约责任', 'high', 'active', 44, 1, 'expr',
 '{"our_contract_labels": ["party_b"]}',
 '{"field": "liability_party_b_ratio", "op": "gt", "value": 0.3}',
 NULL, NULL,
 '我方承担的违约金比例超过合同金额的 30%，可能被认定过高，建议下调至合理区间'),

-- ============================================================
-- 5. 管辖地（方向敏感：按我方合同标签拆分）
-- ============================================================
('JURIS_UNFAVOR_FOR_PARTY_A', '管辖约定不利于我方（我方为甲方）', '管辖地', 'high', 'active', 50, 1, 'llm',
 '{"our_contract_labels": ["party_a"]}',
 '{"instruction": "判断争议解决条款是否约定由乙方所在地法院或仲裁机构管辖，或约定明显不利于甲方的境外机构。请说明理由。"}',
 '{"keywords": ["乙方所在地人民法院", "由乙方所在地法院管辖", "乙方所在地仲裁"], "absent": false}',
 NULL,
 '管辖地约定不利于我方，建议改为我方所在地法院管辖或双方认可的仲裁机构'),

('JURIS_UNFAVOR_FOR_PARTY_B', '管辖约定不利于我方（我方为乙方）', '管辖地', 'high', 'active', 51, 1, 'llm',
 '{"our_contract_labels": ["party_b"]}',
 '{"instruction": "判断争议解决条款是否约定由甲方所在地法院或仲裁机构管辖，或约定明显不利于乙方的境外机构。请说明理由。"}',
 '{"keywords": ["甲方所在地人民法院", "由甲方所在地法院管辖", "甲方所在地仲裁"], "absent": false}',
 NULL,
 '管辖地约定不利于我方，建议改为我方所在地法院管辖或双方认可的仲裁机构'),

('JURIS_MISSING', '缺失争议解决条款', '管辖地', 'medium', 'active', 52, 1, 'keyword',
 NULL,
 '{"keywords": ["争议解决", "管辖", "仲裁"], "absent": true}',
 NULL, NULL,
 '合同缺少争议解决条款，建议明确管辖法院或仲裁机构'),

-- 与立场无关：境外仲裁对任何一方都意味着高成本与长周期
('JURIS_OVERSEAS_ARBITRATION', '约定境外仲裁', '管辖地', 'high', 'active', 53, 1, 'keyword',
 NULL,
 '{"keywords": ["境外仲裁", "国外仲裁", "ICC", "新加坡国际仲裁中心", "香港国际仲裁中心"], "absent": false}',
 NULL, NULL,
 '约定境外仲裁，执行成本高且周期长，建议改为境内仲裁或诉讼'),

-- ============================================================
-- 6. 主体信息缺失（与立场无关：任一方主体缺失都是缺陷）
-- ============================================================
('SUBJ_PARTY_A_MISSING', '缺失甲方主体信息', '主体信息缺失', 'high', 'active', 60, 1, 'expr',
 NULL,
 '{"field": "party_a", "op": "is_null"}',
 NULL, NULL,
 '合同未明确甲方名称，建议补充完整签约主体信息'),

('SUBJ_PARTY_B_MISSING', '缺失乙方主体信息', '主体信息缺失', 'high', 'active', 61, 1, 'expr',
 NULL,
 '{"field": "party_b", "op": "is_null"}',
 NULL, NULL,
 '合同未明确乙方名称，建议补充完整签约主体信息'),

('SUBJ_CREDIT_CODE_MISSING', '缺失统一社会信用代码', '主体信息缺失', 'low', 'active', 62, 1, 'expr',
 NULL,
 '{"field": "credit_code", "op": "is_null"}',
 NULL, NULL,
 '未载明统一社会信用代码，建议补充以核实主体资格'),

-- ============================================================
-- 7. 金额缺失（仅适用于有明确价款义务的合同）
-- ============================================================
-- 注意：本类规则必须结合字段抽取状态判断——
--   not_found（确实没写）→ hit；failed / uncertain（没解析出来）→ needs_review
-- 这个区分由规则评价引擎处理（M5），规则本身只表达"值为空"。
('AMOUNT_MISSING', '缺失合同金额', '金额缺失', 'high', 'active', 70, 1, 'expr',
 '{"contract_types": ["procurement", "sales", "software_service", "development", "outsourcing", "lease"]}',
 '{"field": "amount", "op": "is_null"}',
 NULL, NULL,
 '合同未明确金额，建议补充合同总金额及计价方式'),

('AMOUNT_CURRENCY_MISSING', '缺失结算币种', '金额缺失', 'low', 'active', 71, 1, 'expr',
 '{"contract_types": ["procurement", "sales", "software_service", "development", "outsourcing", "lease"]}',
 '{"field": "currency", "op": "is_null"}',
 NULL, NULL,
 '未明确结算币种，建议补充币种及汇率约定'),

('AMOUNT_UNDETERMINED', '金额表述不确定', '金额缺失', 'medium', 'active', 72, 1, 'keyword',
 '{"contract_types": ["procurement", "sales", "software_service", "development", "outsourcing", "lease"]}',
 '{"keywords": ["暂定金额", "以实际结算为准", "据实结算", "另行确定"], "absent": false}',
 NULL, NULL,
 '金额表述为暂定或据实结算，建议设定金额上限与调价机制'),

-- ============================================================
-- 8. 保密缺失（仅适用于会接触保密信息的合同类型）
-- ============================================================
('CONF_MISSING', '缺失保密条款', '保密缺失', 'high', 'active', 80, 1, 'keyword',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"keywords": ["保密", "保密义务", "商业秘密"], "absent": true}',
 NULL, NULL,
 '合同缺少保密条款，建议补充保密范围、保密期限与违约责任'),

('CONF_TERM_SHORT', '保密期限过短', '保密缺失', 'medium', 'active', 81, 1, 'expr',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"field": "confidentiality_years", "op": "lt", "value": 2}',
 NULL, NULL,
 '保密期限不足 2 年，建议延长至 2 年以上或约定长期有效'),

('CONF_UNILATERAL_AGAINST_PARTY_A', '保密义务单方承担（我方为甲方）', '保密缺失', 'medium', 'active', 82, 1, 'llm',
 '{"contract_types": ["software_service", "development", "outsourcing"],
   "our_contract_labels": ["party_a"]}',
 '{"instruction": "判断保密义务是否仅由甲方承担而乙方免责。请说明理由。"}',
 '{"keywords": ["甲方承担保密义务", "乙方无需承担保密", "乙方不承担保密"], "absent": false}',
 NULL,
 '保密义务为单方承担，建议改为双方互负保密义务'),

('CONF_UNILATERAL_AGAINST_PARTY_B', '保密义务单方承担（我方为乙方）', '保密缺失', 'medium', 'active', 83, 1, 'llm',
 '{"contract_types": ["software_service", "development", "outsourcing"],
   "our_contract_labels": ["party_b"]}',
 '{"instruction": "判断保密义务是否仅由乙方承担而甲方免责。请说明理由。"}',
 '{"keywords": ["乙方承担保密义务", "甲方无需承担保密", "甲方不承担保密"], "absent": false}',
 NULL,
 '保密义务为单方承担，建议改为双方互负保密义务'),

-- ============================================================
-- 9. 数据处理（仅适用于涉及数据处理的合同）
-- ============================================================
('DATA_MISSING', '缺失数据处理条款', '数据处理', 'high', 'active', 90, 1, 'keyword',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"keywords": ["数据", "个人信息", "数据处理"], "absent": true}',
 NULL, NULL,
 '合同缺少数据处理条款，建议补充数据范围、处理目的与安全义务'),

('DATA_CROSSBORDER', '涉及数据跨境传输', '数据处理', 'high', 'active', 91, 1, 'keyword',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"keywords": ["跨境传输", "数据出境", "境外服务器", "传输至境外"], "absent": false}',
 NULL, NULL,
 '涉及数据跨境传输，建议评估合规要求并补充安全评估与告知同意机制'),

('DATA_SECURITY_MISSING', '缺失数据安全义务', '数据处理', 'medium', 'active', 92, 1, 'keyword',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"keywords": ["数据安全", "等保", "等级保护", "加密存储"], "absent": true}',
 NULL, NULL,
 '未约定数据安全保护义务，建议补充加密、访问控制与安全事件通知条款'),

-- ============================================================
-- 10. 知识产权（方向敏感）
-- ============================================================
-- 关键示例：本规则只对软件/研发/外包类合同适用。
-- 标准商品采购合同没有知识产权条款时，应为 not_applicable，而不是报高风险。
('IP_MISSING', '缺失知识产权条款', '知识产权', 'medium', 'active', 100, 1, 'keyword',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"keywords": ["知识产权", "著作权", "专利", "商标"], "absent": true}',
 NULL, NULL,
 '合同缺少知识产权条款，建议明确成果归属与授权范围'),

('IP_TRANSFER_AWAY_FROM_PARTY_A', '知识产权无偿归对方（我方为甲方）', '知识产权', 'high', 'active', 101, 1, 'llm',
 '{"contract_types": ["software_service", "development", "outsourcing"],
   "our_contract_labels": ["party_a"]}',
 '{"instruction": "判断是否约定甲方创作成果的知识产权无偿或全部归乙方所有。请说明理由。"}',
 '{"keywords": ["知识产权归乙方", "无偿转让给乙方", "著作权归乙方"], "absent": false}',
 NULL,
 '知识产权归属约定不利于我方，建议改为双方共有或保留我方权利'),

('IP_TRANSFER_AWAY_FROM_PARTY_B', '知识产权无偿归对方（我方为乙方）', '知识产权', 'high', 'active', 102, 1, 'llm',
 '{"contract_types": ["software_service", "development", "outsourcing"],
   "our_contract_labels": ["party_b"]}',
 '{"instruction": "判断是否约定乙方创作成果的知识产权无偿或全部归甲方所有。请说明理由。"}',
 '{"keywords": ["知识产权归甲方", "无偿转让给甲方", "著作权归甲方"], "absent": false}',
 NULL,
 '知识产权归属约定不利于我方，建议改为双方共有或保留我方权利'),

('IP_EXCLUSIVE', '存在独占或排他性约定', '知识产权', 'medium', 'active', 103, 1, 'keyword',
 '{"contract_types": ["software_service", "development", "outsourcing"]}',
 '{"keywords": ["独占", "排他", "独家授权"], "absent": false}',
 NULL, NULL,
 '存在独占或排他性约定，建议明确适用范围、期限与地域限制'),

-- ============================================================
-- 11. 验收标准缺失（仅适用于存在交付物或服务成果的合同）
-- ============================================================
('ACC_MISSING', '缺失验收标准', '验收标准缺失', 'medium', 'active', 110, 1, 'keyword',
 '{"contract_types": ["procurement", "software_service", "development", "outsourcing"]}',
 '{"keywords": ["验收标准", "验收条款", "验收合格"], "absent": true}',
 NULL, NULL,
 '合同缺少验收标准，建议明确验收指标、验收方式与合格判据'),

('ACC_DEADLINE_MISSING', '未约定验收期限', '验收标准缺失', 'medium', 'active', 111, 1, 'expr',
 '{"contract_types": ["procurement", "software_service", "development", "outsourcing"]}',
 '{"field": "acceptance_days", "op": "is_null"}',
 NULL, NULL,
 '未约定验收期限，建议补充验收启动时点与逾期视为验收合格的规则'),

-- llm 规则的显式降级示例：无模型时用确定性关键词匹配，
-- 而不是静默跳过（那会变成"没报风险"的假象）
('ACC_VAGUE', '验收标准表述模糊', '验收标准缺失', 'low', 'active', 112, 1, 'llm',
 '{"contract_types": ["procurement", "software_service", "development", "outsourcing"]}',
 '{"instruction": "判断验收标准是否存在表述模糊、无明确判据或无限期拖延验收的情形（例如以甲方主观满意为准）。请说明理由。"}',
 '{"keywords": ["验收标准以甲方要求为准", "以甲方满意为准", "甲方满意后验收", "达到甲方要求"], "absent": false}',
 NULL,
 '验收标准表述模糊，建议量化为可核验的指标');
