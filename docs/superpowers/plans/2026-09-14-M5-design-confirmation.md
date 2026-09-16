# M5 设计确认与实施计划：四状态规则评价引擎与风险聚合

> **编写时间**：2026-09-14（M4 已完成，`verify_m4` **56/56**，exit=0）
> **状态**：**第 5 轮外部评审已处置（§10–§12），T1–T11 全部完成，M5 关闭** ——
> 验收 **31 条**（24–28 为第 3 轮补入的事实解析项，29–31 为第 4 轮补入的链路级回归），
> `scripts/verify_m5.py` **31/31 通过、exit=0**（验收器已按第 5 轮评审加固：
> pytest 退出码入判、节点解析容忍参数化空格、31 条引用全部精确到用例）；
> 工时 7.75 → **8.75 天**。
> ⚠️ 已知缺口（不在 M5 范围）：`JobType.PARSE` 的处理器尚未接线（M4 遗留），
> `scripts/run_worker.py` 默认只领 `rule`，领了 `parse` 会如实报"未注册处理器"。
> **依据**：项目计划 §4（M5 行）、§5.1（`rule_hits` / `review_runs` / `review_results`）、§6.3（rule 模块）、
> §11（规则评价与 LLM 边界）、§14（测试策略）；`CONTEXT.md`（术语表）；M4 设计文档 §4.8（字段 JSON 契约）。
> **完成标志（计划 §4 原文）**：40 条规则在一个批次内**各产生且仅产生一条**评价；
> 高风险合同总风险 = 高；无模型时按 `fallback` 降级。

---

## 0. 待拍板决策（先确认再动手）

| # | 决策 | 选项 | **建议**与理由 |
| --- | --- | --- | --- |
| **①** | **`review_runs` 的版本绑定放哪** | A. 新增列 `model_version` / `prompt_version` / `config_version`<br>B. 塞进 `context_snapshot_json` | **A**。§11.1 要求批次绑定六项版本；其中 `parse_id` 已是解析版本，`ruleset_version` 已在表上。剩下的塞进 `context_snapshot_json` 会让一个字段承担**两种语义**（"当时的业务事实" vs "当时的模型/提示词版本"）—— 这正是 M4 反复修的一类问题。更实际的理由：**M11 的模型质量统计要按版本聚合**，塞进 JSON 后"哪些批次用了 v2 提示词"必须扫全表解 JSON，**信息在库里但用不了**（与计划 §12 里 `correlation_id` 那条论证同源） |
| **②** | **批次由谁触发** | A. 工具 5 `run_contract_rules(case_id)` 显式触发<br>B. 解析完成自动触发 | **A**（与需求里工具 5 是独立工具一致）。但**入口不做立场门禁**：`context_status != complete` 时**仍然运行**，由每条规则自行判 `needs_review(CONTEXT_MISSING/CONFLICT)`。理由：在批次入口拦截，会把"**某几条规则判不了**"放大成"**整份审查跑不了**"，而 §11.2 的适用性判断本就能精确表达这件事 |
| **③** | **LLM 适配器要几个** | A. 三个：`OpenAiCompatibleLlm` / `RuleFallbackLlm` / `MockLlm`（计划 §1.3 原文）<br>B. 两个：`OpenAiCompatibleLlm` / `MockLlm`，**降级放规则层** | **B**。`fallback_match_json` 是**规则的配置**（40 条里 9 条 llm 规则各自带一份），把它做成适配器等于让适配器去读规则表 —— 而适配器**不该知道规则的存在**。正确落点：evaluator 见 `gateway.available is False` 时执行该规则自己的 `fallback_match_json`。计划 §1.3 的 `RuleFallbackLlm` 建议**不实现**，并在文档里记明这是一处**有意的偏差** |
| **④** | **阈值类规则遇到 `not_found`** | A. `not_hit`（"条件不成立"）<br>B. `needs_review(EVIDENCE_UNCERTAIN)` | **B**。这是 M5 最容易错、且错了**不报错**的一处：`预付款比例 > 30%` 在**金额缺失**时，按 A 会判 `not_hit` —— 输出"这条规则没问题"，而事实是**根本没能判**。注意方向相反的一面：对**缺失类**规则（`金额缺失`），`not_found` 恰恰**就是 hit**。同一个字段状态在两类规则下含义相反，必须按规则语义分派 |
| **⑤** | **币种不可比** | A. 直接比较数值<br>B. `needs_review` + 新原因码 | **B**。`USD 200,000 > 30%` 与 `CNY 1,200,000 > 30%` 是**不可比**的。M4 §4.8 把金额拆成"十进制字符串 + 独立币种字段"的**理由之一**就是让这件事可判断；如果比较时把币种丢掉，那个字段就白拆了 |
| **⑥** | **摘要/关注点无模型时怎么办** | A. 留空<br>B. 确定性兜底文案 | **B**。§11.3 允许用 LLM"根据已保存的评价生成中文摘要"，但计划 §2.2 要求"**无 GPU 也能完成全部业务开发**"。留空会让无模型路径下 M5 无法验收（摘要最终落在 `review_results.summary_text`/`focus_points_json`，**由 M6 写入** —— 见决策 ⑦；它是 M8 模块 5 的展示内容） |

| **⑦** | **M5 是否写入 `review_results`** | A. M5 直接写（总风险 + 摘要 + 正文 + 内容摘要）<br>B. **M5 只写 `review_runs` + `rule_hits`；`review_results` 由 M6 写** | **B**（见下方说明）。A 会让 M5 提前实现一半 M6 —— 而**原始需求里"保存审查结果"是一个独立的工具 6**：<br>`save_review_result(case_id, overall_risk_level, summary_text, focus_points_json, comment_text)`。<br>边界一旦交叉，工具 6 就只剩"把同样的东西再写一遍"，且必然引出**重复保存、结果版本、人工编辑语义**三个说不清的问题 |

**⑦ 是结构性前置决策**：它决定 M5 的**交付边界**，其余六条都在它内部。
本稿初版在这一点上**自相矛盾** —— §2.2 写"结果保存与失效归 M6"，
§8 却写"`review_results` 在 M5 写入"。两处都言之成理，但只能留一处。
**已按 B 统一**（见 §2.2 / §4.4 / §5 / §6 / §8）。

**B 的完整分工**：

| 归属 | 写什么 | 为什么 |
| --- | --- | --- |
| **M5** | `review_runs` + 40 条 `rule_hits` | 机器结论，**唯一真相** |
| **M5 计算但\*不落库\*** | 总风险等级、四态计数、关注点候选 | 它是 `rule_hits` 的**纯函数**，随要随算。落库就是**第二份真相**（M4 §3.4「字段与工件不重复存储」是同一条纪律）；而"结果"要到**人工确认之后**才值得固化 |
| **M6** | `review_results` | 工具 6 接收**工具 5 返回的**结论与人工编辑内容，写入 `summary_text` / `focus_points_json` / `comment_text` / `content_digest`，并负责确认与失效 |

> **怎么防止这条边界被悄悄越过**：验收里加一条**可证伪**的断言 ——
> 跑完一次批次后 `review_results` 行数**必须仍为 0**（验收 22）。
> 只写"职责说明"而没有断言，边界会随着后续开发自然腐蚀 ——
> 这与 M4 里"断言必须能失败"是同一条教训。

---

## 1. 事实核对（动手前实测，不照抄文档）

### 1.1 已具备的资产（实测，不是文档声明）

| 项 | 实测结果 |
| --- | --- |
| `review_rules` | **40 条，全部 `active`**；`match_mode` = **`expr` 18 / `keyword` 13 / `llm` 9**（**没有 `regex`**）；`rule_category` **11 类**；`risk_level` = high 19 / medium 17 / low 4；`applies_when_json IS NULL` 6 条（全局适用）；**llm 规则缺 `fallback_match_json` 的有 0 条** |
| 表结构 | `review_runs`（`task_id`/`parse_id`/`version_no`/`context_snapshot_json`/`ruleset_version`/`run_status` + **`UNIQUE(task_id, version_no)`** + **复合外键 `(parse_id, task_id)`→`contract_parses`**）；`rule_hits`（**`UNIQUE(run_id, rule_id)`** + 复合外键 `(run_id, task_id)`）；`review_results`（`overall_risk_level`/`review_status`/三个计数/`content_digest`/`confirmed_digest`/`manual_confirmed`） |
| 枚举 | `EvaluationStatus`（四态）· `ReasonCode`（8 个）· `RiskLevel` · `MatchMode` · `RunStatus` · `ReviewStatus` · `ContextStatus` · `FieldStatus` —— **均已存在** |
| 聚合口径 | `RISK_CONTRIBUTING_STATUSES = {hit}`、`REQUIRES_HUMAN_STATUSES = {needs_review}` **已存在**（供引擎与结构校验共用） |
| `app/rules/applicability.py` | **已实现且是纯函数**：三种结果（`APPLICABLE` / `NOT_APPLICABLE` / `UNKNOWN`），且**只对规则真正声明了的维度做冲突检查** |
| 规则配置 DTO | `app/schemas.py`：`KeywordMatchConfig`（含 `absent`）/ `RegexMatchConfig`（**构造期编译**）/ `LlmMatchConfig` / `ExprMatchConfig`（`is_null`/`not_null` 不得带 `value`） |
| M4 的产出 | `contract_parses.basic_info_json` / `clause_info_json`（8 项 + 8 类，带四态与 `reason_code`）、质量指标、`_party_consistency` / `_contract_type_consistency` 两个保留键；标准文档与 `resolve_span()` **可供证据反向定位复用** |

