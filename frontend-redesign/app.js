/* ============================================================
   合同审批审查系统 · 前端重设计（静态原型）
   演示数据 + 五模块渲染与交互
   ============================================================ */
"use strict";

/* ---------------- 演示数据（对应 mock 审批系统 6 条待办） ---------------- */

var SUMMARY = [
  { label: "全部待办", num: 6, color: "#2f54eb" },
  { label: "审查中", num: 1, color: "#6941c6" },
  { label: "需人工处理", num: 2, color: "#b54708" },
  { label: "已完成", num: 3, color: "#067647" },
  { label: "回写失败", num: 1, color: "#cf1322" }
];

var TASK_STATUS = {
  pending:   { text: "待办",   cls: "b-neutral" },
  parsing:   { text: "解析中", cls: "b-info" },
  reviewing: { text: "审查中", cls: "b-violet" },
  blocked:   { text: "阻塞",   cls: "b-warn" },
  done:      { text: "已完成", cls: "b-ok" }
};

var WRITE_STATUS = {
  not_written: { text: "未回写",   cls: "b-neutral" },
  writing:     { text: "回写中",   cls: "b-info" },
  success:     { text: "回写成功", cls: "b-ok" },
  failed:      { text: "回写失败", cls: "b-danger" }
};

var RISK = {
  "高": { cls: "b-danger", hero: "high" },
  "中": { cls: "b-warn",   hero: "mid" },
  "低": { cls: "b-ok",     hero: "low" }
};

var TASKS = [
  {
    id: 102, code: "HT-2026-0002", title: "原材料采购合同（苏州精工）", applicant: "李四",
    attachments: 1, taskStatus: "done", risk: "高",
    writeStatus: "not_written", reasonCode: "WRITEBACK_POLICY_DENIED", reasonText: "门禁未通过：高风险结果尚未人工确认",
    updated: "09-14 10:22"
  },
  {
    id: 106, code: "HT-2026-0006", title: "设备租赁合同（华东物流园）", applicant: "陈明",
    attachments: 1, taskStatus: "reviewing", risk: null,
    writeStatus: "not_written", reasonCode: null, reasonText: "等待审查结果",
    contextStatus: "conflict", updated: "09-14 09:58"
  },
  {
    id: 105, code: "HT-2026-0005", title: "运维服务合同（年度框架）", applicant: "王五",
    attachments: 2, taskStatus: "blocked", risk: null,
    writeStatus: "not_written", reasonCode: null, reasonText: null,
    blockedStage: "download", errorCode: "ATTACHMENT_MISSING",
    blockReason: "附件已被审批系统删除，无法下载", updated: "09-14 09:31"
  },
  {
    id: 103, code: "HT-2026-0003", title: "软件开发服务合同", applicant: "王五",
    attachments: 1, taskStatus: "done", risk: "中",
    writeStatus: "failed", reasonCode: "APPROVAL_API_ERROR", reasonText: "审批系统接口超时（第 2 次尝试）",
    updated: "09-13 17:40"
  },
  {
    id: 101, code: "HT-2026-0001", title: "办公设备采购合同", applicant: "张三",
    attachments: 1, taskStatus: "done", risk: "低",
    writeStatus: "success", reasonCode: null, reasonText: null,
    updated: "09-13 16:05"
  },
  {
    id: 104, code: "HT-2026-0004", title: "标准品采购合同（办公耗材）", applicant: "赵六",
    attachments: 1, taskStatus: "done", risk: "低",
    writeStatus: "success", reasonCode: null, reasonText: null,
    updated: "09-13 15:12"
  }
];

