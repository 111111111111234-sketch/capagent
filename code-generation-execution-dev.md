# 机器人 Coding Agent：代码生成与执行开发文档

版本：v0.1 · 日期：2026-09-24 · 状态：待实现的开发设计

2026-09-28 实现进展：已完成第一阶段执行骨架、第二阶段规划进度，并接入第三阶段模型提案、有限修正和工作进程接口。实际支持范围见[第三阶段运行说明](/Users/agiuser/Documents/Codex/2026-09-24/ca/cap-x/docs/stateful-models.md)。本轮使用预设回复完成接口测试，未调用真实模型；下文仍包含尚未实现的完整设计。

适用代码基线：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

总入口：[开发总览与使用指南](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-development-guide.md)。

配套文档：[任务规划与进度管理开发文档](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/task-planning-progress-dev.md)、[整体方案](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-plan.md)。本文细化执行侧，并沿用规划文档的 SubgoalContract、SegmentContract、attempt / execution ID 和报告状态；共享字段以专门的规划文档为基准，本文新增字段为待实现的扩展。本文不表示相关功能已经实现、通过机器人实验或达到真实机器人部署要求。

## 1. 开发目标与职责边界

本模块接收当前子目标、状态与上下文、可用 API，生成一个有边界的代码片段；经过检查后执行，记录实际调用、部分执行结果和观测证据，交还闭环控制器。

核心目标不是让模型一次写完长任务，而是：**每次可靠地推进一段，把控制权和可追踪的结果交回来，再依据新状态继续。**

### 1.1 谁负责什么

| 部分 | 责任方 | 与本模块的关系 |
| --- | --- | --- |
| 任务拆解、计划版本、TaskProgress、attempt 计数 | 你：规划与进度模块 | 提供 SubgoalContract；接收执行事件与验证结果，统一写入进度 |
| 世界状态、对象身份、事实时效、历史压缩/检索、上下文组装 | P：状态与记忆模块 | 提供只读 StateView 与相关上下文；接收执行日志与 CoF 证据 |
| 代码生成、代码校验、API 包装、动作段执行、运行报告 | 你：本文模块 | 消费状态而不接管 P；不能直接宣告子目标完成 |
| 真实执行帧的采集/索引 | 本模块与环境适配器 | 输出与 API 事件对齐的 FrameManifest |
| CoF：Chain of Frame 分析 | 你：CoF 模块 | 从帧链与动作记录提取变化、事件、证据和未知项；算法另行开发 |
| 环境结果验证 | 你：Verifier | 对照冻结条件返回 pass / fail / unknown，不接受代码自报成功 |
| 模块调度、停止/取消协调、episode 预算与终止 | 你：Loop Controller | 发起执行，等待结果，处理不确定动作，再请求验证或规划 |
| IK、底层运动控制、机器人驱动 | 现有 Cap-X / 后端适配器 | 复用其能力；缺失的停止、预算和到达反馈需在适配层补齐 |

接口需共同对齐，但本模块不重复构建世界状态、长期记忆或整个上下文编译器。执行日志的原始记录由本模块保存，P 可引用和压缩；任务进度仍只有规划/进度模块一个写入方。

### 1.2 第一版范围

- 单 episode、单机械臂、同一后端最多一个在途动作段；先接 CLI，后接 Web UI。
- 沿用所选 Cap-X API 配置，不新增“万能 pick/place”能力；先用固定输入和 fake backend 测逻辑，再接一个仿真任务。
- 允许生成 Python 中的数值计算、受限分支、有界循环及经校验的辅助函数，不生成底层伺服控制器。
- 规划与代码生成可使用同一个 LLM；校验、预算、日志和状态转移由程序执行。
- 不在第一版承诺恶意 Python 的完整隔离、跨机器分布式执行或真实机器人安全认证。
- 不运行真实机器人。只有后端具备明确的运动限制、停止和状态确认能力后，才另行设计真实机器人接入。

固定已验证辅助程序和流程模板可随本模块联调，适配方式见[第六部分](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/experience-skills-self-improvement-dev.md)。这些资产复用现有底层能力，仍需参数绑定、前提校验和分段执行；不因命名为 pick/place 就获得未实现的物理能力，也不需要先开放自动学习或发布。

## 2. Cap-X 已有能力与实际缺口

以下路径相对仓库根目录，行号对应本文基线提交。

| 位置 | 已有行为 | 本模块需要补充 |
| --- | --- | --- |
| `capx/integrations/base_api.py:96` | 从函数签名和 docstring 生成 API 文档 | 机器可校验的参数、权限、结果语义、能力声明 |
| `capx/integrations/franka/control_reduced.py:91` | 按配置暴露感知、抓取规划、IK、运动及夹爪函数 | 从实际注册表构建 SkillCatalog，不能把未启用函数写进 prompt |
| `capx/envs/tasks/base.py:153` | 持久 namespace 中 exec；注入 obs、env、APIS，收集 stdout/stderr/RESULT | 新执行入口、受控 API 通道、分段预算和隔离；原 baseline 保留 |
| `capx/envs/tasks/base.py:187` | 初始化跨轮次保留的变量空间 | 新分支采用段级变量，显式传递可复用产物，防止旧坐标和旧 RESULT 泄漏 |
| `capx/envs/tasks/base.py:263` | step 返回 observation、reward、终止标志和 info | 适配 ExecutionReport；当前 info 未转发 exec_result 中的 result |
| `capx/envs/trial.py:469` | 直接请求初始代码 | 增加基于 SubgoalContract 的结构化代码提案 |
| `capx/envs/trial.py:780` | 执行代码块并记录块前后的视频帧范围 | 使用明确动作段；新增每次 API 的事件/帧关联 |
| `capx/utils/launch_utils.py:165` | 提取一个完整代码字符串，旧分块逻辑处于注释中 | 严格协议解析；动作段由新分支定义，不依赖旧分隔标记 |
| `capx/utils/launch_utils.py:310` | 不含 REGENERATE 的输出会归为 finish | 新分支不把空回复、非法 JSON 或无代码当作成功终止 |
| `capx/envs/runner.py:274` | CLI 有 trial 级 SIGALRM 超时和部分产物保存 | 保留总超时，另加动作段/API 预算及超时对账 |
| `capx/web/async_trial_runner.py:502` | Web 有外层等待超时 | 等待超时不证明后台调用停止；不能直接将其视为可重试 |
| `capx/integrations/base_api.py:53` | Web UI 开启时输出执行日志 | 建立与 UI 无关的持久事件流；不能假设 CLI 已自动记录全部 API |