### 1.2 四条决定 M5 形态的实测事实

**① 40 条规则里 `expr` 最多（18 条），而它最依赖字段四态。**
`expr` 比较的是**解析出来的字段值**，因此"字段没解析出来"必然出现在主路径上（不是边缘情况）。
`not_found` / `uncertain` / `failed` 三态在 expr 规则下**都必须先于比较被处理**（决策 ④）。

**② 9 条 llm 规则全部配了 `fallback_match_json`（实测 0 条例外）。**
这意味着**无模型时不会产生 `MODEL_UNAVAILABLE`** —— 降级路径是常态路径。
反过来：如果 evaluator 漏了降级，症状是"无 GPU 时 9 条规则全变 `needs_review`"，
而按计划 §2.2 那本该是"全部业务开发可完成"的场景。

**③ 数据库当前**业务数据量为 0**（`approval_tasks` / `contract_parses` / `review_runs` / `rule_hits` / `review_results` 全是 0）。**
M5 的验收**必须自己造数据**（拉取 → 解析 → 审查），不能依赖"库里已经有"。

**④ `regex` 模式在 40 条规则里一条都没用。**
但 `MatchMode.REGEX` 与 `RegexMatchConfig` 都存在。M5 **仍要实现它**（配置能力已承诺给用户，
留一个"配置能填、运行时没人执行"的分支，比不支持更糟 —— 见 M3 T10 修掉的 `DownloadStatus.FAILED`）。

### 1.3 缺口清单（M5 要补的）

| 缺口 | 落点 |
| --- | --- |
| `review_runs` 缺 `model_version` / `prompt_version` / `config_version` | 决策 ① |
| 原因码缺"币种不可比"等 | 决策 ⑤ |
| `app/rules/evaluator.py` | **新建**（四态判定，`app/rules/__init__.py` 已写明归 M5） |
| `app/rules/aggregator.py` | **新建**（风险聚合） |
| `app/ports/llm_gateway.py` | **新建**（`ocr_gateway.py` 已在 M4 落成） |
| `app/adapters/llm/` | **新建**（`MockLlm` + `OpenAiCompatibleLlm`） |
| `app/services/rule_service.py` | **新建**（批次 + 落库 + 作业） |
| 工具 5 `run_contract_rules` 的 REST 入口 | **新建**（与 M4 的工具 4 同形） |

---

## 2. 范围边界

### 2.1 M5 做

1. **批次**：创建 `review_runs`，在同一事务内固定解析版本、上下文快照与规则集版本；
2. **四状态评价**：按 §11.2 的**固定顺序** 逐条规则产生**恰好一条**评价并落 `rule_hits`；
3. **确定性条件**：`keyword`（含 `exclude_text`）/ `regex` / `expr`（数值与存在性）；
4. **受控 LLM**：`LLMGateway` 端口 + `MockLlm`；`llm` 规则在模型不可用时按**规则自带 fallback** 降级；
5. **证据定位**：命中证据必须能在标准文档中**反向匹配**（复用 M4）。
6. **风险聚合**：**计算并返回**总风险等级、结论完整性、四态计数、摘要与关注点候选 ——
   **不落库**（决策 ⑦：它是 `rule_hits` 的纯函数，随要随算）；
7. **工具 5**：`run_contract_rules(case_id)` 异步入队 → `TaskRef`（与工具 4 同形）。

### 2.2 M5 明确不做

| 不做 | 归谁 |
| --- | --- |
| **写入 `review_results`**（结果固化、`content_digest`、确认与失效） | **M6**（决策 ⑦）。M5 只写 `review_runs` + `rule_hits`；**聚合结论由 M5 算出并返回**，由 M6 在人工确认后固化 |
| Outbox、评论回写 | **M6** |
| 完整任务查询 / 人工确认 / 重试接口、RBAC | **M7** |
| 前端展示与折叠规则 | **M8** |
| 真实 LLM 接入与 GPU 推理 | **M11**（M5 只到端口 + Mock） |
| 黄金合同集的质量统计 | **M12** |
| 回写门禁（`app/harness/policy.py`） | **M6**（M5 只产出可供其判断的字段） |

---

## 3. 数据变更

### 3.1 `review_runs` 增列（决策 ①）

```sql
ALTER-equivalent（SQLite 不自动加列，须 init_db.py --reset，见计划 §5.3 与 M4 §3.5）:
  model_version   TEXT,   -- 本次批次使用的模型标识（MockLlm 亦写入，如 'mock:deterministic')
  prompt_version  TEXT,   -- 提示词版本
  config_version  TEXT    -- 引擎配置版本（阈值、开关等）
```

⚠️ 这三列**不得**为空字符串占位：无模型时的正确取值是**真实描述**（
`model_version = 'none:fallback'`），而不是 `NULL` 或 `''`——
否则"这一批到底用没用模型"在库里看不出来，而这正是 M11 要统计的东西。

⚠️ 但**列本身刻意可空、也不设默认值**（T1 已按此实现，`tests/test_m5_groundwork.py` 有守卫）。
这不是遗漏，是权衡过的选择：`review_runs` 的**最小构造**（M1.5 / M3 时代的夹具）
只插三项，它们验的是**级联与复合外键**，与版本绑定正交。若设成 `NOT NULL`：

- 那些夹具要么被迫填**假版本**（假数据开始进入断言）；
- 要么更糟 —— `test_cross_task_parse_is_rejected`（"复合外键拦住了跨任务拼接"）
  会因为 **`NOT NULL` 先报错**而**照样通过**：断言还在、名字还是那个名字，
  但**它验的东西已经没有了**。

"六项必须绑定"这条不变量由 **M5 的写入路径 + 验收 11** 守，不靠列约束。

### 3.2 原因码增补（决策 ⑤）

| 新码 | 用途 |
| --- | --- |
| `CURRENCY_NOT_COMPARABLE` | 字段币种与规则期望币种不一致，数值不可比 |
| `THRESHOLD_NOT_CONFIGURED` | expr 规则缺 `value`（配置错误；`is_null`/`not_null` 除外） |

⚠️ 两者都是 **`ReasonCode`（规则评价原因码），不是 `ErrorCode`** ——
这一点必须写清楚，因为本稿初版在这里**说反了**（写成"不得登记进可重试集合"）：

| | 回答 | 落在哪 | 处置 |
| --- | --- | --- | --- |
| `ReasonCode` | 为什么**这条规则**判不了 | `rule_hits.reason_code` | 人工看这条规则的证据 |
| `ErrorCode` | **系统**哪里坏了 | 异常 / 作业 | 重试 / 查配置 / 查网络 |

`ReasonCode` **没有"可重试性"这个属性** —— 所以"不得登记进可重试集合"这句话本身
就把两个层级混在了一起。真正的判据只有一条：**它们不得出现在 `ErrorCode` 里**。
否则 `CURRENCY_NOT_COMPARABLE` 会顺着错误码那条路走，
把"某一条规则判不了"放大成**一次任务级失败**。

> T1 已落地（`app/enums.py` 的 `ReasonCode`），守卫在
> `tests/test_m5_groundwork.py::test_new_reason_codes_are_not_error_codes`。

### 3.3 不新建表

`review_runs` / `rule_hits` / `review_results` **均已存在且结构够用**（含 `UNIQUE(run_id, rule_id)`
与两条复合外键）。M5 **不新增业务表** —— 计划 §5.1 的 13 张表里，剩下的
`outbox_events` / `audit_events` 归 M6。

---

## 4. 接口设计

### 4.1 评价流水线（固定顺序，§11.2）

```text
① 适用性（applicability.py，已有）
     UNKNOWN            → needs_review(CONTEXT_MISSING / CONTEXT_CONFLICT / EVIDENCE_UNCERTAIN)
     NOT_APPLICABLE     → not_applicable(APPLICABILITY_NOT_MET)      ← 到此为止，不读字段
② 证据质量（字段四态）
     依赖字段非 extracted → needs_review(EVIDENCE_UNCERTAIN / EXTRACTION_FAILED)
                            ⚠️ 例外：absent 型（缺失类）规则的 not_found 是**命中**，见决策 ④
③ 确定性条件
     keyword / regex / expr → hit(CONDITION_MATCHED) / not_hit(CONDITION_NOT_MATCHED)
④ 必要时 LLM（仅 match_mode='llm'）
     gateway 不可用 → 用该规则自己的 fallback_match_json 走③
     fallback 也缺     → needs_review(MODEL_UNAVAILABLE)
⑤ Schema 校验 + 证据反向核验
     证据在标准文档中匹配不到 → **该结论作废**，降级 needs_review(EVIDENCE_UNCERTAIN)
```

**为什么 ① 之后就直接返回、不读字段**：`not_applicable` 的规则**不该**因为"字段读不出来"而升级成
`needs_review` —— 那会把不适用规则重新拉回人工队列，使 `not_applicable` 失去"减少噪声"的作用
（`HT-2026-0004` 标准品采购的 IP 规则正是这个场景）。

### 4.2 `LLMGateway` 端口（决策 ③）

```python
class LLMGateway(Protocol):
    @property
    def available(self) -> bool: ...
    def extract_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel | None: ...
    def complete_text(self, system: str, user: str) -> str | None: ...
```

- `available` 是**能力声明**，`None` 返回是**单次失败** —— 两者必须分开：
  前者让 evaluator 决定"走降级"，后者只让本条规则 `needs_review`；
