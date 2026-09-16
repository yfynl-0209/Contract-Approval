"""薄出口「人工修改回写正文」的**代价探针**（M8 Task 7）。

## 这个文件现在测什么、为什么不直接测路由

M8 的模块 5 需要"编辑正文并保存为新版本"（M8 设计 §4.5 与验收 9）。
能力**早就有了** —— 工具 6 已经在做版本化：内容变化 → `version_no + 1`、
`supersedes_result_id` 接替当前最大版本、重算 `content_digest`、写审计。
缺的只是它在 `/api/` 下**没有出口**，而控制台的硬约束是只调 `/api/*`。

于是要做的不是"再实现一次版本化"（那会得到**第二份实现**，而
"REST 与 MCP 必须共用同一套应用服务"是这个仓库的架构底线），而是加一个**薄出口**：

```text
POST /api/results/{result_id}/comment
  请求体: { comment_text }
  实现:   读当前结果 → 取它自己的 run_id / overall_risk_level / summary_text / focus_points
          → result_service.save_review_result(...)   ← 与工具 6 **同一个函数**
  返回:   与工具 6 同形（result_id / version_no / content_digest / result_url）
```

**这个文件的现状**：前两条是**服务层探针**，后五条是**路由级用例**（出口已落地）。

**为什么先写成"服务层"的探针**：

1. 出口本身要落在 `app/api/results.py`，而当时**另一会话正在同一批文件上工作** ——
   直接动它会丢掉那边的编辑。探针先回答唯一真正影响决策的问题：
   **"复用 `save_review_result` 够不够？"**
2. 够不够不看签名像不像，而看**行为**：改一次正文，是否自动得到
   新版本 + 版本链 + 旧版本确认失效 + 重复保存幂等。
   这些断言在出口存在之前就能跑，而且**一个字都不用改**地延续到了路由级用例
   —— 探针通过 = 出口不需要任何新的业务逻辑。
3. 出口落地后补的路由级用例只多了几行（`harness.client.post(...)`），
   断言与服务层那两条**逐条对应**：同一件事有两个入口时，
   它们的行为必须一致，否则其中一个迟早分叉。

## 四条硬约束（写进计划，不写在这里就会在别处丢）

1. **只允许改 `comment_text`**。风险等级 / 摘要 / 关注点不由这个出口改 ——
   工具 6 要求 `overall_risk_level` 与批次聚合一致，让它可传就等于重开一个
   被 `RESULT_INPUT_MISMATCH` 关掉的口子；
2. **必须复用 `save_review_result`**（判据就是本文件通过）；
3. **等级与 run_id 取自当前结果**：它们在该结果保存时已被校验过，不是猜的；
   重新聚合反而会让"编辑正文"顺带改了风险等级；
4. **确认失效不需要写任何新代码**：`confirmation_valid` 已经含"仍是当前版本"
   这一条，新版本一落库旧版本的确认自动失效 —— 验收 9 的"由后端判定"天然成立。
"""

from __future__ import annotations

# ⚠️ 复用 M6 的测试台（`harness` 夹具），**不再写第二份种子链**：
# 夹具一旦复制，两份会在"schema 变了"时各自漂移，而其中一份的失败
# 看起来永远像"业务坏了"。
from test_m6_api import harness  # noqa: F401

from app.auth import Actor
from app.services.result_service import get_result_view, save_review_result


def _reviewer(name: str = "reviewer-9") -> Actor:
    """审核人（含 `RESULT_SAVE` / `RESULT_CONFIRM`，见 `app/auth.py::_REVIEWER_PERMISSIONS`）。"""
    return Actor(
        actor_id=name, display_name=name, roles=frozenset(), tenant_id="default"
    )