两个直接相关的细节：

1. `sandbox_rc=0` 只表示 Python 执行未抛异常。主路径为进程内 exec，且暴露原始环境对象，字段名不代表强沙箱。
2. `capx/integrations/franka/control_reduced.py:492` 的 move_to_joints 返回 None；`capx/envs/simulators/robosuite_base.py:174` 的对应底层实现达到容差或达到 max_steps 都会退出，未返回区分结果。应读取或补充真实到达反馈，不能将函数正常返回直接包装为“到达成功”。其他后端需逐个审查，不能由这个实现推断全仓库语义。

## 3. 执行流程与不变量

```text
SubgoalContract + StateView / ContextPacket + SkillCatalog
                         ↓
                  生成 CodeProposal
                         ↓
        schema / 代码 / 引用 / 条件 / 能力校验
                         ↓
        冻结 SegmentContract + 建立 ExecutionRequest
                         ↓
          持久登记派发意图，锁定后端，提交执行
                         ↓
        Python Worker → API Gateway → 环境/控制后端
                         ↓
      ExecutionReport + API 事件 + FrameManifest
                         ↓
     CoF 提取证据 → P 更新事实 → Verifier 检查条件
                         ↓
          进度更新；Controller 决定下一轮
```

必须保持的不变量：

1. 一次执行绑定固定 episode、任务/计划版本、子目标、attempt、segment、execution 及输入状态版本。
2. 生成代码不能改目标、验收标准、权限、预算、日志和 TaskProgress。
3. 所有机器人写操作经过 API Gateway；原始 env、APIS、驱动连接和控制服务凭据不暴露给 worker。
4. 执行前校验不能替代运行时校验；每次 API 调用都重新检查权限、预算及适用的当前条件。
5. 程序结束、控制指令完成、环境目标成立分别记录。没有证据时返回 unknown，不填造成功。
6. 部分执行不会回滚物理世界。代码报错后不自动重放整段。
7. 未知执行或未确认停止时，不派发新的冲突动作；重复请求不能造成重复动作。
8. 预测帧、模型自述、RESULT 或日志中的“成功”文字不能作为已完成证据。
9. 预算在段、attempt、恢复组与 episode 层累积；换代码、换 ID、重启不清零。
10. 任何错误、证据缺失或协议解析失败都不能隐式转为 finish。

## 4. 数据契约与模块边界

所有新类型建议使用严格模型校验并导出 JSON Schema。拒绝未知字段、非法枚举、非有限数值和超出大小限制的输入；大数组、图片、视频存外部文件，只传受控引用。示例不是完整实现。

### 4.1 沿用与新增类型

| 类型 | 关键字段/约束 | 产生方 |
| --- | --- | --- |
| SubgoalContract | 沿用规划文档：contract_id、episode_id、task_version、plan_version、subgoal_id、based_on_state_version、goal、preconditions、acceptance_conditions、allowed_skills、context_refs、budget | 规划程序 |
| StateView / ContextPacket | 只读；含稳定对象 ID、事实来源/观测时间、所需几何数据与证据引用；ContextPacket 具体结构与 P 对齐 | P |
| SkillCatalog | catalog_version、启用 API/已注册辅助 callable 的 SkillSpec；模型可见说明与运行时注册表来自同一来源 | 执行侧 |
| CodeProposal | schema_version、based_on_state_version、intent、code、entry_conditions、expected_conditions、continue_conditions、observation_policy、artifact_refs | LLM 提案 |
| SegmentContract | 沿用 segment_id、subgoal_contract_id、attempt_id、entry_conditions、expected_conditions、continue_conditions、budget；扩展 intent、observation_policy、code_hash、catalog_version | 程序审查后冻结 |
| ExecutionRequest | execution_id、完整身份关联、冻结契约、代码及 hash、只读输入 manifest、有效权限和预算 | Controller / 执行程序 |
| ValidationReport | proposal_id、accepted、字段级 errors、可选 context_requests；不表示机器人执行失败 | 校验器 |
| ApiCallEvent | call_id、execution_id、call_seq、API/版本、开始/结束、参数/返回摘要、控制状态、错误与证据引用 | Gateway / 后端 |
| FrameManifest | execution_id、frames、sampling_policy_version、gaps；每帧有相机/时钟/采样点/正在执行的 call_id | 环境适配器 |
| ExecutionReport | 沿用规划文档基本字段与 status；扩展 report_id、report_revision、segment_id、state_version_before、code_hash、backend 状态、调用/预算/证据摘要 | 执行器 |
| VerificationReport | 沿用规划文档；逐条 condition 返回 pass / fail / unknown 与证据 | Verifier，不由生成代码构造 |

CodeProposal 不包含可自行提高的预算、成功状态或最终执行 ID。proposal_id 由服务端分配；生成器缺少必要信息时返回单独的 ContextRequest，不用空代码假装完成。代码提案可建议段级条件，但不得削弱父契约的验收条件或绕开强制检查点。

本模块的 ValidationReport 仅校验代码提案，不表示技能可以发布。第六部分另定义 CandidateValidationReport 处理技能/经验规则/Agent 候选资格；第四部分的 VerificationReport 负责在线条件判断。三种报告分别导出和消费，不因名称接近或其中某项通过而互相替代。

### 4.2 条件与版本

Condition 沿用 `{id, predicate, args, expected}`，仅引用 PredicateCatalog 中的谓词；不执行模型生成的 Python 条件字符串。expected=false 也需要明确反证，unknown 不满足条件。

- SubgoalContract.preconditions：开始一个新尝试时的条件。
- SegmentContract.entry_conditions：本段动作开始前需要成立的条件。
- expected_conditions：本段结束后需要检查的效果，不等于整个子目标验收。
- continue_conditions：本段后允许继续推进所需的条件；下一段仍需自己的入口检查。

例如第一段抓取前需要 gripper_empty；抓取后抬升则需要当前夹持信息，不能继续要求夹爪为空。具体能否把闭合与短抬升合并，由预设技能规则、传感能力与风险决定，不由模型自由忽略中间检查。