- **`fallback` 不在适配器里**（决策 ③）：适配器不知道规则表的存在。

### 4.3 字段四态 → 评价（决策 ④ 的落地表）

| 规则语义 | 字段状态 | 结论 |
| --- | --- | --- |
| **阈值类**（`expr` 比较） | `extracted` | 正常比较 |
| | `not_found` | **`needs_review(EVIDENCE_UNCERTAIN)`** —— 不是 `not_hit` |
| | `uncertain` / `failed` | `needs_review(EVIDENCE_UNCERTAIN / EXTRACTION_FAILED)` |
| **缺失类**（`absent=True`） | `not_found` | **`hit`**（确实缺） |
| | `uncertain` / `failed` | `needs_review` —— **绝不能报缺失** |
| | `extracted` | `not_hit`（条款在） |

> 这张表是 M5 的**核心正确性**所在。`FieldStatus` 的 docstring 早已写明
> "把 `FAILED`/`UNCERTAIN` 当成 `NOT_FOUND`，是缺失类规则误报的根源"——
> M5 是那句话真正落地的地方。

### 4.4 聚合（§11.4）

```text
总风险   = max(risk_level of hit 评价)；无 hit → low
needs_review 不提高风险等级，但 → review_status = needs_review
计数     = hit_count / needs_review_count / not_applicable_count（三者 + not_hit = 规则总数）
摘要     = LLM（可用时）→ 否则确定性兜底（决策 ⑥）
关注点   = 命中项 + 待判断项，各带规则码、原因码与证据引用
```

⚠️ **`not_hit` 没有单独计数列**（表上只有三个计数），因此"三类计数之和 = 规则总数"**不成立**。
验收断言必须写成 `hit + needs_review + not_applicable + not_hit == 40`，
而**不能**写成"三个计数之和 = 40"—— 后者会把 `not_hit` 悄悄当成 0 并"通过"。

### 4.5 批次幂等：「同一次输入」到底是什么

`UNIQUE(task_id, version_no)` 只保证**序号唯一**，它不回答"这次该不该复用"。
那个问题必须先定义清楚 —— 否则幂等会以一种**看起来完全正常**的方式出错。

#### 「同一次输入」= 下列**六项全部相同**（不是三项）

| # | 输入项 | 换掉它意味着什么 |
| --- | --- | --- |
| 1 | `parse_id` | 解析版本变了 |
| 2 | `context_snapshot_json` | 权威上下文（立场 / 合同类型）变了 |
| 3 | `ruleset_version` | 规则集变了 |
| 4 | `model_version` | **模型变了**（Mock ↔ 真实 LLM） |
| 5 | `prompt_version` | 提示词变了 |
| 6 | `config_version` | 引擎配置（阈值 / 开关）变了 |

⚠️ **六项必须全比，一项都不能省。** 本稿初版只比前三项，而验收 11 又声称"批次绑定六项版本" ——
两者放在一起，就是一个**只有真去用才会撞上**的漏洞：

```text
把 model_version 从 mock:deterministic 换成真实模型
→ 幂等判定"前 3 项没变 ⇒ 同一次输入" → 复用旧批次
→ 返回的批次里 model_version 仍是旧的，而调用方以为新模型已经生效
→ 库里没有任何一处看得出这件事
```

**"复用判据"与"记录内容"必须是同一组字段。** 判据窄、记录宽，等于系统一边声称绑定了模型版本、
一边允许在模型变化时静默复用旧结论 —— 而那恰恰是"版本绑定"这个字段存在的**全部意义**。

#### 六项都是**配置快照**，不含运行期抖动

`model_version` 记的是**接入了哪个模型**，不是"这次调用成没成功"。
单次 LLM 调用失败记在**该条规则**的 `reason_code` 上（`needs_review`），**不改版本**。
否则同一份配置重跑两次会得到两个不同的键，**幂等直接失效**。

#### 快照必须先**规范化**再比较

`context_snapshot_json` 若按字符串比较，dict 键序不同就会判成"输入变了" ——
一次无意义的重复触发**凭空多出一个批次**（`version_no` 白涨，历史里多一份内容相同的评价）。
与 M4 的 `input_digest` 同一条要求：**先校验、再按稳定序列化（排序键、去空白）算摘要**。

#### 复用是默认；**重跑要显式**

- 六项全同 → **复用**既有批次（不新建序号、不重跑、不覆盖历史）；
- **任一项不同 → `version_no + 1`** 新建批次，旧批次与其评价**保留**；
- **`force=true` → 强制新建批次**。

⚠️ `force` 不是可有可无的：批次可能因**单次 LLM 失败**而含 `needs_review`。
没有强制开关时，"修好之后再跑一次"会被幂等挡在门外 ——
用户唯一能做的就是**改一个不相关的参数去骗过缓存**，而那会污染版本绑定。
这与 M4 验收 30（**失败记录不构成缓存命中**）是同一条要求，
只是这里不能自动区分（`needs_review` 是**合法结论**，不是失败），因此只能显式表达。

#### **决策 ⑧：不做规则级缓存**（本稿不实现）

只做**批次级**复用，任一输入变化就**整批重跑**。理由：

1. 40 条里 **31 条是确定性的**（`keyword` 13 + `expr` 18），重跑成本≈毫秒级；
2. 真正的成本在 9 条 `llm` 规则，而无模型时它们走**规则自带的 fallback**（同样是确定性）；
3. 规则级缓存会引入**第二套失效逻辑**（字段变了？证据变了？规则改了？）——
   那正是"批次"这一层抽象已经在处理的事。提前引入等于在 M9 之前做第二次优化，
   而它的失效缺陷会以"**结论看起来对、但基于旧证据**"的形式出现。

> 将来若 LLM 调用成为主要成本，再按规则级加缓存 —— 那时它的键是
> `(rule_version, 字段快照摘要, 证据摘要)`，它与批次级是**两个不同的缓存**，不要合并。

### 4.6 工具 5 的返回形状

与工具 4 **逐字同形**，复用 §4.6 的 `TaskRef` 与 `result_ref`：

```json
{ "outcome": "queued",
  "task_ref": { "job_id": 51, "task_id": 7, "status": "queued", "status_url": "/api/jobs/51" },
  "result_url": "/api/runs/{run_id}" }
```

`GET /api/jobs/{job_id}` 成功时 `result_ref` 指向**批次**（M5 的产物是 `review_runs` + `rule_hits`，
**不是** `review_results` —— 那是 M6 的产物，见决策 ⑦）。因此 M5 需在 `_result_ref()` 里按
`job_type` 分派，**不得**让工具 5 的作业返回一个指向解析结果的 `result_ref`。

**`GET /api/runs/{run_id}` 返回的内容必须够工具 6 直接使用** —— 这是决策 ⑦ 在接口上的含义。
否则 M6 拿不到"要保存的结果"，只能自己重算一遍，边界又交叉了：

```json
{ "run_id": 3, "task_id": 7, "parse_id": 12, "version_no": 1,
  "run_status": "completed",
  "ruleset_version": "…", "model_version": "none:fallback",
  "prompt_version": "…", "config_version": "…",
  "aggregate": {                       ← **现算的**，不是从表里读的
    "overall_risk_level": "high",
    "review_status": "needs_review",
    "counts": { "hit": 2, "not_hit": 31, "not_applicable": 5, "needs_review": 2 },
    "focus_points": [ … ] },
  "evaluations": [ { "rule_code": "PREPAY_RATIO_BIDDER", "evaluation_status": "hit",
                     "risk_level": "high", "reason_code": "CONDITION_MATCHED",
                     "reason_text": "…", "evidence": [ … ], "hit_detail": { … } }, … ] }
```

⚠️ `counts` **必须四个都返回**（含 `not_hit`）。`review_results` 表上只有三个计数列，
但那是 M6 的**存储**选择 —— 接口**不该继承它的缺口**：少了 `not_hit`，
工具 6 就答不出"这条规则为什么没报警"，而那正是 `rule_hits` 连不适用规则都保留的全部理由。

### 4.7 日志与 `correlation_id`

M5 的日志（批次创建、每条规则的结论、聚合结果）沿用 M4 的 `contextvar`，
并**必须在 Worker 段读回注入**（工具 5 是跨进程的）。批次创建时把 `correlation_id`
随作业持久化 —— 与 M4 同一条链路。

---

## 5. 验收标准

