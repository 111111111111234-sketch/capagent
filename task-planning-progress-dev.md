# 机器人 Coding Agent：任务规划与进度管理开发文档

版本：v0.1 · 日期：2026-09-24 · 状态：完整开发设计；确定性首版已实现，模型与仿真联调待完成

2026-09-28 实现进展：已落地本模块的确定性首版，包括计划校验、进度、子目标选择、验证报告接收、局部恢复、共享账本和 mock CLI。模型生成计划以及完整仿真联调尚未实施；本文保留完整目标设计，实际支持范围以[第二阶段运行说明](/Users/agiuser/Documents/Codex/2026-09-24/ca/cap-x/docs/stateful-planning.md)为准。

适用代码基线：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

总入口：[开发总览与使用指南](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-development-guide.md)。

本文覆盖你负责的“任务规划与进度管理”。CoF 指 Chain of Frame；其视觉分析算法、代码执行器及完整循环控制器分别开发，本模块通过接口使用它们的输出。本文出现的新目录、类型、接口和配置均为拟议设计，尚未在 Cap-X 中实现。

## 1. 开发目标与职责边界

给定任务、当前结构化世界状态、相关历史和可用技能，模块需要：

1. 把任务拆成可执行、可验证的子目标，并保存依赖关系。
2. 根据当前状态选择下一子目标，为代码生成器提供清晰的执行要求。
3. 依据执行后证据更新进度，区分已完成、未达成和结果未知。
4. 在环境变化或执行失败后调整当前步骤，必要时修订局部计划。
5. 保留计划版本、尝试记录和验证依据，支持回放与中断后的状态恢复。

第一版采用“初始计划 + 每轮选择当前子目标 + 事件触发的局部修订”。计划不要求一次生成全部低层动作；较远的步骤保留目标和条件，临近执行时才生成动作代码。

### 1.1 谁负责什么

| 部分 | 主要责任方 | 与本模块的关系 |
| --- | --- | --- |
| 世界状态、对象身份、状态融合、历史压缩与检索、上下文组织 | 状态与记忆侧 P | 提供只读的 StateView 和相关上下文；本模块不重复实现 |
| 子目标、依赖、计划版本、执行进度、恢复计划 | 你：本模块 | 持久保存，产生下一子目标决策 |
| 为当前子目标生成代码、调用 API、记录运行结果 | 你：代码生成与执行模块 | 接收 SubgoalContract，返回 ExecutionReport |
| 帧链采样/分析、事件与不确定性提取 | 你：CoF 模块 | 给状态融合与结果验证提供证据；不能直接标记任务完成 |
| 将条件与状态/CoF/传感器证据对应并验证 | 你：验证模块，依赖 P 提供的事实 | 返回 VerificationReport；本模块消费验证结果 |
| 调用各模块、动作预算、取消、确认运动停止、结束 episode | 你：Loop Controller | 调用本模块接口，执行其决策并实施运行控制 |

接口对齐是协作工作；状态构建和长期上下文组织不是本模块的开发内容。本模块自行保存的 TaskProgress 是任务运行记录，可共享给 P 用于组织上下文，但其更新权限保留在本模块。

### 1.2 第一版范围

- 一次处理一个任务、一个正在执行的动作段；子目标按序执行，允许显式依赖。
- 一个 LLM 可同时承担初始规划和局部计划修订；进度更新、结构校验、候选过滤由程序完成。
- 使用固定 StateView、ExecutionReport 和 VerificationReport 样例即可独立开发，不依赖 GPU 或 CoF 已完成。
- 暂不实现并行子目标调度、完整 PDDL 求解器、学习式技能价值函数或真实机器人中断控制。
- 第一版目标是逻辑正确、记录可追踪；物理操作成功率需要接入真实模型与仿真后评估。

历史经验与固定技能检索可随本模块开发，不依赖学习式价值函数或自动技能发布。经验索引、技能资产和发布规则见[第六部分](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/experience-skills-self-improvement-dev.md)；本模块接收固定版本的候选上下文，继续按当前状态和契约校验。

## 2. 当前 Cap-X 的基础与接入位置

源码位置相对仓库根目录；行号对应上述提交。

| 位置 | 当前行为 | 本模块的接入方式 |
| --- | --- | --- |
| `capx/envs/trial.py:629`，`_run_single_trial()` | 初始化环境、生成代码、执行并保存结果 | 在显式配置下进入新的 stateful 分支 |
| `capx/envs/trial.py:469`，`_query_initial_code()` | 直接根据任务上下文生成初始代码 | stateful 分支先规划，再以 SubgoalContract 调用代码生成器 |
| `capx/envs/trial.py:780` | 按代码块执行、进入多轮决策 | 每个动作段返回后，获取新状态/验证报告，更新本模块 |
| `capx/envs/trial.py:505`，`_handle_multi_turn_step()` | 组合代码历史、控制台和视觉反馈进行决策 | 原 baseline 保留；新分支使用显式 PlannerDecision |
| `capx/utils/launch_utils.py:310` | 不含 `REGENERATE` 的回复都归为 finish | 新协议采用严格类型校验；解析失败不能进入成功结束 |
| `capx/envs/tasks/base.py:153` | 在持久 Python namespace 内执行代码 | 复用执行能力；namespace 不是任务进度的持久存储 |
| `capx/envs/tasks/base.py:263` | 返回 observation、reward、终止标记和运行信息 | 由适配器生成运行报告，环境真值和可见反馈分别处理 |
| `capx/utils/launch_utils.py`，`_load_config()` | 显式拼装配置字典 | 新配置需要增加传递逻辑，仅在 YAML 写键不会自动生效 |

