# 机器人 Coding Agent：闭环控制与验证开发文档

版本：v0.1 · 日期：2026-09-24 · 状态：待实现的开发设计

2026-09-28 实现进展：已将执行、CoF、P 处理确认、验证和原有规划管理接成合成闭环；当前使用本地 P 参考适配器，真实 P/模型/仿真未验收。实际支持范围见[第四阶段说明](/Users/agiuser/Documents/Codex/2026-09-24/ca/cap-x/docs/stateful-closed-loop.md)，下文仍保留完整开发设计。

适用代码基线：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

总入口：[开发总览与使用指南](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-development-guide.md)。

配套文档：[任务规划与进度管理](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/task-planning-progress-dev.md)、[代码生成与执行](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/code-generation-execution-dev.md)、[CoF 执行反馈](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/cof-execution-feedback-dev.md)、[联调与评测](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/integration-evaluation-dev.md)、[整体方案](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-plan.md)。

本文细化 Verifier 与 Loop Controller，沿用前三份专项文档的共享契约。新增字段、接口和配置均为拟议扩展；本文不表示已完成代码改造、通过仿真测试或验证了机器人性能。与整体方案中的早期简写冲突时，共享类型以专项文档为准。

## 1. 开发目标与职责边界

本模块根据当前任务、实际执行报告、CoF 证据和 P 提供的新状态，完成两个职责：

1. **Verifier：判断冻结条件是否成立。** 输出逐条件、有来源和时效范围的 VerificationReport。
2. **Loop Controller：决定后续流程。** 调度继续、验证、观察、恢复、重规划和停止，并通过统一进度存储提交状态事件。

目标是使系统明确区分代码运行结束、动作效果成立、子目标完成和整个任务完成，并在失败或信息不足时选择可追踪的后续处理。

### 1.1 责任划分

| 模块 | 负责 | 本文的边界 |
| --- | --- | --- |
| Planner | 分解任务、选择子目标、生成与校验 PlanPatch | Controller 决定何时调用；不另写一套任务图规划器 |
| 进度管理器 | TaskProgress、attempt、恢复组、预算账本及事件的单一写入 | Controller 提交事件，不直接任意修改进度字段 |
| Code Generator / Executor | 生成短段代码、受控执行、报告 API/运行结果、停止与对账 | Controller 派发请求；不直接调用底层运动 API |
| CoF | 从实际帧链与调用记录提出事实、事件、偏差及未知 | Verifier 检查候选证据；不把 CoF 自述直接作为成功标签 |
| P：状态与上下文 | 对象身份、跨轮状态、证据融合、历史与上下文 | 本模块只读 StateView，要求更新确认，不维护第二份 WorldState |
| Verifier | 注册谓词适配、检查条件、证据与时间校验 | 不执行机器人动作，不更改验收标准，不自行恢复 |
| Loop Controller | 决策优先级、调度、预算门控、恢复协调、结束验收 | 不把模型建议直接写成成功，不解除执行端的未知状态锁 |
| 独立评测器 | 依据实验协议评估实际任务结果 | 非特权实验中隐藏真值不提供给在线 Verifier |

Verifier、Controller 是逻辑模块，不要求分别部署独立 LLM。第一版用程序实现流程和条件聚合，语义判断复用 CoF 或受限验证调用，恢复策略交给现有规划/代码模型。

独立验收表示结论需要检查环境证据和固定判据；额外调用同一个模型并不自动产生统计独立性。

### 1.2 第一版范围

- 一个仿真后端、单臂、串行子目标；先固定状态与故障注入，再接 CoF/P 和真实模型。
- 每个短动作段结束后验证；不在第一版承诺 VLM 在线中断正在执行的运动。
- 支持正常推进、效果失败、结果未知、局部恢复、计划修订、取消、预算耗尽与重启对账。
- 条件来自有限 PredicateCatalog，列表语义为 AND；不运行模型临时生成的判断表达式。
- 保留原 Cap-X baseline；在 `agent_mode: stateful` 下接入新流程。

## 2. 现有基础与接入位置

以下行号对应基线提交，接入前应按符号定位复核。

| 位置 | 已有行为 | 本文改造 |
| --- | --- | --- |
| `capx/envs/trial.py`：`_handle_multi_turn_step()` | 组合代码历史、日志和视觉反馈，由模型决定重生成或结束 | 新分支输出结构化控制请求；增加正式验证与调度 |
| `capx/envs/trial.py:833` | regenerate 时替换后续代码块 | 区分同一 attempt 正常推进、局部修复和 PlanPatch |
| `capx/envs/trial.py:855` | finish 后退出执行循环 | 成功出口经过最终验证；其他出口保留具体原因 |
| `capx/utils/launch_utils.py:310` | 不含 REGENERATE 的回复一律解析为 finish | 严格 schema；格式错误有限修复，不能落入成功分支 |
| `capx/envs/trial.py:869` 附近 | 保存阶段产物和 task_completed | 接入控制/验证事件，隔离在线反馈与评测真值 |

现有多轮循环已能根据反馈修改代码。本模块的增量是明确的验证契约、控制动作、状态更新门槛与停止语义。

## 3. 运行流程与核心不变量

```text
开始 / 恢复任务
    ↓
核对任务状态、在途执行、预算与当前观测
    ↓
Planner 选择子目标 → 冻结 SubgoalContract
    ↓
子目标前置验证 → 代码提案/校验 → 冻结 SegmentContract
    ↓
派发前检查本段入口、相关事实时效与预算
    ↓
Executor 执行一个动作段
    ↓
执行结果对账 + 原始证据 + CoF 候选反馈
    ↓
P 融合有效变化，返回状态版本/已处理证据标识
    ↓
Verifier 检查本段效果、继续条件和适用的子目标条件
    ↓
Controller 决定后续 → 统一事件账本提交
    ├─ 继续当前子目标 / 推进下一子目标
    ├─ 补观察 / 补验证
    ├─ 局部恢复 / Planner 修订计划
    └─ 最终验收成功 / 带原因停止
```

核心不变量：