| # | 验收项 | 验证方式 |
| --- | --- | --- |
| 1 | **40 条规则各产生且仅产生一条评价** | 一次批次后 `COUNT(rule_hits) == 40`，且四态计数之和 == 40（§4.4 的算式） |
| 2 | **高风险合同总风险 = 高** | `HT-2026-0002`（预付款 60%）→ **四条同时断言**：<br>① `PAY_PREPAY_RATIO_HIGH_FOR_BUYER` 的 `status == hit`<br>② 它的 `hit_detail.actual == "0.6"`<br>③ 它的 `evidence_text` 含「百分之六十」<br>④ `overall_risk_level == 'high'`<br>⚠️ 只断言④是**评审已指出的漏洞**：它可能被**别的高风险规则撞中**而通过 —— 那时预付款规则根本没运行，测试却是绿的（`HT-2026-0002` 里还有乙方违约金、保密、知识产权等多条规则） |
| 3 | **无模型时按 fallback 降级** | 9 条 llm 规则在 `available=False` 下**不给** `MODEL_UNAVAILABLE`，走 fallback 出 `hit`/`not_hit` |
| 4 | `IP_MISSING` 适用性 | `HT-2026-0003`（软件开发）→ `hit`；`HT-2026-0004`（标准品）→ **`not_applicable`**（防误报） |
| 5 | **立场冲突只影响依赖立场的规则** | `HT-2026-0006` → 立场相关规则 `needs_review(CONTEXT_CONFLICT)`，**主体/金额缺失等规则照常出结论**（`applicability.py` 的既有约定） |
| 6 | **阈值类规则遇 `not_found` 必须 `needs_review`** | 构造金额缺失 + 预付款阈值规则 → **不得** `not_hit`（决策 ④） |
| 7 | **缺失类规则遇 `uncertain`/`failed` 不得报缺失** | 构造条款 `uncertain` → `needs_review`，**不得** `hit` |
| 8 | **币种不可比不参与比较** | 字段币种 ≠ 规则期望币种 → `needs_review(CURRENCY_NOT_COMPARABLE)` |
| 9 | `hit_detail_json` 记录计算过程 | expr 命中 → 含 `{actual, op, threshold}` 且 `actual` 与字段 JSON 的值一致 |
| 10 | **证据反向匹配** | 每条 `hit` 的 `evidence_text` 能在该批次标准文档中定位到（复用 `resolve_span`） |
| 11 | **批次绑定六项版本** | `review_runs` 的 `parse_id` / `context_snapshot_json` / `ruleset_version` / `model_version` / `prompt_version` / `config_version` 全部非空且正确 |
| 12 | `ruleset_version` 是**确定性摘要** | 同日同规则集重跑 → 同值；改一条规则的 `rule_version` → 变值 |
| 13 | **批次幂等：六项全同才复用** | **逐项**构造六项中任意一项的变化（解析版本 / 上下文快照 / 规则集 / **模型** / **提示词** / **配置**）→ **每一项都必须产生新批次**（`version_no + 1`），且旧批次与其评价**仍在**；六项全同 → 复用、`rule_hits` 不增。<br>⚠️ **逐项遍历，不抽查一项** —— 抽查会漏掉"六项里只比了三项"这种缺陷（本稿初版正是如此） |
| 14 | **`needs_review` 不提高总风险，但使结论不完整** | 只有一条 medium 命中 + 一条 `needs_review` → **返回的现算聚合**为 `overall_risk_level == 'medium'`、`review_status == 'needs_review'`（决策 ⑦：不查 `review_results`） |
| 15 | **未命中不加风险且可见** | 断言 `not_hit` 的评价**确实落库**（"为什么这条规则没报警"可回答） |
| 16 | **工具 5 可查询且不阻塞** | 入队 < 1s 返回 `TaskRef`；`GET /api/jobs/{id}` 成功时 `result_ref` 指向**批次**（§4.6） |
| 17 | **`regex` 模式真的被执行** | 造一条 regex 规则 → 命中与不命中各一次（防止"配置能填、运行没人执行"） |
| 18 | **关键词否定词表生效** | 含 `exclude_text` 的规则：正文出现否定词 → `not_hit` |
| 19 | **无 GPU 全链路可跑** | `MockLlm` 下 `HT-2026-0001`（低风险基线）全批次无 `MODEL_UNAVAILABLE`，总风险 = low |
| 20 | **新原因码是 `ReasonCode` 而非 `ErrorCode`** | 逐码断言两个新码**不在** `ErrorCode` 中（原因码是**结论**、错误码是**故障**；混用会把"某条规则判不了"放大成任务级失败）。T1 已落地，见 `tests/test_m5_groundwork.py` |
| 21 | **既有测试全绿** | `pytest` |
| 22 | **M5 不写 `review_results`**（决策 ⑦ 的边界断言） | 跑完一次批次后 `review_results` 行数**仍为 0**；同时工具 5 的返回 / `GET /api/runs/{id}` **已经给出**总风险、四态计数与关注点候选。<br>⚠️ 这条**可证伪**：只写在文档里的边界，会随着后续开发自然腐蚀（M4 的 `M4_ERROR_CODES` 守卫就是这么空转的） |
| 23 | **强制重跑可用** | `force=true` 时即使六项全同也**新建批次**。含 `needs_review` 的批次必须能被改善 —— 否则用户只能"改一个不相关的参数去骗过缓存"，而那会**污染版本绑定**（§4.5） |
| 24 | **甲方违约金比例命中** | 构造「甲方违约，应支付 30% 违约金」→ `LIAB_RATIO_HIGH_FOR_PARTY_A == hit`，且 `liability_party_b_ratio` **不受影响**（"甲方向乙方支付"里的乙方是**收款方**，不是承担方） |
| 25 | **乙方违约金比例命中** | 同上，甲乙互换。两条分开测，是因为"归属判反"会让两个字段**同时**看起来正常（一方多一条、一方少一条，而总数对得上） |
| 26 | **普通付款进度不得误判为预付款** | 夹具 `HT-2026-0003`（`contract_03_dev_no_ip.pdf`）的"合同生效后支付百分之三十，验收合格后支付百分之六十，质保期满后支付百分之十" → `prepay_ratio` 必须 `not_found`。<br>⚠️ 不限定上下文的实现会读出 `0.6` 并**命中** `PAY_PREPAY_RATIO_HIGH_FOR_BUYER` —— 报出一条"预付款 60%"的高风险，而这份合同的预付款是**未约定** |
| 27 | **多个冲突比例必须 `uncertain`** | 正文出现两个不同的预付款比例 → `prepay_ratio` 为 `uncertain`（`EVIDENCE_UNCERTAIN`）且**两处证据都保留**。取任何一个都是猜，而猜出来的值会被拿去和阈值比较 |
| 28 | **每个启用的 `expr.field` 必须有运行期事实生产者** | 静态：`app/rules/fields.py` 在**导入时**断言"白名单字段必有其生产者"；动态：遍历**全部夹具**跑真实链路（解析 + 事实解析），断言 `ALL_EXPR_FIELDS` 无遗漏（`tests/test_rule_fact_resolver.py`）。<br>⚠️ 评审点名的一条：`check_rules.py` 原先只验字段**在白名单里**，而 `prepay_ratio` / `liability_party_a_ratio` / `liability_party_b_ratio` 曾在白名单里**没有任何生产者** —— 6 条 expr 规则在运行期永远拿不到输入，而白名单、校验脚本、全部测试**都一声不吭** |

| 29 | **入队到执行之间规则变了，作业仍须评价它自己那个批次** | 入队（`run_id=N`）→ 新增一条规则 → 执行 → 断言：① 评价的是 `N`；② `review_runs` 总数仍为 1；③ 只评**冻结时**那几条，新加的不得被评。<br>⚠️ 过去的实现会在这里再调 `start_run` → 建出 `N+1`，而作业与 `result_ref` 仍指向 `N`（空批次）—— **作业成功、批次为空、结论全无** |
| 30 | **重复请求命中仍在运行的批次时，状态取自作业** | 同一份输入入队两次 → `reused=True` 但 `status` 必须是**作业的真实状态**（未领取时不得答 `completed`）。<br>⚠️ 从 `reused` 推断状态会让调用方停止轮询，然后去读一个还没有结论的批次 |
| 31 | **工具 5 的响应可轮询到结论** | 入队响应带 `job_id` → 沿 `task_ref.status_url`（`/api/jobs/{id}`）→ `result_ref.result_url`（`/api/runs/{id}`）→ 取得**现算聚合**。全链路 `run_id` 始终是同一个（已合成断言于 `tests/test_rule_service.py`） |

> 与 M4 的 56 条相比 M5 只有 **31** 条，是因为 M5 的复杂度集中在**判定语义**而非**跨进程**；
> 每条都对应一处"写错了也不会报错"的地方。
>
> ⚠️ 24–28 是**外部评审补入**的（见 §10）。这五条的共同点：它们的失败**不抛异常、不报错**，
> 只会让规则拿到**错的输入** —— 而这正是"每条都对应一处静默失败"这句话最典型的形态。
>
> ⚠️ **29–31 是第 4 轮评审补入的**（见 §12）：它们是**链路级**的（跨 入队/执行/查询 三处），
> 与前面 28 条"单点判定"性质不同，因此**单独编号**而不是塞进 T10 的描述里 ——
> 藏在别的条目下会让它们既不被计数、也不被单独执行。

---

## 6. 实施计划