/* 任务详情（重点演示 0002 / 0005 / 0006，其余给通用骨架） */
var DETAILS = {
  102: {
    context: {
      status: "complete",
      party: "华云智造科技有限公司", label: "采购合同",
      role: "采购方 / 付款方", type: "货物买卖"
    },
    form: [
      ["申请人", "李四"], ["申请部门", "采购部"], ["合同金额", "¥1,200,000.00"],
      ["对方主体", "苏州精工机械有限公司"], ["紧急程度", "普通"],
      ["联系电话", "138****6621|13812346621"], ["经办人证件号", "3201**********1234|320102199001011234"],
      ["事由", "年度生产原材料集中采购，含首批预付款安排"]
    ],
    attachments: [
      { name: "contract_02_prepay.pdf", size: "842 KB", type: "PDF", status: "success", sha: "9f2ac4e1b7d3" }
    ],
    parse: {
      file: "contract_02_prepay.pdf", pages: 6, version: "v2", versions: ["v2", "v1"],
      coverage: "0.97", ocrPages: 0,
      fields: [
        { name: "合同编号", value: "HT-CG-2026-0188", state: "extracted", page: 1, bbox: { top: 16, left: 8, width: 46, height: 4.6 } },
        { name: "合同总金额", value: "¥1,200,000.00（人民币壹佰贰拾万元整）", state: "extracted", page: 2, bbox: { top: 25, left: 8, width: 72, height: 4.6 } },
        { name: "预付款比例", value: "60%（合同签订后 15 日内支付）", state: "extracted", page: 2, bbox: { top: 42, left: 8, width: 64, height: 4.6 } },
        { name: "付款周期", state: "uncertain", note: "候选证据 2 处表述矛盾，需人工判断", page: 3, bbox: { top: 31, left: 8, width: 68, height: 4.6 } },
        { name: "验收条款", value: "到货后 10 个工作日内完成验收", state: "extracted", page: 4, bbox: { top: 24, left: 8, width: 70, height: 4.6 } },
        { name: "保密条款", value: "已约定，保密期限 3 年", state: "extracted", page: 5, bbox: { top: 36, left: 8, width: 58, height: 4.6 } },
        { name: "争议解决条款", state: "not_found", note: "文档可检索但未发现相关条款" },
        { name: "到期时间", state: "failed", note: "第 6 页 OCR 置信度过低（0.41），本字段解析失败" }
      ]
    },
    run: {
      batch: "R-2026-0914-03", time: "2026-09-14 10:22",
      parseVersion: "v2", ruleVersion: "v12", model: "qwen-plus · 提示词 v3",
      counts: { needs_review: 2, hit: 3, not_hit: 4, not_applicable: 31 },
      needsReview: [
        { name: "付款周期超过 60 天", level: "中", reasonCode: "EVIDENCE_INSUFFICIENT",
          reason: "证据不足：付款周期存在两处矛盾表述，无法确定适用值",
          evidences: [
            { text: "验收合格后 30 日内支付尾款", page: 3 },
            { text: "收到合规发票后 90 日内支付", page: 4 }
          ] },
        { name: "管辖地约定不明", level: "低", reasonCode: "CLAUSE_AMBIGUOUS",
          reason: "争议解决条款未找到，无法判断管辖地是否对我方不利",
          evidences: [] }
      ],
      hits: [
        { name: "预付款比例超过 30%（采购方）", level: "高", calc: "60% > 30%",
          evidences: [{ text: "合同签订后 15 日内，甲方向乙方支付合同总金额的 60% 作为预付款", page: 2 }] },
        { name: "自动续约条款", level: "中", calc: null,
          evidences: [{ text: "合同期满前 30 日内双方均未书面提出异议的，本合同自动续展一年", page: 5 }] },
        { name: "违约金上限缺失", level: "中", calc: null,
          evidences: [{ text: "违约方应承担由此给对方造成的全部损失", page: 5 }] }
      ],
      notHits: ["保密条款缺失", "数据处理条款缺失", "付款周期超过 60 天（确定值）", "主体信息缺失"],
      notApplicableCount: 31
    },
    result: {
      risk: "高", integrity: "需人工判断（2 条待判断）",
      summary: "本合同为我方作为采购方的原材料采购合同（对方：苏州精工机械有限公司），合同总额 ¥1,200,000.00。审查命中 3 项风险、2 项待人工判断，风险集中在预付款安排、自动续约与违约责任条款。",
      focus: [
        "预付款比例 60%，高于内部 30% 标准，且我方为付款方，资金占用与履约风险高",
        "含自动续约条款，期满前 30 日未书面异议即自动续展一年",
        "违约金未设上限，责任敞口大",
        "付款周期两处表述矛盾（30 日 / 90 日），需与供应商书面确认"
      ],
      comment: "【风险审查意见】经系统审查，本合同总风险等级为「高」：\n1）预付款比例 60%，超过内部标准（30%）；\n2）存在自动续约条款；\n3）违约金未约定上限；\n4）付款周期条款存在矛盾表述，另 2 项规则需人工判断。\n建议审批人重点确认预付款安排与付款周期，并要求供应商明确违约金上限后再行签署。",
      contentDigest: "a3f1c8e2",
      confirmedDigest: null, confirmedBy: null, confirmedAt: null,
      writeStatus: "not_written", reasonCode: "WRITEBACK_POLICY_DENIED",
      reasonText: "暂不可回写：高风险结果尚未人工确认"
    }
  },

  105: {
    context: {
      status: "complete",
      party: "华云智造科技有限公司", label: "服务合同",
      role: "采购方 / 甲方", type: "运维服务"
    },
    form: [
      ["申请人", "王五"], ["申请部门", "信息技术部"], ["合同金额", "¥360,000.00"],
      ["对方主体", "蓝鲸运维服务有限公司"], ["紧急程度", "高"],
      ["事由", "数据中心年度运维服务续签"]
    ],
    attachments: [
      { name: "contract_05_scan.pdf", size: "1.2 MB", type: "PDF", status: "success", sha: "77de0b9a41c2" },
      { name: "contract_05_annex.pdf", size: "—", type: "PDF", status: "failed",
        error: "审批系统返回：附件已被删除（ATTACHMENT_MISSING）" }
    ],
    blocked: true
  },

  106: {
    context: {
      status: "conflict",
      party: "华云智造科技有限公司", label: "租赁合同",
      role: "未知（立场冲突）", type: "设备租赁",
      conflict: {
        declared: { who: "审批单声明（表单）", val: "我方为乙方 · 承租方" },
        actual: { who: "附件正文（合同首部）", val: "我方为甲方 · 出租方" }
      }
    },
    form: [
      ["申请人", "陈明"], ["申请部门", "资产管理部"], ["合同金额", "¥88,000.00"],
      ["对方主体", "华东物流园开发有限公司"], ["紧急程度", "普通"],
      ["事由", "仓库扫码设备租赁，租期 12 个月"]
    ],
    attachments: [
      { name: "contract_06_lease.pdf", size: "663 KB", type: "PDF", status: "success", sha: "b10e55f892a1" }
    ]
  }
};

/* 通用骨架详情（0001 / 0003 / 0004） */
function genericDetail(task) {
  return {
    context: { status: "confirmed", party: "华云智造科技有限公司", label: "采购合同", role: "采购方 / 甲方", type: "货物买卖", confirmedBy: "王敏" },
    form: [["申请人", task.applicant], ["合同编号", task.code], ["事由", task.title]],
    attachments: [{ name: "contract_" + task.code.slice(-4) + ".pdf", size: "512 KB", type: "PDF", status: "success", sha: "aa01bb23cc45" }]
  };
}
DETAILS[101] = genericDetail(TASKS[4]);
DETAILS[103] = genericDetail(TASKS[3]);
DETAILS[104] = genericDetail(TASKS[5]);

/* 规则管理演示数据 */
var RULES = [
  { code: "PREPAY_RATIO_BUYER", name: "预付款比例超过 30%（采购方）", level: "高", mode: "expr", scope: "采购类 · 买方", version: "v12", on: true },
  { code: "IP_MISSING_DEV", name: "知识产权条款缺失（软件开发类）", level: "中", mode: "keyword", scope: "软件开发", version: "v11", on: true },
  { code: "AUTO_RENEW", name: "自动续约条款", level: "中", mode: "keyword", scope: "全局", version: "v12", on: true },
  { code: "JURISDICTION_ADVERSE", name: "管辖地约定不利", level: "低", mode: "llm", scope: "全局", version: "v12", on: true },
  { code: "PENALTY_CAP_MISSING", name: "违约金上限缺失", level: "中", mode: "llm", scope: "全局", version: "v12", on: false },
  { code: "CONFIDENTIAL_MISSING", name: "保密条款缺失（白名单类）", level: "中", mode: "keyword", scope: "技术/定制类", version: "v10", on: true },
  { code: "DATA_PROCESSING", name: "数据处理条款缺失", level: "高", mode: "llm", scope: "涉及个人信息", version: "v9", on: true },
  { code: "ACCEPTANCE_MISSING", name: "验收标准缺失", level: "中", mode: "keyword", scope: "服务类", version: "v11", on: true }
];