state_version 是关联标识，不代表每条事实新鲜。动作前需检查依赖事实的 observed_at、来源、有效性和对象身份。仅无关物体更新导致版本增长时，可以重新校验相关事实后继续；不能只比较版本号，也不能完全忽略版本变化。

### 4.3 CodeProposal 示例

承接规划文档的 `s1_grasp`：前段已经取得可用夹持证据，当前准备进行小幅抬升。下列 INPUTS 中的目标位姿必须由当前观测/几何适配器提供并校验；本文不提供可直接运行于机器人的坐标。

```json
{
  "schema_version": "1.0",
  "based_on_state_version": 13,
  "intent": "在已确认夹持的条件下小幅抬升红块，随后检查夹持与离台状态",
  "code": "joints = solve_ik(INPUTS['target_position_world_m'], INPUTS['target_quaternion_wxyz'])\nmove_to_joints(joints)\nRESULT = {'target_joints': joints.tolist()}\n",
  "entry_conditions": [
    {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true}
  ],
  "expected_conditions": [
    {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true},
    {"id": "c_lifted_red", "predicate": "lifted", "args": ["red_cube"], "expected": true}
  ],
  "continue_conditions": [
    {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true},
    {"id": "c_lifted_red", "predicate": "lifted", "args": ["red_cube"], "expected": true}
  ],
  "observation_policy": "manipulation_boundary_v1",
  "artifact_refs": ["artifact_lift_target_v13"]
}
```

这里 RESULT 只是代码产物；它既不更新世界状态，也不证明红块已经抬升。执行器绑定例如 `episode_demo / plan 1 / s1_grasp / attempt_1 / segment_2 / exec_2`，预算从上层账本分配，模型不能自行生成更宽的权限。

## 5. API 契约与 Gateway

### 5.1 SkillSpec 最小定义

| 字段 | 定义 |
| --- | --- |
| name / version / signature | 精确匹配注册的 callable 及文档；按具体单臂/双臂配置生成 |
| category | observation / planning / manipulation；planning 也可能更新后端缓存，不默认是纯函数 |
| input_schema / return_schema | 参数名称、类型、shape、单位、坐标系、四元数顺序、大小上限 |
| precondition_refs | 可检查条件、时效要求、unknown 时的处理规则 |
| operational_limits | 后端配置的关节/速度/工作范围、时间/步数上限；模型不可改 |
| completion_semantics | 返回到底表示接受、函数结束、达到容差，还是已知动作结果 |
| capabilities | 是否有到达检测、周期采样、合作取消、停止确认、调用状态查询 |
| error_codes / retry_policy | 结构化错误及是否允许无副作用重试；机器人写操作默认禁止透明重试 |
| observation_hooks | 必须采集的调用前后/周期帧；具体阈值由配置提供 |

第一版保持生成代码看到的函数名和成功返回类型不变。例如 solve_ik 仍返回数组，move_to_joints 仍返回 None；Gateway 在旁路记录结构化状态，失败时抛出受控异常。不能把返回值悄悄换成 envelope，导致旧代码把字典当关节数组使用。若未来改为显式 ApiResult，必须升级 API/catalog 版本并做独立对照。

### 5.2 第一批 API 的具体注意事项

| API | 现有输入/输出特点 | 必须增加或核对的约束 |
| --- | --- | --- |
| get_observation | RGB、深度、内参、相机变换等 | 记录采样时刻/相机/来源；校验深度、标定和对象引用时效 |
| plan_grasp | 输入深度、内参和分割；输出相机系抓取矩阵与评分 | 同帧或明确对齐；空候选处理；相机到世界转换；不能把评分当成功概率证明 |
| solve_ik | 世界系位置、WXYZ 四元数；输出 7 关节角 | 米/弧度、shape、有限数值、四元数归一性、IK 残差和关节限位；返回数组不自动证明可达 |
| move_to_joints | 7 关节角，阻塞调用，返回 None | 返回是否达标/耗尽步数；运动停止与到达分别确认；单个长调用内预算和采样 |
| open_gripper / close_gripper | 当前 reduced API 调用固定步数辅助函数 | 开合指令执行与物体被夹持分开；记录夹爪反馈，没有则未知 |

plan_grasp 的结果在相机系，而 solve_ik 内部已经调用 TCP offset 处理。适配时应审查所选后端的坐标/末端约定，写转换测试，防止重复加 offset 或 WXYZ/XYZW 混用。配置文件明确容差，不允许模型临时放宽。

### 5.3 每次调用的固定流程

1. 验证 execution 仍有效、backend lease/fencing token 匹配、API 在允许集合内。
2. 校验参数、引用、时效、适用条件和剩余预算；读写权限分别检查。
3. 持久记录 CallStarted 与参数摘要，保留 call_id，采集必要前帧。
4. 在后端所属执行线程调用；运行中检查取消、步数、时限并采样。
5. 读取实际控制结果，记录 CallFinished / CallFailed，收集必要后帧。
6. 将成功返回值交给代码；操作失败或关键控制状态未知则中断当前片段，不允许代码继续搬运/释放。

API 调用的“被拒绝”“已接受”“已返回”与后端效果分开记录。运动目标检测不支持时标记 unknown 并交回闭环，不能用日志文字猜测。环境安全/任务约束要求必备的能力缺失时，准备阶段直接拒绝该执行模式。

关键失败应在 Gateway 锁存并撤销本段后续写权限，不能只抛一个可能被用户代码 try/except 吞掉的异常。代码捕获异常后也不能继续驱动机器人；即使 Python 最后返回 0，报告仍保留 API 失败，已确认停止时 status 为 error。

## 6. 动作段边界与代码生成策略

### 6.1 切分规则

一个子目标可以跨多个 segment；一个 segment 可以组合多个 API。优先在“继续执行需要新的外部证据”处交回控制权。

| 场景 | 建议边界 | 下一段依赖的证据 |
| --- | --- | --- |
| 目标定位/预抓取 | 感知与预抓取到位后 | 位姿仍有效、控制器到位 |
| 闭合与试抬 | 按技能规则在闭合后或短距离试抬后 | 当前夹持、是否离开支撑面 |
| 长距离搬运 | 搬运终点；途中有低层监测和采样 | 仍夹持、无故障、放置条件满足 |
| 放置释放 | 释放并退开后 | 支撑关系与稳定性；夹爪确已松开 |
| API 失败、状态冲突、预算将耗尽 | 立即禁止后续写操作，受控停止 | 当前后端状态和部分执行效果 |

