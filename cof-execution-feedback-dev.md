# 机器人 Coding Agent：CoF 执行反馈开发文档

版本：v0.1 · 日期：2026-09-24 · 状态：待实现的开发设计

2026-09-28 实现进展：已接入有界证据选择、帧/清单/状态 hash、结构化时间反馈、多图模型接口和 P 交接，完成合成闭环测试。真实视觉模型与仿真未验收；本文件仍包含后续完整设计。现有运行方式见[第四阶段说明](/Users/agiuser/Documents/Codex/2026-09-24/ca/cap-x/docs/stateful-closed-loop.md)。

适用代码基线：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

总入口：[开发总览与使用指南](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-development-guide.md)。

配套文档：[任务规划与进度管理](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/task-planning-progress-dev.md)、[代码生成与执行](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/code-generation-execution-dev.md)、[整体方案](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-plan.md)。本文沿用其中的任务/执行 ID、SegmentContract、FrameManifest、ExecutionReport 和 VerificationReport；本文新增类型均为拟议接口，尚未在 Cap-X 中实现。

CoF 在项目中指 Chain of Frame 执行反馈方向。参考论文 *Chain-of-Frames: Advancing Video Understanding in Multimodal LLMs via Frame-Aware Reasoning* 是通过帧关联数据微调模型的通用视频理解方法，不是现成的机器人反馈器。第一版使用其“结论关联具体帧”的思想，称为 **CoF-inspired 原型**；只加多帧或修改 prompt 不等于复现原论文。

## 1. 开发目标与职责边界

本模块把一个动作段的实际观测、API 事件和相关执行条件，转换为有来源、有时间范围、能表示未知的反馈，回答三个问题：

1. 发生了什么：对象、机器人及其关系有哪些可观察变化？
2. 哪里不符合预期：与本段冻结条件相比，偏差出现在什么区间？
3. 还有什么不能确认：缺失哪些帧、视角、身份或传感器信息？

本模块的核心产物是**可核查的时序证据报告**，不是视频散文、长期世界状态或新的机器人动作代码。

### 1.1 责任划分

| 部分 | 责任方 | 本模块如何衔接 |
| --- | --- | --- |
| 当前子目标、验收条件、计划/进度 | 你：Planner / 进度模块 | 读取冻结要求；不修改目标和 TaskProgress |
| 实际执行、API 开始/结束、运行状态、帧采集和原始索引 | 你：Executor + 环境适配器 | 读取不可变记录；提出采样要求，但不重复实现采集器 |
| 有界帧选择、帧内事实、跨帧事件、证据与偏差分析 | 你：本文 CoF 模块 | 产生 CoFFeedback，保留不确定性 |
| 稳定对象 ID、历史状态、融合/冲突处理、长期记忆和上下文 | 同事：P 模块 | 提供相关先验与对象目录，接收候选事实；CoF 不维护第二份权威状态 |
| 条件是否成立、子目标/最终目标验证 | 你：Verifier | 消费原始证据与 CoF 候选判断，产生独立 VerificationReport |
| 是否补观察、恢复、重新规划、取消或结束 | 你：Loop Controller | 接收信息请求与验证结果，负责预算、权限和动作调度 |

CoF 与 Verifier 可以共用模型或软件进程，但必须保留输入、输出与写入权限边界。Verifier 若完全复用 CoF 的错误判断，不会因为多调用一次模型就获得统计独立性；应检查原始证据、可靠传感器与固定判据。

CoF 可以建议“需要确认当前夹持状态”，但不能自行移动相机、重新抓取或取消执行。涉及环境动作的观察请求必须经过 Controller 和 Executor。

### 1.2 第一版范围

- 一个 Cap-X 仿真任务，单臂，主相机必选、手腕相机可选；先离线轨迹回放，再接动作段闭环。
- 输入限定为当前执行窗口与少量相关先验，不加载整个 episode 的视频到一个 prompt。
- 先使用现有多图/视频模型，不要求立即训练新模型、生成未来视频或构建完整 3D 世界模型。
- 第一批关注：对象抬升、与夹爪共同运动、分离/掉落、目标位置不符、对象身份不符、遮挡与未知。
- 先使用段后反馈；不把 VLM 当作实时碰撞检测器、运动看门狗或急停机制。
- 不承诺从 RGB 推断准确接触力、摩擦系数、隐藏接触或确定物理根因。

## 2. 现有基础与新增部分

源码位置相对仓库根目录，行号对应本文基线提交。

| 位置 | Cap-X 已有行为 | 拟增加内容 |
| --- | --- | --- |
| `capx/envs/trial.py:332` | 最近两帧及可选手腕视图的视觉差异描述 | 保留为 baseline；新增帧引用和明确的未知/偏差结构 |
| `capx/envs/trial.py:393` | 输入一段执行视频，描述动作、变化和看起来是否完成 | 保留视频文字反馈 baseline，不能把多帧本身称为新增能力 |
| `capx/envs/trial.py:571` | 在多轮流程中选择视频或图像差异反馈 | 增加显式 CoF 分支，结构化结果交给 P/Verifier，不只拼接描述 |
| `capx/envs/trial.py:785` | 执行前记录视频缓冲区索引 | 新 manifest 记录身份、真实采集时刻、调用边界与输入快照 |
| `capx/envs/trial.py:790` | 执行后记录索引，形成代码块的帧范围 | 明确本段前帧/后帧/稳定性窗口，不能默认范围内含全部边界帧 |
| `capx/envs/tasks/base.py:336` | 提供 get_video_frames_range 适配 | 复用读取能力，新增按 frame_id/call/time 的有界检索 |
| `capx/envs/simulators/robosuite_base.py:354` | 渲染主相机，按配置渲染手腕图像，存数组缓冲区 | 在选定后端采集时附带元数据；其他后端逐个检查 |
| `capx/utils/video_utils.py:13` | 视频编码默认 fps=30 | 播放时间不当作真实执行时间；时间判断使用采集元数据 |

当前所查帧缓冲接口主要返回图像数组，尚不能直接提供本文需要的稳定 frame_id、完整时间域和 API 关联。第一版复用图像获取与视频基础设施，补齐事件/索引适配，不另建不受控的模拟器渲染线程。