/* 运行管理演示数据 */
var JOBS = [
  { id: 486, type: "WRITEBACK", task: "HT-2026-0003", status: "retry_wait", attempt: "2 / 3", next: "09-14 10:35", error: "APPROVAL_API_ERROR" },
  { id: 481, type: "DOWNLOAD", task: "HT-2026-0005", status: "failed", attempt: "3 / 3", next: "—", error: "ATTACHMENT_MISSING" },
  { id: 479, type: "RULE", task: "HT-2026-0002", status: "succeeded", attempt: "1 / 3", next: "—", error: "—" },
  { id: 478, type: "PARSE", task: "HT-2026-0002", status: "succeeded", attempt: "1 / 3", next: "—", error: "—" },
  { id: 477, type: "PARSE", task: "HT-2026-0006", status: "succeeded", attempt: "1 / 3", next: "—", error: "—" }
];

var LOGS = [
  { level: "ERROR", type: "writeback", cid: "c4f8a2e1", content: "write_approval_comment 审批系统 504，进入第 2 次重试", time: "10:31:12" },
  { level: "WARN", type: "download", cid: "9d21bc07", content: "attachment contract_05_annex.pdf 已被外部删除，标记 blocked", time: "09:31:44" },
  { level: "INFO", type: "rule", cid: "f072e6a9", content: "review_run R-2026-0914-03 完成：40 条规则各产生 1 条评价", time: "10:22:03" },
  { level: "INFO", type: "parse", cid: "f072e6a9", content: "parse_version=v2 缓存命中，跳过 OCR（sha256 9f2ac4e1…）", time: "10:18:57" },
  { level: "INFO", type: "pull", cid: "1ab3d5c2", content: "list_pending 拉取 6 条，created=0 updated=6（去重命中）", time: "09:00:11" }
];

var AUDITS = [
  { action: "COMMENT_WRITTEN", text: "评论回写成功 → HT-2026-0001", who: "系统（Outbox）", time: "09-13 16:05", cid: "7e90c1d4" },
  { action: "RESULT_CONFIRMED", text: "人工确认审查结果（HT-2026-0004 · 低风险）", who: "王敏", time: "09-13 15:10", cid: "2bf64a08" },
  { action: "CONTEXT_CONFIRMED", text: "人工确认权威上下文（HT-2026-0004）", who: "王敏", time: "09-13 15:08", cid: "2bf64a08" },
  { action: "RULE_UPDATED", text: "停用规则 PENALTY_CAP_MISSING（v12）", who: "系统管理员", time: "09-13 11:26", cid: "e5c17b93" },
  { action: "TASK_RETRIED", text: "人工重试 HT-2026-0005，从「下载」检查点恢复 · 原因：联系申请人重新上传", who: "王敏", time: "09-13 10:02", cid: "9d21bc07" }
];

/* ---------------- 工具 ---------------- */

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function badge(mapVal, fallback) {
  if (!mapVal) return '<span class="sub">' + (fallback || "—") + "</span>";
  return '<span class="badge ' + mapVal.cls + '"><span class="dot"></span>' + mapVal.text + "</span>";
}

function riskBadge(risk) {
  if (!risk) return '<span class="sub" title="尚未产生审查批次">—</span>';
  return '<span class="badge ' + RISK[risk].cls + '">' + risk + "风险</span>";
}

var ICON_WARN = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>';
var ICON_INFO = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/></svg>';
var ICON_OK = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>';

/* ---------------- 导航 ---------------- */

var state = { view: "tasks", taskId: null, tab: "detail", parsePage: 1, hlField: null };

var CRUMBS = { tasks: "待办调用", workbench: "任务工作台", rules: "规则管理", ops: "运行管理" };

function switchView(name) {
  state.view = name;
  document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
  document.getElementById("view-" + name).classList.add("active");
  document.querySelectorAll(".nav-item").forEach(function (n) {
    n.classList.toggle("active", n.dataset.nav === (name === "workbench" ? "tasks" : name));
  });
  document.getElementById("crumb").textContent = CRUMBS[name];
  if (name === "workbench") {
    var t = TASKS.find(function (x) { return x.id === state.taskId; });
    document.getElementById("crumb").innerHTML =
      '待办调用 / <b>' + esc(t.code) + "</b>";
  }
}

document.querySelectorAll(".nav-item").forEach(function (n) {
  n.addEventListener("click", function () { switchView(n.dataset.nav); });
});

/* ---------------- 模块 1：待办调用 ---------------- */

function writeStatusCell(t) {
  var html = badge(WRITE_STATUS[t.writeStatus]);
  var reason = "";
  if (t.reasonCode) {
    reason = '<span class="reason"><code>' + esc(t.reasonCode) + "</code> " + esc(t.reasonText || "") + "</span>";
  } else if (t.reasonText) {
    reason = '<span class="reason">' + esc(t.reasonText) + "</span>";
  }
  return '<div class="status-reason">' + html + reason + "</div>";
}