Cap-X 已经有多轮执行、变量保留、API 文档和视觉反馈。本模块的新增点是显式计划、证据驱动的进度管理与局部修订。第一阶段只接 CLI；Web UI 以后调用同一套逻辑，避免复制规划规则。

## 3. 模块流程与不变量

```text
TaskSpec + StateView + SkillCatalog + RelevantHistory
                            ↓
                 LLM 生成 PlanProposal
                            ↓
                 程序校验并保存 TaskPlan
                            ↓
            筛选依赖/前置条件满足的子目标
                            ↓
                  输出 SubgoalContract
                            ↓
          外部：代码生成 → 执行 → CoF / P / 验证
                            ↓
  ExecutionReport + 新 StateView + VerificationReport
                            ↓
          更新 TaskProgress，必要时提交 PlanPatch
                            └──→ 再次选择下一步
```

必须维持以下不变量：

1. TaskSpec 中的最终目标与约束在一次任务内固定；模型不能为便于完成而删除或放宽目标。用户明确修改任务时建立新任务版本。
2. LLM 只提交计划或修订提案，不能直接写入已完成状态、计数器或验证记录。
3. 执行器报告“调用完成”与验证器报告“目标成立”分开保存。
4. 每次动作执行绑定 episode、计划版本、子目标 ID、执行 ID 和执行所依据的状态版本。
5. 同一报告重复到达不会重复增加尝试次数或推进进度；跨 episode、错配或过期报告不能错误推进当前计划。
6. 所有条件使用 true / false / unknown 三值语义；缺信息不等于 false，更不等于 true。
7. 计划或观察重试受预算约束；不能通过不断生成新子目标 ID 清空失败次数。
8. 历史上完成过的步骤与当前世界事实分别记录；最终成功需要重新检查最终目标条件。

## 4. 数据契约

推荐使用 Pydantic 或等价的严格类型校验，序列化为 JSON。枚举和字段类型固定，未知字段默认拒绝。以下是最小字段约定；完整 JSON Schema 在实现阶段从模型定义导出。

### 4.1 输入契约

| 类型 | 必要字段 | 说明 |
| --- | --- | --- |
| TaskSpec | `schema_version`, `episode_id`, `task_id`, `task_version`, `instruction`, `goal_conditions`, `constraints` | 最终目标条件由任务适配器/研究者给定；模型推导的条件需先通过任务约束校验再冻结 |
| StateView | `episode_id`, `state_version`, `observed_at`, `objects`, `facts`, `evidence_refs` | 由 P 提供；本模块只要求能按对象与条件查询，不接管世界状态结构 |
| SkillCatalog | `catalog_version`, `skills` | 技能/API 名称、参数说明、前置条件、支持的操作范围；来自执行侧 |
| PredicateCatalog | `catalog_version`, `predicates` | 支持哪些条件、参数类型、单位/坐标语义、证据来源、时效规则和检查方式；与 P/验证侧对齐 |
| RelevantHistory | `event_refs`, `summary` | 由上下文侧提供；最近失败与尝试记录也保留在 TaskProgress |
| ExecutionReport | `episode_id`, `execution_id`, `plan_version`, `subgoal_id`, `attempt_id`, `status`, `runtime_rc`, `started_at`, `ended_at`, `evidence_refs` | 执行状态为 `completed / error / timed_out / cancelled / outcome_unknown`；未获运行结果时 runtime_rc、ended_at 可为 null；报告运行事实，不宣告目标完成 |
| VerificationReport | `report_id`, `episode_id`, `plan_version`, `scope`, `subject_id`, `execution_id`, `state_version`, `checked_at`, `checks` | `scope` 为 `segment / subgoal / final_goal`；checks 对照已接受契约的条件逐项返回证据；初始/最终静态检查允许 execution_id 为 null |

`VerificationReport.checks` 每项至少包含 `condition_id`、`verdict`（pass / fail / unknown）、`evidence_refs`、`source`、`observed_at`。报告须覆盖该次请求的全部条件；缺项按 unknown 处理。证据来自被允许的实际观测或评测设置明确许可的来源，模型自述和未来预测不作为已完成证据。

状态版本号只用于排序和匹配，不能证明每条事实都新鲜。条件检查还需要使用事实自己的观测时间、有效性与来源；不同后端的仿真时间/真实时间不能直接混用。

