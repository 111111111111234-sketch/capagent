# 第四阶段：CoF、P 与完整闭环

2026-09-28：已把第三阶段的模型规划/代码生成，接入 CoF 证据反馈、P 状态融合与处理确认。新增闭环入口、故障注入、日志回放和 50 项测试；连同前面的回归测试，共 169 项通过。

本阶段完成的是可运行的接口和合成场景闭环。仓库尚未提供外部 P 的实现，因此先提供可替换的 `PAdapter` 和本地参考实现。未调用真实模型服务，未验证真实视觉、外部 P 或机器人性能。CoF 采用帧引用与时序证据的思路，是 CoF-inspired 原型，不是论文训练方法的复现。

## 当前闭环

```text
P 当前状态 → 模型生成计划/代码 → 校验与受控执行
  → 执行帧、状态采样、API 边界及执行报告
  → CoF：引用证据的变化、终态条件、未知项
  → P：融合有效变化，返回状态快照与处理确认
  → Verifier：检查当前条件
  → Controller：继续、补观察、重试、局部修订、停止或最终验收
```

P 未确认处理最新 execution、report revision 和 feedback 时，不能验证成功或派发新动作。只增加 state_version 不够。失败、取消及部分执行后的物理变化也会交给 P；执行器是否停止仍由后端决定。

模型后续上下文包含 P 快照来源、已处理反馈标识、CoF 变化与未知项。计划与进度仍由原来的管理器维护，模型、CoF 和 P 都不能直接改写 TaskProgress。

## 运行

依赖沿用 Python 3.10–3.12 与 Pydantic 2，没有新增第三方依赖。在仓库根目录执行，输出目录须为空：

```bash
python -m capx.agents.stateful closed-loop-demo --scenario all --output outputs/closed-loop-demo
python -m capx.agents.stateful replay outputs/closed-loop-demo/drop_recovery
python -m capx.agents.stateful schema --output outputs/stateful-schemas
python -m unittest discover -s tests/stateful -v
```

当前机器可用的解释器为 `/Users/agiuser/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`。

需要真实模型联调时，可使用第三阶段的 [模型配置示例](stateful-models.example.json)：

```bash
python -m capx.agents.stateful closed-loop-run --config model-config.json --cof-mode frames --output outputs/live-loop
```

该命令会调用真实服务，**本轮未运行**；后端仍为合成 stacking 场景。`frames` 要求所配模型支持多图输入；`sensors` 使用确定性的结构化测量反馈。两种模式都使用本地 P 参考实现。模型地址、鉴权、请求超时和模型额度沿用[第三阶段说明](stateful-models.md)。旧的 demo、plan-demo、model-test-demo、model-run 保留原有行为。

## CoF 当前做什么

`feedback/` 提供请求、证据记录、候选事实、时间事件和反馈类型。每份报告绑定原始任务/执行身份、执行报告版本、输入 hash 与事件水位。

1. 从该执行的不可变记录中选取有界帧和结构化状态，保留首尾并抽取调用边界。图片、帧清单与状态采样都有 hash；拒绝越界路径、被篡改内容和错误时间，历史无 hash 的帧清单不能直接作为新证据。
2. `SensorTimelineAnalyzer` 从显式测量中产生事实；它不读取像素作视觉推断。`ModelFrameAnalyzer` 将标有证据 ID、相机与采集时间的多图交给模型，再校验结构、对象、引用和时间。两者可替换。
3. 检查最新终态事实，并描述同一通道中有证据支持的关系变化。不会把不同相机的两张图直接当成先后变化，也不会从相关性推断掉落的物理原因。
4. 输出 supported/refuted/unknown、证据缺口与只读观察建议。CoF 结果不等于最终验收，也不能发起机器人动作。

`at_end` 使用实际终态采样；`occurred` 只能说明某个采样时刻观察到该事实。当前只有调用边界采样，`maintained` 和 `stable_for_window` 返回 unknown，不能用单帧证明持续保持或稳定窗口。正常释放后 holding=false 可以形成关系变化记录，但放置阶段按 on/gripper_open 验收，不会因此自动重新抓取。

默认分析总上限 32、每个请求最多两次分析、最多 8 帧与 32 个状态记录、证据读取总量 4 MiB。格式或引用错误可以有限修正；相同输入可复用已存结果，调用预算不因重复请求或重启清零。参数通过 `FeedbackLimits` 注入，schema 已导出。

多图分析与规划/代码共用 `ModelClient` 的调用账本和模型额度。请求可能因图像体积超过模型上下文预算而明确失败，不能无限追加图片。合成 PPM 帧会转换为 PNG，模型不能指定任意文件或 URL。