| # | 任务 | 产出 | 依赖 | 预估 |
| --- | --- | --- | --- | --- |
| **T1** | 数据地基 | `review_runs` 增 3 列；`ReasonCode` 增 2 码；两个 `StrEnum`；受控 DTO；`schema.sql` 与 `models.py` 同步 | — | 0.25 天 |
| **T2** | `LLMGateway` 端口与适配器 | `app/ports/llm_gateway.py`、`app/adapters/llm/mock_llm.py`、`openai_compatible.py`（+ 合约测试） | T1 | 0.5 天 |
| **T3** | 确定性匹配器 | `keyword`（含 `exclude_text`）/`regex`/`expr`（数值 + 存在性 + 币种检查） | T1 | 1 天 |
| **T3a** | **规则事实解析**（外部评审补入，见 §10） | `app/rules/fact_resolver.py` —— `expr` 规则所需"量"的**生产者**：`prepay_ratio` / `liability_party_a_ratio` / `liability_party_b_ratio`（中文与阿拉伯数字百分数、上下文分类、冲突值 `uncertain`、原文证据与来源追踪）；`tests/test_rule_fact_resolver.py`。另含 `FIELD_PRODUCERS` 生产者表与**导入时**守卫（`app/rules/fields.py`）<br>⚠️ 这 3 个字段此前**没有任何生产者** → 6 条 expr 规则在运行期永远拿不到输入 | T1 | 1 天 |
| **T4** | **四状态评价引擎** | `app/rules/evaluator.py`：§4.1 固定顺序 + §4.3 字段四态分派 | T3 | 1 天 |
| **T5** | LLM 规则与分派 | `llm` 模式 + 模型可用性分派。<br>⚠️ **"降级"有两种，含义完全不同，不得压成一句"两次失败即降级"**（评审校正，见 §10）：<br>① **未配置模型**（`available=False`，**调用前**即知）→ 用规则的 `fallback_match_json` 出**确定性结论**（验收 3）；<br>② **已配置、但连续两次调用失败**（**运行期**故障）→ `needs_review(MODEL_UNAVAILABLE)`，**不得**静默换成 fallback 答案。<br>理由见 `app/rules/llm_judge.py` 的 docstring：运行期换成 fallback，会让 M12 的模型质量统计把 fallback 的答案**算成模型答案**，"模型到底答得怎么样"这个指标从此永远失真 | T2 T4 | 0.5 天 |
| **T6** | 证据定位 | 复用 M4 的 `resolve_span`；命中 → `evidence_text`/`position`/`evidence_json` | T4 | 0.5 天 |
| **T7** | 风险聚合（**现算，不落库** —— 决策 ⑦） | `app/rules/aggregator.py`：总风险、结论完整性、**四态计数**（含 `not_hit`）、摘要/关注点候选（含确定性兜底） | T4 | 0.5 天 |
| **T8** | 批次与作业 | `app/services/rule_service.py`：**六项全同才复用**的批次幂等 + `force` 强制重跑（§4.5）、落库、六项版本快照（含 `ruleset_version` 计算与快照规范化）、`correlation_id` | T7 | 1 天 |
| **T9** | 工具 5 入口 | `POST /tools/run_contract_rules`、`GET /api/runs/{run_id}`、`result_ref` 按 `job_type` 分派 | T8 | 0.5 天 |
| **T10** | 测试与故障演练 | **31 条验收**的自动化部分（含**验收 29–31** 三条链路级回归，见 §5 与 §12）；模型不可用、证据匹配失败、币种不可比、**六项幂等的逐项遍历**、`force` 强制重跑 | T1–T9 | 1 天 |
| **T11** | 验收证据 | `scripts/verify_m5.py`（沿用 `verify_m3`/`verify_m4` 形态：逐条实测值 + 退出码） | T10 | 0.5 天 |

**合计约 8.75 天**（外部评审补入 T3a 的 1 天，见 §10）。

关键路径 T1 → T3 → T4 → T7 → T8 → T9 → T10 → T11 **不变** ——
T3a 与 T3/T4 可并行，不拉长关键路径；但 **T8 依赖 T3a**：
没有它，6 条 expr 规则在运行期拿不到输入，而批次会**照常跑完**（只是结论都是错的）。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| **四态与 `not_hit` 混淆** | 误报（把"判不了"说成"没问题"），审批人不再信任系统 | §4.3 的落地表**逐格写成测试**；验收 6/7 专门守它 |
| `expr` 规则最多（18 条），数值类型杂 | 比较写错但"看起来在比" | 一律经 `Decimal`；币种与数值分开校验；`hit_detail_json` 留计算过程供核对 |
| LLM 输出不可控 | 幻觉证据 | 端口强制 `schema`；**证据反向核验**（§4.1 第⑤步）；两次失败 → `needs_review(MODEL_UNAVAILABLE)`。⚠️「降级」= 降级为**人工判断**，**不是**降级为另一个答案（T5 行与 `app/rules/llm_judge.py`） |
| 无模型时"M5 做不完" | 进度停滞 | `MockLlm` + 规则级 fallback 双保险；验收 19 专门守"无 GPU 全链路可跑" |
| 批次重复/覆盖历史 | 结论无法追溯 | `UNIQUE(task_id, version_no)` + §4.5 的复用/新建规则；验收 13 |
| 总计划与实现漂移 | 文档不再可信 | 沿用 M4 的**执行记录**小节（§0.x）+ `verify_m5.py` 逐条实测值 |

---

## 8. 与其他里程碑的接口

| 对方 | M5 需要满足它什么 |
| --- | --- |
| **M4（上游）** | 消费 `contract_parses`（字段四态 + `reason_code` + 两个保留键）与标准文档；`parse_status='succeeded'` 表示已过质量门禁，M5 可信任结论完整性 |
| **M6（下游）** | M5 **不写** `review_results`（决策 ⑦）。M6 从 `GET /api/runs/{run_id}` 取**工具 5 的聚合结论**（现算的），连同**人工编辑内容**写入 `review_results`（`summary_text` / `focus_points_json` / `comment_text` / `content_digest`），并负责确认与失效 |
| **M7** | 工具 5 的 REST 入口在 M5 交付；M7 补 MCP 注册与完整查询，**不改变** `TaskRef` 形状；`GET /api/tasks/{id}/evaluations` 直接读 `rule_hits` |
| **M8** | 模块 4「规则命中」消费 `rule_hits` 的**四态 + `risk_level` + 规则名/版本 + `reason_code`/`reason_text` + `evidence_json` + `hit_detail_json`**；`hit` 与 `needs_review` 展开、`not_hit`/`not_applicable` 折叠 |
| **M11** | `model_version` / `prompt_version` / `config_version` 必须**落在列上**（决策 ①），否则模型质量统计无法按版本聚合 |
| **M12** | 不可判断比例 = `needs_review_count / 规则总数`；该口径由 M5 的三个计数直接给出 |

---

## 9. 待确认清单（请拍板）

- ✅ **决策 ⑦ 已确认**（2026-09-14，外部评审第 1 轮）：**M5 不写 `review_results`** ——
  与原始七工具边界一致，从根上避免 M5 与 M6 的职责交叉。
  本稿初版在两处自相矛盾，已统一（§0 ⑦ / §2.2 / §4.6 / §5 验收 22 / §6 T7 / §8）。
- ✅ **「同一次输入」已改为六项全同**（2026-09-14，外部评审第 2 轮）：
  初版复用判据只比三项，却声称绑定六项 —— **判据窄、记录宽**，
  会在模型 / 提示词 / 配置变化时**静默复用旧结论**。已重写 §4.5，
  并新增**逐项遍历**的验收 13 与强制重跑的验收 23。**决策 ⑧（不做规则级缓存）**同时确定。
- **1. 其余六个决策**（§0 的 ①②③④⑤⑥）—— 尤其 **①②③**（唯一影响**表结构**与**适配器数量**的三条）；
- **2. M5 的验收条数 28 条**是否够（要不要加入"40 条规则的逐条快照"作为回归基线）；
  ⚠️ 24–28 是外部评审第 3 轮补入的，见 §10；
- **3. 是否在 M5 一并实现 `app/harness/policy.py`（回写门禁）** —— 本稿把它留给 M6，
  理由是门禁的输入（确认摘要、结果完整性）在 M6 才齐；若希望 M5 提前定义，请指出。
- ✅ **4. 已定并落地（2026-09-14）**：`PAY_PREPAY_MISSING` **补立场限定** ——
  它原先只按合同类型限定，我方为**采购方**（不需要预付、全额验收后付款）时也会命中 medium，
  导致验收 19「基线总风险 = low」跑不到。已按 §10 的方案 1 处置，**验收 19 现成立**（实证见 §10）。

---

## 10. 外部评审（第 3 轮）处置记录（2026-09-14）

四条意见：**P0①** 缺事实生产者、**P0②** 验收过松、**P1**「降级」语义、**P2** 规则版本审计。

### P0① 增加事实解析任务（T3a）—— 已落地 ✅

评审指出：**T3 只有确定性匹配器，缺少匹配器所需要的事实生产者**。核实属实，
且缺口比意见里写的更具体：

| 项 | 事实 |
| --- | --- |
| 缺生产者的字段 | `prepay_ratio`、`liability_party_a_ratio`、`liability_party_b_ratio` |
| 受影响规则 | **6 条** expr 规则（预付款 4 条 + 违约金 2 条），含**验收场景 `HT-2026-0002`** |
| 原表现 | 字段取不到值 → `is_null` 类报**"未约定预付款"**、阈值类报"判不了"。两种都像**业务结论**，报告上看不出这是实现缺口 |
| 白名单为什么没拦住 | **白名单只管"名字合不合法"，不管"有没有人生产"** —— 这是它的能力边界，不是缺陷。因此新增**生产者表 + 导入时守卫**（见下） |

交付：`app/rules/fact_resolver.py` + `tests/test_rule_fact_resolver.py`（**25 条**）。

三档结论：**唯一确定的比例 → `extracted`**；**多个矛盾比例、或有条款但读不出 → `uncertain`**；
**通篇没有该条款 → `not_found`**。
⚠️ 后两档必须分开：合并会让 `PAY_PREPAY_MISSING` 把"**我们没读出来**"报成**"合同没约定"**。

实施中抓到两处**判据**问题（都不是"漏了功能"，而是"看起来对、其实错"）：

| 陷阱 | 夹具原文 | 不设上下文判据的后果 |
| --- | --- | --- |
| **付款进度被当成预付款** | `HT-2026-0003`：「合同生效后支付百分之三十，验收合格后支付百分之六十，质保期满后支付百分之十」 | 读出 `0.6` → **命中** `PAY_PREPAY_RATIO_HIGH_FOR_BUYER` → 报出一条"预付款 60%"的高风险，而这份合同的预付款是**未约定** |
| **收款方被当成承担方** | 「甲方违约的，应向**乙方**支付…30% 的违约金」 | 按"提到谁就算谁"判 → **甲乙各 30%** → 乙方凭空多出一条约定，且证据指向一句没提乙方义务的句子 |