function renderTasks() {
  var summaryHtml = SUMMARY.map(function (m) {
    return '<div class="metric" style="--mc:' + m.color + '">' +
      '<div class="metric-num">' + m.num + '</div>' +
      '<div class="metric-label">' + esc(m.label) + "</div></div>";
  }).join("");

  var rows = TASKS.map(function (t) {
    var blocked = t.taskStatus === "blocked"
      ? '<div class="block-reason">' + ICON_WARN +
        "<span>阻塞于「" + esc(t.blockedStage) + "」 · <code>" + esc(t.errorCode) + "</code><br>" +
        esc(t.blockReason) + "</span></div>"
      : "";
    return "<tr data-task=\"" + t.id + "\">" +
      '<td><span class="mono"><b>' + esc(t.code) + "</b></span>" + blocked + "</td>" +
      "<td>" + esc(t.title) + '<div class="sub">更新 ' + esc(t.updated) + "</div></td>" +
      "<td>" + esc(t.applicant) + "</td>" +
      "<td>" + t.attachments + "</td>" +
      "<td>" + badge(TASK_STATUS[t.taskStatus]) + "</td>" +
      "<td>" + riskBadge(t.risk) + "</td>" +
      "<td>" + writeStatusCell(t) + "</td>" +
      "</tr>";
  }).join("");

  document.getElementById("view-tasks").innerHTML =
    '<div class="summary-grid">' + summaryHtml + "</div>" +
    '<div class="toolbar">' +
      '<select><option>状态：全部</option><option>待办</option><option>审查中</option><option>阻塞</option><option>已完成</option></select>' +
      '<select><option>回写：全部</option><option>未回写</option><option>回写成功</option><option>回写失败</option></select>' +
      '<select><option>阻塞原因：全部</option><option>ATTACHMENT_MISSING</option><option>OCR_UNRECOGNIZABLE</option></select>' +
      '<input type="search" placeholder="搜索编号 / 标题 / 申请人" />' +
      '<span class="spacer"></span>' +
      '<button class="btn btn-primary" id="btnPull">从审批系统拉取待办</button>' +
    "</div>" +
    '<div class="card"><table class="table"><thead><tr>' +
    "<th>审批编号</th><th>标题</th><th>申请人</th><th>附件</th><th>状态</th><th>总风险</th><th>回写（状态 · 原因）</th>" +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>";

  document.querySelectorAll("#view-tasks tr[data-task]").forEach(function (tr) {
    tr.addEventListener("click", function () {
      openWorkbench(parseInt(tr.dataset.task, 10));
    });
  });

  document.getElementById("btnPull").addEventListener("click", function () {
    var btn = this;
    btn.disabled = true; btn.textContent = "拉取中…";
    setTimeout(function () {
      btn.disabled = false; btn.textContent = "从审批系统拉取待办";
      alert("拉取完成：fetched=6 · created=0 · updated=6（按 provider+tenant+instance 去重）");
    }, 700);
  });
}

/* ---------------- 任务工作台 ---------------- */

function openWorkbench(taskId) {
  state.taskId = taskId;
  state.tab = "detail";
  state.parsePage = 1;
  state.hlField = null;
  renderWorkbench();
  switchView("workbench");
}

function renderWorkbench() {
  var t = TASKS.find(function (x) { return x.id === state.taskId; });
  var d = DETAILS[t.id];

  var headChips =
    badge(TASK_STATUS[t.taskStatus]) +
    (t.risk ? '<span class="badge ' + RISK[t.risk].cls + '">总风险 ' + t.risk + "</span>" : "") +
    badge(WRITE_STATUS[t.writeStatus]);

  var blockedNotice = t.taskStatus === "blocked"
    ? '<div class="notice notice-warn">' + ICON_WARN +
      "<div><b>任务阻塞（业务结论，待人工处理）</b><br>" +
      "阻塞阶段「" + esc(t.blockedStage) + "」 · 错误码 <code>" + esc(t.errorCode) + "</code> · " + esc(t.blockReason) +
      '<div style="margin-top:8px"><button class="btn btn-sm" id="btnRetry">从检查点重试</button></div>' +
      "</div></div>"
    : "";

  var tabs = [
    ["detail", "详情查看", null],
    ["parse", "解析结果", d.parse ? d.parse.fields.length : null],
    ["rules", "规则命中", d.run ? (d.run.counts.hit + d.run.counts.needs_review) : null],
    ["result", "结果处理", d.result ? d.result.focus.length : null]
  ].map(function (tb) {
    return '<button class="tab' + (state.tab === tb[0] ? " active" : "") + '" data-tab="' + tb[0] + '">' +
      tb[1] + (tb[2] != null ? '<span class="cnt">' + tb[2] + "</span>" : "") + "</button>";
  }).join("");

  document.getElementById("view-workbench").innerHTML =
    '<div class="wb-head">' +
      "<div>" +
        '<div class="wb-title">' + esc(t.title) + "</div>" +
        '<div class="wb-meta"><span class="mono">' + esc(t.code) + "</span>" +
        "<span>申请人 " + esc(t.applicant) + "</span><span>附件 " + t.attachments + " 个</span>" +
        "<span>更新 " + esc(t.updated) + "</span></div>" +
      "</div>" +
      '<div class="wb-chips">' + headChips + "</div>" +
    "</div>" +
    blockedNotice +
    '<div class="tabs">' + tabs + "</div>" +
    '<div id="wbPane"></div>';

  document.querySelectorAll("#view-workbench .tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      state.tab = tab.dataset.tab;
      renderWorkbench();
    });
  });

  var retry = document.getElementById("btnRetry");
  if (retry) retry.addEventListener("click", function () {
    alert("已重新入队（作业 #487），从「" + t.blockedStage + "」检查点恢复。\n操作人与原因已写入审计事件。");
  });

  renderTabPane(t, d);
}

function renderTabPane(t, d) {
  var pane = document.getElementById("wbPane");
  if (state.tab === "detail") pane.innerHTML = paneDetail(t, d);
  else if (state.tab === "parse") pane.innerHTML = paneParse(d);
  else if (state.tab === "rules") pane.innerHTML = paneRules(d);
  else pane.innerHTML = paneResult(t, d);
  bindPaneEvents(t, d);
}

/* ---- 模块 2：详情查看 ---- */

var CONTEXT_MAP = {
  complete:  { cls: "b-ok",      text: "完整 · 来自审批系统" },
  missing:   { cls: "b-warn",    text: "缺失 · 立场未知" },
  conflict:  { cls: "b-warn",    text: "冲突 · 需人工裁定" },
  confirmed: { cls: "b-info",    text: "已人工确认" }
};