### 4.2 条件表达：有限谓词，不执行模型生成的表达式

每个 Condition 使用 `id`、`predicate`、`args`、`expected` 四个字段。第一版一个条件列表表示逻辑 AND；不支持任意 Python 表达式或自然语言作为可执行判断。

```json
{
  "id": "c_holding_red",
  "predicate": "holding",
  "args": ["robot", "red_cube"],
  "expected": true
}
```

示例谓词包括 `pose_known(object)`、`gripper_empty(robot)`、`holding(robot, object)`、`lifted(object)`、`above_for_placement(object, support)`、`on(object, support)`、`gripper_open(robot)`。这些是待实现的领域谓词，不能假定 Cap-X 已直接提供。

PredicateCatalog 必须定义它们的检查依据。例如 `on` 需要明确支撑/稳定性判据，不能只看图像中两个物体上下相邻；`lifted` 的参考支撑面和判定窗口也要明确。具体阈值由任务适配器配置，规划模型不能自行编造或降低阈值。

对 `expected: false`，只有已观测到 predicate 为 false 才通过；unknown 不满足否定条件。同一状态中证据冲突时交由 P/验证侧解决或返回 unknown，本模块不自行选择有利证据。

### 4.3 输出与持久结构

| 类型 | 关键字段 | 更新权限 |
| --- | --- | --- |
| PlanProposal | `based_on_state_version`, `goal_coverage`, `subgoals` | LLM 提议，程序验证 |
| TaskPlan | `task_id`, `task_version`, `plan_version`, `based_on_state_version`, `goal_coverage`, `subgoals` | 校验后提交；旧版本只读保留 |
| Subgoal | `id`, `goal`, `depends_on`, `preconditions`, `success_conditions`, `candidate_skills`, `recovery_group_id` | 由计划管理器提交；不包含模型可写的运行状态 |
| TaskProgress | `episode_id`, `task_status`, `plan_version`, `revision`, `active_subgoal_id`, `subgoal_progress`, `budget_usage` | 进度管理器为单一写入方 |
| SubgoalProgress | `status`, `attempt_count`, `active_attempt_id`, `execution_ids`, `last_failure`, `verification_refs`, `completed_at` | 执行登记和验证事件驱动更新 |
| SubgoalContract | `contract_id`, `episode_id`, `task_version`, `plan_version`, `subgoal_id`, `based_on_state_version`, `goal`, `preconditions`, `acceptance_conditions`, `allowed_skills`, `context_refs`, `budget` | 程序根据已接受计划生成，交给代码生成/执行模块 |
| SegmentContract | `segment_id`, `subgoal_contract_id`, `attempt_id`, `entry_conditions`, `expected_conditions`, `continue_conditions`, `budget` | 执行侧提出本段边界，经契约/谓词检查后在派发前冻结；不能改写子目标验收条件 |
| PlannerDecision | `kind`, `reason_code`, `subgoal_id`, `required_conditions`, `evidence_refs`, `contract` | 由选择逻辑产生；不同 kind 校验不同必填字段 |
| PlanPatch | `base_plan_version`, `base_progress_revision`, `reason_code`, `evidence_refs`, `retire_subgoals`, `add_subgoals`, `dependency_updates` | LLM 提议，程序原子校验并提交 |

`goal_coverage` 把最终目标 condition ID 映射到拟议子目标 ID，便于检查是否遗漏任务要求。映射存在只是结构检查，不证明计划可达或物理上可执行；实际执行前仍要检查前置条件，最终仍需目标验证。

`TaskProgress.task_status` 采用 `active / succeeded / failed / blocked / budget_exhausted / interrupted`。Controller 提交任务终止事件，由进度存储统一记录；从 blocked/interrupted 恢复时保留历史和预算，并重新观察。Subgoal 状态与任务级状态不混用。

### 4.4 任务计划样例

下面是经校验后接受的计划示例。最终条件 `g_on_red_green` 与 `g_gripper_open` 来自 TaskSpec，分别对应 `on(red_cube, green_cube)` 与 `gripper_open(robot)`。实际 skill 集合随 Cap-X API 配置提供，本例沿用 reduced API 中的部分名称。