1. runtime_rc=0、CoF ready、模型请求 finish 均不能直接推出目标成功。
2. 检查使用派发前冻结的条件；执行后不能降低标准、改目标或遗漏难以检查的条件。
3. 所有关键条件都允许 unknown；未知不满足正条件，也不满足否定条件。
4. 本段尚未完成整个子目标，可以正常继续；不能仅凭子目标条件 fail 判定本段失败。
5. 入口、段后继续、过程保持和最终条件有不同时间语义，不能混用。
6. 失败、部分执行和取消产生的有效事实也交给 P，不只更新成功轨迹。
7. 后端仍运行或执行结果未知时，禁止新运动、冲突 PlanPatch 和成功结束。
8. 历史成功保留；当前依赖条件失效时重新恢复，不要求过去所有中间效果永久成立。
9. Controller、Planner、Executor 共用 attempt/预算/派发账本；重规划、改 ID、重启均不重置总量。
10. 决策基于有版本的输入快照，提交和派发时再次核对；迟到反馈不能直接更新当前进度。
11. 最终任务的实际评测与在线验证分开；oracle 模式单独声明。
12. 停止调度、Python 退出和机器人已停止是三个事实，必须分别记录。

## 4. 共享契约与新增类型

### 4.1 沿用的类型与枚举

| 类型 | 沿用约定 |
| --- | --- |
| Condition | `{id, predicate, args, expected}`；有限谓词，AND 条件列表 |
| SubgoalContract | preconditions 是新 attempt 入口条件；acceptance_conditions 是子目标验收 |
| SegmentContract | entry_conditions、expected_conditions、continue_conditions、attempt_id、budget 等保持原定义 |
| ExecutionReport.status | completed / error / timed_out / cancelled / outcome_unknown |
| VerificationReport.scope | segment / subgoal / final_goal |
| VerificationReport.checks[].verdict | pass / fail / unknown；针对完整 Condition 命题，不是原始布尔读数 |
| TaskProgress.task_status | active / succeeded / failed / blocked / budget_exhausted / interrupted |
| CoFFeedback | condition_evidence.assessment 为 supported / refuted / unknown；正式验收仍由 Verifier 产生 |

聊天中的 CONTINUE、REPAIR、FINISH_SUCCESS 是行为说明。代码接口采用本文第 6 节的小写 ControllerDecision 枚举，不替换已有 PlannerDecision 或任务状态枚举。

新增 phase、condition_groups 等字段时，同步升级共享 schema 与全部生产/消费方，尤其是进度模块的报告校验；严格旧版消费者不能直接接收扩展字段。下面的 schema_version=1.0 表示待实现协议的首版样例，不代表已发布的兼容接口。

### 4.2 VerificationRequest

新增请求由 Controller 构造，不允许执行代码自行创建或修改正式请求。

| 字段 | 语义 |
| --- | --- |
| request_id / schema_version | 服务端分配，协议版本固定 |
| episode_id / task_version / plan_version | 冻结任务与计划关联 |
| scope / subject_id | 检查动作段、子目标或最终目标；对应 segment/subgoal/task ID |
| execution_id / attempt_id | 对应已派发执行；初始或独立最终检查允许 null |
| phase | before_dispatch / after_segment / final_check，限定检查时机 |
| condition_groups | 条件 ID 分组，见下文；定义来自已有契约，不接受替换后的条件正文 |
| contract_ref / query_spec_ref | 冻结契约与 CoF AnalysisQuery；保留定义版本和 hash |
| state_version / evidence_manifest_ref | P 快照与允许读取的实际证据范围 |
| execution_report_ref / cof_feedback_ref | 可为空；存在时固定报告版本，不能随读取变化 |
| evidence_cutoff / event_watermark | 检查截止范围和已知事件序号；时钟域写入引用元数据 |
| predicate_catalog_version / policy_version | 判据和来源/新鲜度规则版本 |
| verification_budget | 模型/检索/耗时上限，来源于权威账本 |

condition_groups 的键为 preconditions、entry_conditions、expected_conditions、continue_conditions、acceptance_conditions、goal_conditions。一次请求只带 scope/phase 允许的组。过程保持条件使用冻结的 AnalysisQuery 关联，不把 continue_conditions 重新解释成全过程约束。

不同时间语义的检查应使用不同 request/query ID。若同一 condition 在不同组复用，其定义及该次检查的时间语义必须一致；否则拆分请求。

**只有 subgoal 的 acceptance_conditions 验证才能更新子目标完成，只有 final_goal 的 goal_conditions 验证才能支持任务成功。** 前置条件报告即使全部 pass，也不能被进度模块当作完成报告。

TaskSpec.constraints 中需要验证的过程约束、必需事件和稳定性要求，须在执行前映射为冻结的检查清单及 AnalysisQuery，并与最终请求关联。纯执行限制由 Gateway/预算事件提供遵守记录。成功门控同时要求这份清单完整通过；无法映射或缺证据的要求不得在最终验收时静默忽略。

### 4.3 VerificationReport 扩展

保留已有必需字段：report_id、episode_id、plan_version、scope、subject_id、execution_id、state_version、checked_at、checks。为明确消费规则，新增：

- request_id、schema_version、task_version、phase、condition_groups 与 query_spec_ref。
- execution_report_ref、cof_feedback_ref、evidence_manifest_ref、event_watermark，以及判据/策略版本。
- checks 每项保留 condition_id、verdict、evidence_refs、source、observed_at；扩展 query_id、reason_code、evidence_summary。
- group_verdicts：程序按组聚合，属于派生字段，不接受模型自行填写后跳过重算。

缺失的请求项由程序补为 unknown/MISSING_CHECK；整体身份、契约 hash 或引用根不合法时拒绝报告，不能将其余条目直接提交。合法的 partial CoF 可支持一部分检查，其余保留未知。

每次新验证分配新 report_id，报告不可变。需要纠正旧结论时，新报告引用被替代报告和原因；不得覆盖历史应用事件。执行报告的 report_revision、CoF 的 revision 沿用各自规则。

### 4.4 ControllerDecision