function paneDetail(t, d) {
  var c = d.context;
  var ctxBadge = badge(CONTEXT_MAP[c.status]);
  var ctxNote = "";
  if (c.status === "missing") {
    ctxNote = '<div class="notice notice-warn">' + ICON_WARN +
      "<div><b>权威审查上下文缺失</b>：立场未知，方向敏感的规则无法可靠判断，请先人工确认。</div></div>";
  }
  var conflictBox = "";
  if (c.status === "conflict" && c.conflict) {
    ctxNote = '<div class="notice notice-warn">' + ICON_WARN +
      "<div><b>审批单声明与附件正文不一致</b>，方向敏感规则已暂停定论，需人工裁定立场。</div></div>";
    conflictBox =
      '<div class="conflict-box">' +
        "<b>立场冲突双方</b>" +
        '<div class="conflict-vs">' +
          '<div class="conflict-side"><div class="who">' + esc(c.conflict.declared.who) + '</div><div class="val">' + esc(c.conflict.declared.val) + "</div></div>" +
          '<div class="vs">VS</div>' +
          '<div class="conflict-side"><div class="who">' + esc(c.conflict.actual.who) + '</div><div class="val">' + esc(c.conflict.actual.val) + "</div></div>" +
        "</div>" +
        '<div style="margin-top:10px"><button class="btn btn-sm btn-primary" id="btnCtxConfirm">人工确认：以附件正文为准</button></div>' +
      "</div>";
  }

  var formRows = d.form.map(function (kv) {
    var val = String(kv[1]);
    if (val.indexOf("|") > -1) {
      var parts = val.split("|");
      return "<dt>" + esc(kv[0]) + '</dt><dd><span class="masked" data-masked="' + esc(parts[0]) +
        '" data-full="' + esc(parts[1]) + '">' + esc(parts[0]) + "</span>" +
        '<button class="mask-toggle">显示</button></dd>';
    }
    return "<dt>" + esc(kv[0]) + "</dt><dd>" + esc(kv[1]) + "</dd>";
  }).join("");

  var attachRows = d.attachments.map(function (a) {
    var failed = a.status === "failed";
    var statusHtml = failed
      ? '<span class="badge b-danger">下载失败</span>'
      : '<span class="badge b-ok">已下载</span>';
    var meta = failed
      ? '<div class="attach-meta" style="color:var(--danger)">' + esc(a.error) + "</div>"
      : '<div class="attach-meta">' + a.size + " · " + a.type + ' · SHA-256 <span class="mono">' + a.sha + "…</span></div>";
    return '<div class="attach-row' + (failed ? " failed" : "") + '">' +
      '<div class="attach-icon">PDF</div>' +
      "<div><div class=\"attach-name\">" + esc(a.name) + "</div>" + meta + "</div>" +
      '<div class="attach-actions">' + statusHtml +
      (failed
        ? '<button class="btn btn-sm">重新下载</button>'
        : '<button class="btn btn-sm">预览</button>') +
      "</div></div>";
  }).join("");

  return ctxNote +
    '<div class="detail-grid">' +
      '<div class="card card-pad">' +
        '<div class="card-title">权威审查上下文 ' + ctxBadge + "</div>" +
        '<dl class="kv">' +
          "<dt>我方主体</dt><dd>" + esc(c.party) + "</dd>" +
          "<dt>合同标签</dt><dd>" + esc(c.label) + "</dd>" +
          "<dt>业务角色</dt><dd>" + esc(c.role) + "</dd>" +
          "<dt>合同类型</dt><dd>" + esc(c.type) + "</dd>" +
        "</dl>" +
        (c.confirmedBy ? '<div class="sub" style="margin-top:8px">确认人：' + esc(c.confirmedBy) + "</div>" : "") +
        conflictBox +
      "</div>" +
      '<div class="card card-pad">' +
        '<div class="card-title">审批表单（敏感值默认掩码）</div>' +
        '<dl class="kv">' + formRows + "</dl>" +
      "</div>" +
      '<div class="card card-pad span2">' +
        '<div class="card-title">附件列表</div>' + attachRows +
      "</div>" +
    "</div>";
}

/* ---- 模块 3：解析结果 ---- */

var FIELD_STATE = {
  extracted: { cls: "b-ok",      icon: "✓", text: "已提取" },
  not_found: { cls: "b-neutral", icon: "∅", text: "未发现" },
  uncertain: { cls: "b-warn",    icon: "⚠", text: "不确定" },
  failed:    { cls: "b-danger",  icon: "✗", text: "解析失败" }
};

function paneParse(d) {
  if (!d.parse) {
    return '<div class="card empty"><div class="big">暂无解析结果</div>' +
      "任务尚未进入解析阶段，或解析因阻塞未产出可用版本。</div>";
  }
  var p = d.parse;

  var fields = p.fields.map(function (f, i) {
    var st = FIELD_STATE[f.state];
    var canLocate = !!f.bbox;
    return '<div class="field-item" data-field="' + i + '">' +
      '<div class="field-head">' +
        '<span class="field-name">' + esc(f.name) + "</span>" +
        '<span class="badge ' + st.cls + '">' + st.icon + " " + st.text + "</span>" +
        (canLocate
          ? '<button class="locate" data-locate="' + i + '">定位 →</button>'
          : '<button class="locate" disabled title="无可用坐标证据">定位 →</button>') +
      "</div>" +
      (f.value ? '<div class="field-value">' + esc(f.value) + "</div>" : "") +
      (f.note ? '<div class="field-note">' + esc(f.note) + "</div>" : "") +
      (f.page ? '<div class="field-note">证据：第 ' + f.page + " 页</div>" : "") +
    "</div>";
  }).join("");

  var lines = "";
  var widths = ["w100", "w85", "w100", "w70", "w100", "w55", "w85", "w100", "w40", "w100", "w85", "w70"];
  for (var i = 0; i < widths.length; i++) lines += '<div class="pdf-line ' + widths[i] + '"></div>';

  var versionOpts = p.versions.map(function (v) {
    return "<option" + (v === p.version ? " selected" : "") + ">" + v + "</option>";
  }).join("");

  return '<div class="parse-layout">' +
    "<div>" +
      '<div class="parse-meta">' +
        '解析版本 <select id="parseVersion">' + versionOpts + "</select>" +
        "<span>文本覆盖率 " + p.coverage + "</span><span>OCR " + p.ocrPages + " 页</span>" +
      "</div>" +
      '<div id="fieldList">' + fields + "</div>" +
    "</div>" +
    '<div class="pdf-shell">' +
      '<div class="pdf-toolbar">' +
        "<span>" + esc(p.file) + "</span>" +
        '<span class="pg" id="pgIndicator">1 / ' + p.pages + " 页</span>" +
        "<button data-pg=\"-1\">‹</button><button data-pg=\"1\">›</button>" +
      "</div>" +
      '<div class="pdf-page" id="pdfPage">' +
        '<div class="pdf-line title"></div>' + lines +
        '<div class="pdf-hl" id="pdfHl" data-label="" style="display:none"></div>' +
      "</div>" +
      '<div class="pdf-caption">坐标系 <code>pdf-point-bottom-left</code> · 已按页面旋转角校正 · 证据框与字段双向联动</div>' +
    "</div>" +
  "</div>";
}

/* ---- 模块 4：规则命中 ---- */