```json
{
  "task_id": "stack_red_on_green",
  "task_version": 1,
  "plan_version": 1,
  "based_on_state_version": 12,
  "goal_coverage": {
    "g_on_red_green": ["s3_place"],
    "g_gripper_open": ["s3_place"]
  },
  "subgoals": [
    {
      "id": "s1_grasp",
      "goal": "抓住并抬起红块",
      "depends_on": [],
      "preconditions": [
        {"id": "c_pose_red", "predicate": "pose_known", "args": ["red_cube"], "expected": true},
        {"id": "c_empty", "predicate": "gripper_empty", "args": ["robot"], "expected": true}
      ],
      "success_conditions": [
        {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true},
        {"id": "c_lifted_red", "predicate": "lifted", "args": ["red_cube"], "expected": true}
      ],
      "candidate_skills": ["get_observation", "plan_grasp", "solve_ik", "move_to_joints", "open_gripper", "close_gripper"],
      "recovery_group_id": "grasp_red"
    },
    {
      "id": "s2_transport",
      "goal": "把红块搬运到绿块上方的可放置位置",
      "depends_on": ["s1_grasp"],
      "preconditions": [
        {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true},
        {"id": "c_pose_green", "predicate": "pose_known", "args": ["green_cube"], "expected": true}
      ],
      "success_conditions": [
        {"id": "c_above", "predicate": "above_for_placement", "args": ["red_cube", "green_cube"], "expected": true},
        {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true}
      ],
      "candidate_skills": ["get_observation", "solve_ik", "move_to_joints"],
      "recovery_group_id": "transport_red"
    },
    {
      "id": "s3_place",
      "goal": "放置红块并释放夹爪",
      "depends_on": ["s2_transport"],
      "preconditions": [
        {"id": "c_above", "predicate": "above_for_placement", "args": ["red_cube", "green_cube"], "expected": true},
        {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true}
      ],
      "success_conditions": [
        {"id": "g_on_red_green", "predicate": "on", "args": ["red_cube", "green_cube"], "expected": true},
        {"id": "g_gripper_open", "predicate": "gripper_open", "args": ["robot"], "expected": true}
      ],
      "candidate_skills": ["get_observation", "solve_ik", "move_to_joints", "open_gripper"],
      "recovery_group_id": "place_red"
    }
  ]
}
```

本例的成功条件是逻辑层要求，不是可直接运行的机器人代码。子目标可覆盖多个动作段；代码生成器还需将候选技能和具体状态组合成代码。

## 5. 初始规划与下一子目标选择

### 5.1 创建计划

规划上下文包括原始任务及冻结目标、当前对象/相关事实、可用技能和谓词定义、输出 schema、少量任务分解示例。初始开发直接读固定输入，正式联调由 P 的上下文模块提供相同信息。

启用固定复用时，P/检索适配器还可提供获准的历史案例、经验规则和技能版本。案例是规划建议，不直接写成当前事实。SkillCatalog.skills 和 candidate_skills/allowed_skills 中的名称指向实际注册 callable；第六部分的 procedure 作为流程资产引用进入上下文，经正常计划提案或 PlanPatch 校验后展开为子目标/动作段，不能将流程名称伪装成一个底层 API。

提示词要求：

- 每个子目标描述可观察的目标变化，并绑定成功条件。
- 只引用已知对象、已注册谓词和可用技能。
- 使用现有状态；已满足的目标不安排重复动作，可请求验证后跳过。
- 对缺失信息显式请求观察；允许插入必要的准备步骤。
- 输出 PlanProposal，不输出成功状态，不输出动作代码，不修改 TaskSpec。

程序校验顺序：JSON/schema → ID 唯一性 → 依赖引用存在且无环 → 对象/技能/谓词有效 → 同一 condition ID 定义一致 → 验收条件非空且可检查 → 最终目标覆盖完整 → 步骤与预算上限。

校验失败返回字段级错误给模型修复。建议允许一次修复，即最多两次提案调用；仍失败输出 `PLAN_INVALID`，由 Controller 以失败/阻塞原因处理，不能假定任务已完成。

### 5.2 下一步选择

第一版采用稳定、可复现的拓扑顺序选择，不必每轮额外调用 LLM 排序。

1. 校验 episode、任务/计划版本和当前运行状态。
2. 若仍有动作在运行或执行结果未知，返回等待执行对账的决策，不派发新动作。
3. 初始化时、候选最终目标已达成时、计划耗尽时，请求最终目标验证。最新最终目标全部 pass 则建议结束；任务约束中若包含必须发生的动作，其事件证据也要满足，不能只看最终摆放状态。
4. 优先处理 active 子目标的验证或恢复；第一版不并行跳去其他子目标。
5. 在未完成子目标中筛选所有依赖已完成或经验证跳过的节点。
6. 查询其当前前置条件。全部 pass 才可输出新尝试的 SubgoalContract；同一尝试的后续动作段按 SegmentContract 的当前入口条件检查。
7. 条件 unknown 时请求对应事实的补充观测；条件 fail 时检查是否有既有准备步骤，否则请求局部修订。活跃子目标耗尽但最终目标未达成时，继续恢复/修订或明确阻塞，不按步骤计数结束。

未来多分支任务可以由 LLM 在合法候选中选优，或增加代价评分，但候选过滤和最终检查仍由程序执行。动作派发前还需再次检查相关事实时效；无关对象导致状态版本增长时，不必机械拒绝整个计划。

### 5.3 PlannerDecision 枚举

