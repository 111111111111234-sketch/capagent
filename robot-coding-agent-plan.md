# Robot Coding Agent：任务规划与代码执行方案

日期：2026-09-24。代码依据：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

本文是基于当前源码的设计建议；尚未实现新 Harness，也未运行仿真评测。

最新方向：用户明确采用 Chain of Frame（CoF）替换目标方案中的 VDM 反馈模块。下文的 CoF 是本项目拟议设计；目前仅确认名称，尚未指定某篇论文或已有实现。对原 Cap-X 的 VDM 描述保留为基线事实。

## 1. 你的研究范围

研究问题：给定任务、结构化状态、相关历史和可用 API，Coding Agent 如何选择下一子目标、生成可执行代码，并利用执行后的证据持续推进长程任务？

你负责 E 侧的 Planner、Code Executor 和 Loop Controller。P 侧负责把 observation 和执行记录整理成世界状态及上下文。双方共同定义接口，但各自维护的数据应有明确的更新责任。

| 数据/能力 | 主要维护方 | 交互方式 |
| --- | --- | --- |
| 世界状态：对象、关系、机器人状态、观测时间与来源 | P / 状态与记忆模块 | E 读取；E 可请求刷新或补充观测 |
| 历史压缩、检索、上下文组织 | 状态与记忆模块 | E 提供当前子目标、所需对象/事实及上下文预算 |
| 子目标、依赖关系、执行顺序、计划版本 | E / Planner | 产生和修订 TaskPlan |
| 已尝试动作、失败原因、执行资源消耗 | E / Executor | 产生结构化 ExecutionReport，交给 P 保存和整理 |
| 子目标完成状态、重试次数、当前计划位置 | E / Loop Controller | 仅基于执行后验证更新；共享给上下文模块 |
| 世界/视觉生成、环境 Proxy、底层控制器 | 对应环境与控制模块 | 通过适配器接入 E |

世界模型给出的预测状态与执行后实际观测要有不同的 `source`。预测效果可以辅助规划，但不能单独作为“任务已经完成”的证据。在生成式环境中，应预先定义该环境接受的状态转移与评测语义，并与真实机器人结果分开报告。

Planner、Executor、Verifier 是逻辑模块，第一版不必各自配置独立 LLM。可以先使用一个模型做任务拆解和代码生成，由程序负责调度、接口校验、预算和状态转移。

## 2. 当前 Cap-X 已有的基础

| 源码位置 | 已有行为 | 对你的工作的意义 |
| --- | --- | --- |
| `capx/envs/trial.py:780` | 执行代码块，再进行多轮决策 | 复用试验流程，新增可切换的 Stateful Harness |
| `capx/envs/trial.py:540` | 拼接已执行代码以及当前 stdout/stderr | 可替换为有预算的结构化决策上下文 |
| `capx/utils/launch_utils.py:251` | 组合原始任务/API 提示、执行反馈、图像或 VDM 描述 | 保留感知入口，接入 P 的 ContextPacket |
| `capx/envs/tasks/base.py:153` | 使用持久 Python namespace 执行代码 | 已有跨轮变量，但需要显式管理状态与失效的中间结果 |
| `capx/envs/tasks/base.py:263` | `step(code)` 返回 observation、reward、terminated、truncated 和 info | 作为 ExecutionAdapter 的基础 |
| `capx/integrations/base_api.py:96` | 从 API 函数签名和 docstring 生成文档 | 增加前置条件、执行预算、后置验证及失败语义 |
| `capx/integrations/franka/control_reduced.py:91` | 暴露观测、分割、抓取规划、IK、关节运动和夹爪 API | 第一版可以沿用已有技能粒度 |
| `capx/utils/execution_logger.py:49` | 有工具调用描述、时间、图片与代码块历史结构 | 可扩展为带参数摘要、结果和状态版本的执行事件 |

因此不能把 baseline 描述成“完全没有历史或循环”。更准确的缺口是：已查看的主执行路径主要依靠代码历史、文本反馈和多轮重生成，缺少显式 TaskPlan、经验证的 TaskProgress，以及细分的执行结果与控制决策。

另有两个直接相关的细节：