function ruleItem(r, showLevel) {
  var evidences = (r.evidences || []).map(function (e) {
    return '<div class="rule-evidence">证据：<q>' + esc(e.text) + "</q>" +
      '<span class="pg">第 ' + e.page + ' 页</span>' +
      '<button class="locate" style="margin-left:8px">查看原文</button></div>';
  }).join("");
  return '<div class="rule-item" style="--rc:' + (r.level === "高" ? "var(--danger)" : r.level === "中" ? "var(--warn)" : "var(--info)") + '">' +
    '<div class="rule-item-head">' +
      '<span class="name">' + esc(r.name) + "</span>" +
      (showLevel !== false && r.level ? '<span class="badge ' + RISK[r.level].cls + '">' + r.level + "</span>" : "") +
      '<span class="acts"><button class="btn btn-sm">查看原文</button></span>' +
    "</div>" +
    (r.reasonCode
      ? '<div class="rule-reason">' + ICON_WARN + "原因：" + esc(r.reason) + " <code>" + esc(r.reasonCode) + "</code></div>"
      : "") +
    (r.calc ? '<div class="rule-calc">计算过程：' + esc(r.calc) + "</div>" : "") +
    evidences +
  "</div>";
}

function ruleGroup(title, cls, count, bodyHtml, open) {
  return '<div class="rule-group">' +
    '<button class="rule-group-head' + (open ? "" : " closed") + '" data-group>' +
      '<svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M6 9l6 6 6-6"/></svg>' +
      title + '<span class="cnt badge ' + cls + '">' + count + "</span>" +
    "</button>" +
    '<div class="rule-group-body' + (open ? "" : " closed") + '">' + bodyHtml + "</div>" +
  "</div>";
}

function paneRules(d) {
  if (!d.run) {
    return '<div class="card empty"><div class="big">暂无规则评价</div>' +
      "尚未执行规则审查批次，或审查因上下文冲突等待人工裁定。</div>";
  }
  var r = d.run;
  var meta = '<div class="parse-meta" style="margin-bottom:14px">' +
    "<span>批次 <b>" + esc(r.batch) + "</b>（" + esc(r.time) + "）</span>" +
    "<span>解析 " + esc(r.parseVersion) + "</span><span>规则 " + esc(r.ruleVersion) + "</span><span>" + esc(r.model) + "</span>" +
    '<span style="margin-left:auto">命中 ' + r.counts.hit + " · 待判断 " + r.counts.needs_review +
    " · 未命中 " + r.counts.not_hit + " · 不适用 " + r.counts.not_applicable + "</span>" +
  "</div>";

  var needsHtml = r.needsReview.map(function (x) { return ruleItem(x, false); }).join("");
  var hitsHtml = r.hits.map(function (x) { return ruleItem(x, true); }).join("");
  var notHitHtml = r.notHits.map(function (n) {
    return '<div class="rule-item" style="--rc:var(--border-strong)"><div class="rule-item-head"><span class="name">' +
      esc(n) + '</span><span class="badge b-neutral">未命中</span></div></div>';
  }).join("");
  var naHtml = '<div class="card card-pad" style="box-shadow:none"><span class="sub">' +
    "其余 " + r.counts.not_applicable + " 条规则经适用性判断不适用本合同（如 IP_MISSING 对标准品采购），此处折叠展示。</span></div>";

  return meta +
    ruleGroup("需人工判断（结论需要你来做）", "b-warn", r.counts.needs_review, needsHtml, true) +
    ruleGroup("命中", "b-danger", r.counts.hit, hitsHtml, true) +
    ruleGroup("未命中", "b-neutral", r.counts.not_hit, notHitHtml, false) +
    ruleGroup("不适用", "b-neutral", r.counts.not_applicable, naHtml, false);
}

/* ---- 模块 5：结果处理 ---- */

function paneResult(t, d) {
  if (!d.result) {
    return '<div class="card empty"><div class="big">暂无审查结果</div>' +
      "结果尚未生成。上下文冲突或任务阻塞时，请先在「详情查看」完成人工处理。</div>";
  }
  var r = d.result;
  var risk = RISK[r.risk];

  var focusItems = r.focus.map(function (f) { return "<li>" + esc(f) + "</li>"; }).join("");

  var confirmed = !!r.confirmedDigest;
  var digestMatch = confirmed && r.confirmedDigest === r.contentDigest;

  var digestLine = confirmed
    ? (digestMatch
        ? '<span class="badge b-ok">' + ICON_OK + ' 与确认摘要相同</span>'
        : '<span class="badge b-warn">' + ICON_WARN + ' 正文已变更，确认已失效</span>')
    : '<span class="badge b-neutral">尚未人工确认</span>';

  var gateNotice = r.writeStatus === "not_written" && r.reasonCode === "WRITEBACK_POLICY_DENIED"
    ? '<div class="notice notice-neutral">' + ICON_INFO +
      "<div><b>暂不可回写</b>：" + esc(r.reasonText) + "（<code>" + esc(r.reasonCode) + "</code>）。这是正常的业务约束，完成确认后即可回写。</div></div>"
    : "";
  var failNotice = r.writeStatus === "failed"
    ? '<div class="notice notice-danger">' + ICON_WARN +
      "<div><b>回写失败</b>：<code>" + esc(r.reasonCode) + "</code> " + esc(r.reasonText) +
      "。系统已按指数退避重试，可在运行管理中查看作业。</div></div>"
    : "";
  var okNotice = r.writeStatus === "success"
    ? '<div class="notice notice-ok">' + ICON_OK + "<div><b>评论已成功写回审批系统。</b></div></div>"
    : "";

  return '<div class="result-hero">' +
      '<div class="hero-box"><div class="lab">总风险等级（取全部命中最高级）</div>' +
        '<div class="hero-risk ' + risk.hero + '">' + r.risk + ' 风险</div></div>' +
      '<div class="hero-box"><div class="lab">结果完整性</div>' +
        '<div class="hero-risk" style="font-size:16px;color:var(--warn)">' + esc(r.integrity) + "</div></div>" +
    "</div>" +
    gateNotice + failNotice + okNotice +
    '<div class="detail-grid">' +
      '<div class="card card-pad span2"><div class="card-title">中文摘要</div>' +
        '<div style="font-size:13.5px">' + esc(r.summary) + "</div></div>" +
      '<div class="card card-pad"><div class="card-title">审批关注点</div>' +
        '<ul class="focus-list">' + focusItems + "</ul></div>" +
      '<div class="card card-pad"><div class="card-title">回写正文（编辑后需重新确认）</div>' +
        '<textarea class="comment-editor" id="commentEditor">' + esc(r.comment) + "</textarea>" +
        '<div class="digest-line">正文摘要 <span class="mono" id="digestNow">' + esc(r.contentDigest) + "…</span>" +
          '<span id="digestState">' + digestLine + "</span></div>" +
        '<div class="result-actions">' +
          '<button class="btn btn-primary" id="btnConfirm">确认结果</button>' +
          '<button class="btn" id="btnWrite">发起回写</button>' +
          '<div class="write-state" id="writeState">' + writeStatusCell({
            writeStatus: r.writeStatus, reasonCode: r.reasonCode, reasonText: r.reasonText
          }) + "</div>" +
        "</div>" +
        (r.confirmedBy ? '<div class="sub" style="margin-top:8px">最近确认：' + esc(r.confirmedBy) + " · " + esc(r.confirmedAt) + "</div>" : "") +
      "</div>" +
    "</div>";
}