处理：**「预付」必须与比例同句**；**按日/按月计费的是费率、不是总比例**
（`contract_02` 的"每逾期一日按…千分之一" → `uncertain`，**不得**写成 `0.001`）；
承担方由 `_OBLIGOR_PATTERNS` 判（**先判"谁违约"，再判"谁付款"**）。

**已知边界**（已写进测试，钉住）：同句既有日费率又有总上限（"按日千分之一，累计不超过…百分之二十"）
时一律判 `uncertain`。要区分就得按逗号再切一次，而切逗号会丢掉主语归属 ——
取舍是**宁可交人工，也不取一个可能错的数**。

**生产者守卫**（`app/rules/fields.py`）：`FIELD_PRODUCERS` 表 + **模块级断言** ——
白名单字段缺生产者时**导入即失败**（`check_rules.py` 因此根本起不来）。
⚠️ 放在导入时而不是测试里，是因为"手工维护的集合会漏"本仓库**已经发生过两次**
（M4 的 `M4_ERROR_CODES` 漏登错误码；本表的三个派生字段漏登生产者）。
测试会被跳过、被 xfail、被改宽 —— 导入时的断言不会。

### P0② 收紧高风险合同验收 —— 已落地 ✅

验收 2 由「`overall_risk_level == 'high'`」改为**四条同时断言**（规则状态 / `actual` / 证据文本 / 总风险）。
理由与评审一致：只断言总风险，它可能被**别的高风险规则撞中**而通过 —— 那时预付款规则根本没运行，测试却是绿的
（`HT-2026-0002` 里还有乙方违约金、保密、知识产权等多条规则）。

新增验收 **24–28**：甲方 / 乙方违约金比例命中（**分开测**，因为"归属判反"会让两个字段**同时**看起来正常）、
普通付款进度不得误判为预付款、多个冲突比例必须 `uncertain`、**每个启用的 `expr.field` 必须有运行期事实生产者**。

最后一条（评审特别点名）的落地方式：

- **静态**：导入时断言（见 P0①），并在 `check_rules.py` 的报告里显示覆盖数；
- **动态**：遍历**全部夹具**跑真实链路（解析 + 事实解析），断言 `ALL_EXPR_FIELDS` 无遗漏。

> ⚠️ 在 `check_rules.py` 里**再**查一遍"规则引用的字段有没有生产者"会是**死代码**：
> 引用未登记字段的规则早被 `RuleConfig.from_row`（白名单校验）拦下，两处判据完全相同。
> 一个"看起来在检查、其实永远为真"的检查，比没有检查更糟 ——
> 所以那道检查放在**导入时**，而不是脚本里。

### P1 澄清 LLM 的「降级」语义 —— 代码本就正确，**错的是计划的一行字** ✅

核实结果：`app/rules/llm_judge.py` 的 docstring **已经**就此事写了三条理由，
**代码是对的**，与评审描述一致。因此本项只需把计划文字改准（T5 行 + §7 风险表）：

| 情形 | 何时可知 | 处置 |
| --- | --- | --- |
| **未配置模型**（`available=False`） | **调用前** | 用 `fallback_match_json` 出**确定性结论**（验收 3） |
| **已配置、连续两次调用失败** | **运行期** | `needs_review(MODEL_UNAVAILABLE)`，**不得**静默换成 fallback 答案 |

统一口径：**「降级」= 降级为人工判断，不是降级为另一个答案。**

### P2 规则版本审计 —— 选**方案一（保存规范化 `ruleset_snapshot_json`）**

评审给出二选一。选方案一（快照），理由：

1. **它是加法**：`review_runs` 增一列即可 —— 不动 `review_rules` 的既有语义、
   不动 `init_db` 的种子、不动 M6 对规则的读写；方案二（版本不可变）三处都要动，
   还要处理"规则被改后旧版本存哪里"；
2. **它直接解决评审提的那个问题**（"仅靠哈希与当前规则表无法还原当时的配置"）：
   批次自带当时的**全部规则配置**，规则后来被原地改掉也不影响历史批次的可还原性；
3. **与方案二不冲突**：将来若要做"版本不可变"，已落地的快照仍是有用的旁证，不需回填。

落地位置：**T8**（批次落库，与六项版本快照同一处），因此**不阻塞** T3a / T7。

**已落地（2026-09-14，随 T8）：**

- `db/schema.sql` + `app/models.py`：`review_runs.ruleset_snapshot_json`（规范化 JSON，按 `rule_code` 排序）；
- `app/services/rule_service.py`：**版本就是快照的摘要**，且两者都在 `start_run` **内部**派生 ——
  调用方**无法**传进一个与规则集不匹配的版本号，"版本没变、内容却变了"在类型上就不可能发生；
- 快照记的是**库里的原始列**（`match_text` 等文本），不是"解析成对象再序列化一遍"：
  后者会多出一层"我们怎么表示它"的定义，而那层定义随代码演进 —— 旧快照的**含义会悄悄改变**；
- `_SNAPSHOT_COLUMNS` **不含 `updated_at`**（易变列）：碰一下就换版本号，
  "重新导一次种子"会凭空产生一批新批次，而规则内容一个字都没变。

#### T8 落地时踩到的两处"同名不同义"（都不是逻辑错，是名字错）

| 现象 | 根因 | 代价 |
| --- | --- | --- |
| `'RuleConfig' object has no attribute 'model_dump'` | `RuleConfig` 是**普通 dataclass**（不是 pydantic 模型） | 快照不能用 `model_dump`；改为直接取原始列 —— 反而更对（见上） |
| `'RuleEvaluation' has no attribute 'hit_status'` | `hit_status` 是**物理列名**（需求 2.4.9 规定），Python 属性是 `evaluation_status` | 测试里按列名写属性直接 `AttributeError` |

两者的共同点：**同一件事有两个名字，而错的那一侧永远是静默的** ——
第一个若不报错，快照会记下一个"我们重新表示过"的版本；
第二个若不报错，测试会断言一个根本不存在的列。写这类映射时，
"名字对得上"本身就该是一条断言。

### 附：本轮发现的一处**验收与实现冲突**（已记入 §9 第 4 项）

**验收 19 说 `HT-2026-0001`（低风险基线）总风险 = `low`，但按现有规则配置跑不到。**

| 项 | 值 |
| --- | --- |
| 实例 | `HT-2026-0001`：`contract_type=procurement`、`our_party_business_role=` **`buyer`** |
| 夹具正文 | "双方约定验收合格后三十日内支付合同总金额" —— **无预付款约定**（全额验收后付款） |
| 规则 | `PAY_PREPAY_MISSING`：`{"field": "prepay_ratio", "op": "is_null"}`，`risk_level=medium`，`applies_when={"contract_types": […"procurement"…]}` |
| 推演 | `prepay_ratio` = `not_found` → `is_null` + **缺失类** = `hit` → 总风险 = **medium ≠ low** |

这**不是** M5 判定逻辑的问题，而是**规则配置**的问题：`PAY_PREPAY_MISSING`
**只按合同类型限定、没有按立场限定**，而同一类里的
`PAY_PREPAY_RATIO_HIGH_FOR_BUYER` / `..._LOW_FOR_SELLER` **都是立场敏感的**。

业务上：**我方是采购方（买方）时，"没有预付款约定"不是风险，反而有利** —— 垫资风险在**收款方**。

两种处置：① 给规则补立场限定（验收 19 的 "low" 成立）；② 保留原样（验收 19 改为 `medium`，
且 `fixtures.json` 里"低风险基线"这句说明也要一并改，否则"基线"的定义与事实相反）。

#### ✅ 已按方案 ① 落地（2026-09-14）

`db/seed.sql` 的 `PAY_PREPAY_MISSING` 补上
`"our_business_roles": ["seller", "service_provider", "licensor"]`（与 `PAY_PREPAY_RATIO_LOW_FOR_SELLER` 对齐）；
`check_rules.py` 通过，方向敏感规则 **17 → 18 条**。

**实证**：对全部 **40 条**启用规则各跑一遍（我方立场取 `fixtures.json` 的声明）：

```text
HT-2026-0001 (procurement / buyer)  →  总风险 = low    ✅ 验收 19 成立
    hit 仅 1 条：SUBJ_CREDIT_CODE_MISSING（low）
    PAY_PREPAY_MISSING 已不再命中  ← 修复生效
HT-2026-0002 (procurement / buyer)  →  总风险 = high   ✅ 验收 2 的前提
    PAY_PREPAY_RATIO_HIGH_FOR_BUYER 命中，actual = 0.6  ← T3a 确实产出了该字段
HT-2026-0003 (software_service)     →  medium，IP_MISSING 命中      ✅ 验收 4
HT-2026-0004 (procurement)          →  medium，IP_MISSING 未命中    ✅ 验收 4
```

⚠️ 这次预演用的是一次性脚本，**已删除**：T8 落地后由真实批次取代它，
留着会让"预演逻辑"与"批次逻辑"各自漂移。

#### ⚠️ 通用守卫**抓不到**这个缺陷 —— 这一点要单独记下来

