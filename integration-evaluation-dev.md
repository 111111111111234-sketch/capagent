# 机器人 Coding Agent：联调与评测开发文档

版本：v0.1 · 日期：2026-09-24 · 状态：待实现的开发设计

适用代码基线：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

总入口：[开发总览与使用指南](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-development-guide.md)。

配套文档：[任务规划与进度管理](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/task-planning-progress-dev.md)、[代码生成与执行](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/code-generation-execution-dev.md)、[CoF 执行反馈](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/cof-execution-feedback-dev.md)、[闭环控制与验证](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/closed-loop-verification-dev.md)、[经验学习、技能复用与自进化](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/experience-skills-self-improvement-dev.md)。

本文将前四部分连接成可回放、可对照、可统计的实验系统。新增类型、接口、配置和测试均为拟议设计，尚未实现或执行。接口检查、真实感知测试、仿真闭环和机器人实验分别验收，不能互相替代。

## 1. 开发目标与责任划分

本部分有两条工作线：

- **联调：** 确认执行、反馈、状态、验证、规划和控制之间的语义与生命周期一致。
- **评测：** 在固定任务、信息权限、模型与预算下，衡量实际完成、长程能力、恢复、误判和成本，并定位收益来源。

主要交付四个组件：IntegrationRunner、ScenarioSuite、IndependentEvaluator、ExperimentReporter。它们分别负责运行与交接、任务/扰动定义、独立结果判断、统计与轨迹回放。

### 1.1 分工

| 责任方 | 交付内容 |
| --- | --- |
| 你：Agent/Harness 侧 | 实验驱动器、执行与控制接入、CoF/验证适配、结果记录、对照组与评测报告 |
| 同事：P 侧 | StateView、对象身份、事实来源/时效、反馈融合确认、上下文接口 |
| 双方共同 | 冻结共享 schema、交接时序、固定 fixture、信息权限、端到端验收场景 |
| 环境/执行适配器 | reset、动作执行、帧/传感器、时间与停止状态、受控扰动能力 |
| 独立评测器 | 依据允许的评测真值与冻结任务判据打分，不向在线决策提供答案 |

IntegrationRunner 协调已有模块，不接管 P 的状态实现，也不建立第二份权威 TaskProgress。计划、attempt、恢复组、派发和预算沿用同一事件账本。

### 1.2 首版范围

先接一个 Robosuite 后端的 lifting/stack/restack 任务；再扩展到现有 LIBERO 多步骤任务和 LIBERO-PRO 变体。CALVIN 用于参考连续任务评测设计，不作为首版必须新增的环境依赖。更复杂的 BEHAVIOR 或真实机器人放在后续阶段。

首版采用段后反馈与串行控制。在线中断性能不属于已具备能力；执行中急停和取消必须由 Executor/后端完成，不由 VLM 替代。

## 2. Cap-X 基础与接入点

以下位置已按基线源码核对，开发时按函数名定位；行号可能随改动变化。

| 位置 | 已有行为 | 联调/评测增量 |
| --- | --- | --- |
| `capx/envs/trial.py:629` `_run_single_trial()` | 环境初始化、代码生成、执行、反馈与产物保存 | 在 stateful 分支调度完整链路，保留实验身份和事件水位 |
| `capx/envs/trial.py:332/393` | 两帧差异及整段视频文字反馈 | 保留原始反馈基线，增加显式相同帧的受控对照 |
| `capx/envs/tasks/base.py:263` `step()` | 返回 reward、terminated、truncated、sandbox_rc、task_completed 等 | 将运行事实、Agent 可见输入和评测真值分开路由 |
| `capx/utils/launch_utils.py:52` `TrialSummary` | 保存代码执行、reward 与任务完成等摘要 | 新增实验结果关联，不把代码执行成功率当作任务成功率 |
| `capx/utils/launch_utils.py:464` `_print_and_save_summary()` | 汇总收到的 summaries | 使用预登记试验清单对账，补齐崩溃、超时、缺失试验 |
| `capx/utils/launch_utils.py:81` `_load_config()` | 显式构建配置并读取 trials/num_workers 等 | 显式透传实验配置，校验组别、种子、预算与 schema |
| `capx/envs/trial.py:928` 附近 | 可按任务结果演化技能库 | 主评测冻结技能库；在线演化单独实验，避免跨试验污染 |

`task_completed` 在某些后端可能为 null，其语义也不必覆盖新增的稳定性、释放或过程要求。需要逐任务核对成功函数，不能统一把 reward=1、terminated=true 或日志中出现 finish 当作完整任务成功。

Robosuite 与 LIBERO 所依赖的 Robosuite 版本存在冲突，按仓库说明分别管理环境。首版纯协议测试可在当前 Mac 使用 fake adapter；完整仿真采用对应 Linux/GPU 环境。本文不执行安装或试验。

## 3. 分阶段联调与通过条件

### 3.1 G1：固定契约与故障注入

输入固定 TaskSpec、StateView、ExecutionReport、CoFFeedback 和 VerificationReport，使用 fake 环境及确定性模型输出。覆盖正常、失败、未知、迟到、重复、取消和预算耗尽。

通过条件：

1. ID、scope、phase、条件定义和版本引用一致；非法输入被拒绝而非转成 finish。
2. 只有正式验收报告推进成功；前置条件全 pass 不标记目标完成。
3. 同一报告重复投递不重复更新进度、动作或用量。
4. outcome_unknown 锁定后续动作；取消不自动恢复。
5. 失败后已发生的变化仍交给 P，unknown 不被强行映射为 pass/fail。

此阶段证明协议与控制逻辑，不证明视觉识别或机器人执行能力。

### 3.2 G2：真实轨迹的只读回放

记录实际或仿真帧、API 事件、传感器与独立标注，按原始顺序运行 CoF → P → Verifier → Controller。Controller 只记录拟议决定，不驱动机器人。

回放分两种模式，结果必须分开：

- causal_replay：每个决策只读取当时已可获得的证据，按原时间和可用性水位推进。
- full_trace_analysis：允许完整后续视频，用于标注与离线诊断；不计作在线检测能力。

通过条件：有中间证据时能够区分未抬升、抬升后掉落与正常释放；证据不足时保留未知；P 不被迟到事件覆盖当前状态；验证和控制决定能追溯到对应证据。

回放不能证明替代动作会成功。原轨迹中 Agent 执行了 A，回放中建议 B 后，不能直接继续沿用 A 的后续观测来计算 B 的闭环成功率。此后仍可做逐时刻判断诊断，但不能当作反事实执行结果。

### 3.3 G3：单任务在线闭环

先使用固定脚本/技能调用检查环境、执行器、观测、停止和独立成功判据；再接代码模型和真实 CoF/P。每次替换一个模块，保存替换前后的同类轨迹。

必要时用参考状态或人工事件检查下游，但标记 oracle_diagnostic，不混入主评测。完整非特权闭环必须使用声明的实际观测和真实 P 更新。

通过条件：正常任务能闭环推进；空抓、掉落、遮挡分别进入适当恢复/观察；停止与预算生效；日志能还原每次派发依据和最后结果。功能门槛与性能门槛分开记录，单条成功视频不作为统计结论。

### 3.4 G4：固定清单的批量评测

冻结任务划分、配置、模型/提示、API、技能库、判据、扰动规则、随机数和预算。使用预登记 episode 清单运行所有组，生成逐任务和总体指标。