- `capx/utils/launch_utils.py:310` 将不包含 `REGENERATE` 的返回都解析为 finish，包括格式错误或空内容。新协议应把无法解析的返回标为决策格式错误，有限次修复；这类输入不应触发成功结束。原实现的 finish 只意味着停止生成，并不自动证明环境成功。
- `capx/envs/tasks/base.py:291` 的 `sandbox_rc` 只区分代码执行是否抛异常。没有异常不能证明抓取成功。主路径实际使用进程内 `exec`，允许访问 `env`、`APIS` 和持久变量；字段名称不代表具备强隔离。

仓库还有跨任务 SkillLibrary，以及 CLI/Web UI 的执行路径。第一版建议先接 CLI，并保持原 baseline 可单独运行；以后让两种入口共用 Harness，避免决策规则分叉。

### 目标反馈方案：用 Chain of Frame 接替 VDM

当前图像 VDM 在 `capx/envs/trial.py:332` 读取最近两帧，返回关于变化与完成情况的文字。仓库也已在 `capx/envs/trial.py:393` 实现整段执行视频的文字反馈，因此“使用多帧”本身不足以界定新增贡献。

拟议 CoF 把一次动作段的实际观测组织为有时间顺序、与 API 执行记录对齐的帧链，推断对象关系如何随执行变化，并输出带证据引用的状态变化与事件。P 侧据此更新持久 WorldState；E 侧利用新状态、事件及运行时报告决定下一动作段。

```text
执行前状态 + 当前子目标 + 预期效果
                    ↓
短段代码执行 → 带时间戳的 API 记录 + 实际观测帧
                    ↓
CoF：关键帧链 → 状态变化 / 事件 / 不确定信息
                    ↓
P：更新 WorldState、组织 ContextPacket
                    ↓
E：验证子目标 → 继续 / 补观察 / 修复 / 重规划 / 结束
```

建议先从执行前、接触/夹爪变化、抬升、释放、执行后等节点取帧，结合有上限的周期采样。关键事件不一定恰好发生在 API 返回时；单个长 API 内也需要观测采样，才能发现中途掉落。帧不足以支持某个中间过程时，输出 unknown，不补造过程。

每个帧条目记录 `frame_id`、时间戳、相机 ID、所属 `execution_id`，并关联当时正在执行的 API 调用。不同视角表示同一时刻的多视图，不应串成先后事件；时间对齐误差也应记录。完整帧与工具轨迹外部保存，每轮只传当前动作段的有界帧链和相关历史。

建议的输出契约：

| 字段 | 内容 | E 如何使用 |
| --- | --- | --- |
| `frame_refs` / `action_refs` | 本轮实际使用的观测与执行引用 | 追踪反馈依据 |
| `state_delta` | 对象位置、夹持/支撑关系等变化，附来源与时间 | 重新检查前置条件、使旧缓存失效 |
| `events` | 按时间排序的接触、抬升、释放、掉落等事件及证据 | 定位发生偏差的阶段 |
| `condition_checks` | 子目标条件的 true / false / unknown 判断 | 为验证器提供证据支持的判断 |
| `uncertainties` | 遮挡、身份不确定、缺帧、视角冲突等 | 决定是否补观察 |

这是一份拟议接口，不假设 CoF 已能可靠输出这些字段。单凭视觉也不应承诺精确 3D 坐标、接触力或确定的物理因果；需要相应深度、标定或传感器支持。不确定的失败原因作为假设保留。

例如抓取后物体仍在桌面，至少存在“从未抓住”和“抬起后掉落”两条不同轨迹。若帧链清楚记录了物体先随夹爪上升、再脱离夹爪，CoF 可以报告 `lifted → dropped`；E 因此知道夹持状态已经失效，需要根据最新位置恢复抓取。是否由夹力不足造成，不能只凭该帧链确证。

CoF 不直接修改计划进度。Verifier 结合 CoF 证据、允许使用的传感器反馈和目标条件做判断；Controller 再更新 TaskProgress。运行时错误仍由 Executor 单独报告。CoF 失败或不可用时标记反馈缺失，不能默认成功。

用于执行后反馈的帧链默认来自实际执行观测。如果世界模型另行生成未来帧链，应标记为 predicted 并作为规划参考；预测帧不能成为验证已完成的观测证据。若 CoF 的实际定义与此不同，需要再调整具体适配器。