`tests/test_rule_config.py` 里早已有一条"方向敏感规则必须声明立场条件"的守卫，
但它对 `PAY_PREPAY_MISSING` **静默不适用**：`is_direction_sensitive` 是
**从 `applies_when` 反推出来的** —— 没声明立场条件的规则，它自然报 `False`。

> **一条从被检对象自身推导出来的判据，永远发现不了该对象的缺失。**

因此新增的回归守卫断言的是**语义**（我方为买方 → `not_applicable`；为卖方 → `applicable`），
而不是"它声明了什么"。

---

## 11. T9 落地记录（**已完成**；2026-09-14）

> ⚠️ **本节是历史过程记录**：下文出现的「进行中」「仍未接上的一处」「尚未补的测试」
> 「已知不稳定项（未解决）」等表述，**均已在第 4/5 轮评审中解决**
> （组装根已落地、断言已补、不稳定项已定位为 IDE shim 并修复）。
> 保留原文是为了留下决策与排查的轨迹 —— **当前最终状态以文档头部与 §12 末尾为准**，
> 执行 M6 的 Agent 不要把这里的"进行中"当成现状。

### 已完成

| 项 | 产出 |
| --- | --- |
| **批次执行** | `rule_service.run_batch`：取批次 → 评价全部启用规则 → 证据定位/反向核验 → 落库 → 现算聚合 |
| **评价读回** | `evaluations_of_run` / `aggregate_of_run` —— 决策 ⑦ 的**前提**（聚合不落库，就必须随时能算回来） |
| **批次查询接口** | `GET /api/runs/{run_id}`：六项版本绑定 + **现算**聚合 + 逐条评价（四态全返回） |
| **复用语义修正** | 见下 |

#### 复用语义：**"存在但为空"不是复用，是"已入队、还没算"**

工具 5 在入队时就建好批次（与工具 4 预留解析占位同理），worker 拿到的正是
一个"存在但没有任何评价"的批次。若按"存在即复用"处理：

```text
任务成功、批次为空、结论全无 —— 而作业状态显示 succeeded
调用方拿到一份**没有结论的空结果**，却没有任何一处报错
```

已改为：**已有评价才算复用；存在但为空则就地评价**（不新建批次）。
这也顺带覆盖了"worker 崩在落库之前"的重试。

### 已侦察清楚（下一步直接用，不必再摸）

| 问题 | 事实 |
| --- | --- |
| 标准文档存在哪？ | **`parse_artifacts` 表**：`kind = 'standard_document'`，键是**内容寻址**的 `sha256/{ab}/{cd}/{sha}.standard_document.json`，载荷就是 `StandardDocument.model_dump(mode="json")`。因此用 `StandardDocument.model_validate_json(...)` 可**精确还原** —— 合同正文（`page.text`）也随之恢复 |
| 合同正文要单独存一列吗？ | **不用**：`contract_parses` 里确实没有正文列，但文档工件里有 `page.text`（`DocumentPage.text` 是权威明文） |
| 字段从哪来？ | `contract_parses.basic_info_json` / `clause_info_json`（M4 已抽好），加上 **T3a 的派生字段** |
| worker 的分派在哪？ | `app/worker.py` **只有一个分支**（`job_type == JobType.PARSE`，约 L580）—— 需要新增 RULE 分支 |

### 已落地（本轮补齐）

| 项 | 位置 |
| --- | --- |
| 装配：解析产物 → 批次输入 | `rule_service.load_batch_inputs`（还原文档工件 + 直接字段 + **T3a 派生字段** + 正文 + 币种） |
| 工具 5 入队 | `rule_service.request_rule_run` + `POST /tools/run_contract_rules`（校验门禁 → 建批次 → 建 `JobType.RULE` 作业） |
| 作业处理器主体 | `rule_service.run_rule_job`：从批次读回**冻结的上下文** → 装配 → `run_batch` |
| `result_ref` 分派 | `app/api/jobs.py::_result_ref` 按 `job_type` 分派（`PARSE` → `/api/parses/{id}`、`RULE` → `/api/runs/{id}`） |
| 权威上下文 | `rule_service.context_for_parse`：立场/合同类型取自审批记录，**两个核验状态分开取**（M4 §4.8 的两个保留键） |

#### 三处刻意的取舍

1. **`result_ref` 按 `job_type` 分派，不是"试着从输入里找个 id"** —— 两种作业的结果是**不同的资源**，
   而"取到哪个 id 就用哪个"会在输入里恰好同时有 `parse_id` 与 `run_id` 时指向错误的那个，且不报错；
2. **上下文从批次读回，不重新推导**：`context_snapshot_json` 是入队时冻结的权威上下文，
   重新推导会让"入队时算的批次"与"worker 跑时的批次"在立场上有两种答案 ——
   而六项幂等判据比的正是这个快照，两者不一致就会**凭空多出一个批次**；
3. **我方立场不是入参**（`RunContractRulesRequest` 里没有这个字段）：它决定每条方向敏感规则
   该不该判，让调用方传等于让它变成一个可以随手改的入参。

### 仍未接上的一处（组合根，不是服务层）

⚠️ **`Worker` 的处理器是注入式的，而仓库里还没有组装点**（`app/worker.py:433`
接收 `handler`，但 `app/` 与 `scripts/` 里没有任何 `Worker(...)` 构造）。

因此"worker 真的会去跑 RULE 作业"这一步**尚未接通** —— `run_rule_job` 已就绪，
差的是组合处按 `job_type` 分派到它（与 M4 的 PARSE 处理器并列）。

另外 `worker._log` 的 `LogType` 映射目前只判了 `PARSE`，其余全落 `SYSTEM` ——
RULE 作业应映射到 `LogType.RULE`，否则"审查"的日志会混进系统日志里。

### 尚未补的测试（下一轮第一件事）

`request_rule_run` / `run_rule_job` / `POST /tools/run_contract_rules` 三条
**目前只有实现、没有断言**（`load_batch_inputs` 与 `run_batch` 已有）。
按本项目的惯例，没有断言的路径不算完成 —— 特别是"入队后 worker 就地评价空批次"
这条链路，它正是本轮修掉的那个静默缺陷所在。

---

## 12. 外部评审（第 4 轮）处置记录（2026-09-14）

三条：**P0①** `TaskRef` 缺 `job_id`、**P0②** worker 可能评价新批次而作业指向旧空批次、
**P1** 重复请求把"运行中"误报为"已完成"。

### P0① `TaskRef` 缺 `job_id` —— 已修 ✅

评审属实，而且是**代码与自己的注释自相矛盾**：注释写着「`job_id` 必须有」，
返回里却没有它，`status_url` 还指向 `/api/runs/{run_id}` ——
而批次**没有"排队中"这个状态**（一建出来就是 `running`），
于是"入队了没"这件事在接口上无法回答。

根因与评审判断一致：`create_job` 的返回值被丢弃，`RunStart` 里也没有 `job_id`。

已修：`RunStart` 增 `job_id` / `job_status`；`request_rule_run` 收下作业并填回；
`task_ref.status_url = /api/jobs/{job_id}`；`result_url = /api/runs/{run_id}`。

### P1 重复请求误报"已完成" —— 已修 ✅

原先写的是 `status = "completed" if start.reused else "running"` —— **从 `reused` 推断状态**。
六项全同但上一次仍在跑时 `reused=True` 而作业是 `running`，答成"已完成"
会让调用方停止轮询，然后去读一个**还没有结论**的批次。

已改为**照实读回** `WorkflowJob.job_status`。并把分工写进注释：
`outcome` 说"这次调用做了什么"，`status` 说"作业跑到哪了"。

### P0② worker 可能评价**新**批次，而作业仍指向旧空批次 —— 已修 ✅（断言待补）

**已落地**（按评审建议的拆分）：

- `execute_existing_run(run_id, storage, llm_judge)`：**只执行指定批次** ——
  上下文从 `context_snapshot_json` 读回，规则集从 `ruleset_snapshot_json` **重建**
  （`_rules_from_snapshot`），版本从批次列读回；**不调 `start_run`**，因此不可能另建批次；
- `run_rule_job` 退化为薄委托，**不再接受调用方传版本号**（版本由批次冻结，
  传进来要么与批次不符，要么促成另建批次）；
- 批次已完成 → 读回结论不重跑；半途失败的批次 → **就地补完**；
- ⚠️ 副作用按下面那段处置：**删除**的规则明确拒绝（`_ensure_rules_still_exist`），
  而不是静默改用当前规则集。

✅ **断言已补**（`tests/test_rule_service.py`，对应验收 **29–31**）：

- 入队到执行之间规则变化 → 仍评价原批次（批次总数不增、新规则不被评）；
- 完整批次幂等（复用不重跑）；**删除规则明确拒绝**；
- `job_id` 可轮询（作业输入里的 `run_id` 指向同一批次）；
- 状态取自**作业**而非 `reused`；
- 一条**全链路合成**断言：入队 → 执行 → 查询，`run_id` 全程一致、批次总数仍为 1、
  批次 `completed`、聚合与评价条数正确。

### ✅ 组装根已落地（`scripts/run_worker.py`）—— T9 收尾

```bash
python scripts/run_worker.py --job-types rule   # 今天就能跑
```

- **按 `job_type` 分派**：`RULE` → `run_rule_job`；
- ⚠️ **未注册的作业类型抛错，不静默跳过** —— 若"什么都不做、直接返回"，
  `complete_job` 会把作业标成 `succeeded`，于是**作业成功、批次为空、结论全无**
  （P0② 的另一副面孔：让「没做」看起来像「做完了」）；
- `--job-types` 可指定，因此 RULE 链路**不必等 M4 的解析接线**就能端到端跑起来。

