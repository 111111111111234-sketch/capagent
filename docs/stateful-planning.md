# 第二阶段：任务规划与进度管理

日期：2026-09-28。基于第一阶段执行器，现已实现显式计划、子目标选择、验证进度和有限局部恢复。本文演示使用固定计划与 mock 状态；后续已接入[第三阶段模型提案接口和测试](stateful-models.md)，真实模型质量尚未验证。

## 当前流程

```text
任务与初始状态 → 计划提案 → 结构/依赖/目标覆盖校验
    → 选择当前可执行子目标 → 固定代码通过现有执行器执行
    → 条件验证 → 更新进度
    → 继续 / 补观察 / 重试 / 局部修订 / 最终验证或停止
```

贯穿案例是“抓取红块 → 搬运到绿块上方 → 放置并松开夹爪”。原来的执行结果不再是任务进度的替代品：代码正常返回后，节点先进入待验证状态，条件得到证据支持后才完成。

## 新增能力

| 内容 | 当前实现 |
| --- | --- |
| 计划 | PlanProposal / TaskPlan；子目标、依赖、前置条件、完成条件、技能范围、计划版本和最终目标覆盖 |
| 校验 | 拒绝重复节点、依赖环、悬空引用、未知技能/对象/谓词、条件定义冲突、遗漏或放宽最终目标 |
| 选择下一步 | 同时检查历史依赖和当前条件；结果未知时请求观察；目标已经满足时先验证后跳过 |
| 进度 | pending、running、awaiting_verification、succeeded、needs_recovery、skipped_verified、superseded 等状态 |
| 多段子目标 | 一个 attempt 可执行多段代码；后续段检查自身入口条件，不重复要求已经消耗的初始条件 |
| 验证 | 检查冻结条件、状态版本、事实时间、证据来源、执行关联；输出 pass/fail/unknown |
| 恢复 | 同一节点重试或提交 PlanPatch；保留成功历史，替代未完成节点并重连后继依赖 |
| 限额 | 恢复组尝试次数、每次尝试的动作段数、计划修订次数、连续无进展补观察次数；共享第一阶段 episode 执行/API 预算 |
| 重启 | 从同一个 SQLite 事件账本重建计划与进度；未对账执行阻止新动作；JSON 快照不是权威状态 |

历史上的“抓取成功”不会因随后掉落被删除；当前 holding=false 会阻止继续搬运。正常放置后 holding=false 则无需恢复抓取，因为最终摆放条件已经成立。结束任务要检查当前最终目标，不能只统计历史成功节点。

## 运行

使用第一阶段相同的 Python 3.10–3.12 与 Pydantic 2 环境，无新增依赖。在仓库根目录运行：

```bash
python -m capx.agents.stateful plan-demo --scenario all --output outputs/planning-demo
python -m capx.agents.stateful replay outputs/planning-demo/drop_recovery
python -m capx.agents.stateful schema --output outputs/stateful-schemas
python -m unittest discover -s tests/stateful -v
```

当前机器可用的解释器为：

```text
/Users/agiuser/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
```

可用该绝对路径替换命令中的 `python`。演示要求空输出目录，再次查看使用 replay。CLI 中原来的 `demo` 子命令仍运行第一阶段演示。

## 九种演示

| scenario | 预期结果 | 展示能力 |
| --- | --- | --- |
| normal | succeeded，计划 v1，3 段执行 | 三个子目标依次执行并验证 |
| grasp_retry | succeeded，计划 v1，4 段执行 | 首次空抓，增加一次尝试并调整接近动作；不重建任务图 |
| drop_recovery | succeeded，计划 v2，5 段执行 | 搬运中掉落，保留抓取成功历史，插入恢复抓取/搬运并重新连接放置 |
| occlusion | succeeded，计划 v1，3 段执行 | 夹持结果未知，补观察后继续验证；不重复抓取 |
| unknown_forever | budget_exhausted，1 段执行 | 连续三次补观察仍无有效证据，在限额内停止 |
| budget_exhausted | budget_exhausted，3 段执行 | 同一恢复组尝试三次后停止，不重置预算 |
| already_satisfied | succeeded，0 段执行 | 初始最终目标已满足，经验证直接结束 |
| final_disturbance | succeeded，计划 v2，6 段执行 | 历史步骤完成后支撑关系被破坏，最终检查发现问题并生成恢复计划 |
| stop_unknown | interrupted，2 段执行 | 搬运动作停止状态不明，保留在途记录，等待显式对账 |