新增价值应落在：**证据可定位、API 与帧对齐、过程事件可区分、未知可表达，以及下游错误继续减少**。这些都需实验验证，不预设提升。

## 3. 模块流程与不变量

```text
冻结动作段条件 + 执行前相关状态
ExecutionReport + API 事件 + FrameManifest / 传感器
                         ↓
           对齐检查、来源检查、帧选择
                         ↓
           本段局部事实与变化候选
                         ↓
           跨帧事件 + 时间范围 + 证据
                         ↓
        对照预期：偏差 / 反证 / 未知
                         ↓
         schema / 引用 / 时间 / 一致性校验
                         ↓
                   CoFFeedback
                         ↓
   P 融合事实（失败也更新）→ Verifier 判断条件
                         ↓
             Controller / Planner 决策
```

必须保持的不变量：

1. 每个反馈绑定 episode、plan_version、subgoal、attempt、segment、execution 和分析输入版本。
2. 只用被允许的实际观测支持已发生事件；预测、模型自述和 API 名称不是动作效果证据。
3. 结论不得强于证据：未看到不等于没发生，看不清不等于失败，更不等于成功。
4. 事实、事件、偏差、原因假设分开记录；时间相关不自动变成因果关系。
5. 事件至少关联证据帧或传感器记录；未知帧 ID、越界时间或错配执行的引用拒绝接受。
6. 帧索引只标识所选输入位置，不代表真实时间；多视角同时观测不能串成先后事件。
7. 本段效果、子目标效果和最终目标分开；不能因首段尚未完成子目标就判失败。
8. 本段曾发生的效果与段末仍成立的事实分开；历史抬升不能证明当前仍被夹持。
9. CoF 不直接写 WorldState、TaskProgress，不解除执行器的 outcome_unknown 或后端锁。
10. P 融合所有有效变化，包括失败、取消和部分执行后的变化；不能只在成功后更新状态。
11. 有限分析预算内仍不清楚则显式未知；禁止无限回看和模型自洽式确认。
12. 引用和格式正确不证明视觉判断正确；程序校验和感知质量分别验收。

## 4. 输入与数据契约

建议采用严格类型模型并导出 JSON Schema。未知字段默认拒绝；拒绝 NaN/Infinity，限制对象数、事件数、输出长度和图像大小。原始媒体外部存储，引用限定在获准的 evidence manifest，不接受模型给出的任意文件路径或 URL。

### 4.1 CoFRequest

| 字段 | 内容与来源 |
| --- | --- |
| request_id / schema_version | Controller 分配；不可由模型改写 |
| episode_id / task_version / plan_version | 沿用上层任务版本 |
| subgoal_id / attempt_id / segment_id / execution_id | 沿用执行关联，不创建另一套命名 |
| based_on_state_version / state_view_ref | 执行前相关先验，来自 P；每条事实保留观测时间/来源 |
| segment_contract_ref / query_spec_ref | 冻结本段条件与检查问题；原始契约保留可追溯引用 |
| execution_report_ref / api_events_ref | 运行与调用事实；不是环境成功标签 |
| frame_manifest_ref / sensor_manifest_ref | 实际帧/传感器记录；无传感器时明确空集 |
| input_revision / evidence_cutoff | 本次分析的不可变快照版本与截止范围 |
| analysis_budget / policy_version | 有限模型调用、帧数、回看次数、时间/成本和规则版本 |

先验可帮助定位目标对象，但必须标记为 prior；模型不能把“执行前认为 holding=true”复制成执行后的观测证据。对象不可唯一匹配时产生身份未知，向 P 请求映射，不重新随意命名同一物体。

### 4.2 AnalysisQuery：不更改已有 Condition

沿用 Condition 的 `{id, predicate, args, expected}`。新增独立、派发前冻结的 AnalysisQuery，绑定 condition_id 或明确事件查询：

| temporal_mode | 检查问题 | 处理原则 |
| --- | --- | --- |
| at_end | 观察窗口末尾是否成立 | 检查最新有效后帧；旧成功帧不能替代 |
| occurred | 指定事件是否在窗口内发生 | 有事件证据可支持；未采样到不能一般性证明未发生 |
| maintained | 某关系是否在指定过程内保持 | 检查中间证据及覆盖；存在反例可反对，稀疏帧不证明连续保持 |
| stable_for_window | 指定结束窗口内是否保持稳定 | 窗口长度、容差、覆盖要求来自任务/谓词适配器 |

这是分析范围扩展，不修改 SegmentContract 的既有字段或偷偷加验收条件。过程/稳定性要求必须来自任务约束或经认可的技能契约；执行后不得由模型新增或放宽要求。有限采样下的 maintained 结论要标为采样支持，若任务要求严格连续保证，需相应可靠监测；不具备时返回 unknown。

只有 at_end 查询的语义和时间条件与正式验收相符时，才可作为相应条件的候选证据。occurred(lifted) 与 at_end(lifted) 是不同问题，即使引用同一个谓词也不能合并。

### 4.3 FrameManifest 与 ModelInputManifest

沿用执行文档的 FrameManifest；每帧至少含：frame_id、execution_id、camera_id、capture_time、clock_domain、sim_step（如适用）、active_call_id、source、文件引用、checksum。补充 sample_group_id、采样原因、标定版本、可选 ROI 与质量标记；字段升级需版本化。

ModelInputManifest 在 CoF 侧生成，保存每次模型实际看到的内容：

- input_batch_id、model_visible_index、原始 frame_id、camera_id、时间/采样组。
- resize/crop/拼图变换和派生产物 hash；裁剪保留原图关联。
- 原生视频接口采用的解码/抽帧映射；不能假设服务端编号就是本地原始帧号。
- selection_policy_version、实际帧数、跳过区间及选择原因。

建议第一版使用顺序多图输入，每张明确标注视图与 frame_id，便于准确引用。若模型只能给局部 Frame 1/2 编号，服务端按该 batch 的映射转回永久 ID。回看生成新 batch，旧编号映射保持不变。