def _edit_comment_text_only(harness, result_id: int, new_comment: str):
    """**照薄出口将要做的步骤**调服务层：读当前结果 → 只换正文 → 保存。

    这段是探针的核心：除了 `comment_text`，其余入参一律**取自当前结果本身**
    （`run_id` / `overall_risk_level` / `summary_text` / `focus_points`）——
    因此它不引入任何新的口径判断。
    """
    with harness.session() as session:
        current = get_result_view(session, result_id=result_id)
        saved = save_review_result(
            session,
            run_id=current.run_id,
            overall_risk_level=current.overall_risk_level,
            summary_text=current.summary_text,
            focus_points_json=list(current.focus_points),
            comment_text=new_comment,
            actor=_reviewer(),
        )
        session.commit()
        return saved


def test_editing_the_comment_reuses_save_review_result_end_to_end(
    harness,  # noqa: ANN001 - M6 的测试台夹具
) -> None:
    """改正文 → 新版本 + 版本链 + **旧版本确认自动失效**（薄出口的全部效果）。"""
    _task_id, run_id = harness.seed_chain()
    first_id = harness.save(run_id, comment_text="原始正文")["result_id"]

    with harness.session() as session:
        # 确认走服务函数：`POST /api/results/{id}/confirm` 是 M7 的接口
        from app.services.result_service import confirm_result

        confirm_result(session, result_id=first_id, actor=_reviewer())
        session.commit()

    before = harness.client.get(f"/api/results/{first_id}").json()
    assert before["confirmation_valid"] is True
    assert before["version_no"] == 1

    # ---- 薄出口将要做的事 ----
    saved = _edit_comment_text_only(harness, first_id, "人工改过的正文")

    # ① 新版本成立，且**不是复用**
    assert saved.reused is False
    assert saved.result_id != first_id
    assert saved.version_no == 2

    # ② 版本链接替当前最大版本（与工具 6 同一条链）
    new = harness.client.get(f"/api/results/{saved.result_id}").json()
    assert new["supersedes_result_id"] == first_id
    assert new["version_no"] == 2
    assert new["content_digest"] != before["content_digest"]
    # 摘要与关注点**照抄当前结果**：编辑正文不该顺带改结论
    assert new["summary_text"] == before["summary_text"]
    assert new["focus_points"] == before["focus_points"]
    assert new["overall_risk_level"] == before["overall_risk_level"]

    # ③ ⚠️ 旧版本的确认**自动失效**（`confirmation_valid` 含"仍是当前版本"）
    after = harness.client.get(f"/api/results/{first_id}").json()
    assert after["confirmation_valid"] is False
    # 确认的**痕迹**仍在（失效的是有效性，不是审计事实）
    assert after["confirmed_by"] == "reviewer-9"
    assert after["manual_confirmed"] is True
    assert after["is_current_version"] is False


def test_editing_with_the_same_text_is_idempotent(
    harness,  # noqa: ANN001
) -> None:
    """同一份正文保存两次只会得到**一个**版本。

    这条守的是薄出口的"双击安全"：同批次 + 内容与输入完全一致 → 命中复用。
    没有它时，界面上一次误触会凭空多出一个版本，而版本链上看起来
    "有人改过两次" —— 那是关于**谁改了什么**的错误记录。
    """
    _task_id, run_id = harness.seed_chain()
    result_id = harness.save(run_id, comment_text="原始正文")["result_id"]

    first = _edit_comment_text_only(harness, result_id, "改过的正文")
    second = _edit_comment_text_only(harness, result_id, "改过的正文")

    assert first.reused is False
    assert second.reused is True
    assert second.result_id == first.result_id
    assert second.version_no == first.version_no

    current = harness.client.get(f"/api/results/{first.result_id}").json()
    assert current["version_no"] == 2
    assert current["is_current_version"] is True


# ============================================================
# 出口：`POST /api/results/{id}/comment`
# ============================================================