| 字段 | 语义 |
| --- | --- |
| decision_id / kind / reason_code | 服务端 ID、严格枚举、简短理由 |
| episode_id / task_version / plan_version | 决策对应的任务版本 |
| base_progress_revision / state_version / event_watermark | 提交时检查的一致性条件 |
| verification_refs / execution_report_ref | 决策依赖的报告快照 |
| subgoal_id / attempt_id / execution_id | 根据 kind 要求提供，不由模型重分配 |
| payload | 类型化的观察、验证、恢复、重规划或停止请求 |
| evidence_refs / policy_version | 可审计依据与控制规则版本 |

ControllerDecision 是运行时的调度结果。模型可以提出恢复策略和 reason，但不能直接设置 base revision、成功状态、尝试次数、预算或后端停止标志。

## 5. Verifier 的实现规则

### 5.1 谓词注册表

每个 PredicateSpec 至少声明：名称、参数类型、单位/坐标语义、允许证据源、所需观测字段、时间窗口、有效性/失效规则、实现版本和评估函数。

第一批可采用 pose_known、gripper_empty、holding、lifted、above_for_placement、on、gripper_open。这些是领域适配器待实现能力，不能假定 Cap-X 已提供。

- pose_known 需检查对象身份、坐标系及位置是否仍有效。
- holding 需要符合该后端协议的传感器或视觉证据；“夹爪闭合”不能单独证明抓到目标。
- on 需要任务定义的支撑关系；图像里上下相邻不自动等于受到支撑。
- stable_for_window 要求窗口、容差与覆盖规则；不能用一张末帧代替。

调用路径采用 `registry[predicate].evaluate(...)`。不执行模型输出的 Python 条件字符串。可以借鉴 Code-as-Monitor 的代码化约束思想；任意生成监控代码不属于首版授权接口。

### 5.2 检查顺序

1. 核对请求身份、冻结条件、组别和检查时机；验收组不能为空。
2. 核对证据真实存在、来源获准、执行/对象/时间一致；预测帧和执行者自评不可作为完成证据。
3. 检查每条相关事实的新鲜度与后续反证，不仅比较 state_version 或 checked_at。
4. 调用谓词适配器，返回已观测布尔值或 unknown，以及支持/反对证据。
5. 与 expected 比较，形成 pass / fail / unknown；expected=false 时同样要求明确证据。
6. 校验完整覆盖并聚合；持久化报告后交给进度/控制模块消费。

若谓词或传感器适配器未实现，返回 unknown/UNSUPPORTED_CHECK，并在计划校验阶段阻止依赖它的不可验证方案进入执行。不得临时用模型猜测补齐。

### 5.3 聚合与时间语义

对一个非空 AND 条件组：全 pass 才为 pass；至少一项 fail 则为 fail；其余情况为 unknown。聚合结果不丢弃单项 unknown，例如整体已 fail 时，恢复动作仍可能需要先观察未知的物体位置。

| 语义 | 可以得出的结论 | 不允许的推断 |
| --- | --- | --- |
| at_end | 截止范围内最新有效观测支持的段末状态 | 用早先成功帧忽略后续掉落 |
| occurred | 窗口内有证据表明事件发生过 | 曾抬升等于现在仍夹持 |
| maintained | 指定过程和覆盖规则下关系保持，或存在反例 | 稀疏帧证明未采样时刻也始终成立 |
| stable_for_window | 指定时长、覆盖和容差下成立 | 多次立即调用同一帧当作时间窗口 |

有限采样的结论标明采样支持范围；如果任务要求更强的连续监测而后端不具备，返回 unknown。过程违规是否使任务不可恢复，由冻结任务约束定义；后来恢复的正确终态不能抹掉不可逆的过程违规。

### 5.4 多源证据与未知

P 与 CoF 引用相同原始帧时共享 evidence lineage，不作为两份独立证据加权。模型多次同意、相邻相似帧数量增加，都不能自动提高结论等级。

不同来源冲突时，按 PredicateSpec 预先约定的适用性和可靠性规则处理并记录反证；无法解决则 unknown/EVIDENCE_CONFLICT。不能默认传感器或 VLM 永远优先，也不能只保留支持成功的来源。

Verifier 的语义模型调用只读证据，不拥有运动工具；输出短 evidence_summary 和引用，无需输出冗长自由推理。数值置信度不在首版门控中使用；以后引入时需校准并报告拒判覆盖率。

### 5.5 验证与进度的关系

- 段级 expected_conditions 全 pass、continue_conditions 全 pass，但子目标验收未全 pass：可以正常多段推进。
- 子目标验收全 pass：记录历史完成；下一动作仍要检查自己的入口条件。
- 必需的过程条件 fail：进入恢复或约束违规处理，不能只凭终态正确忽略。
- 某条件 unknown：只阻断依赖它的动作；独立且已验证的历史结果仍可保存。
- 运行 error 但子目标验收 pass：保留两份事实；是否继续取决于故障解除与后端状态。

验收标准不包含“必须无 stderr”。运行故障与环境目标分开判断；也不能因为环境目标达成而删除错误记录。

## 6. Controller 决策与优先级

### 6.1 控制动作

| kind | 触发与必要条件 | 处理 |
| --- | --- | --- |
| continue_segment | 当前段符合预期、继续条件通过，子目标未完成且预算允许 | 同一 attempt 生成下一段；新入口再次验证 |
| advance_subgoal | 子目标验收通过且无必须先处理的运行故障 | 提交验证事件，调用 Planner 选择下一子目标；不自动派发 |
| request_observation | 后续决策依赖缺失、过期或冲突信息 | 提交有问题/对象/证据缺口的 ObservationRequest |
| request_verification | 已有候选证据但尚无有效正式验收 | 创建 VerificationRequest |
| repair_current | 已确认停止；原子目标仍有效；存在有依据的局部调整 | 结束失败 attempt，校验恢复提案；实际派发时登记新 attempt |
| request_replan | 依赖失效、步骤缺失、局部恢复耗尽或方案不适用 | 提交受影响节点和失败证据给 Planner |
| wait_for_execution | 有运行中动作、未对账派发或 outcome_unknown | 查询原 execution，必要时请求停止；不新建动作绕过锁 |
| finish | 最新最终目标与任务约束通过、无在途/未对账执行 | 原子提交 TaskTerminated(succeeded) |
| stop | 取消、预算耗尽、不可恢复失败或当前阻塞 | 停止派发，按第 9 节记录原因和运动状态 |