如果原生视频接口无法暴露实际抽帧映射，不承诺精确帧归因；可退回显式多图模式，或仅返回受支持的区间级引用并标注限制。对现成 CoF 权重还需保持其训练/推理采样协议，不能任意改采样而宣称等价复现。

## 5. 帧采集、选择与时间对齐

### 5.1 采集和分析采样分开

Executor/后端负责采集原始帧；CoF 从已采集的不可变缓存选择模型输入。二者预算分别记录。

第一版选择策略：

1. 保留本段前后观测以及相关操作的边界帧。
2. 加入有上限的均匀时间采样，覆盖长调用的内部。
3. 对怀疑分离、位移异常或遮挡切换的区间，在已有缓存中追加更密帧。
4. 留出正常完成后的必要观察窗口；窗口中是否允许仿真推进、何时采样由环境适配器明确并计费。

必须帧超过预算时，不静默丢掉某个动作阶段；应拆成有重叠的有限窗口，或标记覆盖不足。外层窗口大小和采样频率是开发配置，不是通用最佳参数。

回看只能利用当时已保存的帧。原始采集稀疏导致漏掉过程时，不能通过插值/生成帧补成已发生证据。缓存不足明确报告；后续可以改善采集，但不能修复当前历史记录。

### 5.2 时间与多相机

- 每台相机/传感器明确时钟域；用已校准的映射和误差界限进行跨源对齐。
- 时钟不同或顺序误差大于事件间隔时，不给出确定先后关系。
- 同时多视角作为一个采样组，在各视角分别识别证据，再判断是否一致。
- 手腕视图移动不等于对象移动；几何变化应在共同参考系或相对夹爪关系下判断。
- API 的 active_call_id 表示采样时正在执行的调用，不表示该调用已被证明是变化的原因。
- 采用实际 capture_time / sim_time 计算过程时长；视频默认编码 fps 只决定播放速度。
- 事件定位优先给区间 `[最后仍保持的证据时间, 首次明确改变的证据时间]`；不伪造采样间的精确发生时刻。

### 5.3 历史回看与新观测请求

| 请求 | 目的 | 谁执行 |
| --- | --- | --- |
| replay_interval | 从已有缓存补充过去区间的帧/传感器 | CoF 只读检索，受分析预算限制 |
| refresh_current_observation | 确认目前位置、夹持或目标关系 | Controller 通过环境适配器获取 |
| observe_other_view | 获取可消除遮挡的另一视角 | 无动作读取也走协调接口；需要移动时经 Executor |
| resolve_identity | 确认当前观测对应哪个稳定对象 | P 的对象映射/上下文接口 |

请求需包含 question、对象/条件、相关区间、缺失原因和优先级，而不是直接附可执行恢复代码。新观测可以证明现在的状态，不能证明漏拍时是否曾抓住过。

## 6. 分析算法：事实、事件、偏差分层

### 6.1 第一层：帧内事实候选

对任务相关对象及邻近对象提取可见性、对象身份、局部空间关系和机器人状态。保留一个完整场景视图或低成本全局扫描，避免只裁剪目标而漏掉邻近物体被碰倒等非预期变化；能力仍有边界，不承诺开放世界所有变化都能发现。

第一批 EvidenceClaim 可包含：

- 对象可见、被遮挡、身份无法唯一匹配。
- 夹爪外观为打开/闭合；有传感器时分别记录实际反馈。
- 对象离开支撑面、接近夹爪或位于目标区域。
- 对象与夹爪相对位置、对象与支撑面的关系候选。

每条 claim 附对象、时间、证据引用及来源方法/版本。布尔谓词的 candidate_value 使用 true / false / null，null 表示未知并必须附原因；这与后文对查询命题的 supported / refuted / unknown 不同。3D 距离与稳定性阈值由已注册判据给定；RGB 推断和深度几何测量分开记录，不包装成相同精度。

事实提取阶段尽量只提供目标身份、观测和必要的调用时间；不提供执行模型的自评“已经成功”。预期条件用于后续偏差比较，避免模型根据期望补画结果。

### 6.2 第二层：时序事件候选

联合分析相邻帧与短窗口中的相对运动、关系变化及传感器记录，不仅把逐帧 caption 再摘要一次。可将结构提取与事件分析放在同一次 VLM 请求中，但保留两类可检查输出。

| event_type（拟议） | 必需的证据特征 | 不能据此断言 |
| --- | --- | --- |
| gripper_closed | 夹爪反馈或足够清晰的开合变化 | 目标已抓住 |
| object_lifted | 目标相对支撑面离开，身份连续 | 段末仍夹持、完整任务成功 |
| co_motion_observed | 对象与夹爪在多帧中保持相对关系并移动 | 仅凭一次图像重叠就证明接触 |
| object_detached | 先有共同运动/夹持证据，后出现分离证据 | 摩擦不足或夹力不足等根因 |
| object_dropped | 先离开支撑、随后分离下落/回到更低支撑的时序证据 | 每次检测丢失都是掉落 |
| object_released | 分离与释放指令/阶段语义一致，并有结果证据 | 已稳定放在正确目标上 |
| target_region_reached | 位置/关系符合任务定义和允许误差 | 物体已稳定、夹爪已释放 |
| object_mismatch_observed | 稳定身份映射支持实际对象不同于目标 | 外观相似或遮挡情况下随意换对象 ID |

这些是候选领域事件，不是 Cap-X 已提供的 API。缺证据时降级为更弱描述或 unknown。例如只有末帧看见物体在桌上，只能说“段末没有完成抬升”，不能区分从未抓住还是抓住后掉落。

事件允许重叠与组合，不强制单一失败标签。滑落、错误对象和放置偏差可能同时出现。去重按对象、类型、重叠区间和证据合并；跨窗口身份/时间冲突返回未知，不能拼接两段不同物体成一个事件。

### 6.3 第三层：与冻结预期对照

检查 SegmentContract 的 expected_conditions、continue_conditions 与对应 AnalysisQuery；不把子目标整体未完成当作本段失败。