这些是合成状态转换与脚本化规划，不是机器人操作成功率实验。图像延用第一阶段的合成帧，新增的支撑/搬运事实来自 fixture，不声称已用视觉推断。

## 查看计划与进度

每个演示保留第一阶段的全部执行产物，并新增：

```text
planning/
  plan-v1.json / plan-v2.json     不同版本的完整计划
  progress.json                  当前进度的可重建导出
  summary.json                   状态、执行次数、恢复尝试次数、停止原因
  evidence/*.json                每次验证实际使用的状态快照
  observations/*.json            补观察得到的状态
```

完整验证报告、条件判断、计划修订与决策理由保存在 `events.sqlite3` 和导出的 `events.jsonl`。`replay` 直接从数据库事件重建 plan/progress，即使 progress.json 丢失或损坏也不会靠猜测恢复。旧事件和旧计划保留供核对。

新增的规划字段随 ExecutionDispatched 在同一个事务中持久化，尝试数只在真正登记派发时增加。格式错误、重复反馈、补观察不会增加动作尝试次数。取消/运行错误带来的停止策略与验证结果也在同一事件中落盘，避免重启后丢失停止要求。

## 接口与责任

`planning/contracts.py` 定义规划专有模型，继续引用共享的 Condition、ExecutionRequest 和 ExecutionReport，未维护第二套执行结果格式。

`PlanningManager` 提供主要调用：

- `create_plan(proposal, state)`：接收提案，校验并提交初始计划。
- `next(state)`：选择下一步；达到预算时记录终止。
- `execute_segment(...)`：绑定计划、子目标与 attempt，将固定代码交给第一阶段执行器。
- `request_verification(...)` / `apply_verification(report, state)`：冻结验证条件，再校验报告和证据；`verify_now` 是当前确定性验证适配器的便捷入口。
- `observe()`：计入补观察预算，并向现有状态提供器读取新状态。
- `commit_patch(patch, state)`：验证版本、历史、依赖、目标和恢复预算后提交局部修订。
- `read()` / `export()`：从事件恢复状态，或导出方便阅读的 JSON。
- `terminate(...)` / `resume(state)`：记录停止或在已确认后端空闲的情况下显式恢复，保留原预算。success 不能通过 terminate 直接写入。

启用规划的运行目录会要求派发经过 PlanningManager；重启后未恢复管理器时，原执行器不能绕过计划直接派发新动作。一个后端会话仍由一个进程/线程拥有，复用第一阶段的目录锁与执行线程约束。

## 首版采用的保守规则

- 节点执行成功依赖“子目标条件”的验证；初始或最终条件验证不能冒充某次动作的完成证据。
- 验证报告必须对应已登记的请求，状态至少覆盖最近执行后的版本；事实不得早于最近停止确认。此规则较严格，可能要求 P 刷新未变化事实的证据。
- 当前验证器只消费结构化事实，检查来源、时效和三值结果。它不是 CoF，也没有完整的多传感器证据融合；P 负责提供真实且一致的事实。
- recovery_of 绑定原目标，恢复节点必须继承预算组、保持相同完成条件、保留原前提，且不能扩大技能范围。等价目标也不能通过改条件 ID 获得新预算。首版支持恢复既有目标；更一般的任务分解变化需要后续扩展。
- 任一关键条件 unknown 时保持待验证，不直接判成功或盲目重试。补观察限额只在验证确认进展时清零，不因状态版本增长清零。
- 已确认的 Python/API 错误可以与“物理效果已达成”同时保留，但先将任务标为 blocked，需显式恢复；取消记 interrupted，超时和预算耗尽记 budget_exhausted。
- 未确认停止的执行先对账，不允许重规划绕过。恢复记录不等于恢复物理环境，新建 mock 后端不能接管旧会话。

第三阶段另提供模型计划/代码生成器与独立工作进程，继续让程序负责校验、派发、预算与进度更新；本页 `plan-demo` 保留固定规划器用于回归。真实 P、CoF、完整仿真和真实服务联调仍按后续阶段接入。