/* ---- 工作台交互 ---- */

function bindPaneEvents(t, d) {
  /* 掩码切换 */
  document.querySelectorAll(".mask-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var span = btn.previousElementSibling;
      var showing = btn.textContent === "隐藏";
      span.textContent = showing ? span.dataset.masked : span.dataset.full;
      btn.textContent = showing ? "显示" : "隐藏";
    });
  });

  /* 上下文确认 */
  var ctxBtn = document.getElementById("btnCtxConfirm");
  if (ctxBtn) ctxBtn.addEventListener("click", function () {
    d.context.status = "confirmed";
    d.context.role = "出租方 / 甲方";
    d.context.confirmedBy = "王敏";
    renderWorkbench();
  });

  /* 字段 → PDF 定位 */
  document.querySelectorAll("[data-locate]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      locateField(parseInt(btn.dataset.locate, 10), d);
    });
  });

  /* PDF 页码 */
  document.querySelectorAll("[data-pg]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var p = d.parse;
      state.parsePage = Math.min(p.pages, Math.max(1, state.parsePage + parseInt(btn.dataset.pg, 10)));
      document.getElementById("pgIndicator").textContent = state.parsePage + " / " + p.pages + " 页";
    });
  });

  /* 解析版本切换 */
  var ver = document.getElementById("parseVersion");
  if (ver) ver.addEventListener("change", function () {
    alert("已切换到解析版本 " + ver.value + "：字段列表与 PDF 证据同步切换，不同版本证据不混用。");
  });

  /* 规则分组折叠 */
  document.querySelectorAll("[data-group]").forEach(function (head) {
    head.addEventListener("click", function () {
      head.classList.toggle("closed");
      head.nextElementSibling.classList.toggle("closed");
    });
  });

  /* 模块 5：确认 / 编辑失效 / 回写 */
  var editor = document.getElementById("commentEditor");
  var confirmBtn = document.getElementById("btnConfirm");
  var writeBtn = document.getElementById("btnWrite");

  if (editor && d.result) {
    editor.addEventListener("input", function () {
      if (d.result.confirmedDigest) {
        d.result.confirmedDigest = "STALE";
        document.getElementById("digestState").innerHTML =
          '<span class="badge b-warn">' + ICON_WARN + " 正文已变更，确认已失效（最终以后端判定为准）</span>";
      }
    });
  }

  if (confirmBtn) confirmBtn.addEventListener("click", function () {
    d.result.confirmedDigest = d.result.contentDigest;
    d.result.confirmedBy = "王敏";
    d.result.confirmedAt = "2026-09-15 " + new Date().toTimeString().slice(0, 5);
    d.result.reasonCode = null;
    d.result.reasonText = "已人工确认，可发起回写";
    state.tab = "result";
    renderWorkbench();
  });

  if (writeBtn) writeBtn.addEventListener("click", function () {
    var r = d.result;
    if (!r.confirmedDigest || r.confirmedDigest !== r.contentDigest) {
      document.getElementById("writeState").innerHTML =
        '<div class="status-reason"><span class="badge b-neutral">未回写</span>' +
        '<span class="reason"><code>WRITEBACK_POLICY_DENIED</code> 后端门禁拒绝：请先完成人工确认（409）</span></div>';
      return;
    }
    var task = TASKS.find(function (x) { return x.id === state.taskId; });
    r.writeStatus = "writing";
    document.getElementById("writeState").innerHTML = badge(WRITE_STATUS.writing);
    setTimeout(function () {
      r.writeStatus = "success";
      r.reasonCode = null; r.reasonText = null;
      task.writeStatus = "success";
      renderWorkbench();
    }, 1100);
  });
}