| kind | 含义 | 必须携带的信息 |
| --- | --- | --- |
| `execute` | 执行或继续当前子目标的一个动作段 | SubgoalContract；是否新 attempt 由进度管理器明确登记 |
| `request_observation` | 补充缺失、过期或冲突的信息 | 所需条件/对象、原因；无动作代码 |
| `request_verification` | 验证当前子目标或最终目标 | 范围、条件 ID、证据/状态版本要求 |
| `request_replan` | 需要改变计划内容或依赖 | 受影响节点、失败证据、修订原因 |
| `wait_for_execution` | 已派发动作未完成或需确认实际执行结果 | execution_id；Controller 负责等待、取消和对账 |
| `finish` | 当前任务最终目标经验证满足 | 最新最终目标验证报告引用 |
| `blocked` | 在当前可用能力与信息下无法继续 | 具体阻塞原因与缺失条件 |

`finish` 是给 Controller 的建议；Controller 还要检查任务约束、终止原因和是否存在未结束动作。失败、中断、预算耗尽不是成功，不通过 finish 伪装。

## 6. 进度状态与更新规则

### 6.1 子目标状态机

| 当前状态 | 事件与条件 | 新状态 / 处理 |
| --- | --- | --- |
| `pending` | 前置条件满足，持久登记派发 | `running`；建立 attempt 与 execution 关联 |
| `running` | 动作段已结束，包括异常或取消后的确定停止 | `awaiting_verification`；核对实际效果 |
| `running` | 超时且不能确认底层已停止 | 保持阻塞在执行对账；不派发替代动作 |
| `awaiting_verification` | 全部成功条件 pass | `succeeded`，保存报告和完成时间 |
| `awaiting_verification` | 条件尚未全达成，但该动作段正常推进且仍在本次尝试预算内 | `running`；为同一 attempt 派发下一段，重新检查适用条件 |
| `awaiting_verification` | 明确失败且需要改变策略 | `needs_recovery`；结束本次 attempt |
| `awaiting_verification` | 关键条件 unknown | 保持原状态，请求补观察/验证 |
| `needs_recovery` | 当前子目标可局部重试 | 重新检查当前条件后进入 `running`，建立新 attempt |
| `needs_recovery` | 无法局部处理 | 请求局部修订，或进入 `blocked` |
| `pending` | 无需执行且当前成功条件已经获得验证 | `skipped_verified`，保留证据 |
| `pending / needs_recovery / blocked` | 校验通过的新计划替代此步骤 | `superseded`，只保留历史；不作为依赖满足依据 |

`blocked` 表示该节点当前不能推进，可在新信息到来后重新评估；是否终止整个任务由 Controller 决定。第一版禁止在动作仍运行时提交影响该动作的 PlanPatch。

子目标有多个动作段时，不能把“第一段尚未完成整个子目标”直接判成失败。SubgoalContract 需要与执行侧约定本段意图、继续条件及预算；每段后检查状态，必要时调整下一段。

具体采用 SegmentContract 表达本段条件：例如“抓住并抬起”的第一段为闭合夹爪，第二段为小幅抬升。`gripper_empty` 是开始抓取的条件，闭合后不能要求它仍成立；第二段根据当前夹持信息与动作约束重新检查入口条件。子目标整体验收条件保持不变。expected_conditions 用于评估本段效果，continue_conditions 用于判断是否能继续，unknown 时需要补充证据。

如果运行时报错但后置证据证明子目标确已达成，可以保留运行错误并记录子目标成功；是否继续还取决于故障是否已解除、任务约束及控制器状态。也不能仅因代码没报错就认定取得了进展。

### 6.2 尝试、动作段与重试计数

- `execution_id`：一次被派发的动作段。每次派发使用新 ID，重发同一请求必须使用原 ID 并先对账。
- `attempt_id`：一次达成当前子目标的尝试，可包含多个 execution。
- `attempt_count`：首次派发该 attempt 时增加一次；解析错误、重复报告、补观察不增加。
- 同一 attempt 的正常多段推进不计为多次失败重试；执行侧应明确返回当前动作段是否结束本次尝试，进度侧结合验证与预算接受该事件。
- 修复代码后的再次尝试或更换抓取策略建立新 attempt；部分生效时先观察再决定如何继续，不自动重放整段代码。
- `recovery_group_id` 由管理器跟踪同一目的的恢复链，新节点沿用或被校验归入原组，共享总尝试预算；全局执行预算始终累计。

### 6.3 曾经完成与现在仍成立

`succeeded` 表示某一历史时刻已验证完成，不表示该效果永远成立。依赖关系回答执行顺序，当前前置条件回答现在能否执行，两者必须同时检查。

例一：抓取成功后搬运中掉落。保留原抓取成功记录，但 P 中 `holding` 已变为 false，后续搬运/放置不得继续；插入恢复抓取，再继续受影响流程。

例二：正常放置后 `holding` 变为 false。此时最终目标可能已经成立，不应触发恢复抓取。

如果最终已满足的摆放关系随后被打乱，最终目标检查应发现它并请求恢复。不能只统计历史 succeeded 节点数来决定结束，也不要求所有中间成功条件同时保持为真。

### 6.4 验证报告接收

收到报告后先匹配 episode、计划版本、subject、execution、条件定义和证据时效。重复 report_id 为幂等 no-op。不能用执行前的帧证明执行后的成功，也不能用旧计划中另一个子目标的报告更新当前节点。