偏差类别建议：expected_effect_absent、effect_lost、wrong_object、wrong_target_relation、unexpected_change。证据不足使用 unresolved 项，不强塞进某种物理失败类。

同一事件的含义依赖阶段：搬运中 object_detached 通常违背保持夹持要求；正常释放阶段的分离则可能符合预期。对照真实指令、阶段与观察，不仅凭关键词分类。

必须分开：

- **事件**：红块先抬升后脱离夹爪。
- **偏差**：段末 holding / lifted 条件缺少支持或被反证。
- **原因假设**：夹力不足、抓取接触不稳等；没有相应传感器则不作确定结论。
- **恢复决策**：是否重新抓取、换策略；归 Planner / Controller。

### 6.4 先检查结果，再有界回溯

借鉴 REFLECT 的渐进分析，但做以下区分：

1. at_end / stable_for_window 查询先看符合时间范围的后观测。
2. 发现不一致或 unknown 时，回看中间帧，寻找最后正常与首次异常的证据区间。
3. maintained / occurred 等过程查询必须检查相关中间证据，即使终态正确也不能跳过。
4. 若需要执行前更早的原因，向 P 请求有限历史证据引用；CoF 不自行载入整个任务历史。
5. 预算耗尽或重复查询没有获得新证据时，停止分析并报告未知，不以模型多次同意替代证据。

方法可先通过单次有界多图调用实现；后续添加一次区间回看。多阶段划分是系统职责，不要求每层都运行一个独立模型。

## 7. 输出 CoFFeedback

### 7.1 字段定义

| 字段 | 定义 |
| --- | --- |
| feedback_id / revision / schema_version | 服务端生成；同输入也可能重分析，必须保留版本和模型/策略来源 |
| request_id / 身份关联 | 完整保留 episode、任务/计划、子目标/attempt/segment/execution |
| input_revision / input_manifest_ref / query_spec_ref | 定位本次实际看到的帧和分析问题 |
| analysis_status | ready / partial / unavailable / invalid；仅表示反馈可用性，不表示机器人成功 |
| observed_changes | 带时间、对象、来源与证据的事实变化候选；不直接写状态 |
| events | event_id、type、对象、时间区间、帧/传感器/API 引用、简短证据摘要 |
| condition_evidence | query_id、condition_id、assessment、证据及观测范围；正式 verdict 由 Verifier 产生 |
| deviations | deviation_id、类别、关联查询/事件、区间、简短说明；不含可执行代码 |
| cause_hypotheses | 可选原因假设、支持与反证、缺失信息；无依据则空数组 |
| uncertainties | 证据缺口与受影响的对象/查询，不用一个全局低置信度掩盖所有问题 |
| observation_requests | 有限、类型化的信息请求；不执行环境动作 |
| coverage / provenance / usage | 实际采样范围/缺口、模型/提示/选择策略版本、帧数/调用/耗时/token |

analysis_status=ready 表示所有请求项都已按协议处理，可以包含合法 unknown；partial 表示采集/服务/输出缺失导致部分项未能分析；unavailable 表示没有可用反馈；invalid 表示输入身份或输出完整性不通过。无论哪个状态，缺项都不能默认成功。

condition_evidence.assessment 为 supported / refuted / unknown，表示证据对**完整查询命题**的支持程度，不是任务成功标签。例如 expected=false 时，supported 需要支持该否定条件的实际证据；未检测到对象不构成反证。

不要求模型暴露冗长的自由推理过程；要求简短、可核查的 evidence_summary 和引用。模型自报 confidence 不是校准概率；第一版可不使用数值置信度，后续如增加阈值，需在独立验证集校准并报告拒判覆盖率。

### 7.2 贯穿 fixture：抬起后掉落

这是文档用模拟记录，不是实际实验。沿用规划文档的红块/绿块对象及 s1_grasp 条件；本例独立于执行文档的 exec_2，使用 `episode_cof_demo / attempt_cof_1 / segment_cof_1 / exec_cof_1`。

本段意图为闭合并短抬升，段末要求 holding、lifted 均成立。开发 fixture 假设校准视角和深度足以支持所列事实，每个 frame_id 引用包含该时刻的可用 RGB-D 记录，而不仅是文字标注。实际系统若证据不足，必须改为 unknown。表中标注用于人工/测试校验，不作为待测视觉模型输入，否则构成答案泄漏。

| frame_id | capture_time_s | camera_id | sample_group_id | active_call_id | 已标注的可见证据 |
| --- | --- | --- | --- | --- | --- |
| f0 | 0.0 | main | g0 | null | 红块在桌面，夹爪打开 |
| f1 | 0.4 | main | g1 | call_close | 夹爪闭合，红块尚在桌面 |
| f2 | 0.8 | main | g2 | call_lift | 红块已离台，与夹爪共同上升 |
| f3 | 1.0 | main | g3 | call_lift | 红块离开夹爪向下运动 |
| f4 | 1.4 | main | g4 | call_lift | 红块回到桌面，夹爪为空 |
| f5 | 2.0 | main | g5 | null | 红块仍在桌面，夹爪为空 |

时间统一为本 execution 的仿真相对秒；capture_time_s 是表格简写，真实 manifest 还需时钟域、checksum、source=observed 等字段。call_close 对应 close_gripper，区间 0.1–0.5；call_lift 对应 move_to_joints，区间 0.5–1.5。执行器在 1.5 确认空闲，f5 是计入预算的段后观测；分析期间没有新动作。

该 fixture 的查询规格：

```json
{
  "query_spec_id": "queries_cof_1",
  "schema_version": "1.0",
  "time_basis": "execution_sim_time_s",
  "queries": [
    {
      "query_id": "q_hold_end",
      "condition": {"id": "c_holding_red", "predicate": "holding", "args": ["robot", "red_cube"], "expected": true},
      "temporal_mode": "at_end",
      "window_s": [1.5, 2.0]
    },
    {
      "query_id": "q_lift_end",
      "condition": {"id": "c_lifted_red", "predicate": "lifted", "args": ["red_cube"], "expected": true},
      "temporal_mode": "at_end",
      "window_s": [1.5, 2.0]
    }
  ]
}
```

