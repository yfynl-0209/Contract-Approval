"""业务可预期错误的统一层级。

调度器只需回答一个问题：**这个失败是"再试一次就好"，还是"再试也没用"？**
搞反两个方向都有代价：瞬时当确定性 → 一次网络抖动把任务打成永久 `blocked`；
确定性当瞬时 → 白费重试次数与退避等待。

可重试性**由错误码派生**（`app.enums.is_retryable`），不允许各调用点自行判断。
`TransientError` / `PermanentError` 在构造时就校验错误码是否匹配，
把"分类写错"变成**构造即失败**的开发期错误。
"""

from __future__ import annotations

from app.enums import ErrorCode, is_retryable


class AppError(Exception):
    """所有**可预期**业务错误的基类。

    代码缺陷（`TypeError`、`KeyError`）**不属于**这里 —— 它们应直接抛出，
    而不是被包装成一个看起来正常的业务错误。
    """

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    @property
    def retryable(self) -> bool:
        """是否值得退避重试。**由错误码派生，不允许覆盖。**"""
        return is_retryable(self.code)

    def __str__(self) -> str:
        # 带上错误码，避免日志里只剩中文描述而无法统计
        return f"[{self.code}] {self.message}"


class TransientError(AppError):
    """瞬时错误：退避重试有意义，**耗尽重试次数后**才让任务 `blocked`。"""

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message, code=code)
        if not self.retryable:
            raise ValueError(
                f"{type(self).__name__} 只能承载瞬时错误码，但收到 {code}。"
                f"分类写错会让确定性错误被反复重试，请改用 PermanentError 子类。"
            )


class PermanentError(AppError):
    """确定性错误：重试也不会变好，**立即**让任务 `blocked`，不浪费重试。"""

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message, code=code)
        if self.retryable:
            raise ValueError(
                f"{type(self).__name__} 只能承载确定性错误码，但收到 {code}。"
                f"分类写错会让一次网络抖动把任务打成永久失败，"
                f"请改用 TransientError 子类。"
            )


# ============================================================
# 分类标签：用于"按依赖归因"，不单独实例化
# ============================================================


class GatewayError(AppError):
    """与外部**审批系统**交互产生的错误（分类标签）。"""


class StorageError(AppError):
    """与**对象存储**交互产生的错误（分类标签）。"""


class WorkflowError(AppError):
    """**工作流状态机**相关错误（分类标签）。"""


# ============================================================
# 四个具体基类（业务代码抛这些，不抛裸的 AppError）
# ============================================================


class TransientGatewayError(TransientError, GatewayError):
    """审批系统暂时不可用：超时 / 5xx / 连不上。"""


class PermanentGatewayError(PermanentError, GatewayError):
    """审批系统给出了明确拒绝：401/403、实例不存在、幂等键冲突、响应结构不符。"""


class TransientStorageError(TransientError, StorageError):
    """存储后端暂时不可用：超时 / 连接失败。

    ⚠️ 这一类**必须**可重试：归为确定性错误会让一次网络抖动把任务打成永久失败。
    """


class PermanentStorageError(PermanentError, StorageError):
    """存储操作无论如何都不会成功：**路径越界**、权限不足、内容校验不符。

    与 `TransientStorageError` 的区别不是"严重程度"，而是**"重试是否会改变结果"**。
    """


class AttachmentValidationError(PermanentError):
    """附件校验未通过：类型 / 大小 / 空文件 / 校验和不符。

    一律确定性 —— 重试只会再次下载同一份不合规的附件。
    """


class TaskNotFound(WorkflowError, PermanentError):
    """本系统里没有这条任务的记录（重试不会让任务凭空出现，处理动作是先拉取）。

    ⚠️ 不要与 `PermanentGatewayError(INSTANCE_NOT_FOUND)` 混用：
    那个是"**外部审批系统**说没有这个单子"，这个是"**我们这边**还没拉过它"。
    """


class InvalidStateTransition(WorkflowError, PermanentError):
    """任务状态机不允许的转换。

    它几乎总是暴露**代码缺陷**（两个流程分支对当前状态的判断不一致），
    因此需要立即炸出来，而不是靠重试掩盖过去。
    """


class LeaseLost(WorkflowError, PermanentError):
    """本次执行**已失去作业的租约**（§4.4.2）。

    出现它意味着另一处（回收扫描，或另一个 Worker）已经接手了这个作业。
    因此持有者必须**立即停止并作废本次结果** —— 包括**回滚已经写入的业务数据**，
    否则库里会留下一份没有任何作业引用它的结果，
    而症状会出现在**另一个** Worker 身上（它写入时撞唯一约束，报"解析失败"）。
    **症状与病因完全错位。**

    确定性：重试不会让"我已经不是持有者"变成"我是"。
    """


class IdempotencyConflict(WorkflowError, PermanentError):
    """同一个幂等键被用于**不同的输入**。

    ⚠️ 与 `PermanentGatewayError(IDEMPOTENCY_CONFLICT)` 不是一回事：
    那个是**外部审批系统**拒绝了我们的重复提交；这个是**我们自己的台账**发现
    调用方把一个键用在了两份不同的输入上。

    必须**立即并确定性地**报错（映射为 409），而不是静默返回一份输入不同的旧作业 ——
    后者会让调用方以为参数生效了，实际拿到的是别人的旧作业与旧数据，
    而库里没有任何一处看得出这件事。
    """


# ============================================================
# "业务事实" —— 不是故障
# ============================================================


#: 描述**业务事实**的错误码：业务对象本身有问题，而不是系统故障。
#:
#: 它们与"系统故障"的区别不是程度，而是**性质**，因此处置完全不同：
#: 附件在审批系统里已经没了，重试一万次也不会回来，正确的下一步是**找人来处理**。
#: 协议层据此把它们渲染成 200 + 业务结论（见 `app/api/errors.py`）。
#:
#: ⚠️ `AUTH_FAILED` **不在**这里：它描述的是"我们的凭据不被接受"，
#: 属系统配置问题，该由运维去改，而不是等人工处理某份合同。
#:
#: ⚠️ 定义在本模块（业务错误层）而不是 `app/api/errors.py`（协议层）：
#: 工具门面 `app/tool_facade.py` 需要按它分类，而门面**不得**依赖协议层 ——
#: 一旦门面 import 了 `app.api`，MCP 形态就会连带被拖进 FastAPI 的依赖里，
#: 而"两种协议共用同一套业务判断"正是门面存在的理由。
BUSINESS_FACT_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.ATTACHMENT_MISSING,
        ErrorCode.ATTACHMENT_EMPTY,
        ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED,
        ErrorCode.ATTACHMENT_TOO_LARGE,
    }
)
