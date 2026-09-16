"""工作流模块 —— 任务状态与后台作业的唯一入口。

两个层级**严禁混用**（一个任务对应多个作业，某个作业失败 ≠ 任务阻塞）：

    business  approval_tasks.task_status   进行到哪个业务阶段
    job       workflow_jobs.job_status     Worker 执行得怎么样

`state_machine.transition()` 是唯一允许修改 `task_status` 的地方，非法转换抛
`InvalidStateTransition`。

M3 只提供最小件：状态机 + 作业台账（**只写不消费**）。M4 引入 Worker 时，
表结构与写入路径都不需要改。
"""