CoFFeedback 核心内容示例；省略的 provenance/usage 等服务端元数据由正式 schema 补齐，不能让模型伪造。input_batch_cof_1 的映射为 model_visible_index 1–6 对应 f0–f5。

```json
{
  "schema_version": "1.0",
  "feedback_id": "cof_exec_1_r1",
  "revision": 1,
  "request_id": "cof_request_1",
  "episode_id": "episode_cof_demo",
  "task_version": 1,
  "plan_version": 1,
  "subgoal_id": "s1_grasp",
  "attempt_id": "attempt_cof_1",
  "segment_id": "segment_cof_1",
  "execution_id": "exec_cof_1",
  "based_on_state_version": 12,
  "input_revision": 1,
  "input_manifest_ref": "input_batch_cof_1",
  "query_spec_ref": "queries_cof_1",
  "time_basis": "execution_sim_time_s",
  "analysis_status": "ready",
  "observed_changes": [
    {"claim_id": "claim_hold_mid", "predicate": "holding", "args": ["robot", "red_cube"], "candidate_value": true, "at_s": 0.8, "evidence_refs": ["f1", "f2"]},
    {"claim_id": "claim_hold_end", "predicate": "holding", "args": ["robot", "red_cube"], "candidate_value": false, "at_s": 2.0, "evidence_refs": ["f5"]}
  ],
  "events": [
    {"event_id": "e_lift", "type": "object_lifted", "object_ids": ["red_cube"], "interval_s": [0.4, 0.8], "evidence_refs": ["f1", "f2"], "call_refs": ["call_close", "call_lift"], "evidence_summary": "红块由桌面位置变为随夹爪离台上升"},
    {"event_id": "e_drop", "type": "object_dropped", "object_ids": ["red_cube"], "interval_s": [0.8, 1.4], "evidence_refs": ["f2", "f3", "f4"], "call_refs": ["call_lift"], "evidence_summary": "红块先与夹爪共同上升，随后分离下落并回到桌面"}
  ],
  "condition_evidence": [
    {"query_id": "q_hold_end", "condition_id": "c_holding_red", "assessment": "refuted", "observed_at_s": 2.0, "evidence_refs": ["f5"]},
    {"query_id": "q_lift_end", "condition_id": "c_lifted_red", "assessment": "refuted", "observed_at_s": 2.0, "evidence_refs": ["f5"]}
  ],
  "deviations": [
    {"deviation_id": "d_effect_lost", "type": "effect_lost", "query_refs": ["q_hold_end", "q_lift_end"], "event_refs": ["e_lift", "e_drop"], "interval_s": [0.8, 1.4], "summary": "抬升效果曾出现，但未保持到段末"}
  ],
  "cause_hypotheses": [],
  "uncertainties": [
    {"kind": "cause_unobserved", "event_refs": ["e_drop"], "summary": "缺少力和接触证据，不能确定掉落的物理根因"}
  ],
  "observation_requests": [],
  "coverage": {"window_s": [0.0, 2.0], "selected_frame_count": 6, "max_selected_gap_s": 0.6, "known_capture_gaps": [], "continuous_guarantee": false}
}
```

本例“曾抬升”与“段末 lifted=false”不矛盾，分别描述事件与当前状态。连续性未被保证，但已有清晰反例足以反对段末条件。对于高频 maintained 查询，六帧未必足够，不能因本例可定位掉落就认为采样策略普遍充分。

observed_changes 在本协议中包含中间/末尾事实候选，不要求每项都有确定的 before 值；P 负责将其与已有状态比较。某一状态未被观察到时不自动删除该事实，而是按时效与未知规则处理。

后续 P 按观测时间融合中间/末尾事实，保留历史抬升事件并更新当前夹持信息；Verifier 再按原条件生成 fail 或 unknown。CoF 不把事件直接变成计划状态，也不决定新 attempt。

## 8. 输出校验、不确定性与接入规则

### 8.1 校验顺序

1. schema、枚举、长度、字段类型及数值范围。
2. episode/执行/输入版本匹配；对象和 condition/query ID 存在且定义一致。
3. 所有证据确实存在于本次输入或获准检索结果；不能引用“知道但未提供”的帧。
4. 时间位于分析窗口、顺序合法、时钟映射可用；跨 batch 编号映射正确。
5. at_end 证据满足查询时间要求；maintained 检查覆盖规则；否定条件不能从检测缺失推出。
6. events/deviations/claims 内部无无法解释的身份或时间冲突；原因假设未混入观测事实。
7. predicted/oracle/运行自评等来源没有被用于非特权完成证据。

输出格式可有限修复一次；修复只解决结构或引用错误，不通过反复提示“再想想是否成功”筛选有利答案。有效子集可以保留为 partial，受影响查询强制 unknown；身份/执行整体错配则 invalid，不应用任何候选状态变化。

at_end 不能任取结束窗口中最有利的一帧：需要最新有效观测，并检查截至 evidence_cutoff 是否有更晚的反证/新动作。stable_for_window 的时长与覆盖要求不能用单张末帧替代。模型响应顺序也不能用来判断事件先后。

### 8.2 证据与置信度

- 不把多次相似帧当作独立证据数量相乘；相邻帧和多个 VLM 输出可能高度相关。
- 视觉/深度/夹爪传感器冲突时保留来源与反证，交给 P/Verifier 按规则融合，不挑有利结果。
- 遮挡期间不自动沿用旧 held=true，也不直接置 false；产生未知与观测需求。
- 事件有证据但原因未知是正常结果；不要因为缺根因把已观察的掉落也抹掉。
- 若与前版报告结论冲突，应保留修订来源与证据，不静默覆盖原始记录。

### 8.3 与 P、Verifier、Controller 的交接

P 根据 claim 的 observed_at/source/evidence 融合，不按反馈抵达顺序写当前状态；对旧 execution 的迟到报告，只更新对应历史，必要时触发当前重新观察。CoF 派生文本与它的原始帧必须共用证据 lineage，不能作为两份独立观测重复加权。

Verifier 读取固定查询/条件、可用原始证据、CoF 候选和更新后的允许状态，生成正式 VerificationReport。不得以 P 中由同一 CoF 推导出的事实反过来“独立证明”CoF 结论。