## 3. 三个模块分别输出什么

### Planner：维护可以被检验的计划

每个子目标至少包含 `id`、`description`、`depends_on`、`preconditions`、`success_conditions`、`status`。计划还需包含 `plan_version` 和 `based_on_state_version`。

例如任务“将红块放到绿块上并松开夹爪”：

| 子目标 | 前置条件 | 完成条件 | 验证失败后的处理 |
| --- | --- | --- | --- |
| 获取抓取目标 | 红块已定位，夹爪可用 | 有当前状态下可用的抓取候选 | 补充观测或重新选候选 |
| 抓住并抬起红块 | 抓取候选仍有效 | 红块被夹持并离开支撑面 | 重新观测，选择其他抓取方式 |
| 放置并释放红块 | 红块仍被夹持，绿块位置有效 | `on(red, green)` 且夹爪不再夹持红块 | 检查偏移/掉落，局部修订计划 |
| 验证最终任务 | 最新执行已结束 | 支撑关系稳定、夹爪打开、满足任务要求 | 补观察或恢复受影响子目标 |

这些是任务层规划，运动轨迹仍由已有 IK、控制 API 或技能完成。只有下一段要执行的子目标需要生成代码；较远的步骤先保留条件和依赖，执行前再依据新状态细化。

已完成事件保留为历史事实，但其效果可能随后失效。例如曾成功抓住物体，后来发生掉落；历史抓取记录不应删除，当前 `holding` 条件应更新，下游步骤也应重新检查。

### Executor：把短段代码变成可追踪的执行结果

执行请求应绑定 `episode_id`、`execution_id`、`plan_version`、`subgoal_id` 和 `state_version`，包含代码、允许使用的技能，以及调用次数/控制步数/耗时预算。

一次执行推进一个可验证的动作段。动作段可以包含多次 API 调用，例如求 IK、移动、闭合夹爪、短距离抬升。优先在抓取后、释放后、状态不确定或出现失败时返回决策层。具体时长通过实验选取，不预设所有任务都适用相同秒数。

执行步骤：校验请求结构和代码 → 校验状态时效及前置条件 → 执行 API → 记录已发生的调用与部分效果 → 获取执行后 observation → 返回报告。

AST 和 API 白名单可用于限制代码形式，但不能当成完整 Python 安全沙箱。调用预算应在 API 包装层强制执行；动作中断需要控制器支持合作取消或停止。仅给外层等待设置超时，不能证明底层运动已停止。

### Loop Controller：选择后续动作并更新进度

| 条件 | 下一步 |
| --- | --- |
| 当前子目标验证成功，下一子目标前置条件满足 | 更新进度，继续下一动作段 |
| 观测缺失、过期，或关键结果不确定 | 请求新观测；保持 pending verification |
| 局部可恢复失败，任务分解仍有效 | 修复代码或参数，在预算内重试 |
| 对象状态/前置条件变化，或局部恢复耗尽 | 修订受影响的子计划，记录新版本 |
| 收到取消/中断请求 | 停止派发动作，确认底层执行状态，保存检查点 |
| 所有最终目标经验证成立 | 返回 succeeded |
| 总预算耗尽、环境结束或无法继续 | 返回 failed、budget_exhausted 或 interrupted，并带原因 |

子目标状态可以采用 `pending → running → pending_verification → succeeded`，失败和恢复作为独立分支。终止原因必须与成功判定分离，LLM 的“我完成了”只是请求验证的信号。

## 4. 与状态/记忆模块先约定的最小接口

以下字段是拟议接口，不是 Cap-X 已存在的接口。

`ContextPacket`：任务描述与目标条件、最新 WorldState、必要的历史摘要/检索记录、技能契约、最近的执行结果，以及 CoF 提供的相关时序事件、条件判断和不确定信息。E 的 TaskPlan/TaskProgress 作为输入交给上下文模块组织，也由 E 保留权威版本。

WorldState 的最低要求：稳定对象 ID、机器人执行相关状态、关系/谓词、`state_version`、观测时间、证据来源、未知信息。坐标和姿态必须注明参考系、单位、四元数顺序；新版本号不自动代表所有字段都新鲜。