通过条件：计划中的每个试验都能对账；缺失与失败不被静默过滤；配对设置可追溯；统计脚本通过人工可算的固定结果 fixture；主结果、诊断、原生基线和受控对照分表。

## 4. 模块交接与信息权限

### 4.1 标准交接链

```text
ExperimentSpec + ScenarioSpec → 注册 episode → 环境 reset
    ↓
初始实际观测 → P → StateView / ContextPacket
    ↓
Planner → Code Generator → Executor
    ↓
ExecutionReport + FrameManifest + API / 传感器事件
    ↓
CoF → 候选事实、时序事件、证据缺口
    ↓
P 融合确认 → Verifier → Controller → 下一轮

同一实际轨迹的评测侧副本
    → 独立真值适配器 → 扰动记录 / 外部里程碑 / 最终结果
    → EvaluationResult → ExperimentReporter
```

评测侧副本通过只读日志/后端适配器获取，不把答案回传给决策层。扰动器可改变环境，但注入标签和触发真值属于评测通道。

### 4.2 接口最小交接要求

| 交接 | 必须携带/确认 |
| --- | --- |
| Executor → CoF/P | episode/execution/segment/attempt、执行状态、报告版本、帧/调用引用、时钟域、缺失项 |
| CoF → P/Verifier | feedback_id/revision、实际输入 manifest、query、候选事实与 lineage、unknown |
| P → Controller/Verifier | StateView 及已处理 execution/feedback 标识或等价 watermark、冲突与缺失项 |
| Verifier → 进度/Controller | request/report、scope/phase/condition_groups、逐条件结果、证据与有效时间 |
| Controller → Runner | 决策/派发/停止事件、任务状态、预算、未对账执行；不要求 Runner 再做一轮自由决策 |

state_version 增长不证明刚才的反馈已经融合；checked_at 较新也不证明证据较新。Runner 应验证关联关系并显式处理等待/超时，不能用旧数据填充“新状态”。

共享 Condition、TaskProgress、ExecutionReport、VerificationReport 等直接引用前四份定义。新增实验类型不覆盖其枚举；评测结果不写回 TaskProgress。旧消费者升级 schema 时同步修改全部生产/消费方。

### 4.3 权限配置

每个实验必须固定 information_profile：

- observed_only：Agent/P/CoF/在线 Verifier 只能读取声明的实际观测、正常 API 返回和自身历史。
- oracle_diagnostic：明确列出额外提供的参考状态/标签，用于诊断或上限研究，单独报告。
- native_baseline：保留原始系统信息权限，并记录其实际可访问的 env/API；未经审计不称为 observed_only。

非特权组中，隐藏物体坐标、成功函数、评测标签、扰动触发器、测试答案不能通过 prompt、工具、文件名、stdout、缓存或原始 env 对象泄漏。仅从 prompt 删除真值不够；受控组复用 Executor Gateway 限制可访问能力。

评测器和在线验证器可以共享任务条件定义，但实现/证据路径应分别核验。独立进程不自动证明语义独立；需用手工标注或已知几何场景验证评测器本身。

## 5. 实验数据契约

所有类型建议严格校验并导出 JSON Schema。大媒体与轨迹用不可变 manifest 引用，模型输出不能创建新的实验身份或修改评测判据。

### 5.1 ExperimentSpec

| 字段 | 定义 |
| --- | --- |
| experiment_id / schema_version / protocol_version | 实验及统计协议身份 |
| code_revision / dependency_lock_ref | 仓库、子模块及环境版本 |
| suite_ref / split_ref / scenario_manifest_ref | 固定任务、数据划分与完整场景清单 |
| variants | 方法组及精确实现/配置引用，含 information_profile |
| model_manifest_ref / prompt_manifest_ref | 代码/规划/视觉模型与服务版本、采样参数、提示 hash |
| skill_catalog_ref / predicate_catalog_ref | API/技能/在线判据版本 |
| evaluation_catalog_ref | 独立评测判据与时间协议 |
| seed_manifest_ref / repetitions | 环境、扰动、模型等独立随机源与重复安排 |
| budget_profile_ref / runtime_profile_ref | 总量限制、计时规则、推理期间环境是否推进、资源/并发策略 |
| retry_policy_ref / missing_result_policy_ref | 预先固定的重跑与缺失处理规则 |
| metrics / aggregation_plan | 主次指标、分母、宏/微平均、区间估计方法 |

### 5.2 ScenarioSpec

| 字段 | 定义 |
| --- | --- |
| scenario_id / task_ref / task_version | 与方法组无关的任务实例 |
| environment_profile / initial_state_ref | 后端与可恢复的初始状态/生成规则 |
| instruction_mode | full_task 或 sequential_instruction；分开统计 |
| goal_spec_ref / milestone_spec_ref | 冻结最终条件、过程要求与外部里程碑 |
| perturbation_spec_ref | normal 也显式标识，不能缺省猜测 |
| feasibility_profile_ref | 初始状态、扰动后可恢复性检查规则 |
| split / grouping_keys | dev/validation/test；任务/物体/布局/原始轨迹分组 |
| environment_seed / perturbation_seed | 配对场景随机源，模型随机源独立 |
| evaluation_protocol_ref | 停止时机、最终观察窗口、时间预算及失败语义 |

scenario_id 标识相同外部场景，不等于 episode_id。每个 variant × scenario × repetition 注册唯一 trial_id；物理 reset 后产生 episode_id。重跑新建 run_attempt_id，并保留原始试验记录，不作为额外独立样本随意计数。

### 5.3 EvaluationResult

至少包括：experiment_id、trial_id、run_attempt_id、episode_id、scenario_id、variant_id、各版本/hash、种子、agent_task_status、agent_claimed_success、claim_source、evaluator_outcome、goal_checks、constraint_checks、milestone_trace、perturbation_trace、stop_reason、execution_resolution、failure_origin、budget_usage、timings、artifact_refs、protocol_deviations。

其中：

- agent_task_status 沿用 active/succeeded/failed/blocked/budget_exhausted/interrupted；崩溃未取到则 null。
- agent_claimed_success 为 true/false/null；只记录明确的完成声明，不能把解析失败后的默认 finish 算成正面声明。
- evaluator_outcome 为 pass/fail/unscorable，是独立评测结果，不沿用在线 VerificationReport 作为答案。
- unscorable 表示判据所需证据缺失/损坏或运行未产生可判断结果；不篡改成“物理任务一定失败”。
- failure_origin 为标签数组，可包含 agent、execution、perception、state、verification、controller、infrastructure、evaluator 或 unresolved；初始自动分类允许后续有审计记录的修订。

对原生 baseline 的 finish，按其实际输出协议记录声明：明确表达“任务完成”的标记可计为声明；其他停止或解析器兜底将 agent_claimed_success 记为 null，并单独报告覆盖率。

## 6. 任务集与长程设计

### 6.1 环境和任务路线

| 层级 | 任务来源 | 检验重点 |
| --- | --- | --- |
| 基础操作 | Cap-X Robosuite cube_lifting / cube_stack | 感知、抓取、释放与成功判据 |
| 依赖任务 | cube_restack 或预先定义的多对象操作 | 顺序、当前前提、部分完成后恢复 |
| 多场景任务 | LIBERO 的 libero_10 等已接入任务 | 多对象、多阶段与环境差异 |
| 泛化变体 | LIBERO-PRO 的 swap/task 等可用 suite | 布局、对象、指令、目标及环境变化 |