匹配失败的报告写入日志并请求核对，不静默接纳；可作为历史证据保留，但不能直接推进当前状态。同一 execution 后又有新动作改变环境时，迟到的验证只证明其对应时间点，后续前置条件必须重新检查。

## 7. 局部恢复与计划修订

### 7.1 何时需要修改计划

| 触发情况 | 推荐处理 |
| --- | --- |
| 代码语法/调用参数错误，目标与前置条件仍有效 | 执行侧修复代码，在预算内新建尝试；不修改任务图 |
| 第一次抓取失败，仍能抓同一对象 | 保留子目标，换候选/调整动作；先处理部分执行效果 |
| 观测遮挡或状态过期 | 请求观察；不是规划失败 |
| 夹持丢失、对象不可达、必要前置步骤缺失 | 修订受影响子计划，补充恢复/准备步骤 |
| 同类恢复多次失败或目标分解有误 | 重新考虑受影响子目标及其后继 |
| 原任务已经不可满足 | 返回明确 blocked 原因；不能自行换成更容易的最终任务 |

### 7.2 PlanPatch 规则

修订输入包括原始 TaskSpec、当前 TaskPlan/TaskProgress、新 StateView、相关验证及失败记录。模型只返回局部差异和简短决策理由，不重写成功历史。

提交前检查：

1. `base_plan_version` 和 `base_progress_revision` 与当前值一致；否则基于新版本重新生成。
2. 没有修改冻结目标、任务约束、运行计数或历史证据。
3. 新图无环，无悬空依赖，最终目标仍有覆盖。
4. 已完成节点不删除、不改写；可失效的当前条件由新状态说明。
5. 被替代的未完成节点标为 superseded，所有活跃后继显式重连。
6. 新技能、谓词、对象和预算均有效；恢复节点继承对应恢复组预算。
7. 无冲突的在途执行。校验通过后原子增加 plan_version，并追加 PlanRevised 事件。

例如搬运时掉落，可将失败的搬运节点保留为历史并标为 superseded，增加“恢复抓取”“重新搬运”，把原放置节点依赖改为“重新搬运”。若只是同一位置重试搬运，不改变目标和依赖，则无需创建新计划版本。

## 8. 模块接口与实现组织

### 8.1 公共接口建议

以下为接口草案，不是可直接运行的实现；类型对应第 4 节。

```python
def create_plan(task, state, skills, predicates, context) -> "PlanProposal":
    """调用模型产生提案，不修改持久进度。"""
    ...

def validate_and_commit_plan(proposal, task, state, catalogs, store) -> "TaskPlan":
    """程序校验初始提案并保存首个计划版本。"""
    ...

def select_next_subgoal(task, plan, progress, state, checks) -> "PlannerDecision":
    """无环境写入；返回下一步决策。"""
    ...

def register_dispatch(contract, execution_id, attempt_id, store) -> "TaskProgress":
    """派发前持久登记；按 ID 幂等，检查执行/尝试预算。"""
    ...

def apply_execution_report(report, store) -> "TaskProgress":
    """记录动作结束或不确定状态，不自行宣告成功。"""
    ...

def apply_verification(report, state, store) -> "TaskProgress":
    """匹配证据后做确定性状态转移，重复报告不重复生效。"""
    ...

def propose_revision(task, plan, progress, state, feedback) -> "PlanPatch":
    """模型提出局部修订。"""
    ...

def commit_revision(patch, catalogs, store) -> "TaskPlan":
    """校验基础版本及图约束后原子提交。"""
    ...
```

对 CoF/P 的依赖通过适配器隔离：本模块只消费已约定格式的状态及验证报告，不直接解析帧、不在规划逻辑里调用视觉模型。CoF 的语言描述必须先由验证侧对照条件转成 pass / fail / unknown。

### 8.2 建议目录

```text
capx/agents/stateful/planning/
  contracts.py          # 所有数据类型和 schema
  planner.py            # 初始规划、局部修订的 LLM 调用
  validation.py         # 计划、条件引用、图与补丁检查
  selector.py           # 合法候选筛选与决策
  progress.py           # 事件驱动状态转移与预算账本
  store.py              # 事件存储、快照、恢复
  adapters.py           # StateView / SkillCatalog / 验证接口
  prompts.py            # 初始规划与修订模板，带版本号

tests/planning/
  fixtures/             # 固定任务、状态、反馈和预期行为
  test_validation.py
  test_progress.py
  test_selection.py
  test_recovery.py
```

核心模块不应导入仿真器、SAM3 或机器人控制服务；这样可以在当前 Mac 上运行逻辑测试。依赖通过 adapters 注入。

### 8.3 持久化与重启

每个 episode 使用独立目录；保存 TaskSpec、每版 TaskPlan、TaskProgress 快照和追加事件日志。关键事件包括 PlanCreated、ExecutionDispatched、ExecutionReported、VerificationApplied、ObservationRequested、PlanRevised、TaskTerminated。