是否需要调用大模型与是否采样不是一回事：低层可持续监测，只有语义边界/异常才进入下一轮决策。必须支持必要的长调用内采样；只记录整段前后帧无法可靠识别中途掉落。

代码生成器不得把抓取、未经验证的长距离搬运和释放串成一个无检查片段。第一版使用人工定义的 segment 类型/允许阶段作为校验规则；任意 Python 的语义不能仅靠 AST 自动证明。无法验证边界的提案拒绝或要求拆短。

### 6.2 生成上下文与修复

生成输入只包含当前子目标、段相关状态/事实、所需 API、有效产物、最近相关错误、剩余预算和输出 schema。P 负责整体组织；执行侧提供明确的 context 请求与必需字段检查，不自行累加全部历史 prompt。

提示词需明确：

- 使用实际注册 API，不虚构 pick、is_grasped 等函数。
- 未知/过期几何信息先请求上下文或观察，不编造坐标。
- 只推进当前段，声明下一观察边界；不修改任务和成功标准。
- 保留数值计算、有限分支和 API 组合能力，但禁止无限重试。
- 不隐藏异常，不通过打印成功或写 RESULT 结束任务。

结构或静态校验失败时，返回字段/代码位置和原因，最多修复一次作为开发默认值。尚未派发不增加 attempt_count，但消耗代码生成预算。真实执行后修复代码时，先对账与重新观察，再由规划侧决定新 attempt 或合法后续段；执行器不能自动续跑旧代码。

## 7. 执行范围、隔离与预算

### 7.1 三层校验

| 层次 | 检查内容 | 不保证什么 |
| --- | --- | --- |
| 提案/schema | 字段、API/对象/条件引用、版本、权限、代码大小、非空边界 | 不保证代码能运行或任务可完成 |
| 静态代码 | Python 语法；限制 import、动态执行、反射、文件/网络/进程操作；限制循环和辅助函数 | AST 白名单不是完整 Python 安全沙箱，也不能证明物理安全 |
| 运行时/后端 | 每次调用参数/权限/时效/预算、关节与运动限制、停止检测、执行事件 | 控制正常不代表子目标达成，仍需观测验证 |

第一版代码子集建议：有限局部变量、计算与数组索引、if、受限 for、已审查的数值工具与辅助函数；禁止递归、while True、eval/exec/compile、动态 import、任意属性反射、文件/网络/子进程访问和线程创建。限制集合/数组大小，不能只限制循环次数而允许无限内存分配。

可注入受控的数值能力，但不要把完整模块和有环境引用的 Python callable 当作隔离边界。即使去掉 env/APIS，单靠受限 globals 或 AST 仍不足以防逃逸。

### 7.2 推荐运行结构

- **Python Worker**：只拥有本段代码、输入拷贝、受限 API stub 和数值能力；每段新建或可靠重置，不继承任意旧变量。
- **API Gateway**：持有可信注册表、预算账本和调用记录；校验 worker 的 RPC 请求，不能信任 worker 自报已调用次数。
- **Backend Owner**：独占环境和驱动对象，在要求的线程执行运动与渲染，支持查询/取消/停止；后端生命周期不随代码 worker 任意销毁。
- **Supervisor**：监控 worker CPU/内存/时间；退出时撤销后续 API 权限并协调停止，保存已发生事件。

MuJoCo 渲染存在 GL 上下文线程归属要求；Cap-X Web 路径已把 step 与 render 放在同一环境线程。新实现保持这个约束，不为采样另开一个随意读取模拟器的线程。

fake backend 阶段可在测试进程里演示逻辑，必须标注“无隔离测试模式”；真实 LLM + 仿真联调前实现 worker/gateway 分离与预算。独立进程仍不等于完整 OS 沙箱：文件、网络、资源隔离必须由宿主运行策略真正限制，不能只靠 prompt。未配置完整隔离时，不声称支持不可信代码安全运行。

### 7.3 多层预算

有效上限取 episode 剩余、attempt 剩余、segment 配额与具体 API 上限的交集。单位明确区分墙钟秒、仿真秒、控制步数和 API 次数。

- API Gateway 统计所有调用；被拒绝调用也计入请求上限，避免拒绝循环。
- Backend Owner 在每个控制步检查动作/段预算，不能等阻塞 API 返回后才检查。
- Supervisor 限制不调用 API 的纯计算死循环、巨大数组、输出体积与 worker 资源。
- 模型生成、补观察、验证、停止确认各有独立上限，全部纳入 episode 总成本。
- 正常动作预算耗尽后禁止新动作；预留受控停止与状态确认通道，不能因为动作额度耗尽而无法停止。
- stdout、stderr、RESULT、图像、API 参数和返回值都有大小限制；截断需显式标记，不把截断记录伪装成完整证据。

停止确认有单独时限。达到该时限仍不能确认停止时，锁住后端进入需人工/外部对账状态；真实设备依赖独立控制器看门狗/急停能力，不能用杀 Python 进程替代。

## 8. 结果语义、错误与恢复

### 8.1 ExecutionReport 状态

沿用规划文档的 status，不新增与其冲突的顶层成功枚举：

| status | 使用条件 | 后续限制 |
| --- | --- | --- |
| completed | Python 正常结束且该执行已对账，无在途命令 | 等待环境验证；不表示动作效果或子目标成功 |
| error | 异常/API 拒绝/操作失败，且已确认无在途命令 | 保存部分效果；观察后决定修复/恢复 |
| timed_out | 达到执行时限，后端与 worker 已确认停止/隔离 | 重新观察，禁止自动重放 |
| cancelled | 收到取消并已确认无在途命令 | 不自动恢复；保留历史与当前环境状态 |
| outcome_unknown | 结果缺失、状态无法查询或停止未确认 | 阻止新动作，等待对账；不等同于失败后可重试 |

runtime_rc 表示 Python 运行结果：正常返回 0，已知异常用非零值；没有可靠退出结果为 null。记录 OS exit code 时使用独立 worker_exit_code，不能把两者混用。