准确任务名、suite 和环境版本从实际安装的 registry 导出并写入 manifest；不能只凭名称假定任务相同。修改 benchmark 的成功判据、指令或时间限制时，标记为项目自定义协议，不与官方分数直接比较。

### 6.2 外部里程碑

由任务适配器/研究者在评测前定义 milestone：ID、前置依赖、达成判据、所需证据、是否为历史事件或需保持的关系。它与 Planner 子目标映射可记录，但评测不使用模型自己生成的步骤数作分母。

对于任务链，报告连续完成前 k 个外部里程碑的比例；链内不 reset 环境，也不清空记忆/预算。对于分支 DAG，报告固定任务目标完成度、关键依赖深度和最终成功；不要强行套用任意线性前缀指标。

“曾经放好”与“结束时仍放好”分开。历史里程碑可记录曾达成；最终目标仍需按原任务检查当前关系。正常释放消耗夹持关系，不因此否定过去抓取事件。

### 6.3 指令协议与泛化划分

主评测推荐 full_task：一次给出完整任务，Agent 自行规划。若按阶段提供下一条指令，则标为 sequential_instruction，不能把外部分解算作自主规划能力。

开发样例、阈值/提示调优与最终测试分离。划分单位至少包含完整原始轨迹/episode，避免相邻帧跨集合；需要宣称未见任务/物体/布局泛化时，对应分组必须隔离。已知任务的新种子测试只说明该分布内泛化。

长程长度分层由固定外部里程碑/依赖深度决定，并尽量控制单步技能难度；不能把更难物体与更长任务同时变化后，将全部下降归因于长度。

## 7. 扰动与故障注入

### 7.1 分类

| 类别 | 示例 | 主要用途 |
| --- | --- | --- |
| 初始状态/分布变化 | 位置、对象属性、指令改写、背景 | 泛化，参考 LIBERO-PRO |
| 执行中的物理扰动 | 抓取偏差、抬起后掉落、目标移动、摆放被打乱 | 发现偏差和恢复，参考 DoReMi/AHA |
| 观测变化 | 遮挡、缺帧、延迟、身份混淆 | unknown、观测请求、证据时效 |
| 服务/协议故障 | API 超时、重复/迟到报告、P 暂不可用、重启 | 生命周期与协议健壮性 |

物理恢复实验与基础设施故障实验分表，正常轨迹保留为必要对照。合成扰动之外还应收集自然策略失败，避免系统只识别注入器特有痕迹。

### 7.2 PerturbationSpec

每项定义 perturbation_id、type、trigger、magnitude/distribution、duration、max_occurrences、seed_stream、effect_check、feasibility_rule、visibility_policy、implementation_version。

- trigger 优先绑定物理阶段，例如独立确认目标已抬起后，在固定规则下施加一次扰动；不依赖 Planner 生成的任意步骤名。
- 同组间使用相同触发规则、幅度分布和随机流。执行轨迹不同导致实际触发时间不同，应如实记录。
- 不用“第 5 次 API 调用”冒充相同物理阶段；需要测试 API 特定故障时才按调用触发，并单列实验。
- effect_check 确认注入是否真正产生预定效果。发出了扰动命令，不等于已经发生掉落。
- visibility_policy 默认不向 Agent 暴露注入标签、随机种子、未来时刻或参考状态。

### 7.3 可恢复性与分母

扰动幅度应先用与被测方法无关的规则/参考控制器检查：物体仍在工作区、存在可行恢复路径、目标未被无意替换。不能依据被测方法是否成功事后认定“可恢复”。

保留四个标识：trigger_reached、injection_attempted、effect_confirmed、recoverable_by_protocol。恢复率使用 effect_confirmed 且协议定义可恢复的样本；同时报告触发到达率、有效注入率和全部场景最终成功率。

某方法未到触发阶段，仍计入该方法整体任务结果，不算一次已遭扰动的恢复失败或恢复成功。方法之间触发样本构成不同，条件恢复率存在选择偏差，不能单独据此排名。

需要控制恢复起点时，可从共同物理检查点启动单独的恢复诊断集，各组获得同样的允许历史、状态和剩余预算；新建 episode，独立报告，不能混入完整长程试验。

### 7.4 无效场景处理

初始碰撞、不可达目标、未定义成功判据等应在正式运行前检测并按冻结规则修正/剔除，保留清单。运行中发现方法无关的场景缺陷，需保存证据，按预设规则对所有配对组同样处理；不能只删除某组失败样本。

物理任务变成不可恢复的情况可作为阻塞识别测试保留，但不混入“可恢复扰动”指标。正常移动、释放与预期消耗条件要作为负例，防止把所有状态变化都当作异常。

## 8. 独立评测器

### 8.1 三类结果分开

| 结果 | 来源 | 回答的问题 |
| --- | --- | --- |
| 执行结果 | ExecutionReport | 程序/命令是否运行、是否停止、是否报错？ |
| 在线判断 | VerificationReport、TaskProgress、明确成功声明 | Agent 当时认为哪些条件成立，为什么停止？ |
| 实际评测 | EvaluationResult | 按冻结协议，任务是否实际完成、是否违反约束？ |

评测器读取环境真值、独立传感器或人工标注，不读取在线 verdict 作为评分答案。真值可以提供给评测器和扰动器，但不能通过它们间接进入 observed_only 组。

仿真中逐任务实现 TruthAdapter，明确对象身份、姿态/接触/支撑、关节状态、事件和时间。人工抽查已知成功、失败与边界场景；隐藏状态字段存在并不说明成功函数定义正确。

### 8.2 结束、稳定窗口与时间截止

统一冻结评价时刻和条件，包括最终目标、必需历史事件、过程约束及执行是否已确定结束。默认在 Agent 主动停止或预算截止后，以最后一个获准动作产生的轨迹进行判断。

- 稳定性检查在预先定义的窗口和容差下进行，禁止窗口内继续派发任务动作。
- 若物理/仿真时间需继续推进才能检查稳定性，各组采用相同规则；这段时间计入任务预算并提前预留。
- 在冻结快照上读取评测数据不应改变环境；如果后端读操作有副作用，必须通过适配器消除或记录。
- 超预算后的新动作即使完成任务，也不能计入预算内成功。截止前自然发生的效果与截止后才发生的效果分别记录。
- 后端还在运行或停止未确认，不能仅凭某一帧目标正确认定整个 episode 已完成。
- 任务曾经短暂成功、之后又失效，不等于最终成功；官方 benchmark 若采用首次满足即终止，则保留原协议并单独说明。

判据有确定反例则 fail；必需数据缺失且无确定失败依据则 unscorable。不得为获得完整表格把未知强改为实际失败，也不得把无法评分的运行过滤掉。

### 8.3 评测器验收

先构造可人工确定的 fixture：目标放错、正确支撑但夹爪未释放、稳定后被扰动、过程约束违规、缺乏最终记录。结果需与冻结定义逐项一致。

对有歧义的真实图像，可使用双人盲审和分歧裁定；标注者不看方法组、在线判断或期望结论。指标注明人工标注覆盖范围，不能把少量抽查当作全量真值。

生成式环境的预测/渲染结果不能当作物理实验真值。若后续使用生成式后端，应另定义状态转移与评分协议，并与物理仿真、真实机器人分开报告。

## 9. 对照实验与消融