建议采用单写者、递增 event_seq。先写事件并确保持久化，再原子替换快照；快照保存已应用的 event_seq。重启时从快照重放后续事件，按 report_id / execution_id 去重；尾部损坏记录需检测并停止恢复，不猜测内容。

派发前已登记但未收到可靠执行结果时，重启后标记需要对账，不能自动再次执行。Controller 向执行端查询动作状态、确认停止并重新观察，再决定恢复方式。本模块的检查点能恢复计划记录，不意味着可以精确恢复物理世界或 Python namespace。

### 8.4 配置草案

```yaml
agent_mode: stateful
planning:
  max_subgoals: 12
  max_plan_repairs: 1
  max_attempts_per_recovery_group: 3
  max_segments_per_attempt: 4
  max_replans: 5
  max_observation_requests_without_progress: 3
  max_execution_segments: 30
```

这些数值只作为开发默认值，后续根据任务调参，不构成已有性能结论。真实机器人或仿真 Controller 还需独立的时间/控制步数预算；所有限制实际执行，不能只写入提示词。

无进展计数按任务相关条件是否获得新信息/变得满足来判断，不能仅凭 state_version 增长清零。执行/观察/重规划均消耗对应预算，计划修订不重置 episode 总量。超过预算由 Controller 输出明确的 budget_exhausted 终止结果。

## 9. 贯穿案例：抓取失败后恢复

固定场景：红块、绿块在桌面，初始夹爪为空；目标为红块稳定放在绿块上且夹爪打开。

| 阶段 | 输入证据 | 本模块应做什么 |
| --- | --- | --- |
| 初始化 | StateView v12，物体位置有效 | 创建三步计划，当前子目标 s1_grasp |
| 派发第一次抓取 | 合法 SubgoalContract | 先登记 attempt-1 / exec-1，状态 running |
| 执行结束 | runtime_rc=0 | 转 awaiting_verification，不标成功 |
| CoF/P/验证返回 | 红块没有被抬起，成功条件 fail | 标 needs_recovery，记录失败证据 |
| 决定重试 | 最新状态允许再次抓取，预算充足 | 保留 plan v1，开启 attempt-2，要求调整策略 |
| 第二次抓取结束 | holding、lifted 均 pass | s1_grasp 成功，保存证据 |
| 搬运中掉落 | CoF 有抬升后脱离夹爪证据，新状态 holding=false | 保留历史抓取成功，阻止放置，修订搬运相关子计划 |
| 恢复后放置 | on、gripper_open 均 pass | 完成相关节点，再请求最终目标验证 |
| 最终验证 | 最新状态全部最终目标成立，无在途动作 | 返回 finish 建议，Controller 记录 succeeded |

另设遮挡分支：若反馈只能说明“看不清夹持状态”，保持待验证并请求观测，不直接重试抓取，也不标记成功。

## 10. 开发任务、测试与验收

### 10.1 开发顺序

| 阶段 | 开发内容 | 交付与完成条件 |
| --- | --- | --- |
| D1 数据与规则 | contracts、PredicateCatalog 桩、校验器 | 能接受样例计划；拒绝环、未知技能、缺失验收条件和目标遗漏 |
| D2 进度与选择 | progress、selector、预算、固定反馈回放 | 正常、失败、未知三条路径正确；不重复派发或假完成 |
| D3 计划模型 | create_plan、修复提示、版本化 prompts | 真实模型输出经校验进入流程；非法输出受限处理 |
| D4 局部修订与恢复 | PlanPatch、事件存储、重启对账 | 掉落场景能恢复；保留历史且不改最终目标 |
| D5 Cap-X CLI 联调 | 新配置、stateful runner、现有 env/API 适配 | 单任务端到端有代码、观测、验证、计划和结果记录；baseline 仍可单独运行 |

每阶段先使用替身模块验证接口。D1–D4 的逻辑测试不依赖 GPU；D5 的完整感知仿真链需相应运行环境。脚本化或 mock 通过不等于模型规划能力或机器人实验通过。

### 10.2 必须覆盖的行为测试