Controller 处理观察请求、预算与下一步；CoF 报告动作看似完成也不能解除执行器的 outcome_unknown。运动停止只由后端确认。分析过程若已有新动作，原反馈只能证明旧窗口，继续动作前重新检查当前状态时效。

## 9. 异常、版本与存储

| 情况 | CoF 行为 | 系统后续 |
| --- | --- | --- |
| 缺少末帧、图像损坏、相机未启用 | partial/unavailable；对应查询 unknown | 请求当前观测，不默认成功 |
| 只有前后帧，没有中间过程 | 保留终态结论，过程未知 | 不编造抓取或掉落经过 |
| 模型超时/服务不可用 | 记录未处理查询、预算与错误 | Controller 有限重试或按反馈缺失处理 |
| 时钟不匹配、对象身份不唯一 | 降级相关关系或 invalid 输入 | 请求同步/身份确认 |
| API 异常但帧显示效果已经发生 | 分开保留执行错误与环境证据 | Verifier 判断效果，Controller 处理故障 |
| 完成后又被扰动 | 旧反馈作为历史 | P 更新新事实，重新验证当前条件 |
| 重复 feedback_id、相同 revision | 内容一致幂等；不重复事件/融合 | 进度和预算不重复生效 |
| 同 ID 不同内容、版本倒退 | 完整性错误或历史保留，不覆盖 | 记录并请求核对 |

缓存键包含 execution、evidence/input hash、query_spec 版本、模型、prompt、选择策略与几何/判据版本，不能只用 execution_id。新帧或新标定需新 input_revision；模型重新分析也需新报告 revision，原证据保持不变。

保存 request、冻结查询、原始 manifest、每个模型输入 batch、原始模型输出、校验错误、最终反馈及用量。大媒体共享不可变文件，以 hash 引用，不为每次回看复制整个视频。删除/保留策略由实验存储策略定义，不能在 CoF 分析完成后立即清理仍被报告引用的证据。

日志、图像文字和 API 返回内容作为数据，不接受其修改系统策略或发起工具调用。模型不拥有环境操作工具、任意文件访问或长期状态写权限。

## 10. 实现接口、目录与配置

### 10.1 公共接口草案

以下为可解析的接口示意，非实际实现；通过依赖注入访问模型、媒体和状态，不在核心逻辑中导入机器人/GPU 栈。

```python
def prepare_cof_request(contract, query_spec, state, execution, manifests) -> "CoFRequest":
    """冻结身份/时间/来源范围；不修改目标或世界状态。"""
    ...

def select_evidence(request, media_store, selection_policy) -> "ModelInputManifest":
    """按预算选择实际记录，保存局部编号与永久 ID 映射。"""
    ...

def analyze_window(request, model_input, model_client) -> "CoFProposal":
    """产生事实、事件、偏差与未知候选，不写 TaskProgress。"""
    ...

def validate_feedback(proposal, request, manifests, catalogs) -> "ValidationResult":
    """检查引用、身份、时间与证据规则；不宣称已证明感知正确。"""
    ...

def request_replay(feedback, budget, media_store) -> "ReplayResult":
    """只读取已采集历史，不能重新执行机器人来补造过去。"""
    ...

def finalize_feedback(validated, provenance, usage, store) -> "CoFFeedback":
    """保存不可变版本与 lineage，交给 P / Verifier / Controller。"""
    ...
```

### 10.2 建议目录

```text
capx/agents/stateful/feedback/cof/
  contracts.py          # CoFRequest / Query / Feedback；引用共享执行契约
  query_adapter.py      # 条件→冻结查询；时间/证据规则，非任意代码表达式
  selector.py           # 边界/均匀采样/区间回看
  alignment.py          # 帧/调用/多视图/时钟映射
  analyzer.py           # 有界模型调用和结构化提案
  prompts.py            # 模板、版本、简短证据摘要约束
  validation.py         # schema / 引用 / 时间 / 来源与一致性
  replay.py             # 原始媒体的只读区间检索
  store.py              # 输入/输出版本、缓存、证据 lineage
  adapters.py           # Cap-X / P / Verifier / 传感器能力适配

tests/cof/
  fixtures/             # 帧、调用、条件、标注；注明 synthetic/mock/observed
  test_alignment.py
  test_selection.py
  test_contracts.py
  test_evidence_rules.py
  test_feedback_lifecycle.py
  test_integration.py
```

局部 EvidenceClaim / Event 可在 CoF 中定义，但 FrameManifest、Condition、ExecutionReport、VerificationReport 与既有模块共享，不能复制出不同枚举。P 的事实结构通过 adapter 转换，不在 CoF 内实现长期融合。

### 10.3 配置草案

```yaml
agent_mode: stateful
feedback_mode: cof
cof:
  backend: frame_grounded_vlm
  input_mode: explicit_images
  selection_policy: boundary_uniform_v1
  max_frames_per_request: 16
  max_total_frame_presentations: 32
  max_replay_rounds: 1
  max_format_repairs: 1
  max_model_calls: 3
  max_analysis_wall_time_s: 45
  max_events: 24
  max_output_bytes: 32768
  require_evidence_refs: true
  accept_predicted_as_evidence: false
  allow_environment_actions: false
```

以上仅为开发默认值，非最佳参数或安全阈值。一次初始分析、一次回看、一次格式修复共享调用/时间/帧预算；重发相同图像也计入 frame presentations。token、图像分辨率和费用上限由实际模型适配配置给出，超过预算不继续调用。

frame_capture 的频率和保存上限沿用执行模块配置，CoF 不再另写一套采集器。同步容差、稳定窗口和几何阈值从后端/PredicateCatalog 读取，缺失关键规则时拒绝该查询或返回 unknown，不由模型编造。

### 10.4 Cap-X 接入顺序