/* 字段定位：右侧 PDF 翻页 + 高亮框；反向：点高亮框回显字段 */
function locateField(index, d) {
  var f = d.parse.fields[index];
  if (!f || !f.bbox) return;
  state.parsePage = f.page;
  state.hlField = index;

  var ind = document.getElementById("pgIndicator");
  if (ind) ind.textContent = f.page + " / " + d.parse.pages + " 页";

  var hl = document.getElementById("pdfHl");
  if (hl) {
    hl.style.display = "block";
    hl.style.top = f.bbox.top + "%";
    hl.style.left = f.bbox.left + "%";
    hl.style.width = f.bbox.width + "%";
    hl.style.height = f.bbox.height + "%";
    hl.style.pointerEvents = "auto";
    hl.style.cursor = "pointer";
    hl.setAttribute("data-label", f.name + " · 第 " + f.page + " 页");
    hl.title = "点击反查字段";
    hl.onclick = function () {
      document.querySelectorAll(".field-item").forEach(function (el) { el.classList.remove("hl"); });
      var item = document.querySelector('.field-item[data-field="' + index + '"]');
      if (item) { item.classList.add("hl"); item.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    };
  }

  document.querySelectorAll(".field-item").forEach(function (el) { el.classList.remove("hl"); });
  var item = document.querySelector('.field-item[data-field="' + index + '"]');
  if (item) item.classList.add("hl");
}

/* ---------------- 扩展：规则管理 ---------------- */

function renderRules() {
  var rows = RULES.map(function (r, i) {
    return "<tr style=\"cursor:default\">" +
      '<td><span class="mono">' + esc(r.code) + "</span></td>" +
      "<td>" + esc(r.name) + "</td>" +
      '<td><span class="badge ' + RISK[r.level].cls + '">' + r.level + "</span></td>" +
      "<td>" + esc(r.mode) + "</td>" +
      "<td>" + esc(r.scope) + "</td>" +
      '<td><span class="mono">' + esc(r.version) + "</span></td>" +
      '<td><button class="toggle' + (r.on ? " on" : "") + '" data-rule="' + i + '" title="启停用"></button></td>' +
      "</tr>";
  }).join("");

  document.getElementById("view-rules").innerHTML =
    '<div class="notice notice-neutral">' + ICON_INFO +
      "<div><b>规则库共 40 条</b>，覆盖预付款、付款周期、自动续约、违约责任、管辖地、主体缺失、金额缺失、保密、数据处理、知识产权、验收标准 11 类。规则修改采用版本化发布，已被运行引用的版本不可就地质改。</div></div>" +
    '<div class="toolbar">' +
      '<input type="search" placeholder="搜索规则编码 / 名称" />' +
      '<select><option>风险等级：全部</option><option>高</option><option>中</option><option>低</option></select>' +
      '<select><option>状态：全部</option><option>启用</option><option>停用</option></select>' +
      '<span class="spacer"></span>' +
      '<button class="btn">新增规则</button>' +
      '<button class="btn btn-primary" id="btnReload">发布版本（/api/rules/reload）</button>' +
    "</div>" +
    '<div class="card"><table class="table"><thead><tr>' +
    "<th>规则编码</th><th>名称</th><th>风险</th><th>匹配模式</th><th>适用条件</th><th>版本</th><th>状态</th>" +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>";

  document.querySelectorAll("[data-rule]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var r = RULES[parseInt(btn.dataset.rule, 10)];
      r.on = !r.on;
      btn.classList.toggle("on", r.on);
      AUDITS.unshift({
        action: "RULE_UPDATED",
        text: (r.on ? "启用" : "停用") + "规则 " + r.code + "（" + r.version + "）",
        who: "王敏", time: "刚刚", cid: "—"
      });
    });
  });

  document.getElementById("btnReload").addEventListener("click", function () {
    alert("激活前校验通过：40 条规则配置合法，11 类覆盖完整。\n新版本 v13 已发布并写入审计事件。");
  });
}

/* ---------------- 扩展：运行管理 ---------------- */

var JOB_STATUS = {
  queued:     { text: "排队中",   cls: "b-neutral" },
  running:    { text: "运行中",   cls: "b-info" },
  retry_wait: { text: "等待重试", cls: "b-warn" },
  succeeded:  { text: "成功",     cls: "b-ok" },
  failed:     { text: "失败",     cls: "b-danger" }
};

var LOG_LEVEL = {
  ERROR: "b-danger", WARN: "b-warn", INFO: "b-info"
};

function renderOps() {
  var jobRows = JOBS.map(function (j) {
    return '<tr style="cursor:default">' +
      '<td><span class="mono">#' + j.id + "</span></td>" +
      "<td>" + esc(j.type) + "</td>" +
      '<td><span class="mono">' + esc(j.task) + "</span></td>" +
      "<td>" + badge(JOB_STATUS[j.status]) + "</td>" +
      "<td>" + esc(j.attempt) + "</td>" +
      "<td>" + esc(j.next) + "</td>" +
      '<td>' + (j.error === "—" ? "—" : '<span class="mono" style="color:var(--danger)">' + esc(j.error) + "</span>") + "</td>" +
      "</tr>";
  }).join("");

  var logRows = LOGS.map(function (l) {
    return '<tr style="cursor:default">' +
      '<td><span class="badge ' + LOG_LEVEL[l.level] + '">' + l.level + "</span></td>" +
      "<td>" + esc(l.type) + "</td>" +
      '<td><span class="mono">' + esc(l.cid) + '</span> <button class="mask-toggle" data-copy="' + esc(l.cid) + '">复制</button></td>' +
      "<td>" + esc(l.content) + "</td>" +
      '<td class="sub">' + esc(l.time) + "</td>" +
      "</tr>";
  }).join("");

  var auditRows = AUDITS.map(function (a) {
    return '<div class="audit-item">' +
      '<div class="audit-icon">' + ICON_OK + "</div>" +
      "<div><b>" + esc(a.action) + "</b> · " + esc(a.text) +
      '<div class="audit-meta">操作人 ' + esc(a.who) + " · " + esc(a.time) + ' · 关联 <span class="mono">' + esc(a.cid) + "</span></div>" +
      "</div></div>";
  }).join("");

  document.getElementById("view-ops").innerHTML =
    '<div class="notice notice-neutral">' + ICON_INFO +
      "<div>运行管理是<b>排障入口</b>：只读 + 检查点重试，不允许在此修改任务状态。日志支持按 <code>correlation_id</code> 检索，贯穿「请求 → 作业 → Worker」。</div></div>" +
    '<div class="detail-grid">' +
      '<div class="card span2"><div class="card-pad" style="padding-bottom:0"><div class="card-title">后台作业（workflow_jobs）</div></div>' +
        '<table class="table"><thead><tr><th>作业</th><th>类型</th><th>任务</th><th>状态</th><th>尝试</th><th>下次重试</th><th>错误码</th></tr></thead>' +
        "<tbody>" + jobRows + "</tbody></table></div>" +
      '<div class="card span2"><div class="card-pad" style="padding-bottom:0"><div class="card-title">运行日志（task_logs）</div>' +
        '<div class="toolbar" style="margin-bottom:0">' +
          '<select><option>级别：全部</option><option>ERROR</option><option>WARN</option><option>INFO</option></select>' +
          '<input type="search" placeholder="按 correlation_id / 错误码检索" />' +
        "</div></div>" +
        '<table class="table"><thead><tr><th>级别</th><th>类型</th><th>关联 ID</th><th>内容</th><th>时间</th></tr></thead>' +
        "<tbody>" + logRows + "</tbody></table></div>" +
      '<div class="card card-pad span2"><div class="card-title">审计事件（不可变）</div>' + auditRows + "</div>" +
    "</div>";

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = "复制"; }, 1200);
    });
  });
}

/* ---------------- 启动 ---------------- */

renderTasks();
renderRules();
renderOps();