| 编号 | 场景 | 预期行为 |
| --- | --- | --- |
| T01 | 重复 ID、依赖环、未知对象/技能/谓词、同 ID 条件定义冲突 | 拒绝提案，给出字段级错误 |
| T02 | 计划没有覆盖最终目标或修订试图放宽目标 | 拒绝提交 |
| T03 | API 无异常但抓取验证 fail | 不标记 succeeded，进入恢复 |
| T04 | 关键事实 unknown，或否定条件对应事实 unknown | 请求观察，不能当作满足或不满足 |
| T05 | 多段执行尚未完成子目标但有正常进展；首段消耗了起始前置条件 | 按 SegmentContract 检查后续入口，同一 attempt 继续，段预算累计 |
| T06 | 重复报告、错误 episode、旧 execution 的迟到报告 | 幂等或拒绝推进，不错记尝试次数 |
| T07 | 抓取完成后掉落 | 历史记录保留；当前前置条件阻止搬运/放置 |
| T08 | 正常放置后不再夹持 | 不因中间效果消失而恢复抓取 |
| T09 | 初始目标已满足 | 经验证直接结束或跳过，无多余机器人动作 |
| T10 | 部分执行后报错、超时后底层状态未知 | 先确认执行结果和新状态，不盲目重放 |
| T11 | 新恢复节点重复重命名 | 恢复组预算和全局预算不能重置 |
| T12 | PlanPatch 基础版本过旧、引用 superseded 节点 | 拒绝提交，要求重新生成合法补丁 |
| T13 | 已派发动作尚无结果时进程重启 | 恢复为需对账，不重复执行 |
| T14 | 所有历史步骤完成但最终目标已被扰动破坏 | 不结束成功，请求恢复或明确阻塞 |
| T15 | 持续未知、循环重规划或无进展 | 在配置预算内停止并返回准确原因 |
| T16 | 只收到模型自述成功或预测未来帧 | 不能用作完成验证证据 |

### 10.3 Demo 验收输出

一次回放应能展示：初始计划、每轮所选子目标及理由、execution/attempt ID、运行结果、条件验证及证据引用、进度变化、每次计划修订和最终停止原因。成功/失败/未知样例均须可复现。

初始演示只需一个方块任务和可注入的失败轨迹；扩展到多对象或长任务后再考察非固定顺序的任务分解。状态模块和 CoF 未完成时，使用具名 fixture 并明确标注 mock。

### 10.4 实验设计

研究假设：在状态输入、CoF/验证模块、模型、API 和执行预算固定时，显式计划与验证进度能减少漏步骤、无效重复和失败后的错误继续。

建议三个主要对照：原 Cap-X 多轮策略（接入相同可见反馈）、显式计划但无局部修订、完整计划与局部修订。保留原始未经修改的 Cap-X 作为额外参考；若为公平对照做了状态适配，需单独命名并说明变化。所有组使用相同任务/种子、总动作预算和可用观测，额外模型调用成本如实记录。

主要指标：任务成功率、扰动后的恢复率、错误宣告完成率、无效重复动作数、动作段数、重规划次数、LLM 调用/token 和延迟。子目标完成率受拆分粒度影响，不宜跨不同计划直接当作主要成功指标。

oracle 状态只用于明确标注的特权实验或独立评测；非特权组不向 Planner 泄漏评测真值。CoF 的感知收益单独做实验，本部分主实验先固定它。

## 11. 参考工作与采用边界

以下依据前一轮已阅读的论文方法部分或作者项目页，不把我们的设计建议归为论文原有实现。

| 工作 | 已核对来源 | 本设计借鉴 | 不直接照搬的部分 |
| --- | --- | --- | --- |
| LongHorizon-Harness, 2026 | [论文第 2 节](https://arxiv.org/html/2608.01964v1#S2) | 任务状态独立持久化、子任务契约、依据独立环境验证更新进度 | 实验针对计算机环境；不据此声称机器人有效。机器人还要处理物体效果失效、部分执行和停止对账 |
| ProgPrompt, ICRA 2023 | [作者项目页：方法与 FAQ](https://progprompt.github.io/) | 依据可用动作/对象生成计划、前置条件与恢复步骤 | 原方法计划生成是开环；本文采用结构化计划和运行时进度，不把 assert 等同于真实环境验证 |
| Inner Monologue, 2022 | [作者项目页：Approach / Results](https://innermonologue.github.io/) | 使用成功检测和场景反馈来决定剩余任务、恢复失败 | 本文进一步规定持久记录与验证门槛；这些具体 schema 是本项目建议 |
| SayCan, 2022 | [作者项目页：Approach](https://say-can.github.io/) | 下一步同时考虑任务相关性与当前可执行性 | 不实现其学习式价值函数；第一版采用可检查条件的硬筛选 |

CoF 的具体实现尚未选定。本模块只约定它提供可追踪的时序证据，并通过独立的验证接口消费，不预设某种帧链方法已经具备可靠的因果诊断能力。

## 12. 开发前的协作确认项

这些事项可以先用默认 fixture 推进，不要求等待完整 P 或 CoF 实现：

1. P 侧确认稳定对象 ID、事实时效、证据引用和 unknown 的表示方法。
2. 验证侧共同确定第一批谓词及其语义；优先覆盖 pose_known、holding、lifted、on、gripper_open。
3. 执行侧确认动作段与 attempt 的对应方式、执行 ID 去重、超时后状态查询能力。
4. CoF 侧确认事件和证据引用如何关联 execution_id，缺帧或遮挡时如何表示 unknown。
5. 选定一个 Cap-X 任务、API 配置和最终目标判据，固定 baseline，再逐步扩展任务长度。

本模块的首个可交付版本应能以固定状态和反馈回放完成“创建计划 → 选择子目标 → 验证进度 → 局部恢复 → 正确终止”，并输出完整可追踪记录；随后替换为真实模型与仿真输入验证能力。