**进程级证明**（`tests/test_rule_service.py::test_the_worker_claims_and_executes_a_rule_job`）：
用**真实 `Worker`**（含领取、租约、条件完成、一次提交）+ 与入口同一个 `make_handler`，
断言 `run_once()` 领到了作业、`job.job_status == succeeded`、
`run.run_status == completed`、评价 3 条、批次总数 1。**T9 完成。**

### ⚠️ 已知不稳定项（M5 收尾的最后一件事，未解决）

验收 31（`test_the_full_chain_keeps_the_same_run_id`）出现过**一次** `FAILED`
（在"先跑全量 pytest、紧接着跑 verify_m5"的那次门禁里），随后 **6 次运行全部通过**
（3 次单独、全量后紧跟 1 次、2 次 verify）。间歇、未复现、**失败时的输出没有留存**。

两个候选机制（按可疑度）：

1. **临时库churn**：一次完整 verify 会把整个 `tests/` 跑一遍（约 1000 个用例、
   数千个 `.pytest_tmp/` 临时目录），再叠加门禁里那次全量 —— Windows 的文件句柄 /
   Defender 扫描在持续创建删除小文件时最容易出现一次瞬时失败；
2. **时钟敏感**：worker 链路的领取依赖 `datetime.now()` 与租约时间。

**处置约定**：连续两次 `verify_m5.py` 31/31 且 exit=0 之前，M5 不宣告收尾。

#### ✅ 已修复（2026-09-14）：连续两次 31/31，exit=0

修法不在产品代码，在**验收子进程的环境**：`verify_m5.run_node` 给每个 pytest
子进程剥掉 `PYTHONPATH`（`_subprocess_env`）——IDE 的 `sitecustomize` 钩子正是
从那里注入的，剥掉之后退出钩子不再抛 `SystemExit(1)`。

失败时的自动补跑与 `verify_rerun_last.log` 保留为**常设机制**：
下一次再遇到"时红时绿"，第一反应就是看那份日志，而不是凭记忆复述现象。

#### ✅ 根因已定位（2026-09-14）：**不是产品缺陷，是 IDE 的 Python shim**

失败时的完整回溯（`verify_rerun_last.log`，由 `verify_m5` 在失败时自动补跑落盘）：

```text
test_the_full_chain_keeps_the_same_run_id PASSED [100%]   ← 测试本身通过
test_the_full_chain_keeps_the_same_run_id ERROR  [100%]   ← 报 ERROR 的是收尾阶段
tests\conftest.py:50: in work_dir
...CodeBuddy CN\resources\app\extensions\genie\out\vendor\shim\sitecustomize.py
raise SystemExit(1)
E   SystemExit: 1
```

即：**链路测试通过**，`work_dir` 夹具清理之后，CodeBuddy IDE 注入的
`sitecustomize.py`（Python 启动钩子）在解释器退出时抛了 `SystemExit(1)`，
pytest 把它记成该用例的 ERROR —— 所以它**只咬最后一个用例**（离解释器退出最近），
单独跑不触发、全量后紧跟也不触发，唯独 verify 的长序列必现。

**两个推论**：

1. 验收 31 的**实质**（入队 → 领取 → 执行 → 查询，`run_id` 全程一致）成立 ——
   测试本体是绿的，坏的只是收尾记账；
2. 修复在**测试环境**层：让 verify 产生的 pytest 子进程不被那只 shim 钩住
   （例如给 `run_node` 的子进程显式清掉相关环境变量），而不是改产品代码、
   更不是把这条验收从清单里拿掉。

**附带收益**：`verify_m5` 现在失败时会自动补跑一次带 `--tb=short` 的用例并把完整输出
落到 `verify_rerun_last.log` —— 这次能定位，靠的是它；上一轮抓不到，也是因为还没有它。

### 主证据的选取（原「待拍板」项，已定）

验收 2 的原文要求 `evidence_text` 含「百分之六十作为预付款」，而这份合同的
**抬头**也写着"预付 60%"且先出现。按出现顺序取首处会让主证据落在**摘要**上。

**已定**：主证据优先取**完整句子**（句末带 `。`/`；` 的那一处）——判据是排版事实，
不是关键词表；全部证据仍保留在 `evidence_json`，且**第一条就是主证据**
（`evidence_text` 与 `evidence_json[0]` 必须指同一段文字，否则两个消费方
会各拿到一段不同的"主证据"而互不知情）。同时，**验收 2 的断言改为语义三要素**
（预付款语境 + 比例事实 + 可定位区间），**不绑定 60%/百分之六十 这类具体写法** ——
写法取决于合同怎么印，不取决于契约。

### 第 5 轮评审的另一处修正：验收脚本自身必须能跑完

`verify_m5.py` 初版在打印 `⚠️`（U+26A0）时于 Windows 默认 GBK 终端抛
`UnicodeEncodeError`，**脚本当场中止** —— 后 29 条一条都没跑，而它看起来
像是"跑到某一条失败了"。已改为：标记一律 ASCII（`[OK]`/`[FAIL]`/`[WARN]`），
stdout 的错误处理设为 `replace` —— **验收脚本不允许因为输出不出去而中止**。

#### ⚠️ 但侦察后发现：缺的不只是 RULE 分支，而是**整个组装根**

全文检索确认：**仓库里没有任何作业处理器**（`app/` 与 `scripts/` 下既没有
`execute_parse` / `handle_parse`，也没有任何 `Worker(...)` 构造；
`tests/test_adapter_mock_gateway.py` 里的 `handler` 是 httpx 的，与此无关）。

也就是说 **M4 的 PARSE 作业同样没有接线** —— 不是"worker 能跑解析、只是不会跑审查"，
而是"worker 从未被组装过"。因此 `scripts/run_worker.py` 不是加一个分支，
而是一个**完整的组合根**：

| 要注入 | 现状 |
| --- | --- |
| 会话工厂 | `app/db.py` 有 `engine`；应用用的 `sessionmaker` 名称需确认 |
| 对象存储实现 | `LocalFileStorage` 已存在（`app/api/deps.py` 里用过）；导入路径需确认 |
| **PARSE 处理器** | **不存在** —— 需要接 `parse_service` 的解析入口 + 附件字节 + `build_document` 可调用对象 |
| RULE 处理器 | ✅ 已就绪（`rule_service.run_rule_job`） |
| 处理器内的 `JobRun` 取用 | `run.session` / `run.heartbeat()` 已知；作业输入的取法需确认 |

**建议的落地方式**（增量、可验证）：

```bash
python scripts/run_worker.py --job-types rule      # 今天就能跑：只领 RULE
python scripts/run_worker.py                       # 等 PARSE 处理器接上后再全量
```

这样 RULE 链路可以**先**端到端跑起来（验收 29–31 的证明），
而不必等 M4 的解析接线一起完成 —— 后者是**另一个里程碑的缺口**，
不应阻塞 M5 的 T9 收尾。

评审的复现是准确的：

```text
作业输入指向 run_id=10
worker 重新加载当前规则（或入队后规则变了）
run_batch 里 start_run 判定"六项不同" → 建 run_id=11
作业成功，result_ref 仍指向 run_id=10
run_id=10 仍是空批次   ← 上一轮修掉的静默缺陷，换了个入口又出现
```

根因：`run_rule_job` 只冻结了**上下文**，而 `run_batch` 会**重新加载当前启用规则**
并再调一次 `start_run`。评审建议的拆分是对的，且**已核实可行**：

| 职责 | 函数 | 约束 |
| --- | --- | --- |
| 入队时创建/复用批次 | `start_or_reuse_run`（现 `request_rule_run`） | **唯一**可以新建批次的地方 |
| 执行**指定**批次 | `execute_existing_run(run_id)` | **禁止**新建批次；不得重新读取当前规则集或当前配置 |

关键可行性：`ruleset_snapshot_json` 里存的就是**原始列**
（`match_mode` / `match_text` / `applies_when_json` / `fallback_match_json` /
`exclude_text` / `rule_version` / `rule_id`），**足以重建 `RuleSpec`** ——
"按冻结的规则集执行"不需要另建结构。worker 要用的六项**全部从批次读回**：
`context_snapshot_json` / `ruleset_snapshot_json` / `model_version` / `prompt_version` / `config_version`。

⚠️ 副作用需一并处理：按冻结规则集执行时，若某条规则已被**删除**，
`rule_hits.rule_id` 的外键会失败。处置是：规则**停用**（`rule_status`）不受影响；
**删除**则应明确拒绝（而不是静默改用它、或改写成当前规则）—— 这条进 T10 的回归场景。

### 评审确认无误的四项（不改）

`result_ref` 按 `job_type` 分派；上下文从批次读回；我方立场不是入参；
立场核验与合同类型核验分开取。

⚠️ 但评审指出「冻结上下文」应**扩大为冻结整个批次的六项输入与规则快照** ——
这正是 P0② 的内容，已作为该条的验收条件。

### 两个组合根缺口（评审已确认）

1. 仓库里**没有生产用的 `Worker(...)` 组装点**，只有测试构造 —— 需正式启动入口
   （`scripts/run_worker.py` 或 `app/bootstrap.py`），由组合根注入数据库、对象存储、
   LLM 适配器与**按 `job_type` 分派的处理器**；
2. `worker._log` 的 `LogType` 只映射了 `PARSE`，其余全落 `SYSTEM` ——
   `JobType.RULE` 应映射到 `LogType.RULE`（否则审查日志混进系统日志）。