advance_subgoal 只代表计划调度，不能省略下一步前置条件检查。等待和观察也不是无限循环：分别消耗或受限于查询/时间/信息获取预算。

### 6.2 决策顺序

1. **接收外部取消及硬约束事件。** 撤销新动作派发权限；在途动作交给 Executor 停止与对账。保持原始停止原因，不被后来到达的模型 finish 覆盖。
2. **核对执行生命周期。** 无可靠停止/完成确认则只允许等待、停止协调和获准的非干扰状态查询。
3. **处理已有结果。** 去重并保存执行、CoF 和验证报告，确认 P 已处理相关有效变化或明确缺失项。
4. **检查最终成功候选。** 初始化、计划耗尽、Planner 请求 finish、最终目标看似达成或恢复完成时，提出最终验证请求。已有有效最终报告通过时可完成；新请求实际提交前仍必须经过下一条预算门控。
5. **检查下一操作预算。** 拒绝超额动作/调用；预算不足且没有有效最终成功证据时停止。已完成的有效验证不会仅因执行余额恰好为零被改判失败。
6. **处理决策必需的 unknown。** 根据缺口选择已有证据回看、当前观测或验证；不自动升级为物理失败。
7. **处理明确失败。** 结合执行故障、失效条件和历史选择局部恢复或请求重规划。
8. **正常推进。** 已通过子目标验收则 advance；否则只有本段预期与继续条件通过时才 continue。

每个昂贵操作包括最终验证都需要预算。建议预留最终观测/验收额度；总时限已到时不能为了获取成功标签继续无限调用。终态物理结果可能已达成但缺验证时，记录 `completion_verified=false` 和证据缺口，不猜测结果。

### 6.3 与 PlannerDecision 的衔接

PlannerDecision 保持规划文档已有枚举。execute 转入生成、入口验证与派发流程；request_observation / request_verification / request_replan 转成对应受控请求；wait_for_execution 转入对账；finish 只触发最终成功门控；blocked 根据原因等待外部变化或停止本次调度。

Controller 不用第二次自由 LLM 决策重新推翻已有合法选择。模型主要在生成恢复方案和 PlanPatch 时调用；关键生命周期、成功验收、预算和协议规则由程序执行。

格式错误、空回复、未知 kind 返回 CONTROL_INVALID，允许有限结构修复。修复不能修改事实或诱导模型反复回答成功；仍非法则以协议错误停止或阻塞，不走 finish。

## 7. 观察与局部恢复

### 7.1 ObservationRequest

请求包含 request_id、episode/plan 关联、question、condition_ids、对象、缺失原因、允许观察方式、预算和基于哪个状态提出。复用 CoF 文档的 replay_interval、refresh_current_observation、observe_other_view、resolve_identity 分类。

- 历史回看交给只读证据检索；不能恢复当时没有采集的帧。
- 当前刷新经 P/环境协调接口；新观测可以确定现在，不能补证历史上曾经发生的事件。
- 移动相机或机器人以消除遮挡，必须形成受约束动作段，经 Executor 执行并记录副作用。
- 后端状态未知时，只允许 capability 声明为非干扰的查询；不得用观察请求隐藏新运动。
- 相同问题、相同证据、相同状态的重复请求不算获得新信息；达到上限后明确阻塞或预算耗尽。

观察不增加 manipulation attempt_count，但实际运动、控制步数、LLM/图像与耗时仍计入对应执行和总预算。不得靠把抓取命名为观察绕开恢复组限制。

### 7.2 RecoveryRequest

恢复请求包含：目标子目标/恢复组、失败条件、已确认事件、当前相关状态、部分效果、已尝试策略、剩余预算、失效产物引用、原因假设和缺失信息。

| 情况 | 首选处理 |
| --- | --- |
| 派发前代码/参数格式错误 | 有限修复代码；没有实际派发不增加 attempt |
| 空抓且目标仍可定位 | 换抓取候选/接近方式；实际重新尝试用新 attempt |
| 搬运中掉落 | 确认当前位置，恢复夹持，再搬运；涉及新增依赖时提交 PlanPatch |
| 未达运动目标 | 保留实际姿态，检查候选路径/输入；不能直接执行后续释放 |
| 目标被遮挡或身份不确定 | 先补观察，不直接恢复抓取 |
| 当前顺序缺少必要准备步骤 | 请求 Planner 插入准备步骤 |
| 相同失败反复出现且没有新调整依据 | 提升到局部重规划或阻塞，不原样重试 |

原因未知不妨碍记录已观察到的掉落，但不能据此断言夹力不足。恢复方案需说明将改变什么以及依据；同一次尝试内正常多段推进与失败后的重新尝试分开登记。

### 7.3 部分执行和计划修订

实际动作不回滚。恢复前通过新状态检查已经生效的效果，失效旧位姿、抓取候选、路径与依赖产物；不恢复旧 Python 栈，也不默认从异常代码行继续。

PlanPatch 沿用规划文档规则：匹配 base_plan_version/base_progress_revision，保留已完成历史，替代未完成节点时标 superseded，重连后继、检查无环与最终目标覆盖，继承恢复组预算，原子增加版本。

在途动作未确认停止时不能提交影响该动作的补丁。旧执行的迟到事实可留作历史；新计划的派发需要基于当前条件重新验证。

## 8. 最终验收与任务状态

### 8.1 成功出口

成功提交必须同时满足：

1. TaskSpec 原始目标的全部 goal_conditions 已由有效 final_goal 报告验证 pass。
2. 任务约束和必须发生的事件要求均已满足；不可恢复的过程违规不存在。
3. 当前无在途命令、未对账派发或 outcome_unknown；后端与 worker 生命周期已明确。
4. 报告匹配当前任务和计划，事实仍有效；验证截止点之后没有相关新动作或反证。
5. 没有尚待处理的外部取消/停止事件；提交时 revision 和事件水位仍一致。