扩展 backend_motion_state 为 idle / running / unknown，并记录 stop_reason、stop_evidence_refs、partial_effects_possible、last_call_id、call/event/frame 引用、预算消耗、输出截断和反馈缺失。idle 仅表示后端确认无在途命令；具体停稳判据由 backend profile 定义，不能只因请求队列为空就确认机器人已停止。它也不意味着对象位置或夹持状态正确，更不构成安全认证。partial_effects_possible 表示环境可能已改变、不得假设可以回滚，不专指“本段只执行了一半”。

### 8.2 报告样例：代码正常，但没有完成抬升

以下为模拟数据，不是实际实验结果。执行关联前面的 segment_2；后端报告机械臂已到目标且停止，但真实帧显示红块仍在桌上。

```json
{
  "schema_version": "1.0",
  "report_id": "report_exec_2_r1",
  "report_revision": 1,
  "episode_id": "episode_demo",
  "execution_id": "exec_2",
  "task_version": 1,
  "plan_version": 1,
  "subgoal_id": "s1_grasp",
  "attempt_id": "attempt_1",
  "segment_id": "segment_2",
  "subgoal_contract_id": "contract_s1_v1",
  "catalog_version": "reduced_single_arm_v1",
  "code_hash": "sha256:085b395730c9e6faf2b296c6ac042e6f47da65a8efe67040d365820a24c4320e",
  "state_version_before": 13,
  "status": "completed",
  "runtime_rc": 0,
  "worker_exit_code": 0,
  "started_at": "2026-09-24T10:00:00Z",
  "ended_at": "2026-09-24T10:00:02Z",
  "backend_motion_state": "idle",
  "stop_reason": "normal_return",
  "stop_evidence_refs": ["event_backend_exec_2_idle"],
  "partial_effects_possible": true,
  "last_call_id": "call_exec_2_2",
  "api_summary": {"requested": 2, "returned": 2, "rejected": 0},
  "budget_usage": {"api_calls": 2, "control_steps": 80, "wall_time_s": 2.0},
  "evidence_refs": ["event_call_exec_2_2_finished", "frame_exec_2_post"],
  "result_ref": "artifact_exec_2_result",
  "stdout_ref": "artifact_exec_2_stdout",
  "stderr_ref": "artifact_exec_2_stderr",
  "api_events_ref": "events_exec_2",
  "frame_manifest_ref": "frames_exec_2",
  "output_truncated": false,
  "feedback_missing": [],
  "error": null
}
```

示例代码 hash 对应第 4.3 节 code 字段的 UTF-8 字节（包含末尾换行）。示例 artifact/event 引用是 fixture 标识，不是已经生成的文件；实现时需检查引用存在且来源可信。FrameManifest 的真实来源/时间戳及后端完成证据由相应引用指向，不是由 LLM 填写。

Verifier 随后产生独立报告，检查 `c_holding_red` 和 `c_lifted_red`。若观测足够，两项返回 fail；若看不清，返回 unknown。不能从 runtime_rc=0 推导任何一个条件 pass。

运行异常但目标已达成也是合法情况：保留 error 报告与通过的环境验证，再检查故障是否解除、控制器是否空闲以及约束是否满足；不能为了“目标成功”删除运行错误。

### 8.3 错误分类与下一步

| reason_code（拟议） | 典型情况 | 推荐处理；最终决策归 Controller/Planner |
| --- | --- | --- |
| CODE_INVALID / API_NOT_ALLOWED | 语法、非法 API、超出子集 | 派发前有限修复；不增加动作尝试次数 |
| ARGUMENT_INVALID / FRAME_MISMATCH | shape、单位、坐标或四元数错误 | 纠正输入/代码，不能通过运动试错猜单位 |
| STATE_STALE / PRECONDITION_UNKNOWN | 位姿过期、夹持不确定 | 请求特定观测/状态刷新，不先动作 |
| IK_INFEASIBLE / NO_GRASP_CANDIDATE | 无有效解或抓取候选 | 换候选、补观察或局部重规划 |
| MOTION_NOT_REACHED | 达到步数上限但未达容差 | 停止并记录实际姿态；不继续抓取/放置 |
| API_ERROR / WORKER_ERROR | 服务故障、运行异常 | 先判断是否发生动作和是否停止，再处理恢复 |
| BUDGET_EXCEEDED / CANCEL_REQUESTED | 段预算耗尽、用户取消 | 走停止与对账流程，不自发重试 |
| EXECUTION_UNKNOWN | 响应丢失、后端状态未知 | 查询原 execution/call，禁止新 ID 重放 |
| OBSERVATION_UNAVAILABLE | 动作结束但帧采集/CoF 失败 | 执行报告可仍 completed，反馈标缺失，验证 unknown |

环境层的 GRASP_MISSED、DROPPED 等诊断来自验证/CoF，不伪装成 Python 异常。证据只能支持“物体仍在桌上”时，不强推“夹力不足”等物理原因；原因假设需单独标注不确定性。

## 9. 派发、取消、幂等与恢复

### 9.1 派发顺序

1. Controller 确认无冲突 execution，取得单写者后端 lease，并检查任务/计划版本。
2. 校验提案与事实时效，冻结 SegmentContract；分配 execution_id，计算代码/请求 hash。
3. 在进度/派发事件账本中原子登记派发意图和预算预留；首次派发 attempt 才增加 attempt_count。
4. 提交 ExecutionRequest；执行服务先持久记录请求 ID/hash，再开始调用后端。
5. 报告/API 事件回写日志，进度模块接收执行结束或未知状态；只有 Verifier 的结果能推进成功状态。

ID 规则继承规划文档：同一 attempt 可有多个正常推进的 execution；修复策略/重试由进度模块分配新 attempt。相同 execution_id + 相同 hash 返回既有状态，不再执行；相同 ID + 不同 hash 拒绝。状态未知时，客户端不能通过换 ID 绕过后端锁。

本机制不能承诺物理世界 exactly-once。若崩溃发生在命令发送后、完成日志写入前，可能已经执行；必须查询后端与重新观察。后端不支持命令去重或查询时，进入不确定状态，而不是假装从未执行。

### 9.2 取消与超时

取消流程：撤销后续写调用权限 → 请求当前动作停止 → 获取后端停止/空闲确认 → 获取必要观测 → 形成 cancelled 或 timed_out 报告。若任一关键确认缺失，形成 outcome_unknown，保留原始超时/取消原因。