一个执行请求的示意结构：

```json
{
  "episode_id": "stack-001",
  "execution_id": "stack-001-exec-03",
  "plan_version": 1,
  "subgoal_id": "grasp_red",
  "based_on_state_version": 12,
  "decision": "execute",
  "code": "...仅当前动作段的 Python 代码...",
  "allowed_skills": ["solve_ik", "move_to_joints", "close_gripper"],
  "expected_effects": ["holding(robot, red_cube)"],
  "budget": {"max_api_calls": 6, "max_sim_steps": 160}
}
```

预算数值仅示意，应按 API 粒度和仿真器调参。`expected_effects` 是计划期待，不应直接写回为已发生事实。

一个“代码正常执行，但没有抓到物体”的报告：

```json
{
  "episode_id": "stack-001",
  "execution_id": "stack-001-exec-03",
  "subgoal_id": "grasp_red",
  "state_version_before": 12,
  "state_version_after": 13,
  "runtime_rc": 0,
  "execution_status": "completed",
  "verification_status": "failed",
  "reason_code": "GRASP_NOT_CONFIRMED",
  "evidence_refs": ["observation-13"],
  "verified_effects": [],
  "unmet_conditions": ["holding(robot, red_cube)"],
  "retryable": true
}
```

这里的 failed 需要有足够证据支持；如果只是夹爪遮挡、看不清结果，应使用 `verification_status: unknown`。保留 runtime、动作执行结果、环境验证三个维度，便于区分代码修复、动作恢复与补充观测。

验证结果来自运行时/观测验证器。模型生成的 `RESULT`、工具的“调用结束”日志、计划中的预期效果，都不能直接替代它。

SkillContract 除名称与参数外，还应说明：单位/坐标系、前置条件、运行结果、需要验证的后置条件、可能的错误码、最大执行范围、能否中断、能否重试。对于已经部分生效的动作，重试前必须重观测；不能假设重新执行整段代码无副作用。

持久保存的是 JSON 可序列化的计划、进度、执行事件及必要引用。Python namespace 可以作为运行缓存，但抓取候选、对象位置等缓存需要状态版本/有效条件；重启后应从检查点和新观测恢复，不依赖隐式全局变量。

## 5. 如何在 Cap-X 上落地

建议新增一个边界清晰的包，以下是计划中的目录，并未创建：

```text
capx/agents/stateful/
  contracts.py       # Plan / Decision / ExecutionReport / 接口类型
  planner.py         # 初始分解、选子目标、局部重规划
  codegen.py         # 根据当前子目标与技能契约生成短段代码
  executor.py        # 校验、API 调用计数、执行反馈
  verifier.py        # 根据后置观测产生 true / false / unknown
  controller.py      # 决策循环、预算、重试、停止与检查点
  adapters.py        # ContextProvider、CoF 反馈与 Cap-X env.step 适配
```

逻辑流程：

```text
ContextPacket → Planner / 当前子目标 → Codegen → Executor
      ↑                                           ↓
P：状态与记忆 ← CoF ← 本段实际观测帧 + API 执行记录
      ↓
Verifier（同时读取运行时结果）→ Controller 更新进度与下一步决策
```

上下文模块可以按 E 的需求替换实现：开发期使用固定 JSON 和确定性状态转移；联调期使用同学提供的 State/Memory；仿真期使用视觉与执行日志生成的状态。E 不应依赖某个特定世界模型的内部实现。

CoF 的接入位置是目前 `_handle_multi_turn_step()` 选择图像/视频反馈的分支。新适配器接收动作段帧、工具事件及子目标条件，向 P 交付结构化反馈；决策上下文不再只追加 VDM 文字。初始观测仍需单独初始化状态，不应凭一帧产生变化事件。对你负责的 E，可先使用固定 CoFReport 样例独立开发，并与 P 侧共同约定事件与谓词语义。

第一版决策协议建议覆盖 `execute / observe / revise_plan / finish_request`。继续、重试、中断和最终终止由 Controller 结合反馈与预算执行。解析失败应返回明确错误、有限重试，并记录原响应，不进入 finish 分支。

## 6. 分阶段交付与验收

### M0：锁定基线与实验接口