final_goal 报告只证明指定窗口内结果；成功结束后不承诺对环境永久监控。若结束前目标又被扰动，旧报告失效，需要重新验证/恢复。

任务开始时目标已满足，也应通过最终验收后直接完成。计划为空、节点全 succeeded 或模型输出 finish 都不是成功依据。

### 8.2 任务状态与原因

| task_status | 适用情况 | 必须记录 |
| --- | --- | --- |
| succeeded | 成功门控全部通过 | 最终验证引用、完成时刻、任务版本 |
| failed | 已确定的不可恢复约束违规，或明确的系统失败终止 | reason_code、失败层级、证据；不泛化为任务物理上不可能 |
| blocked | 当前缺信息/能力/外部条件，无法继续 | blocker、解除条件、未解决执行及后端锁 |
| budget_exhausted | 本次运行的有效预算已耗尽 | 耗尽维度、实际用量、未完成条件 |
| interrupted | 用户取消或明确的外部中断 | 原始请求、停止/对账状态、检查点 |

终止结果拟增加 completion_verified、remaining_condition_ids、unresolved_execution_ids、backend_motion_state、stop_evidence_refs。completion_verified 表示是否具有本次最终完成的有效证明，不等同于运行是否出错。

blocked/interrupted 可在明确恢复事件后重新进入 active，保留原有预算和历史；用户取消后不得自动恢复。预算耗尽需要显式的新运行预算决议，不能重启后清零；原始累计用量仍保留。

## 9. 超时、停止和执行结果未知

ExecutionReport.outcome_unknown 属于执行生命周期问题，不是感知未知。即使图像显示红块已放好，也不能据此确认机器人不会继续执行队列中的命令。

停止流程：持久化 StopRequested → 禁止后续写动作 → Executor 撤销权限并请求当前动作停止 → 查询 worker/后端完成状态 → 获得停止证据 → 获取必要观测 → 保存任务状态与剩余问题。

停止确认超时后，保留 unresolved_execution_ids 和后端锁，进入阻塞/人工处理或保留已请求的 interrupted 状态；不得声称“机器人已停止”。此时任务调度可以已暂停，但仍需独立的执行对账路径处理迟到状态。控制程序结束不应关闭执行端仍需完成的停止协调。

用户取消、环境终止和预算停止都不允许自动重新派发原动作。环境 terminated/truncated 的具体含义由后端适配器给出，不能简单映射成 succeeded；成功需要任务证据。仿真 reset 创建新 episode，不在原轨迹中伪装成恢复。

若延长运行等待会超过任务时限，停止/对账工作仍须由执行服务继续处理；该处理属于生命周期收尾，不授权新的任务动作。

## 10. 版本、一致性与持久化

### 10.1 状态与证据交接

P 接收 ExecutionReport 和有效 CoF 候选，即使执行失败也更新真实变化。建议适配器返回已处理的 execution/feedback ID、最新 StateView 与未解决冲突，或等价的 watermark；单看 state_version 增长不能证明最新反馈已融合。

P 暂时不可用时，可只读原始证据做历史验证，但在依赖新状态的派发前等待融合/刷新；超时明确阻塞。Verifier 不绕过 P 建立第二份持久当前状态。

### 10.2 单写者提交

使用规划/执行模块同一事务账本或同一追加事件流，记录 VerificationRequested、VerificationProduced、VerificationApplied、ControlDecisionCommitted、ObservationRequested、RecoveryRequested、StopRequested、TaskTerminated 等事件。名称为拟议新增事件，已有同义事件直接复用。

- 消费时匹配 episode、任务/计划、执行、条件 hash 和输入快照；同 ID 同内容幂等 no-op，同 ID 不同内容报完整性错误。
- 提交控制结果采用 base_progress_revision/event_watermark 检查；冲突时从新快照重算，不执行旧决定。
- 验证通过后与派发之间再次检查依赖事实是否过期、是否出现相关反证或取消事件。无关对象更新可在复核后继续。
- Observation/Execution 等外部请求通过持久 outbox 发送；重送使用原请求 ID。账本先登记，执行端再幂等接收。
- TaskProgress 与预算由同一权威记录更新，不在 Controller 内另维护一套可漂移计数。

软件幂等不保证物理世界 exactly-once。发送命令后日志丢失时，恢复流程先查询原 execution 和当前环境，不能换 ID 重发。

### 10.3 迟到报告与重启

迟到结果按执行与观测时间归档，不能按到达顺序覆盖当前事实。旧计划报告不得直接推进当前节点；可通过新请求引用仍有效的证据重新验证。较高 ExecutionReport revision 可解析 unknown，按执行文档规则应用，不重复登记尝试和预算。

重启只恢复事件、计划、预算、未完成请求和锁。存在派发意图但无可靠终态时，先对账；恢复后重新观察并检查当前条件。不得自动重启已被用户取消的任务。

## 11. 预算与无进展控制

预算沿用 planning/execution/CoF 配置并由统一账本计算。Controller 负责对下一请求预留、结算和拒绝超额派发，不重复扣除各模块已经报告的同一笔用量。

至少记录：episode 总耗时、执行段、控制步数/API 调用、LLM/token/图像成本、观察请求、验证调用、重规划次数、每 attempt 段数、恢复组尝试数。未知执行的预留不能直接释放，需先对账。

无进展判据采用任务相关条件及信息变化：新增有效证据、关键 unknown 得到解决、目标条件推进或必要恢复步骤完成。仅修改 plan_version/state_version、重复模型解释、换一个 ID，不构成进展。

保存有界决策指纹：当前子目标、关键条件结果、失败类别、拟执行策略与参数摘要、证据版本。相同状态下重复同一无效策略达到上限时，要求改变有依据的策略、请求重规划或停止。是否允许相同动作有限重试由技能策略明确；不能一概禁止随机执行器的合理重试。

局部恢复耗尽不自动意味整个任务预算耗尽；可在全局预算内请求不同有效方案。同一恢复目的的新节点继承 recovery_group_id 限制，禁止借重规划重置尝试次数。

配置草案如下；数值只用于 fake/仿真开发，未经过性能或部署验证：