### 9.1 原生基线

保存原 Cap-X 的两帧差异和视频文字反馈配置，冻结提交、提示、模型、API、技能库、预算与信息权限。记录其原始执行粒度、状态输入、访问 env 的权限及停止语义。

原生基线用于系统级比较。若其信息权限、分段、模型调用或输入与新系统不同，明确列出差异；结果不能单独归因于某一新模块。

### 9.2 受控 2×2 主对照

| variant_id | 控制框架 | 反馈 |
| --- | --- | --- |
| H0F0 | 简化多轮循环 | 普通多帧文字反馈 |
| H1F0 | 显式计划/进度、验证与恢复 Harness | 普通多帧文字反馈 |
| H0F1 | 简化多轮循环 | CoF 结构化反馈 |
| H1F1 | 完整 Harness | CoF 结构化反馈 |

受控组共享：模型及解码设置、P 版本和更新规则、执行器/Gateway、底层 API、允许观测源、动作段边界约束、总预算与场景清单。H0 也保留执行生命周期、资源上限和取消/停止机制，只移除研究中的显式任务管理/验证恢复策略，不能以破坏执行基础设施形成对照。

H0 通过相同接口接收 P 状态和反馈，使用简化生成/继续/结束逻辑；不隐式获得 H1 的已分解计划或评测里程碑。H1 的计划、验证和恢复调用均计入预算。

F0 与 F1 在基础对照中使用相同可用帧、分辨率、API 日志权限和采样上限。为 P 提供统一 ingestion adapter：F0 的文字解析、F1 的字段映射需记录规则和成本，不能给某组额外真值。它们造成的真实状态差异属于“反馈方案及其接口”的端到端效果，不等于纯输出格式效应。

H/F 实现必须在 VariantSpec 中精确定义。不能只改方法标签，却在帧数、技能或采样预算上同时变化。

上述 H/F 对照不同时研究跨任务学习。各组应共享固定的技能/经验来源、版本和获准检索规则，或统一关闭跨任务复用；不能只给 H1 或 F1 增加历史经验后把收益归为 Harness/CoF。若启用流程技能，H0 也需具备预先定义的同等资产消费入口；无法保持可比时关闭该项或单列实验。经验、技能和持续更新的收益按第六份文档另设对照。

### 9.3 比较与可归因范围

- H1F0 − H0F0：在文字反馈下，Harness 这一组机制的收益。
- H1F1 − H0F1：在 CoF 下，Harness 的收益。
- H1F1 − H1F0：完整 Harness 内替换反馈方案的收益。
- `(H1F1 − H0F1) − (H1F0 − H0F0)`：在选定指标尺度上的交互效应，附区间而非只报单点。

这里 Harness 同时包含计划、进度、验证和恢复，2×2 只能归因到组件集合。若研究单独机制，再做限定消融：去掉显式进度、只做最终验证、移除局部恢复、去掉历史失败、固定 unknown 策略等。每次固定其余实现和权限。

### 9.4 CoF 专项与长程专项

沿用 CoF 文档的同帧文字/结构化、API 对齐、有界回看、unknown 和训练模型消融。研究纯表示时，使用相同事实内容的受控渲染；研究感知质量时，直接比较同一批原始轨迹上的判断。原生视频服务端抽帧不透明时，不能声称与显式多图输入完全等价。

自适应回看和补观察单列实验，报告相同预算上限下的结果与实际成本曲线。在线组动作不同，后续观测自然不同；只固定初始条件、传感器和生成规则，不要求或伪称全程帧相同。

长程专项按外部里程碑/依赖深度分层，检查成功率下降、重复步骤、遗忘已完成事实、错误沿用失效条件和预算消耗。不要用同一轨迹里大量相关帧当作大量独立任务样本。

## 10. 指标定义与统计口径

### 10.1 样本分母

预先冻结可用场景与完整 trial 清单。每个场景 × 方法 × 重复对应一个统计单元；失败后的内部 retry 不产生新独立样本。预登记的运行即使崩溃或未返回 summary，也必须生成缺失/异常记录。

令 N 为该组冻结清单中的试验数；S 为 evaluator_outcome=pass，F 为 fail，U 为 unscorable，满足 N=S+F+U。待完成的试验暂列 pending，只有完成对账后才发布最终报告；中间报告明确 pending 数量。

- **认证完成率：S/N。** 全部登记试验中有独立证据完成的比例；U 对此分数不贡献成功，但不能描述为已知物理失败。
- **评分覆盖率：(S+F)/N。** 单独报告缺失/无法评分比例。
- **可评分样本成功率：S/(S+F)。** 仅为辅助指标，不能替代 S/N 掩盖缺失。

所有分母为零时返回 null/NA 并显示样本数，禁止填 0 或 100%。不可评分的最保守/最乐观完成边界可报告为 `[S/N, (S+U)/N]`，与统计置信区间分开。

### 10.2 主指标

| 指标 | 精确定义/注意事项 |
| --- | --- |
| 最终完成 | 上述 S/N、评分覆盖率，另保留每个目标/约束检查结果 |
| 连续前缀成功 P(k) | 固定任务链中前 k 个外部里程碑依次达成的试验数 / 该链长度至少 k 的登记试验数；按长度层报告 |
| 平均前缀长度 | 每条试验首个未完成里程碑之前的连续完成数平均；不同最大链长分层或注明归一化 |
| 条件恢复率 | 已确认有效扰动且按协议可恢复的试验中，最终完成数 / 该类试验数；无法评分者不计成功并披露 |
| 触发到达率 | trigger_reached 试验数 / 该扰动场景全部登记试验数 |
| 有效注入率 | effect_confirmed 次数 / injection_attempted 次数；max_occurrences>1 时另分事件/episode |
| 错误宣告成功 | 明确宣告成功且评测 fail 数 / 明确宣告成功且可评分数；同时报告声明覆盖率、声明后不可评分数及错误占全部 N 的比例 |
| 错误继续率 | 在独立记录确认必要入口/继续条件不成立时仍派发的动作数 / 具备该类可评估条件的派发数；同时报告条件不可评分比例和次数 |
| 无效重复动作 | 按冻结语义/参数容差判定、状态无进展的重复派发次数；不能只比较代码字符串或 execution_id |
| 成本 | 每个 episode 实际模型/token/图像、API、控制步数、观察/验证/重规划次数、耗时；对全部运行报告 |

错误宣告成功须区分显式声明与 legacy 解析器兜底结束。若 C 条明确声明中 E 条确定错误、U_c 条不可评分，除可评分口径外，报告所有声明的错误比例边界 `[E/C, (E+U_c)/C]`，不将 U_c 默认为正确。

### 10.3 组件指标

- CoF：事件类型 precision/recall/F1、时间区间 IoU/定位误差、引用存在率、证据支持率、对象身份和先后顺序错误。
- P：任务相关事实与参考状态的一致性、陈旧事实使用、冲突/未知传播、反馈融合延迟。参考真值未知时不强行打标签。
- Verifier：明确判断的错误率、pass 误判、fail 误判、unknown 比例及判断覆盖率；返回 unknown 的视觉正确做法与逃避判断分开分析。
- Planner/Controller：遗漏固定目标、依赖错误、错误继续、恢复选择、错误终止、无进展循环和成功历史处理。
- Executor：可执行代码率、到达/调用结果、部分执行、重复物理派发、停止确认与对账。