Supervisor 可终止失控代码 worker，但这不是运动停止确认。停止路径必须独立于被阻塞的用户代码；若后端只能在合作检查点停，能力声明要说明限制。没有可用停止通道时，该模式不通过相应后端验收。

取消后不自动重启任务；外部明确恢复后，重新校验当前环境与预算。仿真 reset 属于独立 episode 重置操作，不能在同一任务记录里偷偷当作“恢复到了动作前”。

### 9.3 迟到报告与重启

ExecutionReport 扩展 report_id 与 report_revision，是为支持 unknown 后的对账更新。处理规则：

- 同 report_id 重复到达为 no-op；相同 ID 不同内容为完整性错误。
- 同 execution 的更高 revision 可把 unknown 解析成确定结果；旧 revision 不覆盖新结论。
- 确定报告之间矛盾时进入人工/后端核对，不能“最后到达者获胜”。
- 对账不再次登记派发、不重复扣预算或增加 attempt_count；预算用累计值的差额更新。
- 迟到结果仍属于原 episode/计划/时间点，不能直接推动已变更的当前子目标；必要时重新验证。

单写者事件流保存递增 event_seq，报告快照保存 applied_seq；派发和 CallStarted 持久化失败时不得开始新动作。关键日志在动作中丢失时停止后续写操作并进入恢复检查。重启重放只恢复记录，不恢复真实物体或任意 Python 栈。

第一版派发、预算和进度登记应使用同一事务账本或同一事件流的派生视图，不是分别向两个无事务关联的存储写入。ExecutionRequest/后端 outbox 的提交状态也需可回放，避免进度已经登记但执行请求是否送达无法辨认。

## 10. CoF 证据、变量与产物管理

### 10.1 帧与调用对齐

每帧至少记录 frame_id、execution_id、camera_id、capture_time、clock_domain、sim_step（若适用）、active_call_id（无则 null）、source、文件引用与 checksum。

- 用单调时钟算时长，UTC 用于人类查看；仿真时间单独保存，不直接与真实时间相减。
- 多视角同刻帧共享采样组，不能把左右相机错误排成两个先后事件。
- 异步相机记录对齐误差；丢帧、采样间隔超限、采集失败写入 gaps。
- 片段前后采样与长调用内周期采样组合；频率/最大帧数由配置限定并记录。
- 真实观测标为 observed，世界模型预测标为 predicted；后者不能进入完成证据白名单。
- CoF 输出引用原始 frame/call，不能覆盖原始轨迹或回填不存在的中间帧。

CoF 分析和 P 状态融合不在本模块执行逻辑中硬编码。接口返回可以异步，但在需要这些证据的后续动作前必须等待或明确 unknown。程序运行正常、帧不足、CoF 服务报错可以同时存在，应分字段记录。

### 10.2 段级变量与可复用产物

新分支每段使用新的执行 namespace，RESULT 初始化为 null。旧段的 Python 变量不是下一段的隐式输入；只导入经校验的 ArtifactManifest。

每个 artifact 记录 artifact_id、类型、schema_version、producer_execution_id、来源 state_version/observation、对象依赖、坐标系/单位、生成时间、失效规则和内容 hash。

- 位姿、分割、抓取候选随物体/相机变化而失效，不能只看文件仍存在。
- 纯辅助函数可以复用，但需按代码 hash 与 API 版本重新校验；禁止捕获旧 env/观测的闭包。
- INPUTS 是输入拷贝，代码修改它不改变 P 的持久状态。
- RESULT 只接受有大小上限的 JSON/数组产物，不接受可执行 pickle 或活对象。
- worker 不提供任意文件路径访问；文件引用须属于本 episode 的允许 manifest，防止路径穿越。

已发布技能可由固定目录中的外部不可变资产提供，不要求复制为本 episode 新生成文件；执行准备阶段将获准的精确代码、依赖版本/hash 纳入本 episode 输入 manifest，再由受控装载器读取。procedure 先由规划侧展开，不能将跨段流程整体 exec 以绕过验证。本文“纯辅助函数可复用”不构成动作技能的自动发布资格。

模型可见日志按子目标请求相关摘要，完整轨迹留在事件存储。任务/对象名称、stdout 或外部服务文本均作为数据，不成为修改权限或执行策略的指令。

## 11. 接口、目录与接入方式

### 11.1 公共接口草案

以下仅为类型职责示意，不是可运行实现。状态、日志存储、后端和模型客户端通过依赖注入；核心规则测试不导入仿真/GPU 库。

```python
def generate_code(contract, state, context, catalog, recent_feedback) -> "CodeGenerationResult":
    """返回代码提案或上下文请求；不派发动作、不修改进度。"""
    ...

def prepare_execution(proposal, contract, state, catalogs, capabilities, budget) -> "PreparedSegment":
    """校验并冻结；失败抛结构化校验错误，无机器人副作用。"""
    ...

def dispatch(request, dispatch_store, progress_store) -> "ExecutionHandle":
    """先登记派发与预算，再幂等提交；由单写者协调账本。"""
    ...

def query_execution(execution_id, backend) -> "ExecutionSnapshot":
    """查询已有执行；绝不隐式重发运动命令。"""
    ...

def cancel_execution(execution_id, reason, backend) -> "CancellationHandle":
    """只表示发起取消；终态还需停止确认与执行报告。"""
    ...

def build_execution_report(handle, events, worker_exit, backend_state, frames) -> "ExecutionReport":
    """归并运行事实与证据，不判断子目标或最终目标成功。"""
    ...
```

### 11.2 建议目录

```text
capx/agents/stateful/execution/
  contracts.py          # 执行特有模型；引用共享规划契约，不复制枚举
  generator.py          # 当前子目标的代码提案与有限修复
  prompts.py            # 提示模板、版本、代码子集说明
  validation.py         # schema / AST / 引用 / 阶段规则
  registry.py           # SkillSpec、API 文档、能力声明
  gateway.py            # 受控调用、预算、日志、失败中断
  worker.py             # 段级 Python 生命周期与资源限制
  supervisor.py         # worker 监控、停止协调、结果对账
  artifacts.py          # 输入与 RESULT 产物、有效性与序列化
  events.py             # 调用/报告事件和去重规则
  frame_capture.py      # 帧索引、采样策略、缺失记录
  adapters/
    capx_backend.py     # 现有 env/API、线程与观测适配
    fake_backend.py     # 确定性测试，可注入故障

tests/execution/
  fixtures/
  test_contracts.py
  test_gateway.py
  test_budget.py
  test_reports.py
  test_recovery.py
  test_artifacts.py
  test_integration_cli.py
```