```yaml
agent_mode: stateful
verification:
  predicate_catalog: manipulation_v1
  evidence_profile: observed_only
  max_model_calls_per_request: 2
  max_schema_repairs: 1
  request_timeout_s: 15
  # 新鲜度、稳定窗口、时钟误差和容差由每个 PredicateSpec 提供。
controller:
  mode: segment_end
  max_decision_schema_repairs: 1
  max_repeated_ineffective_decisions: 2
  state_update_timeout_s: 10
  execution_reconcile_timeout_s: 10
  reserve_final_verification_calls: 1
  automatic_resume_after_user_cancel: false
```

复用 planning.max_attempts_per_recovery_group、max_segments_per_attempt、max_replans、max_observation_requests_without_progress、max_execution_segments，以及执行/CoF 各级预算，不在此复制新的同义配置。启动时校验预算和后端能力一致；最终验证预留还需覆盖所需观测/时间，不能只预留一个空调用名额。

max_model_calls_per_request 包含结构修复调用；示例最多一次原始语义调用加一次格式修复，不能在原上限之外追加。纯程序验证不必调用模型；所有调用还受父级总预算和 request_timeout_s 约束。

## 12. 贯穿案例与接口样例

### 12.1 搬运掉落后的完整流程

以下为开发 fixture，不是实际实验结果。任务为红块稳定放在绿块上并释放夹爪。

| 步骤 | 实际证据/验证 | 控制与进度 |
| --- | --- | --- |
| 抓取与抬升 | holding、lifted 验收 pass | 记录抓取历史 succeeded |
| 搬运一段 | Python completed；CoF 记录先共同运动后分离 | P 更新物体落到桌面；Verifier 检查段末 holding fail |
| 决策 | 无在途动作，位置可确认；原搬运依赖失效 | request_replan，提交恢复抓取和重新搬运补丁 |
| 恢复 | 新 attempt 抓取后验收 pass | 历史原抓取不删除，恢复组/全局预算累计 |
| 再搬运 | 本段中间位置达成，子目标终点尚未到 | continue_segment，同一 attempt 执行下一段 |
| 放置释放 | 释放符合阶段语义，不能触发掉落式恢复 | 请求 final_goal 验证 |
| 稳定窗口 | on、gripper_open 等原始最终条件 pass | 无在途动作且提交无冲突，succeeded |

若掉落后位置看不清，则先 request_observation；若状态显示物体超出可达范围且无可用恢复技能，则 blocked 并记录能力缺口。

### 12.2 段级 VerificationReport

下面的引用均为 fixture ID，开发者需构造对应 manifest/契约。字段采用本文扩展版；新鲜度和时钟域等元数据从引用的 manifest 和 PredicateCatalog 读取。

```json
{
  "schema_version": "1.0",
  "report_id": "vr_transport_7",
  "request_id": "verify_transport_7",
  "episode_id": "episode_demo",
  "task_version": 1,
  "plan_version": 1,
  "scope": "segment",
  "subject_id": "segment_transport_7",
  "execution_id": "exec_transport_7",
  "state_version": 18,
  "checked_at": "2026-09-24T10:00:04Z",
  "phase": "after_segment",
  "condition_groups": {
    "expected_conditions": ["c_at_waypoint"],
    "continue_conditions": ["c_holding_red"]
  },
  "query_spec_ref": "queries_transport_7_v1",
  "execution_report_ref": "er_transport_7_r1",
  "cof_feedback_ref": "cof_transport_7_r1",
  "evidence_manifest_ref": "evidence_transport_7_v1",
  "event_watermark": 42,
  "predicate_catalog_version": "manipulation_v1",
  "policy_version": "observed_only_v1",
  "checks": [
    {
      "condition_id": "c_at_waypoint",
      "query_id": "q_waypoint_at_end",
      "verdict": "pass",
      "evidence_refs": ["sensor_end_effector_pose_7"],
      "source": "measured_pose_adapter_v1",
      "observed_at": "2026-09-24T10:00:03Z",
      "reason_code": "CONDITION_MET",
      "evidence_summary": "末端执行器位置在冻结的中间点容差内。"
    },
    {
      "condition_id": "c_holding_red",
      "query_id": "q_holding_at_end",
      "verdict": "fail",
      "evidence_refs": ["frame_transport_7_42", "frame_transport_7_51"],
      "source": "visual_relation_adapter_v1",
      "observed_at": "2026-09-24T10:00:03Z",
      "reason_code": "CONDITION_NOT_MET",
      "evidence_summary": "末帧目标在桌面，与夹爪分离；过程帧支持先抬起后掉落。"
    }
  ],
  "group_verdicts": {
    "expected_conditions": "pass",
    "continue_conditions": "fail"
  }
}
```

c_at_waypoint 在 fixture 契约中指末端执行器位置；它通过不意味着被搬物体也到位。最终子目标仍有其独立验收条件。

### 12.3 对应 ControllerDecision

```json
{
  "decision_id": "decision_transport_7",
  "kind": "request_replan",
  "reason_code": "REQUIRED_EFFECT_LOST",
  "episode_id": "episode_demo",
  "task_version": 1,
  "plan_version": 1,
  "base_progress_revision": 9,
  "state_version": 18,
  "event_watermark": 42,
  "verification_refs": ["vr_transport_7"],
  "execution_report_ref": "er_transport_7_r1",
  "subgoal_id": "s2_transport",
  "attempt_id": "attempt_transport_1",
  "execution_id": "exec_transport_7",
  "payload": {
    "affected_subgoal_ids": ["s2_transport", "s3_place"],
    "failed_condition_ids": ["c_holding_red"],
    "required_effect": "重新建立红块夹持，再恢复搬运依赖",
    "recovery_group_id": "transport_red",
    "history_refs": ["attempt_transport_1"],
    "cause_hypotheses": []
  },
  "evidence_refs": ["frame_transport_7_42", "frame_transport_7_51"],
  "policy_version": "segment_end_controller_v1"
}
```

决策不直接给当前子目标写 succeeded，也不直接执行恢复代码。Planner 提出 PlanPatch，程序校验并提交；新节点的恢复组分配由管理器统一审查。

## 13. 接口、目录与 Cap-X 接入

### 13.1 公共接口草案