事件/帧指标只是局部能力，不能代替完整任务成功。引用存在也不代表引用内容支持结论；后者需要独立标注。置信阈值在 validation 集冻结；测试时不按方法结果重新调参。

### 10.4 时间和预算

至少分别记录：模型/网络等待、CoF/P/验证延迟、物理/仿真动作时间、从首个决策到终止的 wall time、停止协调收尾时间，以及完整作业含初始化的耗时。

runtime_profile 必须说明推理期间仿真是否暂停。暂停仿真测的是允许停顿的任务完成；不能据此声称能应对真实世界持续变化。若保持推进，则各组使用相同调度政策，并记录动作中监控和延迟。

稳定窗口、补观察、验证和恢复不能免费。预算预留与结算沿用权威账本，Reporter 读取事件计算；共享并行服务的 wall time 用实际区间统计，不能把重叠耗时简单相加当总时长。

除总体耗时外报告成功条件下耗时，并明确其样本选择；失败快速退出不应被解释成高效率。延迟无法观测时保留删失/缺失标识，不能填成零或只统计检测成功的样本。

### 10.5 聚合与不确定性

同时提供逐任务结果、按任务等权的宏平均和按 episode 的微平均；主口径预先指定，避免样本多的简单任务主导结论。跨后端、指令模式和 oracle/native/controlled 组分别分层。

方法差异优先使用配对场景和重复：对同一 scenario/repetition 对齐各方法结果。需要推广到任务集时，可按任务分层/聚类 bootstrap，并在重采样中保持方法配对与同源数据绑定。只有少数任务时披露区间局限，不将海量帧当作任务独立样本。

在固定任务单元内可报告二项比例区间；不要把相关 episode/重跑直接当独立观测。Bootstrap 次数、随机种子、置信水平、主次指标和多重比较处理在统计计划中冻结。

预实验用于估计波动和成本，正式样本量依据希望检测的差异与可接受区间宽度确定。仅因“每配置 10 个种子”不代表统计充分；避免反复查看测试结果，看到显著就停止。

## 11. 可复现运行、缺失与成本管理

### 11.1 运行前冻结

记录仓库与子模块 commit、依赖锁/镜像、硬件、模型版本/服务商/采样参数、prompt/hash、API 和技能库版本、CoF 采样策略、P/判据版本、运行时资源、任务与扰动 manifest、预算、停止规则。

环境、场景生成、扰动和模型随机源分开。各方法对同一配对场景使用相同外部随机源；不能因为某组多调用一次 LLM 导致扰动随机序列整体错位。模型服务不支持可控随机种子时记录这一限制并做重复，不宣称逐位可复现。

测试期冻结技能库、few-shot 示例与跨 episode 记忆。若研究在线学习/技能演化，另设协议，固定方法顺序/数据暴露并报告更新轨迹，不混入静态主表。任务内记忆按设计保留；跨任务泄漏需隔离。

“冻结”允许读取测试前已获准的固定经验快照/技能目录，禁止的是测试结果写回并改变后续样本可见资产。每个 episode 的当前物理状态、对象绑定和动态产物必须重新建立；可以复用历史经验，不可以把历史位姿当作当前事实。

持续学习的更新时机、允许历史和独立序列重置，见配套《经验学习、技能复用与自进化》第 12 节。只有预先声明的学习序列允许按时间积累获准经验；冻结测试与保留探针继续禁止跨任务学习和标签回流。

### 11.2 并发与共享服务

先单 worker 验证，再逐步增加并发。每个 episode 绑定独立环境/状态命名空间和预算；单个机器人后端的写操作不得被多个任务竞争。

共享 GPU/模型服务会影响超时和成本。方法运行顺序随机化或分块交错，记录资源负载、速率限制、并发和服务错误；不能某组空闲时运行、另一组拥塞时运行后将差异归因于算法。

缓存键包含完整输入、模型/策略/环境版本；不能跨组复用其他方法产生的状态或隐藏答案。若共享相同只读模型输出缓存用于受控试验，必须提前声明并公平处理调用成本/时延。

### 11.3 崩溃、重试与恢复

Runner 先持久注册 trial，再启动执行。每条注册记录最终对应结果、明确失败/缺失或仍在运行状态；不能只汇总函数正常返回的 TrialSummary。

- Agent 自身的抓取重试、观察、重规划属于原 episode，消耗原预算。
- 后端命令状态未知时先对账，不能为了“重跑试验”绕过执行锁。
- 主分析默认使用每个 trial 的首次运行；基础设施重跑如获预设规则允许，单独给出重跑结果和成对敏感性分析，不挑选较好的一次。
- 重跑从新 episode 开始；原结果、原因、成本、run_attempt_id 与关联保留。物理 reset 不能隐藏在同一 episode 内。
- 未确认方法无关的失败保留 unresolved，不由模型自行判定为“基础设施问题”后删掉。
- 用户中断整个实验时保留清单和 pending 数，标为未完成批次；不将部分样本报告为完整正式评测。

发布结果前由清单对账检查 N、重复、遗漏、任务版本和全部运行状态。报告中单列基础设施失败率、evaluator unscorable 与协议偏差。

## 12. 失败归因与诊断

每个失败保留完整事件时间线，从首次偏差开始逐层检查：

```text
观测是否采到且及时可用？
→ CoF 是否正确解释可见过程？
→ P 是否正确融合并标记新鲜度？
→ Verifier 是否按冻结判据检查？
→ Controller/Planner 是否选择合理下一步？
→ Executor 是否实际执行并正确报告？
```

自动规则先提出 failure_origin 与证据；复杂案例可多标签，未找到首因时保留 unresolved。避免把最后一次超时统一当作最早原因。

诊断干预可包括参考状态替换 P、人工事件替换 CoF、参考计划替换 Planner、固定技能脚本替换代码生成。每次只改变一个明确接口，原始任务、允许历史和预算保持可比。

参考状态必须采用与实际接口一致的字段、可见性和时间协议；额外全局真值属于 oracle privilege，明确列出。干预后改善说明该接口可能是瓶颈，不构成单一根因的严格证明；模块之间存在交互，需结合原始证据。

未经过真实闭环执行的“如果当时这样做就会成功”只记录为诊断假设。失败报告保存观察、推断、原因假设和实际验证过的恢复效果，四者分开。

## 13. 实现接口与代码组织

### 13.1 公共接口草案

以下是待实现的接口，不是可直接运行的程序。在线控制复用前四份文档中的 Controller、Executor、P 和进度存储；实验层只负责组装、运行登记、记录和评测。

```python
def validate_experiment(spec, resolver, capabilities) -> "ValidatedExperiment":
    """解析并冻结引用，检查权限、任务可评分性、版本、预算和配对规则。"""
    raise NotImplementedError


def register_trials(experiment, registry) -> "TrialManifest":
    """幂等登记 variant × scenario × repetition；持久化成功后才调度。"""
    raise NotImplementedError


def run_episode(trial, agent_factory, env_factory, services, registry) -> "EpisodeRecord":
    """创建隔离实例，调用既有闭环，记录终止/停止对账及证据引用。"""
    raise NotImplementedError


def inject_perturbation(spec, truth_cursor, env_adapter, event_store) -> "InjectionRecord":
    """仅在评测侧消费触发证据；记录注入、实际效果和发生时间。"""
    raise NotImplementedError


def evaluate_episode(record, evaluation_catalog, truth_reader) -> "EvaluationResult":
    """对截止范围内的冻结轨迹独立评分，不调用在线 Agent 获取答案。"""
    raise NotImplementedError


def aggregate_results(manifest, results, analysis_plan) -> "ExperimentReport":
    """先对账、处理首次运行与缺失，再按冻结分母生成配对统计。"""
    raise NotImplementedError


def replay_episode(record, replay_spec, analysis_factory) -> "ReplayReport":
    """按 causal/full-trace 权限回放；环境写接口不可用。"""
    raise NotImplementedError
```