共享类型最初可由 planning/contracts.py 导出；以后统一迁往 stateful/contracts.py 时，同步修改两侧 imports，不维护两份不同的 ExecutionReport / SegmentContract。

### 11.3 Cap-X 接入步骤

1. 在新 `agent_mode: stateful` 分支接管生成/执行；保留原 baseline 的代码提取、namespace 和反馈行为。
2. 从实际 API registry 建 SkillCatalog；模型可见文档和 Gateway 权限保持一致。
3. 不把新代码重新送回会注入原始 env/APIS 的旧 `_exec_user_code`。复用其底层能力，但新建 worker/gateway 执行入口。
4. 复用 trial 的环境初始化、观测及产物保存，新增执行事件与报告；区分 agent 可见反馈和 reward/task_completed 等评测真值。
5. 为选定后端补充到达/预算/采样/停止查询适配，先只支持一个经过测试的后端，其他明确 unsupported。
6. `_load_config()` 显式组装配置，必须增加 stateful 配置透传与启动校验，不能只往 YAML 写键。
7. CLI 验收后 Web 调用同一服务层；保留环境线程归属，不另写一套超时与恢复规则。

真值隔离：非特权实验中 reward、task_completed 和模拟器隐藏状态只交给独立评测器，不偷偷注入模型或在线验证器。若使用 oracle 验证，单独命名并对所有对照组一致提供。

## 12. 配置与开发阶段

### 12.1 配置草案

```yaml
agent_mode: stateful
execution:
  backend_profile: fake_single_arm_v1
  isolation_profile: worker_process
  max_code_repairs: 1
  max_code_bytes: 16000
  max_segment_api_calls: 8
  max_segment_control_steps: 240
  max_segment_wall_time_s: 20
  max_api_wall_time_s: 10
  stop_confirmation_timeout_s: 5
  max_stdout_bytes: 16384
  max_result_bytes: 65536
  allow_raw_env_access: false
  automatic_motion_retry: false
  frame_capture:
    policy: manipulation_boundary_v1
    periodic_hz: 5
    max_frames_per_segment: 128
```

这些值用于 fake/仿真开发，不是安全阈值或经过实验优化的参数。部署配置还必须从后端 profile 加载 CPU/内存、文件/网络隔离、关节/速度/工作空间和每 API 上限；缺失必需约束时启动失败。Linux GPU 仿真与当前 Mac 上的纯逻辑测试分别验收，不宣称完整栈可直接在 Mac 运行。

段预算还受规划文档的 max_segments_per_attempt、恢复组尝试数、episode 总执行数约束；两侧共享权威账本，不各自维护可漂移的计数。frame 上限应考虑多相机数量与调用边界采样；达到上限时标记 gaps，不能无记录丢弃关键证据。

### 12.2 开发顺序与交付门槛

| 阶段 | 开发内容 | 完成条件 |
| --- | --- | --- |
| E1 契约与生成 | 共享模型、SkillCatalog、CodeProposal、结构/AST 校验、固定生成样例 | 合法样例可接受；未知 API、缺失条件、非法代码被拒绝；不执行机器人 |
| E2 Gateway 与报告 | fake backend、API 事件、参数检查、三层结果、产物有效性 | 运行正常但抓取失败、部分执行后异常、unknown 均正确表达 |
| E3 预算与生命周期 | worker、资源限制、取消、停止确认、ID 去重、重启对账 | 无盲目重试、无重复动作、预算真实生效；不支持的后端能力明确拒绝 |
| E4 单任务 CLI 联调 | 一个仿真后端的到达/步数/采样适配，接真实代码模型 | 单个子目标能跨段执行；完整日志；baseline 仍可运行 |
| E5 闭环联调与实验 | 接规划、CoF、P 和验证；对照分段策略与反馈粒度 | 成功/失败/未知轨迹可复现；统计成本与误判，不只展示成功视频 |

E1–E3 可使用固定状态/帧 manifest 和故障注入，不等待 P/CoF 完成。真实感知、仿真或机器人能力只有在对应环境测试后才能报告；mock 通过不等于具身任务成功。

## 13. 测试与验收

### 13.1 必须覆盖的行为测试

| 编号 | 场景 | 预期行为 |
| --- | --- | --- |
| X01 | 合法提案，当前子目标与状态一致 | 冻结契约、持久登记、只执行一次，报告与 ID 全关联 |
| X02 | 空响应、非法 JSON、未知字段、未知 API | 有限修复或明确失败；不派发、不 finish |
| X03 | 非有限关节值、错误 shape、过期 artifact、坐标/四元数不匹配 | 调用前拒绝，不对机器人写入 |
| X04 | 访问 env/APIS、文件、网络、动态 import、反射等 | 静态限制与运行边界生效；测试不能被当作形式化安全证明 |
| X05 | 无 API 的计算死循环/资源耗尽；超量 stdout/RESULT | Supervisor 限制；输出截断/拒绝明确记录 |
| X06 | 单次阻塞运动耗尽步数或时间 | 底层及时停止；不能仅靠 API 调用计数；报告未到达 |
| X07 | close_gripper 正常返回，物体仍在桌上 | runtime 可 completed；验证 fail/unknown，不标子目标成功 |
| X08 | 控制器到 max_steps 但未达容差 | MOTION_NOT_REACHED，不输出“Motion complete”作为到达证据 |
| X09 | 已移动后第二次 API 异常，或代码吞掉异常继续调用 | 记录部分效果；Gateway 锁存故障拒绝后续写；先观察，不整段重放 |
| X10 | 超时但底层仍运行、停止确认丢失 | outcome_unknown，锁定后端，禁止新动作 |
| X11 | 用户取消并完成停止确认 | cancelled；不自动恢复，不偷偷 reset 环境 |
| X12 | 重复 execution_id；同 ID 内容不同 | 相同请求只查已有结果；不同 hash 拒绝 |
| X13 | 派发后无报告即崩溃、命令已执行但日志未完成 | 重启进入对账；不承诺 exactly-once、不重复执行 |
| X14 | 重复/迟到/跨 episode 报告，unknown 后高 revision 报告 | 幂等/拒绝或对账更新；不重复累计预算和 attempt |
| X15 | 第一段消耗 gripper_empty，后续段要求 holding | 同一 attempt 正常继续，不错误重查已消耗前置条件 |
| X16 | 长调用中物体掉落；仅首尾帧看不出过程 | 周期采样可提供证据；缺帧则未知，不编造中间过程 |
| X17 | 多相机异步、时钟域不同、CoF 超时 | 保留对齐误差/缺失，验证 unknown，不虚构成功 |
| X18 | 旧 RESULT/旧位姿/捕获旧环境的函数进入下一段 | namespace 清理；显式产物校验和依赖失效检查 |
| X19 | 代码自报 success 或只给预测帧 | 不推进进度；必须有允许来源的执行后验证 |
| X20 | 修复代码、换 ID 或重启后预算企图重置 | attempt/恢复组/episode 总预算保持累计 |
| X21 | CLI 与 Web 使用同一 fake 轨迹 | 报告与状态转移一致；渲染与控制保持后端线程要求 |
| X22 | reward/隐藏模拟器状态可用但非特权模式 | 不出现在模型上下文或在线验证输入 |
| X23 | 派发或 CallStarted 持久化失败 | 不启动新动作；在途动作走停止/恢复检查 |
| X24 | 代码已正常结束但观测采集失败 | 运行报告保留 completed，反馈缺失明确，不能跳过验证 |