以下为职责示意，不是可运行实现。模型、P、证据存储和后端通过依赖注入，核心逻辑不导入 GPU/仿真库。

```python
def build_verification_request(contract, snapshot, phase, groups):
    """绑定冻结条件、证据截止点和预算；不更改验收标准。"""
    ...

def verify(request, predicate_catalog, evidence_store, state_adapter):
    """只读检查，返回不可变 VerificationReport；不执行观察动作。"""
    ...

def decide_next(snapshot, accepted_reports, policy, budget_view):
    """依据固定优先级返回 ControllerDecision；无环境副作用。"""
    ...

def commit_decision(decision, event_store, outbox):
    """检查版本、幂等登记、预留预算并提交事件/外部请求。"""
    ...

def reconcile_execution(execution_id, executor, evidence_store):
    """查询原执行及停止证据，不能隐式重发命令。"""
    ...

def resume_task(resume_event, event_store, executor, state_adapter):
    """核对恢复依据、未完成执行和预算，刷新状态后重新决策。"""
    ...
```

### 13.2 建议目录

```text
capx/agents/stateful/
  verification/
    contracts.py        # 请求/报告扩展；引用共享 Condition 等类型
    registry.py         # PredicateCatalog、来源/时效/覆盖规则
    adapters.py         # 状态、传感器、几何与视觉证据适配
    verifier.py         # 逐项检查、未知处理、AND 聚合
    validation.py       # 引用、身份、时间和消费范围校验
  controller/
    contracts.py        # ControllerDecision 与类型化 payload
    policy.py           # 固定优先级与控制动作选择
    orchestrator.py     # 连接 Planner、Executor、CoF、P、Verifier
    recovery.py         # 恢复请求与重复无效策略检测
    lifecycle.py        # 停止、恢复、执行对账
    events.py           # 复用权威事件存储和 outbox 适配

tests/closed_loop/
  fixtures/
  test_verification.py
  test_controller_policy.py
  test_recovery_and_stop.py
  test_versions_and_replay.py
  test_fake_pipeline.py
```

共享类型从现有 planning/contracts.py 或统一 stateful/contracts.py 导入；不复制不同枚举。TaskProgress、预算、执行 ID 的分配与存储继续复用原模块。

### 13.3 接入顺序

1. 在 stateful 配置分支接管 trial 的多轮调度，保留环境初始化和产物保存。
2. 复用 Executor 的 dispatch/query/cancel，不将新代码送回会绕过 Gateway 的旧执行入口。
3. 在段后构建报告/帧 manifest → CoF → P 更新确认 → Verifier → Controller。
4. 初始条件和每段派发前调用同一谓词注册表，入口验证不能与段后验收维护两套标准。
5. 将原始模型 finish 限定为建议，新成功出口统一进入 final_goal 门控；明确环境停止和协议失败出口。
6. 在配置加载器显式透传 verification/controller 配置，启动时检查后端能力和判据齐全。
7. 保存所有 decision/request/report ID、来源版本、事件序号及预算；CLI 跑通后 Web 复用同一服务层。

## 14. 开发阶段与验收

### 14.1 开发阶段

| 阶段 | 内容 | 交付门槛 |
| --- | --- | --- |
| L1 契约与验证 | 请求/报告类型、谓词注册、三值逻辑、证据校验 | 固定 fixture 的条件/范围/时效判断正确 |
| L2 控制策略 | 决策表、优先级、观察与恢复请求、预算门控 | 相同输入可复现；无错误继续和无证据成功出口 |
| L3 生命周期 | 幂等、单写者、迟到反馈、停止、重启对账 | 未知执行不能重复派发；取消不自动恢复 |
| L4 完整 fake 闭环 | 接规划/执行/CoF/P adapter 和故障注入 | 正常、失败、未知、预算/取消轨迹端到端可回放 |
| L5 单仿真任务 | 接实际模型、帧和状态；独立评测 | 记录真实成功、错误宣告、恢复和成本，不只展示成功视频 |

L1–L4 可使用固定事实和回放，不等待所有模型组件完成。通过 fake 测试只证明调度规则，不能宣称感知或机器人恢复性能。

### 14.2 必须覆盖的行为测试

| 编号 | 场景 | 预期行为 |
| --- | --- | --- |
| L01 | runtime 正常但抓取效果 fail | 不推进成功，依据当前状态恢复 |
| L02 | 本段到达中间点，子目标终点未到 | 同一 attempt 正常 continue，不误判失败 |
| L03 | 验收缺项、遮挡或证据过期 | 对应 unknown，不默认 pass |
| L04 | expected=false，但谓词未知 | unknown，不因否定条件而通过 |
| L05 | 入口检查全 pass | 只准许派发准备，不标记子目标完成 |
| L06 | 抓取成功后搬运掉落 | 保存历史成功，阻断放置，恢复依赖 |
| L07 | 正常释放后 holding=false | 按阶段预期处理，不重新抓取 |
| L08 | 只有首尾帧，过程保持不可确认 | 过程 unknown；允许独立判定有充分证据的终态条件 |
| L09 | 单张末帧替代稳定性窗口 | 不通过 stable_for_window |
| L10 | P 与 CoF 同源；或多源冲突 | 不重复加权，按规则处理或 unknown |
| L11 | 预测帧、隐藏 oracle 真值、模型自报成功 | 非特权验收拒绝作为完成证据 |
| L12 | 运行 error，但后置条件已 pass | 保留错误与成功事实；故障未解除不继续 |
| L13 | outcome_unknown，但图像看似完成 | 保持执行锁，不 finish、不派发新动作 |
| L14 | 用户取消，随后迟到 finish | 取消意图不被覆盖，不自动恢复 |
| L15 | 取消/超时后停止确认失败 | 保存 unresolved_execution_ids，不宣称机器人停止 |
| L16 | 旧报告、跨 episode、重复 ID、同 ID 不同内容 | 拒绝/归档/幂等按规则处理，进度和费用不重复 |
| L17 | 验证通过后对象移动或提交 revision 冲突 | 拒绝旧派发，刷新相关条件后重算 |
| L18 | 改 plan/attempt/execution ID 或重启试图清零 | 恢复组与 episode 预算仍累计 |
| L19 | 相同未知反复观察且没有新证据 | 有界终止为阻塞或预算耗尽 |
| L20 | 最后一段用尽动作预算，已有有效最终验收 | 可以记录成功；没有证明则不能猜成功 |
| L21 | 初始化已达目标；或节点全完成但目标被扰动 | 前者验证后结束；后者继续恢复或明确停止 |
| L22 | 计划/控制输出格式非法 | 有限修复或协议失败，不走默认 finish |
| L23 | 派发后日志未完成即重启 | 先对账原执行，不重复运动 |
| L24 | P 未融合最新反馈但版本已增长 | 不误以为最新事实已就绪，等待确认/刷新 |
| L25 | 通过观察请求发起移动 | 经 Executor 和预算；不能绕过未知执行锁 |
| L26 | 过程约束不可逆违规但终态正确 | 不记录完整任务成功；保留违规证据 |