## P 适配与处理确认

`state/` 中的 `PAdapter.update(request, previous, timeout_s=...)` 是替换外部 P 的入口。请求包含已登记的 execution/report/feedback、当前测量、候选事实和共同证据来源；返回 `PStateAck` 或 None（待处理）。本地参考实现是无外部数据库的纯融合逻辑，当前状态从共享事件账本恢复。

- 同一个 update_id 对同一输入幂等；同 ID 不同内容拒绝。请求先落盘，重试继续使用原 ID 和剩余次数。
- 已知事实必须由本次允许的测量或已校验候选支持。P 可以保守地返回 unknown，不能把无支持的候选升为 true。
- 同源原始测量与 CoF 派生事实保留共同 lineage，不作为两票累计。当前来源冲突变成 unknown，保留冲突和引用，后续可通过新的明确观测解决。
- 缺少当前观测时不继续沿用旧的 known=true；历史快照仍保留。旧 execution 的迟到反馈只进入历史，不能覆盖当前快照或冒充最新模型上下文。
- 只有匹配的处理确认才解除“等待 P”门控。新反馈到达后，旧确认不能继续使用；原始 backend.observe() 也不能绕过 P 门控直接完成验证。

默认最多 200 个更新请求、每个请求三次尝试、10 秒处理时限。`StateLimits` 可从程序注入。本地/外部适配器须遵守传入的期限；协调器拒绝超时返回并限制重试次数，但不会强行杀死在当前线程中阻塞的任意外部 P 回调。远程实现应使用有硬超时的客户端。

接外部 P 时需要把其对象 ID、时间、证据 lineage、状态字段与处理标识翻译到这份契约。仅返回一个更大的版本号或一段总结文本不足以完成交接。额外状态来源要先作为获准测量接入，不能由外部 P 的断言直接扩张证据权限。

## 执行、验证和恢复

`StateCoordinator` 连接 `PlanningManager`、`Executor`、反馈分析器和 P 适配器。`run_model_loop(..., coordinator=...)` 复用原有模型循环，不另写一套进度与预算逻辑。

- 执行前复查实际测量是否仍匹配已确认的 P 快照；变化时拒绝旧派发，生成器按原有有限修正流程刷新状态。
- 同一 SQLite 派发事务内再次检查闭环门控。重启后如果只恢复执行器或规划器、遗漏闭环协调器，会拒绝新派发。
- 执行 error、cancelled、timed_out 的有效变化先交接 P，再沿用原停止策略。Python/API 错误和物理效果达成可以同时存在，但不会自动重放。
- outcome_unknown 仍锁住后端。P 或 CoF 看起来认为任务完成，也不能证明机器人已停止。显式对账的新 report revision 会产生新的反馈与 P 处理确认。
- 最终验收前刷新 P 当前状态；历史节点都完成后若目标被扰动，不能直接结束。

默认只进行被动读状态/相机的补观察。观察建议不是可执行代码，尚未实现通过该建议直接移动相机或机器人。执行中的连续采样、稳定性窗口和真实环境看门狗仍需后端扩展。

## 日志与回放

新增内容全部写入原来的 `events.sqlite3`：

```text
feedback/cof-*/request.json   冻结身份、查询、选择后的帧/测量清单、缺口、输入 hash
feedback/cof-*/feedback.json  候选事实、终态判断、时间变化、未知与来源
state/p-update-*.json         P 输入与处理确认、状态快照、冲突及处理水位
closed-loop-summary.json      本次闭环的状态与统计
events.jsonl                  单一账本的可读导出
```

原来的 models、planning、executions 产物继续保存。`replay` 直接从账本重建计划、进度、P 状态与 CoF 报告，不驱动环境。CLI 当前只启动新运行和回放；原后端会话恢复、外部 P 运维对账需显式适配，不能新建一个 mock 后端接管旧物理会话。

## 验证范围

169 项测试通过，其中本阶段新增 50 项。15 种合成演示覆盖正常、空抓重试、掉落恢复、遮挡与持续未知、缺帧、部分执行后错误、取消、超时、停止未知、恢复预算、初始目标已满足、最终目标受扰动、P 延迟与 P 超时。

缺帧场景仍可能通过可信结构化测量完成任务，但反馈明确记录视觉缺口；这不是视觉成功案例。多图请求格式、图像关联、候选引用和共同模型额度通过预设响应测试，未测真实 VLM 判断质量。真实 P、真实感知与仿真联调仍待完成。