### 13.2 首个 Demo 的验收产物

选择与规划文档相同的红块/绿块任务，至少回放三条轨迹：正常抓取放置、运行正常但未抓住、执行超时且需要对账。每条保存：

- 冻结任务/子目标/动作段契约与实际模型输入版本。
- 提案、代码、校验结果、代码 hash 和实际 API catalog。
- API 参数/返回/错误摘要、控制步数、耗时及调用事件。
- 帧与 call 对齐清单、CoF/验证引用、缺失或未知项。
- ExecutionReport、TaskProgress 变化、预算与最终停止原因。

记录回放只重建逻辑时间线，不重新驱动机器人。自动化验收分别报告静态检查、fake 测试、仿真联调；未运行的层级明确标注。

### 13.3 实验设计

研究假设：固定规划、状态、模型和底层 API 后，语义分段与结构化执行反馈能减少错误继续、无效重试和错误完成，提高扰动下的恢复能力。

建议主对照：

1. 原始 Cap-X：保留原实现作为参考，说明其已有视觉/多轮能力。
2. 匹配输入的代码执行基线：相同子目标/状态/API/总预算，但采用较长代码块和原始运行反馈。
3. 分段 + 统一调用记录：固定边界及同等反馈来源。
4. 完整执行 harness：语义检查点、结构化报告、明确停止对账与恢复门控。

用固定/语义两种分段、文本/结构化两种反馈做局部消融；保持相同证据内容才能区分“格式”与“更多信息”的收益。CoF 的感知改进另设实验，不能与执行器同时替换后把全部提升归因于 harness。

主要指标：最终任务成功率、错误继续/错误完成率、重复动作次数、扰动恢复率、无效动作步数、停止确认延迟、API/LLM 次数、token、总耗时和帧处理成本。额外观察/验证不算免费；固定总预算并记录实际消耗。不同动作段粒度下，段成功率不能直接作为主要跨组指标。

## 14. 参考工作与采用边界

以下依据已核对的作者项目页或论文相关章节。本文的具体 schema、ID/预算规则、错误码和隔离方案是本项目建议，不是论文提供的现成实现。

| 工作 | 来源 | 借鉴 | 边界 |
| --- | --- | --- | --- |
| Code as Policies, 2022 | [作者项目页](https://code-as-policies.github.io/)，[论文](https://arxiv.org/abs/2209.07753) | 通过 Python 组合感知/控制 API，使用几何计算、反馈逻辑、辅助函数生成 | 原工作本身支持反馈逻辑，不能描述成完全开环；本文新增段级契约、执行生命周期与证据报告 |
| GPT-Policy / In-Context Robot Learning with VLM Agents | [作者项目页](https://cheng-haha.github.io/GPT-Policy/)，[仓库](https://github.com/cheng-haha/GPT-Policy) | 上下文包含任务/状态/参考/历史；结构化 robot-tool request 经受约束控制器验证执行，返回执行/拒绝反馈 | 结构化工具请求不等于任意 Python；不据此声称已经提供我们的代码沙箱或报告协议 |
| Towards the Harness of Embodied Agents / Thea, 2026 | [论文 §3.3、§4.2](https://arxiv.org/html/2608.11246v1) | 工具契约、统一返回、操作后强制独立评估、结果/证据/原因；Evaluation as Exit Codes | 其评估器是后置独立组件，所读工具后置条件不展示给模型；本文模型可见子目标目标，但由外部冻结与验证，不能自行改判据。本文 pass/fail/unknown 也不是照抄其过程/成功/失败枚举 |

本文不假设 CoF 已有确定实现或可靠因果诊断能力。多帧反馈在 Cap-X 中已有基础；研究贡献应通过事件对齐、证据质量、可控执行及闭环行为的可测改进界定，而不是仅以“增加多帧”命名。

## 15. 开发前的协作确认项

以下事项先用 fixture 推进，联调前必须确定：

1. 与规划侧确认共享契约、attempt 分配、派发账本和报告 revision 的处理，不建立双份进度。
2. 与 P 对齐几何输入、事实时效、对象/坐标身份、产物失效和按子目标请求上下文。
3. 选定一个后端，实测其到达判断、长 API 预算、周期采样、取消和停止查询；未支持能力明确降级或拒绝。
4. 与 CoF 对齐 frame/call ID、时钟域、缺帧与 observed/predicted 来源；算法部分另行开发。
5. 与 Verifier 明确首批段级/子目标谓词及证据要求；不能用运行正常替代环境判断。
6. 固定模型、API、输入信息与预算，先保存基线，再开展分段和反馈消融。

首个可交付版本应做到：**接收当前子目标 → 生成有边界的代码 → 经受控 API 执行 → 返回可靠运行记录与证据 → 在成功、失败和未知三种情形下都正确交回控制权。**