### 14.3 首个 Demo 产物

至少保存正常完成、空抓恢复、搬运掉落、遮挡未知、取消/超时对账、预算耗尽六类轨迹。每条包含冻结条件、执行与 CoF 报告、P 状态版本/处理确认、验证请求和结果、控制决定、PlanPatch、预算事件及最终状态。

回放工具重建逻辑时间线，不重新驱动机器人。交付记录分别说明已运行哪些纯逻辑测试、仿真测试和模型试验；未运行层级明确保留待验收。

## 15. 实验设计与文献依据

### 15.1 研究假设与指标

假设：固定模型、状态、底层技能、观测和总预算后，显式条件验收与分层恢复决策能够减少错误继续、错误宣告成功及重复无效动作，提高扰动后的完成率。

主对照可采用：匹配输入的原始 regenerate/finish 循环；增加最终验证；增加段/子目标验证；完整验证与分层恢复。原始 Cap-X 另保留为系统基线，明确额外状态、分段和 CoF 所带来的输入差异。

消融时固定 CoF/状态来源，分别移除 unknown 处理、过程条件、失败历史或局部恢复；实验性变体只用于受控仿真。CoF 本身的增益通过独立消融评估，不能同时替换多个模块后全部归因于 Controller。

| 指标 | 口径 |
| --- | --- |
| 最终成功率 | 由独立评测器判定，报告全部 episode |
| 错误宣告成功率 | 宣告成功但评测失败的数量 / 全部宣告成功；同时报告其占全部 episode 的比例 |
| 错误继续率 | 违反预先标注的必要继续/入口条件仍派发动作的比例 |
| 扰动恢复率 | 注入指定扰动且任务按实验定义可恢复的 episode 中最终成功比例 |
| 验证质量 | pass/fail 错误、unknown 比例及覆盖率；不靠大量拒判掩盖错误 |
| 恢复与检测成本 | 发现偏差到确认停止/恢复的延迟、无效动作、观察/验证/重规划调用 |
| 总成本 | LLM/token、处理帧数、API/控制步数、总耗时和实际预算消耗 |

使用相同任务/扰动种子和预算，多次运行报告离散性。观察、稳定窗口、验证与恢复的时间和成本均计入；失败原因区分感知错误、判据错误、决策错误、执行错误和预算不足。

批量实验的完整分母、无法评分样本、声明覆盖率、扰动触发率及重跑处理，以配套《联调与评测》第 7–11 节的细化口径为准；本节指标表不表示可以忽略缺失试验。

### 15.2 参考工作与采用边界

以下依据本次已核对的论文方法章节或作者项目页。具体 schema、控制枚举、优先级、预算和一致性协议是本项目设计，不是对任一论文的原样复现。

| 工作 | 来源 | 借鉴机制与边界 |
| --- | --- | --- |
| Inner Monologue，2022 | [作者项目页](https://innermonologue.github.io/) | 成功检测、场景描述反馈进入后续规划；支持段后闭环思路，不提供本文完整生命周期协议 |
| DoReMi，2023；2024 修订版 | [方法 §III](https://arxiv.org/html/2307.00329v4#S3) | 生成与执行相关的约束，执行期间用 VLM 检测违反并触发重规划；首版只做段后检查，不能宣称具有其在线打断能力 |
| REFLECT，CoRL 2023 | [作者项目页](https://robot-reflect.github.io/) | 从多传感器历史生成层次摘要，渐进解释失败并生成修正计划；本文保留事实/原因假设边界，不保证 RGB 可推断物理根因 |
| Code-as-Monitor，CVPR 2025 | [作者项目页](https://zhoues.github.io/Code-as-Monitor/) | 将时空约束用代码评估，并通过几何约束元素监控；本文先用已注册谓词适配器，不宣称已实现其感知与生成式监控代码 |
| LongHorizon-Harness，2026 | [方法 §2](https://arxiv.org/html/2608.01964v1#S2) | Manage–Execute–Audit，独立检查环境后更新持久任务状态；实验是计算机任务，物理状态失效、停止与部分执行需本项目另行处理 |

后续如果扩展执行中监控，需要新增在线帧/传感器消费、监控延迟测量、可取消技能、停止确认及并发事件规则，并重新验收；仅增加 CoF 调用频率不能获得可靠实时闭环。

## 16. 联调前确认项

1. 与规划侧确认 condition_groups/phase 扩展、报告消费权限、恢复组归属和唯一进度写入方。
2. 与执行侧确认 backend_motion_state、unknown 对账、取消来源、派发 outbox 和预算预留规则。
3. 与 P 对齐反馈融合确认、事实新鲜度、证据 lineage、冲突表示和当前状态刷新接口。
4. 与 CoF 对齐 AnalysisQuery 的时间范围、原始证据引用、partial/unknown 和有界观察请求。
5. 为首个任务冻结 PredicateCatalog、最终条件、稳定窗口、过程约束及评测真值权限。
6. 用固定 fixture 先跑通六类轨迹，再接仿真与模型；把协议正确性和机器人性能分别验收。

首版交付应能清楚回答：本段发生了什么、哪些条件有证据支持、为什么选择当前控制动作、从哪里恢复，以及任务依据什么完成或停止。