选择一个短任务，如 cube lifting 或 cube stacking，明确 API 层级、模型配置、状态来源和评测方式；保存原始配置、随机种子与日志格式。

主仓库已拉取，但当前全部 Git 子模块尚未初始化；本机是 Darwin arm64，`pyproject.toml` 的 uv 环境配置面向 Linux x86_64，完整感知链依赖 CUDA。正式 baseline 复现需要相应环境，不能将本地接口演示计作复现成功。

### M1：用固定状态样例验证闭环

使用模拟 API 和确定性环境转移，覆盖：正常成功、第一次抓取失败、状态过期、目标被移动、回复格式错误、执行后结果不明、部分执行后异常、预算耗尽和用户中断。

加入固定 CoFReport 样例，至少区分“没有抓住”“抬升后掉落”“夹爪遮挡、结果未知”。这一阶段验证 E 对反馈的处理，不声称已实现视觉 CoF 推断。

验收重点：Agent 能选择下一子目标；不跳过验证；失败后改变策略或补观察；没有证据时不报成功；重复执行受限；所有状态转移有可回放记录。

先用脚本化决策验证 Controller，再接 LLM 生成真实计划/代码。脚本化或 mock Demo 只能验证流程，不能证明模型的机器人任务能力。

### M2：接入一个 Cap-X 仿真任务

把相同 ExecutionRequest 通过适配器转为 `env.step(code)`，采集本段带时间戳的观测帧和工具事件交给 CoF/P，并将运行时结果与后置验证整理为 ExecutionReport。复用已有 API，先做到一次抓取/放置之后重新观察、验证和决策。

仿真器的真实状态可以用于 oracle 对照及评测器；如果给 Agent 直接使用，应明确标为 privileged setting。非特权实验中，Agent 只能看到事先声明允许的观测与反馈，避免把评测真值泄漏成输入。

### M3：扩展到多子目标与扰动任务

逐步增加依赖子目标、对象和恢复事件，例如 cube restack 或适当的 LIBERO 长任务。使用可重现的扰动，验证中途掉落/对象移动后能否只重做受影响步骤，以及恢复后能否接着完成原任务。

长程程度同时报告依赖子目标数量、动作段数和控制步数/耗时。任务运行更久本身不等于长程规划能力更强。

## 7. 怎样证明 E 的贡献

先固定模型、任务分布、API/技能粒度、状态来源和执行预算，再比较：

1. 原 Cap-X 多轮 baseline。
2. baseline + 相同的结构化状态输入。
3. 结构化状态 + 显式 TaskPlan/TaskProgress。
4. 上述组件 + 短段执行、后置验证、错误分类和局部恢复。

额外分别消融验证、局部重规划和上下文检索。状态/记忆由另一侧提供时，应固定该模块再评估 E，随后单独研究 P 与 E 的联合收益。

另设反馈机制对照：两帧 VDM、现有视频文字反馈、拟议 CoF 结构化反馈。比较时保持 E Harness、视觉模型、任务、API 与执行预算一致，并控制或完整报告帧数/分辨率、视觉 token、上下文长度和延迟。进一步在相同帧输入下比较文字反馈与结构化反馈，分离更多视觉信息和反馈组织方式的贡献。E 的主实验应固定使用同一版本的 CoF/P，避免把感知改进全部归因于规划执行。

报告任务成功率、子目标完成率、失败恢复率、错误宣告完成率、重复无效动作次数、动作/控制步消耗、LLM 调用次数、token 与总延迟。所有组使用相同种子和尽可能一致的预算，报告统计不确定性。

如果给某一组加入新的高层 `pick(object_id)` 技能，应将技能抽象变化作为独立变量，不能把减少低层控制难度的收益全部归因于 Harness。

## 8. 参考材料的使用边界

用户提供的 GPT-Policy、GPT-as-Policy、LongHorizon-Harness、Towards the Harness of Embodied Agents 及截图在本轮作为研究方向和分工背景。本文没有独立核验这些论文原文，不将用户摘要表述为已核实的论文结论。

第一项可独立交付的成果应是：接收约定格式的状态输入，输出带子目标和预算的代码执行请求，并在成功、失败和未知三类反馈下正确推进或恢复的 Agent Demo；随后用真实模型与仿真任务验证能力。