接口行为补充：

- `validate_experiment` 输出解析后的不可变配置及内容摘要；缺少必需引用、模型版本或评分适配器时，在动作前失败，不隐式使用默认配置。
- registry 记录实验调度事实，不拥有 TaskProgress、物理派发和预算。调度租约用于避免两个 worker 同时启动同一 trial；重启时先向既有执行账本对账，不依靠租约超时证明机器人已停止。
- 每次运行在初始化前分配 run_attempt_id；开始 reset 前绑定 episode_id。初始化失败同样保存记录，标记初始化阶段，不能从分母中消失。
- `run_episode` 不在 finally 中自动 reset、重复执行或改写 Agent 为成功。终止后先停止与保存证据，reset 由下次运行流程在确认允许后执行。
- 评分读取冻结证据，不触发新的机器人观察动作。需要额外传感器采集/稳定等待时，在运行期按协议完成并计入预算。
- 评测器崩溃可以对同一份不可变证据重新评分，记录 evaluator_run_id 与版本；这不新增物理 episode。修改评分语义时发布新协议并重评所有适用组，不只改不利样本。

### 13.2 VariantSpec 与能力检查

每个 VariantSpec 至少保存 variant_id、agent_factory_ref、controller_profile_ref、feedback_profile_ref、state_profile_ref、ingestion_profile_ref、information_profile 及显式 overrides。启动时展开继承配置，保存每组最终配置与差异表。

受控比较只允许预登记的差异；额外帧源、模型、技能、预算或真值权限差异必须阻止启动或创建独立实验。原生基线走独立配置，不能通过同名 variant 混入受控四组。

EnvironmentAdapter 的能力表至少包含：reset、实际观测、动作/取消、停止确认、时间戳、评测真值、可用扰动、状态快照/恢复。缺少快照恢复能力时，关闭共同检查点实验；缺少执行中扰动能力时，不把段间修改描述为动作中扰动。

### 13.3 建议目录

以下均为相对 Cap-X 仓库根目录的拟议新增路径。

```text
capx/evaluation/stateful/
  contracts.py             # 实验类型；导入已有共享类型
  config.py                # 引用解析、能力/权限校验、配置冻结
  registry.py              # trial 清单、运行租约、运行记录与对账
  runner.py                # 装配既有 Agent/Controller 与环境
  scenarios.py             # 任务、初始状态、划分和外部里程碑
  perturbations.py         # 触发、注入、效果确认
  evaluator.py             # 独立条件聚合与评分结果
  truth_adapters.py        # 后端真值到冻结评测条件
  replay.py                # 只读回放及可用性水位
  metrics.py               # 分母、分层、配对和不确定性
  reporting.py             # 汇总、差异表和失败时间线

tests/integration_evaluation/
  fixtures/                # 手工可核对的消息、轨迹及统计样例
  test_handoffs.py
  test_information_profiles.py
  test_perturbation_protocol.py
  test_evaluator.py
  test_registry_recovery.py
  test_metrics.py

configs/evaluation/         # 经实现校验器支持后再加入正式配置
```

### 13.4 Cap-X 接入顺序

1. 为配置加载器增加显式 experiment 配置入口和校验；仅写 YAML 不会自动改变现有 `_load_config()`。
2. 在外层 launcher 创建/读取 trial 清单，再由 runner 为每条 trial 调用既有环境与 Agent 工厂，避免原有 trials 循环再次乘上 repetitions。
3. `_run_single_trial()` 的 stateful 路径委托既有 Controller；H0 适配器使用同一执行基础设施实现简化循环。原生多轮分支保持为独立基线入口。
4. 在 `step()` 返回处路由两类数据：允许的运行/观测字段进入 Agent；隐藏 reward/成功函数/真值仅进入评测侧。实际哪些字段允许暴露由 information_profile 明确指定。
5. 保存兼容 TrialSummary，同时新增 EvaluationResult 引用；批量汇总从 registry 对账后生成，不依赖只有成功返回才存在的 summaries 列表。
6. 静态主评测关闭跨任务学习写入和动态世界状态复用；可按对照协议读取固定经验/技能快照。持续学习另按第六份文档开放获准更新。先单 worker 验证清理、ID 和停止，再打开多 worker。

## 14. 配置与结果示例

以下标识均为拟议 fixture 引用，不代表已存在的配置文件、模块、实验或结果。数字用于说明配置结构，不是已调优参数；实现后必须先通过引用解析、能力检查和配置冻结才能运行。

### 14.1 受控四组配置

```yaml
schema_version: "0.1"
protocol_version: "integration-evaluation-v0.1"
experiment_id: "fixture_pilot_001"
code_revision: "53e9966d7a8e2fa7494676772bccc35280f5c0ed"
dependency_lock_ref: "fixture:robosuite-lock-v1"
suite_ref: "fixture:three-task-suite-v1"
split_ref: "fixture:dev-split-v1"
scenario_manifest_ref: "fixture:nine-valid-scenarios-v1"
model_manifest_ref: "fixture:fixed-models-v1"
prompt_manifest_ref: "fixture:fixed-prompts-v1"
skill_catalog_ref: "fixture:frozen-skills-v1"
predicate_catalog_ref: "fixture:online-predicates-v1"
evaluation_catalog_ref: "fixture:independent-evaluation-v1"
seed_manifest_ref: "fixture:paired-seeds-v1"
budget_profile_ref: "fixture:shared-budget-v1"
runtime_profile_ref: "fixture:single-worker-paused-inference-v1"
retry_policy_ref: "fixture:first-run-primary-v1"
missing_result_policy_ref: "fixture:retain-unscorable-v1"
repetitions: 2
variants:
  - variant_id: "H0F0"
    agent_factory_ref: "fixture:simple-loop-agent-v1"
    controller_profile_ref: "fixture:simple-loop-v1"
    feedback_profile_ref: "fixture:matched-frames-text-v1"
    state_profile_ref: "fixture:p-state-v1"
    ingestion_profile_ref: "fixture:text-to-p-v1"
    information_profile: "observed_only"
    overrides: {}
  - variant_id: "H1F0"
    agent_factory_ref: "fixture:stateful-agent-v1"
    controller_profile_ref: "fixture:full-harness-v1"
    feedback_profile_ref: "fixture:matched-frames-text-v1"
    state_profile_ref: "fixture:p-state-v1"
    ingestion_profile_ref: "fixture:text-to-p-v1"
    information_profile: "observed_only"
    overrides: {}
  - variant_id: "H0F1"
    agent_factory_ref: "fixture:simple-loop-agent-v1"
    controller_profile_ref: "fixture:simple-loop-v1"
    feedback_profile_ref: "fixture:matched-frames-cof-v1"
    state_profile_ref: "fixture:p-state-v1"
    ingestion_profile_ref: "fixture:cof-to-p-v1"
    information_profile: "observed_only"
    overrides: {}
  - variant_id: "H1F1"
    agent_factory_ref: "fixture:stateful-agent-v1"
    controller_profile_ref: "fixture:full-harness-v1"
    feedback_profile_ref: "fixture:matched-frames-cof-v1"
    state_profile_ref: "fixture:p-state-v1"
    ingestion_profile_ref: "fixture:cof-to-p-v1"
    information_profile: "observed_only"
    overrides: {}
metrics:
  primary: "certified_completion_rate"
  secondary:
    - "scoring_coverage"
    - "conditional_recovery_rate"
    - "false_success_claim_rate"
    - "episode_cost"
aggregation_plan:
  primary_weighting: "task_macro"
  paired_by: ["scenario_id", "repetition_index"]
  uncertainty_plan_ref: "fixture:paired-task-bootstrap-v1"
```