1. 固定原两帧/视频反馈 baseline，增加 `feedback_mode: cof`，不覆盖旧 prompt 或改变旧结果语义。
2. 在执行侧按此前文档补充帧元数据与 API 时间关联；对所选后端验证边界帧和长调用采样。
3. CoF 分支消费已保存记录，先输出文件，不参与动作决策；离线检查再接 P/Verifier。
4. 新 Controller 使用 CoFFeedback 与 VerificationReport；不把 feedback 文本塞进旧 finish/REGENERATE 解析器冒充完整闭环。
5. `_load_config()` 显式透传新增配置并校验模式互斥，避免同时生成两份不同反馈却未说明融合规则。
6. CLI 验收后再接 Web 展示；展示事件区间、证据缩略图、未知原因和模型版本，不把视觉推断渲染成控制器真值。

## 11. 开发阶段与首版交付

| 阶段 | 内容 | 完成条件 |
| --- | --- | --- |
| C1 数据与对齐 | request/query/feedback 类型、输入映射、fixture、来源/时间校验 | 错配引用、跨相机顺序、未知条件被正确拒绝 |
| C2 单窗口反馈 | 固定采样、现有 VLM、结构化事实/事件/未知 | 能区分正常抬升、未抬升、先抬后掉、遮挡四条轨迹 |
| C3 有界回看 | 结果检查、过程查询、区间细化、预算和缓存 | 回看使用原记录；终态成功但过程违例不漏检为已通过 |
| C4 闭环联调 | 接 Executor、P、Verifier、Controller | 不直接写进度；失败后状态更新；unknown 请求补证据而不盲重试 |
| C5 数据与实验 | 注入失败、真实策略轨迹、人工标注、消融 | 报告错误/覆盖/延迟及闭环收益；据瓶颈决定是否训练 |

C1 的程序测试在当前 Mac 可用固定 manifest/mock 运行；C2 需要实际视觉输入与模型服务；仿真完整运行需对应环境。mock 返回预期 JSON 仅验证接口，不验证视觉识别能力。

首个 Demo 建议在红块/绿块任务展示五种记录：正常完成、从未抬升、抬升后掉落、正常释放、遮挡无法判断。保存被选帧、调用时间线、输出事件、证据引用、验证结果、进度变化和总成本；既展示成功，也展示不知道。

## 12. 必须覆盖的测试

| 编号 | 场景 | 预期行为 |
| --- | --- | --- |
| F01 | 合法帧/调用/查询记录 | 输出身份匹配的报告，引用均可回溯 |
| F02 | 两段不同过程但相同终态：没抬起 vs 抬起后掉落 | 有中间证据时区分；只有前后帧时不编造过程 |
| F03 | close_gripper 返回正常但物体留在桌上 | 不以 API 返回判断夹持成功 |
| F04 | 正常释放导致 holding=false | 按放置阶段处理，不误报搬运掉落 |
| F05 | 中间异常后恢复，终态正确 | maintained/过程约束仍检查中间记录 |
| F06 | 抬升事件发生但段末已掉落 | 历史事件与末态条件分开，不能用旧帧支持当前成功 |
| F07 | 对象遮挡、检测丢失、相似对象交叉 | 身份/关系未知，不自动输出掉落或换对象 |
| F08 | 手腕相机移动，静物像素变化 | 不直接认作对象在世界系移动 |
| F09 | 两相机同刻观察、异步或相互矛盾 | 正确分组，记录误差/冲突，不制造先后关系 |
| F10 | 原始时间非均匀，视频编码固定 fps | 真实时间来自 manifest，播放秒数不用于事件定位 |
| F11 | 回看重新编号、跨 batch 引用 | 映射回正确永久 ID；越界或不存在引用拒绝 |
| F12 | 历史关键帧未采集、缓存丢失 | 报告缺口；新拍照片不当作过去过程证据 |
| F13 | 只给生成未来帧、oracle 标签或模型自述成功 | 非特权完成证据拒绝接受 |
| F14 | 稀疏采样且没有看到异常 | 不保证连续保持；按查询覆盖要求支持或 unknown |
| F15 | expected=false，但对应事实未知 | 不将 unknown 当否定条件通过 |
| F16 | 视觉判夹持、传感器反对 | 保留反证与来源，交 P/Verifier，不选择性忽略 |
| F17 | 第一动作段尚未完成整个子目标 | 只评价本段要求，不错误判子目标失败 |
| F18 | 代码异常但已经造成环境变化 | P 仍融合实际变化；运行错误单独保留 |
| F19 | outcome_unknown 但视频看似静止 | CoF 不解除后端执行锁，等待控制器确认 |
| F20 | 重复/迟到/旧 revision/跨 episode 报告 | 幂等或拒绝，不覆盖当前状态或重复推进 |
| F21 | 模型超时、非法 JSON、缺失条件项 | 有限修复；受影响项 unknown，无隐式 finish |
| F22 | 回看、修复持续消耗预算且无新证据 | 在共同预算内停止，记录未解决项 |
| F23 | 正常目标完成，但邻近物体被碰倒 | 有覆盖时报告非预期变化；局部裁剪限制显式记录 |
| F24 | 输出“夹力不足”但只有掉落图像 | 作为未证实假设或删除，不能成为确定根因 |
| F25 | 同一帧、CoF 文本和 P 派生事实重复输入 | 保留共同 lineage，不视为三份独立证据 |
| F26 | 改任务文本/期望、保持帧不变 | 事实证据不应随期望幻变；偏差比较可随合法查询改变 |

单元测试用固定候选输出检查程序规则；感知测试用真实或明确标注的仿真帧及独立人工/传感器标注。不能用“schema 通过”声称 F02/F07 等视觉能力已经达标。

## 13. 数据与训练路线

### 13.1 先建立机器人领域评测集

借鉴 AHA 的 FailGen 思路，从成功轨迹进行受控扰动：抓取偏移、夹爪动作缺失、搬运中失持、错误对象/放置区域、执行无进展。另加入自然策略失败、正常释放、恢复成功以及相机遮挡/缺帧等信息不足样例，避免所有异常都来自一种易识别的人工扰动。

每条至少标注对象身份、真实可见事件及区间、段末条件、支持/反对帧、可见性与未知项。模拟器真值可用于独立评分或生成候选标注，但模型输入中不可泄漏；若真实失败在观测里不可辨认，正确视觉回答应是 unknown，而不是强迫模型猜真值。