def test_comment_endpoint_creates_a_new_version(
    harness,  # noqa: ANN001
) -> None:
    """路由级：改正文 → 新版本 + 旧版本确认失效（验收 9 的完整链路）。"""
    _task_id, run_id = harness.seed_chain()
    first_id = harness.save(run_id, comment_text="原始正文")["result_id"]

    with harness.session() as session:
        from app.services.result_service import confirm_result

        confirm_result(session, result_id=first_id, actor=_reviewer())
        session.commit()
    assert harness.client.get(f"/api/results/{first_id}").json()["confirmation_valid"] is True

    response = harness.client.post(
        f"/api/results/{first_id}/comment", json={"comment_text": "人工改过的正文"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "saved"
    assert body["version_no"] == 2
    assert body["result_url"] == f"/api/results/{body['result_id']}"

    # 旧版本：确认**自动失效**，版本不再是当前版本；确认的痕迹仍在
    old = harness.client.get(f"/api/results/{first_id}").json()
    assert old["confirmation_valid"] is False
    assert old["is_current_version"] is False
    assert old["manual_confirmed"] is True

    new = harness.client.get(body["result_url"]).json()
    assert new["supersedes_result_id"] == first_id
    assert new["comment_text"] == "人工改过的正文"
    assert new["confirmation_valid"] is False
    # 结论口径照抄：编辑正文不该顺带改风险等级 / 摘要 / 关注点
    assert new["overall_risk_level"] == old["overall_risk_level"]
    assert new["summary_text"] == old["summary_text"]
    assert new["focus_points"] == old["focus_points"]


def test_comment_endpoint_shape_is_identical_to_tool_six(
    harness,  # noqa: ANN001
) -> None:
    """⚠️ 两个入口的返回**键集合相同** —— 这条守的是"共用同一个实现"。

    只断言"两条都能用"时，**第二份实现**照样能通过；而它分叉的方式是
    "某天只改了其中一处"，没有一处会报错。键集合相同不足以证明同源，
    但它能挡住"顺手在新接口里多加/少加一个字段"这类分叉的开始。
    """
    _task_id, run_id = harness.seed_chain()
    result_id = harness.save(run_id, comment_text="原始正文")["result_id"]

    via_api = harness.client.post(
        f"/api/results/{result_id}/comment", json={"comment_text": "控制台改的"}
    ).json()
    via_tool = harness.save(run_id, comment_text="外部调用方改的")
    via_tool.pop("status_code")  # 测试台额外加的 HTTP 状态，不属于响应体

    assert set(via_api) == set(via_tool)


def test_repeating_the_same_comment_over_the_api_creates_no_extra_version(
    harness,  # noqa: ANN001
) -> None:
    """路由级幂等：同一份正文提交两次只有一个新版本（界面上误触不会留痕成两次）。"""
    _task_id, run_id = harness.seed_chain()
    result_id = harness.save(run_id, comment_text="原始正文")["result_id"]

    first = harness.client.post(
        f"/api/results/{result_id}/comment", json={"comment_text": "改过的正文"}
    ).json()
    second = harness.client.post(
        f"/api/results/{result_id}/comment", json={"comment_text": "改过的正文"}
    ).json()

    assert first["outcome"] == "saved"
    assert second["outcome"] == "reused"
    assert second["result_id"] == first["result_id"]
    assert len(harness.results()) == 2, "原始一版 + 改过一版，没有第三个版本"


def test_comment_endpoint_on_a_missing_result_is_404(
    harness,  # noqa: ANN001
) -> None:
    """写错 id → 404 + `RESULT_NOT_FOUND`（与结果查询同一约定，不给枚举线索）。"""
    response = harness.client.post(
        "/api/results/424242/comment", json={"comment_text": "正文"}
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "RESULT_NOT_FOUND"


def test_blank_comment_text_is_rejected_before_any_write(
    harness,  # noqa: ANN001
) -> None:
    """空白正文 → 400，且**不留下任何版本**。

    `min_length=1` 单独用拦不住 `"   "`，因此请求体基类开了
    `str_strip_whitespace` —— 两者必须配合（与工具 6 同一套严格性）。
    """
    _task_id, run_id = harness.seed_chain()
    result_id = harness.save(run_id, comment_text="原始正文")["result_id"]

    response = harness.client.post(
        f"/api/results/{result_id}/comment", json={"comment_text": "   "}
    )

    assert response.status_code == 422
    assert len(harness.results()) == 1
