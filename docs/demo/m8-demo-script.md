# M8 五模块连续演示脚本

> 目标：一条合同从**待办列表**走到**已回写**，五个原始模块一个不落。
> 管理页（规则管理 / 运行管理）是扩展入口，**不在**演示主线上。
>
> 自动化证据：`python scripts/verify_m8.py`（无浏览器依赖，9 条全可执行）；
> 真浏览器回归：`npx playwright test e2e/`（spec 已就绪，浏览器二进制未装）。

## 0. 启动现场

```powershell
python scripts/init_db.py --reset          # 建库 + 规则种子
python scripts/make_fixtures.py            # 生成演示合同 PDF
python scripts/run_mock.py                 # 终端 1：mock 审批系统
python scripts/run_worker.py               # 终端 2：RULE/Outbox 等作业
python scripts/run_outbox_dispatcher.py    # 终端 3：回写派发
python scripts/run_api.py                  # 终端 4：API（含控制台静态页）
```

打开控制台，用**法律审查员**身份登录（`legal_reviewer`）。

## 1. 模块 1：待办列表（`/`）

- 列表按 `updated_at` 倒序；用 `HT-2026-0001` 演示。
- **看什么**：状态列的语义（`blocked` 与 `pending` 不是一回事）、
  我方立场徽标、回写状态。列表不给正文（正文只进详情，不进列表/URL）。

## 2. 模块 2：任务详情（`/tasks/{id}`）

- 权威上下文（`context_status`）与我方立场（`party_a` + `buyer`）；
- 人工修正表单：掩码按字段类型走（金额只能数字），改完**保存即生效**；
- 冲突对照：审批系统给的 vs 人工改的，两列并排；
- **不能做的**：修正后的立场重跑规则才生效 —— 界面明说，不假装已生效。

## 3. 模块 3：解析与证据（详情页 `?tab=parse`）

- 触发解析 → 轮询到 `succeeded`；
- 左栏字段四态：`✓ 已提取 / ∅ 未发现 / ⚠ 不确定 / ✗ 解析失败` ——
  **后两种不是"无数据"**；
- 点「定位」→ PDF 翻到证据页并画框（字符级按行合并）；
- 点框 → 选中字段（双向）；
- 无坐标的页自动降级为文本定位，界面**明说**"画不出证据框"。

## 4. 模块 4：规则命中（`?tab=rules`)

- 批次头：总风险 / 结论完整性 / 四态计数（**来自同一个响应**，数字与列表不会对不上）；
- 筛选默认只看 `hit` 与 `needs_review`（不噪音轰炸）；
- 每条给出：结论 + 理由 + 证据文本 + 「查看原文」深链（只带页码与块号，不带正文）。

## 5. 模块 5：结果处理（`?tab=result`）

- 保存：口径=聚合（总风险/关注点与模块 4 一致），**带内容摘要**；
- 人工确认 → `confirmation_valid` 翻真；
- **改正文（薄出口）**：`POST /api/results/{id}/comment` → 新版本 + 旧确认**自动失效** ——
  界面立即显示"需重新确认"，回写被门禁拦下（`MANUAL_CONFIRM_REQUIRED`）；
- 回写：登记意图 → 派发器送达 → 任务 `done`；幂等键相同重放 = 复用，不重复写。

## 6. 权限演示

- 用 `read_only_auditor` 重复步骤 4 的保存 → 403，界面给出"缺什么权限、找谁"。

## 7. 已知未覆盖

| 项 | 状态 | 原因 |
| --- | --- | --- |
| Playwright 截图（五模块 / 高亮框 / 拒绝页 / 失效确认 / 回写成功） | 未拍 | 浏览器二进制未安装；spec 在 `frontend/e2e/`，装好后 `npx playwright test e2e/` |
| `PdfPageCanvas` 的 canvas 渲染 | 无单测 | jsdom 无 canvas 实现；由 Playwright 截图覆盖 |