九个场景清单假设来自三个任务、每任务三个有效场景；其具体初始状态、正常/扰动分类和幅度在清单内冻结。每组 18 条 trial，四组共 72 条首次运行。它是联调预实验规模，用于发现问题和估算成本，不保证能支持显著性或泛化结论。三个任务上的任务级 bootstrap 尤其有限。

### 14.2 一次“Agent 宣告成功，但实际失败”的结果

此例表示执行已确定结束，但最终支撑关系错误；因此 Agent 状态为 succeeded，独立结果仍为 fail。在线状态保留原记录，评测器不回写更正它。

```json
{
  "schema_version": "0.1",
  "protocol_version": "integration-evaluation-v0.1",
  "experiment_id": "fixture_pilot_001",
  "trial_id": "fixture_trial_h1f1_stack03_r0",
  "run_attempt_id": "fixture_run_h1f1_stack03_r0_a0",
  "episode_id": "fixture_episode_h1f1_stack03_r0_a0",
  "scenario_id": "fixture_stack03",
  "variant_id": "H1F1",
  "repetition_index": 0,
  "evaluator_run_id": "fixture_eval_h1f1_stack03_r0_a0_v1",
  "frozen_manifest_ref": "fixture:resolved-experiment-with-content-hashes-v1",
  "seeds": {"environment": 103, "perturbation": 203, "model": null},
  "agent_task_status": "succeeded",
  "agent_claimed_success": true,
  "claim_source": "fixture:event-final-success-claim",
  "evaluator_outcome": "fail",
  "goal_checks": [
    {
      "condition_id": "stack_relation",
      "verdict": "fail",
      "evidence_refs": ["fixture:truth-final-window"]
    }
  ],
  "constraint_checks": [
    {
      "condition_id": "stay_in_workspace",
      "verdict": "pass",
      "evidence_refs": ["fixture:truth-workspace-trace"]
    }
  ],
  "milestone_trace": [
    {"milestone_id": "lift_cube", "reached": true, "evidence_ref": "fixture:truth-lift"},
    {"milestone_id": "stack_cube", "reached": false, "evidence_ref": "fixture:truth-final-window"}
  ],
  "perturbation_trace": [
    {
      "perturbation_id": "fixture_drop_once",
      "trigger_reached": true,
      "injection_attempted": true,
      "effect_confirmed": true,
      "recoverable_by_protocol": true,
      "evidence_ref": "fixture:drop-injection-and-effect"
    }
  ],
  "stop_reason": "agent_success_claim",
  "execution_resolution": {
    "last_execution_status": "completed",
    "backend_motion_state": "idle",
    "stop_evidence_refs": ["fixture:backend-stop-confirmed"]
  },
  "failure_origin": ["unresolved"],
  "budget_usage": {"api_calls": 12, "control_steps": 600, "wall_time_s": 42.0},
  "timings": {"episode_wall_time_s": 42.0, "scoring_offline_wall_time_s": 0.2},
  "artifact_refs": ["fixture:episode-manifest", "fixture:agent-event-stream", "fixture:truth-stream"],
  "protocol_deviations": []
}
```

`goal_checks/constraint_checks` 中逐条件 verdict 仍使用 pass/fail/unknown；只有聚合后的 evaluator_outcome 使用 pass/fail/unscorable。里程碑 reached 允许 true/false/null，缺少相关证据时使用 null；示例的 false 有完整轨迹支持。新增 stop_reason 是评测记录中的原因字段，不是 TaskProgress 枚举。model seed 为 null 需在 manifest 记录服务不支持可控种子或本次未提供的原因。

上例没有足够证据认定首因位于 CoF、P 或 Verifier，因此 failure_origin 保留 unresolved；仅凭在线与实际结果不一致，不能定位是哪一层先出错。

### 14.3 汇总器人工校验样例

构造一组 10 条已对账 trial：6 pass、3 fail、1 unscorable。明确声明成功共 5 条，其中 3 pass、1 fail、1 unscorable。有效且协议可恢复的扰动共 4 条，其中 2 pass、1 fail、1 unscorable；其余试验按未触发、正常或无效注入分类记录。

预期结果：认证完成率 60%，评分覆盖率 90%，可评分样本成功率 6/9，缺失数据完成边界 [60%, 70%]；可评分声明错误率 1/4=25%，全部声明错误边界 [20%, 40%]，已确认错误声明占全部试验 10%；条件恢复率 2/4=50%，其中 1 条无法评分。这里的边界不是置信区间。

另设零声明、零有效扰动、空任务层、尚有 pending、重复 result 与一次重跑等 fixture，分别验证 NA、未完成标识、去重及首次运行规则。

## 15. 开发阶段与验收用例

### 15.1 开发顺序

| 阶段 | 交付 | 进入下一阶段的条件 |
| --- | --- | --- |
| D1 契约/登记 | schema、配置解析、固定 fixture、trial registry | 非法配置拒绝；重复调度/异常退出可对账；G1 协议用例通过 |
| D2 环境/评测 | 一个环境适配器、TruthAdapter、任务与扰动定义 | 人工可核对的成功/失败/未知判据通过；信息权限检查通过 |
| D3 轨迹/回放 | 不可变证据 manifest、因果回放、时间线 | G2 可复现指定判断，禁止未来证据和物理写操作 |
| D4 在线闭环 | 单 worker、真实 CoF/P、既有 Controller 接入 | G3 正常、恢复、未知、停止和预算路径都有完整轨迹 |
| D5 批量统计 | 四组配置、清单对账、配对指标及报告 | 人工统计 fixture 通过；运行预实验，冻结正式实验方案 |

D1–D3 可大量使用 fake/记录数据，但不能据此标记 D4 仿真性能通过。G4 正式效果验收需要另行实际执行冻结批次；本次文档交付不包含这些运行结果。

### 15.2 行为验收矩阵

下列是待实现、待执行的测试要求。优先覆盖跨模块行为和指标口径，不重复测试私有函数内部写法。