按任务/场景/对象和基础轨迹划分训练验证测试；同一成功轨迹的扰动变体不能随机散落到不同划分。测试集包含未见失败组合与视角，报告分布外表现。自然语言原因标注需区分观察与推测，避免训练幻觉式根因解释。

### 13.2 是否微调

先对比通用 VLM 的直接描述、帧关联提示和可用 CoF 模型；查清错误来自帧选择、对象关联、几何判断、事件识别还是下游规则，再决定训练。

若训练，监督重点为帧关联事实、事件区间、简短证据摘要、条件证据及 unknown；不把长篇推理文本长度当成质量指标。CoF 原论文使用专门数据微调并研究帧引用；本项目的机器人契约、事件类型和证据协议属于适配扩展，需要单独评测。

## 14. 实验设计与指标

### 14.1 对照与消融

| 组别 | 配置 | 回答的问题 |
| --- | --- | --- |
| B0 | 原 Cap-X 两帧差异反馈 | 保留原基线 |
| B1 | 原 Cap-X 视频文字反馈 | 已有多帧描述能做到什么 |
| B2 | 相同选定帧、相同模型，普通文字 vs 带引用的结构化反馈 | 输出结构/帧关联是否有收益 |
| B3 | B2 加明确 API 时间对齐 | 调用上下文能否减少阶段误判 |
| B4 | B3 加有界区间回看和 unknown 请求 | 自适应证据获取是否改善检测与恢复 |
| B5（后续） | 固定 harness 和输入，替换领域微调模型 | 感知训练带来的独立收益 |

控制模型、帧分辨率、任务/种子、Planner、Executor、API、允许传感器和总预算。结构消融使用相同事实/帧，不能让实验组偷偷多看中间帧或真值。自适应回看会增加成本，同时提供相同最大预算与实际成本曲线，不声称它免费。

unknown 策略要与采样/格式分别消融；更保守可能降低误成功但增加停止率，不能只报告其有利指标。原生视频内部采样不透明时，注明 B1 输入不完全等价，并增加显式相同帧的受控基线。

### 14.2 指标

- 条件支持/反对的准确率、误成功率、误失败率，以及 unknown 比例；报告选择性风险—覆盖关系，避免全报 unknown 获得虚假高可靠性。
- 事件类型 precision/recall/F1、时间定位区间误差或 temporal IoU；标注区间本身模糊时说明容差，不奖励无意义的整段大区间。
- 引用存在率与证据支持率分别测：合法帧 ID 不等于该帧支持结论；后者用独立标注/人工复核。
- 无证据事件率、无依据根因率、对象身份错误、时序颠倒和相机运动误判率。
- 闭环任务成功率、错误继续率、恢复率、无效重复动作与补观察次数。
- 模型调用/token、图像输入总量、回看次数、端到端延迟、存储与实际控制步数。

在线评测只能使用决策时已获得的证据；离线分析使用完整后续视频时必须单独报告，不能把未来信息的检测效果算作在线能力。置信度阈值和未知规则在验证集固定，测试集不重新调参。

## 15. 文献依据与采用边界

以下依据此前已阅读的论文方法部分或作者项目页；本文 schema、目录、预算与行为规则为项目设计，不是引用工作的原实现。

| 工作 | 核对来源 | 借鉴内容 | 不直接照搬/不能据此声称 |
| --- | --- | --- | --- |
| Chain-of-Frames，CVPR 2026 | [论文 v2 §3、§4、§6](https://arxiv.org/html/2506.00318v2)，[代码](https://github.com/SaraGhazanfari/CoF) | 显式 Frame 引用、帧关联训练数据与单阶段视频推理 | 原论文包含微调，不是仅 prompt；通用视频评测不证明机器人反馈可靠。其采样/编号假设需匹配，本文 API 对齐和区间回看是额外适配 |
| REFLECT，CoRL 2023 | [论文 §3、§5](https://arxiv.org/html/2306.15724#S3)，[作者项目页](https://robot-reflect.github.io/) | 感知/事件/子目标分层摘要；先检查结果，再检索过程定位失败 | 本项目长期状态/历史仍归 P；不重复构建其完整场景图。其仿真实验使用部分感知真值且有环境假设，不直接移植成功率 |
| AHA，ICLR 2025 | [作者项目页](https://aha-vlm.github.io/)，[代码](https://github.com/NVlabs/AHA) | FailGen 扰动轨迹、失败数据与自然语言失败解释 | 失败解释可借鉴，不意味着已有本文帧证据协议；合成失败需补自然失败/未知与领域评测 |
| Guardian | [作者项目页：数据与方法](https://www.di.ens.fr/willow/research/guardian/)，[论文入口](https://arxiv.org/abs/2512.01946) | 多视角、子任务前后比较、规划与执行失败数据 | 其前后观测验证不能替代完整过程事件定位；同样会误判，不作为可靠性保证 |

这里的 frame 指视频观测帧，不是机器人 TF 坐标系链；与世界模型生成未来帧也不同。未来预测可以辅助规划，不能成为“执行已完成”的证据。

## 16. 联调前确认项

1. 与执行侧确认帧真实采集时钟、边界/长调用内采样、call_id 和后段观察窗口的成本。
2. 与 P 确认对象映射、prior/observed 标记、候选事实融合、证据 lineage 和迟到信息处理。
3. 与 Verifier 确认第一批谓词、at_end/maintained/stable 等时间语义及否定条件规则。
4. 固定模型实际抽帧/多图协议，验证局部编号可映射回原始帧，无法映射时明确降级。
5. 固定正常、未抬升、先抬后掉、正常释放和遮挡的五条 fixture，再开展真实模型识别测试。
6. 保持原 Cap-X 两帧与视频反馈可运行，所有新增反馈标注模型/输入/策略版本和成本。

首个可交付版本应做到：**从一个动作段的真实帧链中输出有证据的变化、事件、偏差和未知项，让 P 能更新事实、Verifier 能检查条件、Coding Agent 能据此继续，而不是只得到一句“执行失败”。**