| ID | 场景 | 应满足的行为 |
| --- | --- | --- |
| E01 | P 版本增加，但未处理本次 feedback | 不冒充新状态；等待确认或显式未知 |
| E02 | 重复、乱序、跨 episode 报告 | 去重/隔离；不重复推进和派发 |
| E03 | 前置条件全 pass，目标未完成 | 不标记子目标/最终成功 |
| E04 | CoF 与 P 转述同一原始帧 | 保留 lineage，不算两份独立证据 |
| E05 | 失败执行产生了实际状态变化 | P 接收有效变化，后续不沿用旧状态 |
| E06 | 视觉目标正确，后端 outcome_unknown | 禁止新运动/成功结束，先对账 |
| E07 | 用户取消与 worker 重启 | 不自动恢复动作，保留原预算与在途记录 |
| E08 | 遮挡、缺帧、身份不确定 | 保留 unknown；观察有界且计入预算 |
| E09 | 通过 env、日志、文件或缓存访问真值 | observed_only 无法获得隐藏内容，并留下审计记录 |
| E10 | 回放读取未来帧/已执行 A 后改为 B | 因果模式拒绝越界；不计算 B 的反事实成功 |
| E11 | 只调用扰动 API，效果未发生 | injection_attempted=true，不计有效扰动 |
| E12 | 方法没到扰动触发阶段 | 计整体试验；不计条件恢复分母 |
| E13 | 扰动后物体不可恢复或目标变更 | 按冻结规则分组，不事后按成功挑样本 |
| E14 | 正常释放/移动与历史里程碑 | 不误判正常变化；最终条件独立检查 |
| E15 | 最终支撑正确但未释放，或已违反过程约束 | 依冻结任务判据 fail，不能只看 reward |
| E16 | 成功短暂出现，稳定窗口结束前失效 | 按规定窗口失败；稳定窗口计入预算 |
| E17 | 截止之后新动作才使任务成功 | 不记为预算内成功，保留超期证据 |
| E18 | 无最终证据且无确定反例 | unscorable，保留在登记分母和覆盖率 |
| E19 | 原生解析器把非法响应兜底为 finish | 不作为明确成功声明，记录声明未知 |
| E20 | Agent succeeded，独立评测 fail | 两份结果均保留，计入错误声明指标 |
| E21 | 代码 rc=0，但任务条件不成立 | 可执行成功与任务失败分别统计 |
| E22 | 初始化/worker 崩溃，无 TrialSummary | 清单仍有该 trial，补充异常/不可评分记录 |
| E23 | 相同 trial 被两个 worker 认领 | 仅一个运行获准启动，其他等待/拒绝 |
| E24 | 首次失败、重跑成功 | 主表按首次规则，重跑与成本单列 |
| E25 | 评测器重跑或判据修订 | 不新增物理样本；保留评分版本，适用组统一重评 |
| E26 | 零声明/零有效扰动或 pending 未清 | NA/未完成，不能输出伪造百分比或完整报告 |
| E27 | 第 14.3 节固定结果与重复结果 | 分子/分母/边界正确，结果去重 |
| E28 | 各方法 LLM 调用次数不同 | 不改变外部扰动随机流；保留配对身份 |
| E29 | 相邻帧分集、技能演化或跨 episode P 残留 | 阻止数据泄漏，或明确隔离为不同实验 |
| E30 | 链内 reset、预算清零、重定义步骤长度 | 拒绝该长程成绩或标记协议偏差，不计入合规主表 |

凡发生协议偏差，保留原 trial 和结果记录。合规性能表的纳入规则需预先冻结；主表同时报告全登记完成率与协议偏差数，不能通过删除违规失败让方法看起来更好。

## 16. 产物与报告要求

建议每个 experiment 保存如下结构，Agent 工作目录与评测真值目录须按能力隔离；仅放在两个不同文件夹并不构成权限隔离。

```text
experiment/<experiment_id>/
  resolved_experiment.json     # 所有引用解析后的配置、版本及摘要
  trial_manifest.jsonl        # 调度前登记的完整统计单元
  run_registry.jsonl          # 所有首次运行、异常和重跑的审计记录
  agent_artifacts/            # 按 episode 分区：观测、执行、反馈、P、控制事件
  evaluation_private/        # 独立真值、扰动触发/效果、人工标注
  evaluation_results.jsonl    # 带版本的逐运行评分
  aggregate_metrics.json     # 分子、分母、NA、分层、区间和成本
  report.md                  # 实验条件、结果、限制、失败实例及产物索引
```

最小报告包含：

1. 实际运行层级、代码/模型/P 版本、任务来源与指令协议、四组最终配置差异。
2. 登记数、完成数、pending、S/F/U、重跑、协议偏差及评分覆盖率。
3. 逐任务与总表：认证完成率、前缀能力、触发/恢复、错误声明、成本，均显示样本数和统计口径。
4. 配对差异及适当区间；原生基线、oracle 诊断、全链实验和共同检查点恢复实验分别呈现。
5. 正常、自然失败、有效扰动、未知/停止、预算耗尽的代表轨迹，失败原因有证据链接。
6. 未运行项目、缺失数据、服务随机性和任务数量限制；不把设计预期写成实验结论。

先交付一个任务上的完整联调证据包，再运行第 14 节规模的预实验。根据成本、失败分布和估计误差确定正式任务数与重复数；正式 test 批次冻结后不再用其结果调整提示或阈值。

## 17. 参考资料与采用边界

以下参考已核对作者仓库、项目页或论文实验章节。本文的注册清单、接口、权限隔离、缺失口径和共享账本是项目工程设计，不是这些工作的原样实现。

| 工作 | 可查资料 | 本文借鉴 | 采用边界 |
| --- | --- | --- | --- |
| CALVIN，2022 | [作者仓库：长程语言控制](https://github.com/mees/calvin#long-horizon-multi-task-language-control-lh-mtlc) | 连续任务、任务间保留环境状态、序列完成指标 | 借鉴长程协议；首版不新增 CALVIN 后端。完整指令与逐条指令分开，不直接比较自定义分数与官方成绩 |
| LIBERO-PRO，2025；v2 修订 | [论文实验 §4](https://arxiv.org/html/2510.03827v2#S4)、[作者仓库](https://github.com/Zxy-MLlab/LIBERO-PRO) | 对象、配置、指令和环境等维度的泛化评测；关注变体可行性 | 按实际接入版本导出任务清单；区分目标改变与同目标扰动。论文样本数不能自动作为本项目统计充分性的依据 |
| DoReMi，2023；v4 修订 | [论文实验 §IV](https://arxiv.org/html/2307.00329v4#S4) | 执行扰动下的恢复、任务成功与耗时 | 本项目首版段后反馈；不声称复现执行中 VLM 监控/中断能力 |
| AHA，ICLR 2025 | [作者项目页](https://aha-vlm.github.io/) | FailGen 合成失败、失败理解与后续恢复评估 | 同时保留正常、未知与自然失败；合成错误分布不代表全部真实失败 |

以上工作分别支撑长程、泛化、执行扰动与失败分析的实验思路。是否提高本项目性能，必须由冻结对照和实际运行回答，不能从相关论文的结果直接推得。

## 18. 与 P 侧的首次联调交付清单

- 共同冻结一组最小消息：ExecutionReport、CoFFeedback、StateView/融合确认、VerificationReport；用相同 episode/execution/feedback ID 跑通关联。
- 约定观测权限、时间戳/时钟域、lineage、冲突/unknown、事实有效期与超时；P 的 StateView 更新与反馈处理确认分开核对。
- 提供正常完成、掉落恢复、遮挡未知、迟到反馈四组 fixture，以及每组预期可用事实和控制决定。
- Agent 侧提供只读时间线、运行登记与独立评测结果；P 侧提供可定位到原始证据的状态变化，不新增另一份权威任务进度。
- 每次交付明确标记“契约通过 / 回放通过 / 仿真已跑 / 正式评测已跑”的实际范围，未验证项保留待办。
