# 机器人 Coding Agent：经验学习、技能复用与自进化开发文档

版本：v0.1 · 日期：2026-09-24 · 状态：待实现的开发设计

适用代码基线：`capgym/cap-x`，提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`。

总入口：[开发总览与使用指南](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/robot-coding-agent-development-guide.md)。

配套文档：[任务规划与进度管理](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/task-planning-progress-dev.md)、[代码生成与执行](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/code-generation-execution-dev.md)、[CoF 执行反馈](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/cof-execution-feedback-dev.md)、[闭环控制与验证](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/closed-loop-verification-dev.md)、[联调与评测](/Users/agiuser/Documents/Codex/2026-09-24/ca/outputs/integration-evaluation-dev.md)。

本文是第六部分，在在线闭环内提供经验/技能检索与复用，在任务间提供候选学习、技能发布与受控的 Agent 自改进流程。新增类型、目录、配置和验收用例均为拟议设计，不表示已改造代码、发布技能或获得实验收益。首版基础模型权重保持冻结。

## 1. 目标、层次与首版范围

目标：让已产生的执行证据改善后续任务，减少重复生成程序和无效恢复，同时保留每次选择、执行与更新的来源和版本。

### 1.1 三个层次

| 层次 | 学习对象 | 产物 | 验证重点 |
| --- | --- | --- | --- |
| L1 经验复用 | 历史案例、失败模式、恢复尝试 | 可检索经验与有适用范围的经验规则 | 检索相关性、证据支持、后续决策收益 |
| L2 技能积累 | 参数化程序、分段操作流程及组合 | 带契约和版本的技能资产 | 实际效果、可迁移性、依赖与成本 |
| L3 Agent 自改进 | 检索、代码生成、规划/恢复等策略实现 | 新 Agent 配置或代码版本及谱系 | 新旧版本任务表现、回归、继续改进能力 |

RSI 在此指 Recursive Self-Improvement。L1/L2 属于持续经验学习与能力积累；L3 中只有当改进后的 Agent 继续参与生成、实现后续改进时，才进一步讨论递归性质。固定元 Agent 不断搜索下游配置可称自动化 Agent 设计，不据此宣称完整 RSI。

### 1.2 第一版交付

1. 执行经验包：保存实际执行及效果，保留成功、失败、未知和有效局部片段。
2. 经验检索：接入 P 的历史/上下文能力，为规划、代码生成和恢复提供相关材料。
3. 技能资产库：参数化技能、精确依赖、分段检查点、证据和不可变版本。
4. 候选验证与发布：候选进入隔离验证，通过门槛后加入后续 episode 的目录。
5. 两种评测：学习后冻结的迁移测试，以及按时间推进的持续学习实验。

L3 接口和评测边界在本文预留，默认关闭。自动课程、跨机器人迁移和模型权重训练是后续扩展，不是首版验收条件。

### 1.3 开发顺序与启用条件

第六部分无需等待前五部分全部实现。经验记录、检索和固定技能复用可以随基础闭环开发，自动发布与自改进依据各自依赖逐步启用。

| 能力 | 可以开始的开发工作 | 影响真实任务前的要求 |
| --- | --- | --- |
| 经验记录与索引 | 与执行事件、P 接口一起定义 schema，用 fixture 测试来源/时间关联 | 实际数据保留真实调用、效果与 unknown；不会自动转成成功经验 |
| 固定经验/技能复用 | 接检索、参数绑定和目录适配，用已有获准资产联调 | 固定来源与版本，满足当前前提和原执行门控；不要求先完成全量评测 |
| 候选抽取与验证工具 | 使用记录数据或 fixture 开发抽取、参数化、依赖分析与评测调度 | 新程序的实际效果须另行验证；生成候选不自动获得执行/发布资格 |
| 自动发布、持续更新、L3 | 提前定义协议和接口，逐步验证各依赖 | 具备适用的效果验证、版本/撤销、反馈权限、回归与成本记录后启用 |

这里要求闭环能够如实记录成功、失败和未知、可靠处理执行停止，并检验候选效果；不要求先达到高任务成功率。第 16 节的 D1–D6 是本模块内部的交付路径，不是前五部分完成后的统一排队顺序。

## 2. 现有 Cap-X 基础与改造位置

以下行为已按基线源码核对，开发时按函数名重新定位。

| 位置 | 当前行为 | 需要补充 |
| --- | --- | --- |
| `capx/envs/trial.py:928` | `evolve_skill_library` 开启且 `task_completed` 为真时，从 final_code 抽取函数并保存 | 汇集实际执行版本和报告；成功/失败均可生成经验；抽取与发布分开 |
| `capx/skills/extractor.py` | 用正则提取顶层函数、签名、docstring | AST/依赖分析、实际调用关联、参数化和分段边界校验 |
| `capx/skills/library.py:66` | 按函数名合并，累计 occurrences；同名代码覆盖旧代码 | 技能身份与代码版本分离，保留谱系；不把不同实现累计成同一版本成功数 |
| `capx/skills/library.py:104` | 调用查询方法时可按出现次数自动晋升 | 查询只读；改用显式验证结果和发布事务 |
| `capx/skills/library.py:145` | 对晋升函数执行 namespace 注入 | 通过既有 worker/Gateway 加载已验证依赖；禁止绕过原执行契约 |
| `capx/skills/claude_integration.py` | 提供技能提示和 Python 格式化函数 | 由检索结果组装有限上下文，记录使用版本与选择理由 |
| `capx/utils/launch_utils.py` `_load_config()` | 显式构建运行配置 | 增加学习协议、目录版本和反馈权限的透传及启动检查 |

当前 occurrences 是“从代码中抽取到同名定义的次数”，不证明该函数被实际调用、造成成功效果或迁移到多个任务。`source_tasks` 可辅助追踪，但发布条件不能只依赖该列表长度。

当前 trial 位置调用的是抽取与保存；对相关 Python 调用位置的检查未看到提示格式化和 namespace 注入接成完整主循环。仓库中名称含 SkillLibrary 的人工 API 适配器，也不能等同于自动经验学习已经闭环。

旧 JSON 文件只作为迁移来源：导入为带原始来源的 legacy 候选，重新验证。不能把已有 promoted=true 直接转换成新系统已发布技能。迁移保留原文件；旧演化入口与新发布器不得同时写同一目录。

## 3. 分工与核心不变量

### 3.1 模块职责

| 模块/责任方 | 负责 | 不接管 |
| --- | --- | --- |
| P：状态、记忆与上下文 | 原始经验索引、历史检索、对象身份、当前状态、上下文组装 | 技能代码是否可执行及其发布权限 |
| ExperienceCompiler：Agent 侧 | 从证据包提出经验规则、技能候选，保留归因假设 | 新建另一份权威 WorldState 或任意改写历史事实 |
| SkillRegistry：执行侧 | 代码/契约/依赖版本、验证关联、发布目录和撤销记录 | TaskProgress、动作派发和在线预算 |
| Retriever：双方接口 | P 的经验检索与技能元数据检索，能力过滤、候选排序 | 宣告前置条件成立或直接执行技能 |
| Planner/Code Generator/Controller | 选择、绑定、组合、局部改写和恢复；沿用前四份流程 | 绕过发布门槛或重新定义任务成功 |
| LearningCoordinator：Agent 侧 | 学习清单、候选调度、验证任务、更新节奏与学习成本 | 直接操作机器人或修改在线进度 |
| 独立验证/评测器 | 冻结判据下的技能及 Agent 比较，产出可审计证据 | 把测试答案提供给在线决策或学习器 |

技能代码采用单一权威 Registry；P 可以索引其描述和版本引用，不复制一份可自行晋升的技能库。经验规则可存放在 P 的派生经验命名空间，但发布所用版本由同一 ReleaseManifest 固定。

### 3.2 不变量

1. 经验来自有来源的实际执行；模型反思、推断和预测保留类型，不变成已观察事实。
2. 历史经验只能指导当前决策，不能替代新鲜 StateView 和本次条件验证。
3. 整体成功不证明每个函数有效；整体失败不否定其中已有独立证据的有效片段。
4. 同一帧经 CoF/P 多次转述仍是同源证据；多条复制记录不增加独立支持样本数。
5. 代码、契约、依赖或适用范围改变，均生成新版本并重新验证受影响范围。
6. 发布目录在 episode 开始时固定；正常更新在任务间生效，不热替换在途技能。
7. 固定版本仍受撤销检查约束；发现不可执行/严重错误的版本可阻止后续派发，不能借版本固定继续使用。
8. 技能复用、技能内部调用、恢复和观察均经过原 Executor/Gateway、TaskProgress 和预算账本。
9. 学习器不能降低成功判据、改写评测器、清空失败记录，或解除执行状态未知锁。
10. 测试数据、隐藏真值和公开给学习器的反馈分开；学习协议明确何时允许更新。

## 4. 运行流程与接入时机

### 4.1 在线复用

```text
episode 初始化：固定 AgentRelease + SkillCatalog + ExperienceSnapshot
    ↓
TaskSpec / StateView / 当前进度
    ↓
Retriever → 经验案例、适用规则、技能候选与版本
    ↓
Planner 选择 / Code Generator 绑定或组合
    ↓
契约检查、最新前置条件、预算与撤销检查
    ↓
Executor 分段执行 → CoF → P 融合确认 → Verifier / Controller
    ↓
继续、观察、恢复、重规划或结束；写入原事件账本
```

检索调用位于初始规划、生成某段代码和恢复决策前。避免每个低层动作都重新检索；缓存键至少包含查询、目录/经验快照、策略版本和相关状态摘要。缓存命中也不复用旧的前置条件 pass。

### 4.2 任务间学习

```text
所有 episode 的实际执行证据与允许反馈
    ↓
构建 ExperienceRecord，按来源/时间/数据划分过滤
    ↓
归纳经验规则 / 抽取或改进技能 → 候选档案
    ↓
契约与依赖检查 → 固定 fixture → 独立仿真验证与回归
    ↓
合格候选 + 完整依赖 → 原子发布新 ReleaseManifest
    ↓
后续 episode 获取新版本；旧记录、失败候选和验证成本保留
```

可以在执行过程中异步归档证据，但首版等 episode 终止且执行状态已对账后再建立完整学习包。结果不完整的包仍可归档为 partial，不把未确认的动作或效果变成正面样本。

当前 episode 内已有的失败历史仍按前四份文档使用；本文的任务间更新指跨 episode 经验快照、已发布技能及 Agent 版本，不限制正常的段后修复。

## 5. 数据契约

### 5.1 复用既有类型

Condition 沿用 `{id, predicate, args, expected}`，列表语义为 AND。ExecutionReport.status 沿用 completed/error/timed_out/cancelled/outcome_unknown；VerificationReport 的 scope、phase、condition_groups 及逐条件 pass/fail/unknown 不变。

CoF 的 supported/refuted/unknown 是候选证据判断；TaskProgress 的 succeeded 是在线状态；EvaluationResult 的 pass/fail/unscorable 是独立结果。学习记录分别引用，不能把它们合并成一个含义不清的 success 字段。

经验、技能及发布类型采用独立 schema；需要扩展共享 SkillCatalog 时同步升级生产与消费方，不维护两套同名但含义不同的 SkillSpec。

### 5.2 ExperienceRecord

| 字段 | 含义 |
| --- | --- |
| experience_id / revision / schema_version | 不可变记录身份与修订 |
| provenance_ref | experiment/trial/run_attempt/episode、源轨迹和事件水位 |
| source_split / learning_scope / available_at | 数据划分、允许学习用途、实际可用时刻 |
| task_ref / context_ref | 冻结任务，以及 P 中当时状态/环境/对象角色的引用 |
| executed_artifacts | segment/execution/call 与实际 code_hash、skill_id/version 的映射 |
| execution_report_refs / feedback_refs / verification_refs | 原执行、CoF 与正式在线验证记录 |
| learning_feedback_ref | 经权限过滤的学习反馈包；没有则 null |
| observed_effect_refs / uncertainty_refs | 已观察效果、证据缺口和相互冲突项 |
| recovery_attempts | 真正执行过的恢复、结果和耗费；未执行方案单列 proposed |
| hypothesis_refs | 原因/经验假设及其支持、反例；不得覆盖观察 |
| completeness / lineage_refs | complete/partial，以及同源证据和原轨迹关联 |

原始数据不因新解释被覆盖。后续标签修订产生新 revision，并触发依赖该标签的经验规则/技能资格复核。历史记录包含当时看到什么；当前查询使用最新获准解释时也保留版本。

### 5.3 ExperienceRule

至少包含 rule_id/version、statement、applicability_spec_ref、supporting_experience_refs、counterexample_refs、uncertainty、validation_report_refs、content_hash、source_split 和 lineage。

规则是可撤回的决策建议，例如“在这类遮挡条件下先补观察再选择抓取”。未经验证的原因只能以假设形式进入上下文。规则不新增硬性任务条件，也不能覆盖固定 API 约束、Verifier 或用户任务要求。

### 5.4 SkillArtifact 与已有 SkillSpec 的关系

SkillArtifact 管理可复用程序资产；SkillSpec 仍表示执行侧注册 callable 的精确接口。一个技能资产可采用以下形式：

- callable：范围明确的辅助函数或单段程序；经执行侧校验注册为已有类别的 SkillSpec。
- procedure：跨段操作流程，展开为 Planner/Controller 管理的步骤和 SegmentContract；不伪装成可跳过检查的一次大函数调用。

| 字段 | 含义 |
| --- | --- |
| skill_id / version / display_name | 稳定语义身份、不可变版本与显示名称；不能只按函数名合并 |
| artifact_kind | callable 或 procedure |
| implementation_ref / content_hash | 代码或声明式流程内容及摘要 |
| interface_spec_ref | callable 的 SkillSpec；procedure 的参数及分段接口定义 |
| parameter_roles / binding_schema_ref | 物体、支撑物、机器人等角色的类型、坐标/单位与绑定规则 |
| contract_template_ref | 已注册谓词组成的前置、效果、继续/验收条件及阶段语义 |
| applicability_spec_ref | 后端/API/机器人、对象属性、场景范围及排除条件 |
| dependency_lock_ref | 精确技能/API/资源版本和传递依赖摘要 |
| checkpoint_spec_ref | 段边界、所需采样/验证、退出与取消语义 |
| provenance_refs / parent_versions | 实际执行来源、派生过程和父版本；新抽象不可冒用父版本统计 |
| validation_report_refs | 具体测试范围和结果，不能只存一个成功次数 |

模板中的参数角色采用显式绑定表，实例化后形成正常 Condition；不靠字符串替换自然语言或执行模型生成的判断式。procedure 的新节点必须通过现有 PlanPatch/计划提交规则，保留目标覆盖和恢复组归属。

SkillCatalog.skills 继续承载已注册 callable 的 SkillSpec。procedure 通过 RetrievalPacket 中的流程资产引用交给 Planner，展开后各段的 allowed_skills 只包含实际获准 callable；不能把流程名称直接注入旧 API 注册表。若未来统一成混合目录，必须显式升级共享 schema。

### 5.5 RetrievalRequest / RetrievalPacket / SkillBinding

请求包含 query_id、任务/子目标/失败模式、当前 StateView 引用、允许能力、目录/经验快照、数据权限、检索预算与用途。用途可为 planning/code_generation/recovery，不能据此改变已有控制动作枚举。

返回包含候选 ID/version、证据引用、匹配和排除理由、适用性检查结果、未满足/未知前提、返回截断及检索成本。空结果是合法结果，不强行选择“最相似”的技能。

SkillBinding 固定 skill_id/version、参数角色到当前对象/有效产物的映射、基于的 state_version、条件实例和依赖版本。它是待检查的绑定，不是执行许可；真正派发仍由原合同、进度与 Gateway 授权。

### 5.6 CandidateValidationReport / ReleaseManifest

CandidateValidationReport 至少含 report_id、candidate_id、candidate_kind、候选内容/依赖摘要、validation_profile_ref、适用范围、完整 trial 清单、逐运行结果、覆盖率、成本、回归差异、协议偏差和 decision。candidate_kind 为 skill/experience_rule/agent_variant，decision 为 qualified/rejected/inconclusive，仅表示在该验证协议下的发布资格，不是物理执行状态。

此类型与第二份文档的 ValidationReport（单次代码提案校验）、第四份的 VerificationReport（在线条件检查）分别定义。本文的 validation_report_refs 引用 CandidateValidationReport；代码校验或在线验证报告可作为分阶段证据引用，不能直接替代候选发布资格。

ReleaseManifest 固定 release_id、父版本、全部经验规则/技能版本与摘要、依赖锁、验证报告、适用 profile、发布事件水位和可见时间。SkillCatalog 保留原有顶层 catalog_version/skills；Registry 由发布清单为目标后端生成一致的 API/技能目录视图。

### 5.7 LearningRunSpec 与 AgentVariant

LearningRunSpec 定义 learning_run_id、协议/代码版本、初始发布版本、训练/验证/探针/测试清单、顺序、随机源、反馈权限、学习更新规则、候选生成/筛选上限、验证/发布规则和全程成本限制。

L3 的 AgentVariant 保存 variant_id、父版本、精确代码/prompt/检索配置、允许修改范围、生成者版本、提案与实现记录、全部评测及发布状态。候选是谁生成和实现的必须可追踪，才能区分固定元 Agent 搜索与改进后的 Agent 继续自改进。

## 6. 经验采集、归因与反馈权限

### 6.1 实际执行关联

从执行账本中的已准备/已派发代码 hash、调用轨迹与报告建立来源。final_code、聊天中的最后一版代码、成功任务里的所有函数定义均不足以替代实际调用记录。

区分定义、检索、选择、尝试调用、实际执行和效果验证六个事件。对有独立效果证据的子目标/动作段，可以从失败 episode 提取候选；结尾整体成功也不能自动给未调用函数记一次成功。

跨段技能的来源应保留完整阶段及修复历史。若抽取器重排步骤、删除失败片段或重新参数化，结果已是新的程序候选，必须重新实际验证，不能宣称原轨迹已经执行过它。

### 6.2 归纳与反例

先保存事实、再提出解释、最后验证适用性。对同任务成功/失败对比，要记录初始条件、动作、观测质量等差异；未经控制的相关性不直接解释为因果机制。

保留正常变化和反例，例如释放后不再 holding 是预期效果。经验规则要声明“在哪些条件下可能有效”，避免将一次失败泛化成“这种技能永远不能用”。无法定位原因时保留 unresolved。

### 6.3 学习信号路由

| 场景 | 可供学习的信息 | 更新时机 |
| --- | --- | --- |
| 学习集采集 | 声明的实际观测、执行结果、经验；协议可允许任务结果标签 | 对应 episode 结束且反馈实际到达后 |
| 候选验证/开发 | 按冻结规则提供的验证摘要与开发反馈 | 只进入开发/选择流程，不倒流到过去决策 |
| 冻结迁移测试 | 仅本 episode 正常在线反馈和固定的历史快照 | 不更新跨任务经验、目录或提示 |
| 持续学习序列 | 在时间 t 之前完成且已获准的反馈 | 先记录第 t 个任务成绩，再更新供后续任务使用 |
| 独立盲测/保留探针 | 在线正常观测；评分与标签留在评测侧 | 不用于学习或选版本 |

LearningFeedbackAdapter 只转发协议许可字段。允许学习集任务成功标签，不等于允许隐藏坐标、目标答案或未来扰动信息进入代码生成。若明确研究 oracle 学习信号，另设 profile，并区分学习阶段特权与部署阶段权限。

数据时间至少区分 observed_at 与 available_at；迟到结果只能影响其实际可用之后的更新。P 索引、日志、文件名、缓存和技能描述都执行相同的数据划分与权限过滤。

派生经验规则、技能代码及其依赖继承来源数据的使用限制。总结、改名、重新参数化或复制到新命名空间，不能消除测试来源标签；权限校验沿 lineage 和依赖闭包执行。

## 7. 检索、绑定与在线复用

### 7.1 先过滤，再排序，再验证

1. 依据固定目录、数据划分和可用时间过滤；排除未发布、已撤销或缺依赖版本。
2. 检查机器人/API、能力、对象类型和场景范围；有确定不适用证据的候选剔除，适用性未知的候选标记不可直接执行。
3. 按任务效果、失败模式、场景/对象关系和语义相关性排序；可结合验证覆盖与成本，不只按文字相似度。
4. P 在上下文预算内返回少量案例、规则和技能文档；保留选择/排除理由及原始引用。
5. Planner/生成器绑定当前对象和产物；Verifier 检查本次前提。unknown 交给 Controller 决定补观察、换方案或停止，不能由检索分数代替 pass。

当无可用技能时，沿用现有代码生成路径。检索失败不应导致默认执行不兼容技能；服务失败与“确实无匹配”分开记录。

### 7.2 程序复用约束

动态位姿、分割、抓取候选、IK 解和旧 env/对象句柄不作为跨 episode 的固定技能内容。它们每次从当前观测重建，或作为带有效期和来源的段间产物传入。

依赖固定到版本与内容 hash；避免可变 latest、递归循环、漏声明 helper/import/global。装载时的装饰器、默认参数、顶层语句也可能执行，应纳入既有静态审查和隔离 worker；AST 通过不等于语义效果通过。

Registry 只加载获准依赖，不将全部库无差别 exec 到原始环境 namespace。模型可见函数文档、运行时注册表和 Gateway 权限来自同一解析后的目录；查询技能不能触发晋升或物理动作。

### 7.3 分段技能与局部改写

多步骤 procedure 展开后仍按短段执行、等待 P 融合和条件验证。内部调用和恢复累计到原 episode/恢复组预算，不因封装成一次 skill 调用而重新计数。

生成器可以在当前任务内提出局部改写，交给既有代码审查/执行流程；该程序暂属本次候选代码，不能继承原 skill 的已验证版本身份。任务间学习时再决定是否形成新版本。

## 8. 技能抽取、抽象与组合

### 8.1 候选产生流程

1. 按子目标/段、实际代码版本和效果证据切分来源，保留调用与观测时间。
2. 检查是否存在可复用行为及明确输入输出；没有足够结构时先保留为经验案例。
3. 把具体对象绑定抽象为受类型约束的角色，把动态几何结果改为本次观察/规划输入。
4. 显式声明 helper、API、模型/资源版本、能力和阶段边界；形成 SkillArtifact。
5. 按规范化内容和语义角色检查重复；相同实现可共享内容，具有不同适用契约的候选仍分别验证。
6. 保存从来源到候选的变换记录、提出者模型/prompt 版本、成本和尚未证明的假设。

代码可导入、运行不报错、被多次抽到均不是效果验证。新候选不因来源整体成功而跳过第 9 节。

### 8.2 示例：从放置经历抽取 procedure

目标候选为 `place_on(object, support)`，适用前提包括当前确实夹持 object、support 已定位、放置范围兼容所选后端。

| 阶段 | 调用/生成内容 | 交回闭环时必须验证 |
| --- | --- | --- |
| 到达预放置位置 | 使用当前观测定位 support，规划并移动 | 仍夹持 object、到位/停止已确认、放置入口成立 |
| 释放与退开 | 在入口仍有效时执行放置、松爪和获准的退开 | 当前支撑关系、夹爪释放及冻结稳定窗口 |

若第一阶段掉落，不进入第二阶段；由 Controller 选择观察或恢复。需要重新抓取时采用另一个技能/子目标，并继承同一恢复目的的预算归属。已经释放后不再 holding 是正常变化，不能将所有历史中间条件永久作为最终条件。

如果只观察到整体最终成功而缺少中间执行关联，可以保留待验证候选，但不能声称两阶段都经过来源验证。外部验收目标仍由 TaskSpec 给定，技能抽取器不能把“稳定放置”改成“移动到上方”。

### 8.3 组合与版本依赖

组合技能依赖精确版本，验证参数传递、效果与下一阶段前提的兼容，以及 unknown/失败出口。组件分别通过不代表组合通过；物体关系、状态时效和累积误差可能在组合后改变。

首版禁止技能依赖环；限制展开深度、段数和上下文大小。重复使用同一 helper 的代码体只加载一次，但每次实际调用和耗费分别记录。升级基础技能后，依赖旧版本的已发布组合不自动改绑；需要生成并验证新的依赖闭包。

## 9. 候选验证与资格判定

### 9.1 验证阶段

| 阶段 | 检查内容 | 可以证明什么 |
| --- | --- | --- |
| V0 来源/静态检查 | 内容摘要、调用来源、数据权限、参数、AST、依赖、API/契约与段边界 | 候选具备进入运行验证的结构条件 |
| V1 固定 fixture | 已知输入/输出、状态不足、API 错误、取消、未知停止、预算与拒绝路径 | 协议与控制行为符合要求 |
| V2 独立环境运行 | 新初始条件、对象布局、正常与失败情形、作用范围边界 | 声明范围内的实际效果与证据覆盖 |
| V3 迁移与回归比较 | 与已有技能/生成路径配对比较，检查相关旧任务和成本 | 是否满足预先冻结的发布门槛 |
| V4 发布检查 | 结果完整、依赖闭包可解析、文档/调用一致、资格未失效 | 可形成用于后续 episode 的发布清单 |

纯计算 helper 可使用数值参考、边界输入和语义性质检查完成其适用的 V2；不能因此代替调用该 helper 的物理任务验证。机器人动作技能的 V2/V3 需实际仿真或对应硬件运行，mock 或原轨迹回放不能验证新动作效果。

### 9.2 判据与发布门槛

ValidationProfile 在候选运行前冻结：任务/场景清单、适用范围、独立成功/约束条件、预算、最少有效覆盖要求、不可评分上限、比较对象、成本界限及统计规则。不能由候选代码修改。

沿用第五份文档的 trial 登记、S/F/U 分母和缺失处理。资格判定至少要求：结构/协议条件通过、必要场景确已覆盖、结果完整对账、效果和回归指标达到冻结标准。缺数据或统计证据不足时 inconclusive，不按“没有观察到失败”认定 qualified。

性能门槛可以使用预设绝对要求，或与基线的配对差异及容许回归范围；选择哪一种、容许多少和区间方法都要预先声明。本文不规定未经数据支持的通用“成功两次即可上线”阈值。

首个技能没有历史版本时，比较对象可为原有生成/执行路径或指定参考脚本，并注明其能力/信息差异。更改发布门槛后产生新 ValidationProfile；旧资格不自动适用。

### 9.3 防止筛选过拟合

抽取来源、开发反馈、发布验证和最终盲测的职责分开。反复根据某验证集结果修改候选，实质上已在适应该集合；不能再把该集合分数当独立测试证据。

为候选生成、重试与选版本设置统一预算，记录全部被拒绝候选和评测次数。保留未参与选择的最终测试/探针，发布研究结果时注明搜索花费与选择规则，避免只报告最好一次试验。

可以用分级筛选节约成本，但提前规定升级规则，报告各阶段样本覆盖；小样本筛选成绩不作为正式整体性能结论。

### 9.4 经验规则的验证

ExperienceRule 不需要满足 callable 的代码装载测试，但必须通过来源/权限、支持证据、反例和适用范围检查。跨任务概括需要相应范围的证据，单次案例可以作为局部经验保留，不能伪装成普遍规则。

评估规则收益时，在相同固定上下文和任务分布下比较加入/移除规则的决策及任务表现，并计入额外 token 和检索成本。规则被发布只说明满足其验证协议，不提升为硬性条件或免除当前事实验证；修订与撤销使用同一发布机制。

## 10. 版本、发布、撤销与一致性

### 10.1 候选状态与发布状态分开

候选工作流使用 draft/validating/qualified/rejected/inconclusive。qualified 只对特定内容、依赖与 ValidationProfile 有效。新增验证保留旧报告；代码、契约或数据来源实质改变后创建新候选版本，不能原地改掉历史结果。

是否可被运行目录使用由 ReleaseManifest 决定，不能查询一次就把候选晋升。未通过的候选可留在研究档案中用于归纳或后续改进，但不进入在线可执行目录。撤销使用追加的 revocation 记录，撤销原因与范围可单独追踪。

### 10.2 原子发布

1. 发布器检查合格报告、内容/依赖摘要、权限、适用范围与撤销记录。
2. 准备不可变经验快照、技能文件、依赖及文档；确认全部可解析。
3. 持久保存待发布 ReleaseManifest 和准备事件，此时不改变当前可见指针。
4. P 的索引和执行侧注册表返回同一 release_id 的就绪确认；部分准备失败不激活半份目录。
5. 重新核对资格和撤销状态，在原子事务中写入发布事件并以 compare-and-swap 更新目标 profile 的当前发布指针。
6. 新 episode 固定同一个 release_id、catalog_version、经验快照及 Agent 版本，写入运行 manifest。

并发发布冲突时重新核对基准版本并生成新的组合清单，不能静默 last-writer-wins。发生崩溃时按发布事件对账；指针只能指向完整可用的清单。

既有 allowed_skills 可继续使用已冻结目录内的名称，但 prepared execution 必须同时保存解析后的 skill_id/version/hash。相同 callable 名称冲突在建目录时拒绝，不能依赖 Python 注入顺序选中某个实现。

### 10.3 撤销与回退

撤销某版本时检查传递依赖，阻止依赖它的后续派发。在途动作按 Executor 的取消/停止协议处理，禁止热换函数体或假设 Python 退出即机器人已停止。

默认在下一个 episode 选择此前可用的发布版本。当前任务如已有固定目录内的获准替代路径，可由 Controller 在停止和状态对账后重新规划；否则明确阻塞/终止。任何回退都不撤销已发生的物理效果、不恢复旧坐标、不清空预算。

发布回退只改变后续所选软件/经验版本。原失败轨迹、费用和撤销原因保持可查；P 更新当前事实，不把软件回退理解为世界状态回退。

## 11. L3：Agent 自改进与 RSI 扩展

### 11.1 首批允许的改进对象

可以先限定为检索排序、经验压缩、技能抽取、规划/代码提示、错误处理建议和恢复选择策略。每项变更都形成精确代码或配置 diff，不使用含义不明的“自动优化全部系统”开关。

固定接口、验收判据、原 TaskSpec、执行 Gateway、取消/停止、预算及数据权限作为外部约束。候选策略仍经现有审查、契约与评测路径执行。L3 不直接修改运行中的 Agent 或机器人控制进程。

如果后续确需改共享 schema、底层能力或验证机制，应作为独立工程变更升级依赖和验证协议，不能混入某次策略候选的普通发布。

### 11.2 改进循环

```text
开发轨迹与被允许的评测摘要
    ↓
选择父 AgentVariant → 提出一个可检验的改进假设
    ↓
生成/实现候选 diff，保存提出者与实现者版本
    ↓
隔离检查与协议回归 → 独立任务评测 → 记录完整成本
    ↓
候选归档 / 合格版本发布
    ↓
后续任务，以及由获准的新版本继续参与后续改进
```

保存候选谱系可允许从多个父版本探索；实验档案容纳暂时无性能提升但有研究价值的变体，运行发布依然执行门槛。这两类集合分别管理。

第一阶段可以采用固定改进器，验证自动化策略搜索是否有效。第二阶段再让更新后的代码能力参与后续候选实现，并与固定改进器在相同总预算、初始条件和任务暴露下比较。

### 11.3 递归性与效果的证据

至少记录各代谁分析反馈、谁写代码、谁完成修改、谁被评测，以及实际发布版本。任务分数上升说明下游改进，不单独证明自改进能力上升；还需比较修改成功率、产生有效后代的比例、所需成本和跨代持续性。

固定搜索调度器、冻结基础模型与有限允许改动范围如实记录。即使出现多代改善，也不宣称无限自进化、必然加速或自主改进了未开放的系统部分。

### 11.4 自动课程与权重训练

后续 CurriculumScheduler 可以依据已声明能力缺口选择学习任务，仍受场景可行性、预算及数据划分约束。报告学习任务分布，使用共同保留任务评估，避免只选择容易任务就获得更高学习成绩。

模型权重训练另建数据/训练/模型发布流程，并固定模型版本与训练成本。RoboCat 可作为机器人自生成数据用于后续训练的参考，但该路线不包含在本次 L1/L2 的实现承诺中。

## 12. 学习实验设计

### 12.1 协议 A：学习后冻结的迁移测试

在 learning 集采集经验和生成候选，用 development/validation 选发布版本，再冻结经验、技能、提示和模型，在 held-out test 上运行。测试内保留正常任务历史与恢复，禁止跨任务更新已冻结资产。

划分依据包括原始轨迹、任务族、对象、布局和机器人配置；声称哪一种未见泛化，就隔离对应分组。相同任务换种子不是未见任务泛化，近似代码副本和相邻轨迹也需按来源分组。

第五份文档要求主评测冻结技能库，在此协议下继续完全适用。

### 12.2 协议 B：先评分、后更新的持续学习

每个 learning_run 从同一指定初始发布版本开始，冻结任务流/顺序或可比较的课程规则。第 t 个任务开始时固定版本 v_t，用它执行并先记录成绩；任务结束、允许反馈实际可用且更新/验证完成后，才能将新版本供后续任务使用。

序列内允许过去任务经验保留；序列之间隔离经验与技能资产。若并行运行导致某些结果尚未到达，新任务只能使用当时已发布的版本，记录因果时间和更新水位；首版优先串行序列。

任务流中的分数衡量在线学习过程。另在预设检查点克隆只读发布快照运行保留探针，测迁移与旧能力；探针 episode 不回写学习状态。用于调参/选版本的探针应标为 development，不能同时声称是盲测。

独立统计单位优先采用完整学习序列/初始种子；同一序列内的 episode 相互依赖，不能视为大量独立样本计算狭窄区间。

### 12.3 主对照与归因

| 组别 | 相对完整的前五部分系统新增能力 | 用途 |
| --- | --- | --- |
| A | 无跨任务经验；保留原有底层 API/固定技能 | 基础闭环对照 |
| B | 增加文字经验/案例检索 | 经验层的增量 |
| C | 在 B 上加入验证过的学习技能，测试期固定 | 在经验层上加入程序技能的增量 |
| D | 与 C 相同初始资产，允许协议 B 的任务间更新 | 持续更新的增量 |

B/C 的初始学习来源与预算可比，记录经验压缩和技能验证所增加的实际成本；不能某组使用更多专家演示或隐藏反馈而不说明。D 的冻结对照是采用相同初始目录和任务流、关闭更新的 C。

A→B→C 是递增消融，不能独立估计经验与技能的交互；需要拆分时增加“仅技能、无文字经验”组形成 2×2。L3 再单独比较固定改进器与可更新改进器，避免把所有变化归为 RSI。

对离线比较，可以共享同一获准经验集；对在线学习，各组动作不同，得到的后续经验也会不同，固定外部任务分布和预算并如实记录这一差异。若要只比较学习算法，另做相同日志数据的实验，结果与在线闭环分开。

### 12.4 机制消融

可分别检验经验检索、经验规则、失败案例、前提过滤、参数化、技能组合和独立发布验证。取消发布验证等研究变体仅在相应隔离仿真协议下比较，并保留原执行生命周期/停止机制。

研究课程选择时固定评价任务和总交互预算；研究技能增长时固定检索上下文预算，报告库变大带来的检索延迟和错误选择，不能把库大小本身作为能力分数。

## 13. 指标、成本与异常处理

### 13.1 结果指标

沿用第五份文档的登记分母：N=S+F+U，认证完成率 S/N，评分覆盖率 (S+F)/N；不可评分仍保留，不能静默删除。零分母显示 NA，连续运行未完成则注明 pending。

| 指标 | 定义与限制 |
| --- | --- |
| 迁移完成率 | 冻结版本在明确保留任务集的 S/N，按任务/对象/布局分层 |
| 学习曲线 | 按任务序号与累计学习成本分别绘制完成率/探针表现，使用相同窗口/检查点 |
| 技能使用覆盖率 | 实际调用过已发布学习技能的 episode 数 / 全部登记 episode 数；检索/选择覆盖另报 |
| 调用效果达成率 | 实际执行且被验证目标效果成立的技能调用数 / 全部实际执行调用数，另报效果评分覆盖率 |
| 无效复用 | 不适用选择、绑定拒绝、调用后失败分别计数；按选择/派发等对应分母报告 |
| 候选产出与发布 | 所有提案、有效候选、qualified、实际发布、撤销的数量与原因；数量不等于性能 |
| 旧能力变化 | 相同保留旧任务在各快照上的配对成绩变化；负向变化作为遗忘/回归信号 |
| 新版本收益 | 配对完成率、错误声明/继续、恢复和成本差异；不只比较成功调用的耗时 |
| L3 改进能力 | 修改实现成功率、后代发布率、达到预定性能的总成本和代数 |

任务成功不能全部归因于被调用技能。复用的因果贡献需要固定条件的移除/替换对照；“使用过技能的任务更成功”可能来自任务难度或选择偏差。

旧能力的检测探针若不断反馈给学习器，就成为开发任务，必须另留最终测试来评估泛化。曲线区间按独立学习序列重采样，保留各组配对；不得以同轨迹帧数增加有效样本数。

### 13.2 成本账本

完整成本包含：经验采集、检索/压缩、候选生成、全部验证/失败试验、版本搜索、正式评测及存储/推理开销。分别报告在线执行成本、离线学习成本和二者合计；技能复用节省的生成次数不能抵消未计入的学习费用。

LearningCoordinator 管理学习任务的预算分配和调度清单；每个验证/学习 episode 的真实调用仍由原执行账本计量。聚合通过唯一 usage_event_id 关联并去重，不复制第二份可被覆盖的在线预算。

新建 episode 有其独立在线预算，但整个 learning_run 的累计预算不随候选、版本、重试或任务数清零。发布前冻结学习总预算和终止规则，所有被拒候选费用仍计入。

### 13.3 缺失、重跑与反馈修订

预登记每个学习 episode、候选验证任务和正式评测 trial；崩溃、无返回及基础设施失败保留记录。重跑规则沿用第五份文档，保留首次运行和所有费用，不能挑较好结果冒充一次验证。

尚未收到结果时标 pending；证据缺失而无确定失败依据时为 unscorable。未完成批次不发布完整实验结论。标签修订先记录 lineage，再重新计算相关资格；必要时撤销依赖错误证据的规则/技能。

发布服务、P 索引或检索暂不可用时，可继续使用已经固定且完整可解析、未撤销的目录；否则在动作前拒绝或走现有获准生成路径。不得临时读取任意其他实验的技能文件以凑齐依赖。

## 14. 公共接口、目录与接入顺序

### 14.1 接口草案

以下仅定义边界，不是已实现程序。类型对应第 5 节，物理执行和真实评测复用前五部分。

```python
def build_experience(episode_record, evidence_resolver, feedback_policy) -> "ExperienceRecord":
    """关联实际执行与允许反馈；保留缺失、来源和可用时间。"""
    raise NotImplementedError


def retrieve_reuse_context(request, p_reader, registry_reader) -> "RetrievalPacket":
    """在固定快照中先过滤再排序，返回候选、理由、未知和成本。"""
    raise NotImplementedError


def propose_skill(experiences, catalog, generation_profile) -> "SkillCandidate":
    """抽取或改进程序，生成不可变内容、契约、依赖与变换记录。"""
    raise NotImplementedError


def prepare_skill_binding(candidate, state, contract, frozen_release) -> "SkillBinding":
    """类型化绑定参数和条件；返回待验证绑定，不授权派发。"""
    raise NotImplementedError


def validate_candidate(candidate, profile, evaluation_runner) -> "CandidateValidationReport":
    """登记并执行适用的验证阶段，汇总全部试验及发布资格。"""
    raise NotImplementedError


def publish_release(manifest, expected_parent, registry) -> "ReleaseManifest":
    """校验报告和完整依赖后，原子发布；父版本冲突则拒绝。"""
    raise NotImplementedError


def revoke_artifact(artifact_version, reason, evidence_refs, registry) -> "RevocationRecord":
    """追加撤销及依赖失效记录，供既有执行门控检查。"""
    raise NotImplementedError


def run_learning_cycle(spec, available_experiences, coordinator) -> "LearningCycleReport":
    """在总预算和反馈权限内生成、验证、归档并尝试发布候选。"""
    raise NotImplementedError


def propose_agent_variant(parent, development_feedback, modification_profile) -> "AgentVariant":
    """L3 可选：生成限定范围的候选变更和谱系，不修改运行中实例。"""
    raise NotImplementedError
```

其中 SkillCandidate 是候选 ID、SkillArtifact、创建事件和工作流状态的封装；资格与发布状态仍按第 10 节分开。所有操作带请求 ID 并幂等：重复消息不能重复增加成功数、发布版本或验证运行。

### 14.2 建议目录

以下为相对 Cap-X 仓库根目录的拟议布局。原 SkillLibrary 迁移为兼容适配器，状态、执行和评测模块继续复用原实现。

```text
capx/skills/
  contracts.py            # SkillArtifact、Binding、候选与发布类型
  registry.py             # 不可变资产、依赖、发布与撤销
  extraction.py           # AST、来源关联、参数化候选
  binding.py              # 类型化对象/产物绑定与条件实例化
  loading.py              # 受控装载，与执行侧注册表/Gateway 对接
  legacy_adapter.py       # 旧 JSON 导入为未验证候选

capx/agents/stateful/learning/
  contracts.py            # 经验、检索、学习协议和版本谱系
  experience.py           # 执行包到经验，保留原始证据引用
  p_adapter.py            # P 历史、派生规则及快照接口
  retrieval.py            # 权限/能力过滤与候选排序
  feedback_policy.py      # 学习/开发/测试反馈路由
  coordinator.py          # 学习清单、更新节奏和任务调度
  improvement.py          # 可选 L3 候选提案接口

capx/evaluation/stateful/
  skill_validation.py     # 复用既有 Runner/Evaluator 验证技能
  continual_protocol.py   # 顺序学习、只读探针与序列聚合

tests/experience_skills/
  fixtures/
  test_provenance.py
  test_retrieval_binding.py
  test_skill_validation.py
  test_release_recovery.py
  test_learning_protocol.py
```

### 14.3 接入步骤

1. 先把原 trial 的抽取入口改接经验构建请求；数据写入独立 learning_run 命名空间，旧自动抽取默认不再写发布目录。
2. 与 P 对齐 ExperienceRecord、派生经验规则、可用时间、数据分组及固定快照读取；保留原 WorldState/TaskProgress 写入权限。
3. 建立 SkillArtifact 与旧 SkillSpec 的适配，完成依赖解析和唯一名称检查；已有手工 API 作为基础目录版本保留。
4. 在规划/生成/恢复前接检索；prepared execution 记录绑定后的精确内容，Executor 继续实施执行与停止门控。
5. 复用第五部分的任务注册、扰动、独立评分和对账，先实现候选验证，再开放发布。
6. 配置加载器显式支持 learning 字段并拒绝未知键；初始化时校验运行协议、数据权限、完整目录和预算引用。
7. 先完成单 worker 的学习后冻结流程，再开放串行持续学习；最后单独启用 L3 实验。

步骤 1–4 的记录、资产适配和固定检索可与基础闭环同步开发。步骤 5 的验证能力是自动发布的前提，步骤 7 是学习运行模式的开放顺序，不要求先完成前五份文档的全部功能或正式实验。

## 15. 配置与数据示例

### 15.1 首版配置草案

以下所有 fixture 引用均为示意标识，尚无对应可运行配置或实验产物。这里展示候选验证/发布能力具备后的 learn_then_freeze 模式，不是早期联调必须开启的默认模式。候选数量是开发示例，不是验证充分性的门槛；启动时必须由实现后的解析器解析并冻结所有引用。

```yaml
agent_mode: stateful
learning:
  schema_version: "0.1"
  learning_run_id: "fixture_learning_001"
  mode: "learn_then_freeze"
  protocol_ref: "fixture:learning-protocol-v1"
  initial_release_ref: "fixture:base-release-v1"
  model_manifest_ref: "fixture:frozen-models-v1"
  source_manifest_ref: "fixture:learning-episodes-v1"
  development_manifest_ref: "fixture:development-scenarios-v1"
  validation_manifest_ref: "fixture:release-validation-v1"
  test_manifest_ref: "fixture:held-out-test-v1"
  blind_probe_manifest_ref: null
  seed_manifest_ref: "fixture:learning-seeds-v1"
  feedback_policy_ref: "fixture:observations-and-authorized-train-outcomes-v1"
  budget_profile_ref: "fixture:whole-learning-run-budget-v1"
  online_experiment_ref: "fixture:integration-experiment-v1"
  experience:
    p_namespace: "fixture_learning_001"
    snapshot_policy: "pin_at_episode_start"
    include_failed_episodes: true
    preserve_unknown: true
  retrieval:
    profile_ref: "fixture:filter-then-rank-v1"
    max_experiences: 3
    max_skill_candidates: 3
    context_budget_ref: "fixture:reuse-context-budget-v1"
  candidates:
    generation_profile_ref: "fixture:parameterize-and-preserve-checkpoints-v1"
    max_proposals_per_cycle: 4
    validation_profile_ref: "fixture:independent-skill-validation-v1"
  publication:
    boundary: "between_episodes"
    require_complete_dependency_lock: true
    require_qualified_validation: true
    revocation_check: "before_dispatch"
  rsi:
    enabled: false
    modification_profile_ref: null
```

mode 计划支持 off/frozen_reuse/learn_then_freeze/continual_update。off 关闭本模块的跨任务复用与学习，保留原底层 API/手工技能；frozen_reuse 只读取指定资产；其余两种分别对应第 12 节协议 A/B。切换 continual_update 还必须提供任务流、检查点、反馈到达和独立序列重置规则，不能只改一个字符串即视为协议完整。

早期可用 off 运行原闭环并保留原始日志，或用 frozen_reuse 验证固定资产检索。frozen_reuse 不启动候选生成、自动晋升或发布；即使配置中存在相关字段，加载器也必须按模式禁用写入路径。固定资产的内容摘要和来源仍需记录，不能用“关闭学习”代替输入权限检查。

在线预算从 online_experiment_ref 解析并继续由原账本实施；budget_profile_ref 是整个学习过程的总量约束，两者不能互相重置。max_experiences/max_skill_candidates 限制上下文数量，不授权忽略必需状态、契约或执行反馈。

### 15.2 当前任务的 SkillBinding 示例

以下绑定假设候选来自一个已解析发布清单；它是开发 fixture，不表示实际技能已经发布。全部对象 ID 和证据引用均为示例。绑定后仍需根据最新证据检查前提。

```json
{
  "schema_version": "0.1",
  "binding_id": "fixture_binding_place_01",
  "episode_id": "fixture_episode_12",
  "skill_id": "place_on",
  "version": "0.2.0",
  "artifact_kind": "procedure",
  "release_id": "fixture_release_03",
  "based_on_state_version": 17,
  "role_bindings": {
    "robot": "robot",
    "object": "red_cube",
    "support": "green_cube"
  },
  "preconditions": [
    {
      "id": "binding_holding_red",
      "predicate": "holding",
      "args": ["robot", "red_cube"],
      "expected": true
    },
    {
      "id": "binding_support_pose_known",
      "predicate": "pose_known",
      "args": ["green_cube"],
      "expected": true
    }
  ],
  "contract_template_ref": "fixture:place-on-contract-template-v2",
  "dependency_lock_ref": "fixture:release03-dependency-lock",
  "current_artifact_refs": ["fixture:green-cube-observation-at-state17"],
  "checkpoint_spec_ref": "fixture:preplace-and-release-checkpoints-v1",
  "retrieval_packet_ref": "fixture:retrieval-packet-12"
}
```

其中 preconditions 是模板实例化后的条件子集；完整可执行条件还由所引用的契约、适用范围和阶段入口规定。这里不预先填写 VerificationReport.pass，也不把状态版本 17 当作永远有效。若 green_cube 移动或物体掉落，需要刷新状态和绑定。

### 15.3 统计口径 fixture

构造 10 条已对账测试 episode：6 pass、3 fail、1 unscorable。4 条实际执行过学习技能，其中 2 pass、1 fail、1 unscorable。则整体认证完成率为 60%，评分覆盖率为 90%，技能使用覆盖率为 40%；复用子集中有证据完成比例为 2/4=50%，不能以 2/3 替代并隐藏不可评分。

再构造 5 次真实技能调用：3 次目标效果 pass、1 fail、1 unknown，调用效果达成率为 3/5=60%，效果评分覆盖率为 4/5=80%。episode 数与调用数不同，不能把两者分母混用。上述例子也不能证明技能导致成功率变化，因果贡献需单独对照。

## 16. 开发里程碑与验收

### 16.1 开发顺序

| 阶段 | 交付 | 与整体开发的关系 | 通过条件 |
| --- | --- | --- | --- |
| D1 来源与契约 | 经验包、执行 hash 关联、P 快照和学习反馈路由 | 随共享 schema 与日志从前期建设 | 能区分定义/实际调用/效果；缺失和数据权限用例通过 |
| D2 检索与固定复用 | 固定手工技能目录、检索、绑定、分段执行接入 | 随规划/执行/控制联调，不等待正式全量评测 | 相似但不适用技能被拒；正常与 unknown/停止路径可追溯 |
| D3 候选与验证 | 参数化抽取、依赖检查、独立验证和版本记录 | 工具可以提前开发，物理效果资格依赖对应实际验证 | 新候选按完整清单评测，不以来源成功直接发布 |
| D4 发布与迁移 | 原子目录、撤销/回退、冻结迁移协议 | 相关验证和发布依赖通过后开放 | 发布故障可恢复；任务间版本生效；完成实际迁移试验 |
| D5 持续学习 | 因果更新、独立序列、探针和成本报告 | 固定版本可比较且学习协议已冻结后开放 | 无未来反馈/探针泄漏；按完整序列聚合 |
| D6 可选 L3 | 限定策略 diff、候选谱系、固定与可更新改进器比较 | 单独研究，不阻塞固定复用或 L1/L2 | 不修改固定外部约束；完整记录改进成本与新旧表现 |

D1/D2 的纯逻辑 fixture 不要求 GPU；D3/D4 的物理技能效果须在适用后端实际运行。D6 不阻塞首版 L1/L2 交付，也不能只因生成了 diff 就标记 RSI 效果通过。

### 16.2 行为验收矩阵

以下为待实现、待执行要求，不是本次文档编写已通过的机器人测试。

| ID | 场景 | 预期行为 |
| --- | --- | --- |
| S01 | 成功任务中定义了未调用函数 | 可作待验证候选，不记为已成功执行 |
| S02 | final_code 与实际执行版本不同 | 学习关联真实执行 hash，保留编辑历史 |
| S03 | 整体失败但某子目标效果已验证 | 可提取有证据的局部候选；整体结果仍失败 |
| S04 | 缺帧、outcome_unknown 或反思猜测 | 保留未知/假设，不能计作正面效果证据 |
| S05 | 同源帧经 CoF/P 或复制经验多次出现 | 保留 lineage，独立支持数不增加 |
| S06 | 同名函数代码已改变 | 新版本独立验证，不继承旧版本成功次数 |
| S07 | 旧 promoted=true 的 JSON 导入 | 只成为 legacy 候选，不直接可执行 |
| S08 | 语义相似但机器人/API 不兼容 | 检索过滤或绑定拒绝，记录原因 |
| S09 | 前置条件未知或对象身份冲突 | 不授权动作，由既有 Controller 决定下一步 |
| S10 | 对象移动，旧缓存仍返回相同技能 | 重新验证/绑定，不沿用旧 pass 或坐标 |
| S11 | 缺依赖、版本冲突、循环或名字冲突 | 发布/装载前拒绝，不按注入顺序兜底 |
| S12 | decorator/默认参数/隐藏 global 有副作用 | 纳入装载审查和隔离，不能绕过 Gateway |
| S13 | 多段技能搬运后掉落 | 在检查点退出，禁止继续释放/错误成功 |
| S14 | 技能内部捕获错误或重试 | 执行锁/总预算仍有效，不隐藏失败和调用数 |
| S15 | 复用后局部修改代码 | 标成新候选内容，不冒用已验证版本 |
| S16 | 候选只在原轨迹回放中看似成功 | 不认定新动作效果通过，要求适用的实际验证 |
| S17 | 验证样本缺失、异常退出或覆盖不足 | 清单保留，资格为 inconclusive/不发布 |
| S18 | 组件各自通过但组合入口不匹配 | 组合验证失败，不能自动继承整体资格 |
| S19 | 并发发布、进程崩溃或 P 索引未就绪 | 只暴露完整一致清单，冲突/恢复可对账 |
| S20 | episode 中正常发布新版本 | 当前固定版本不变，后续 episode 再采用 |
| S21 | 固定版本被撤销或基础依赖失效 | 阻止后续派发，处理在途停止并传播依赖影响 |
| S22 | 回退到旧软件版本 | 不回滚物理状态，不清空历史和预算 |
| S23 | 检索/发布请求重复投递 | 幂等，不重复计成功、启动验证或发布 |
| S24 | 冻结测试期间出现可学习新经验 | 归档隔离，不更新下一测试任务可见资产 |
| S25 | 持续学习反馈迟到或序列并发 | 仅影响实际到达后更新，保留版本/时间水位 |
| S26 | 保留探针结果进入 P/规则/提示 | 拒绝泄漏；若用于调参，重标为开发数据 |
| S27 | 不同完整学习序列复用隐式全局库 | 隔离命名空间，除协议明确初始资产外不共享 |
| S28 | 未授权真值通过标签、缓存或技能描述传播 | 反馈/数据策略阻止，保留违规记录 |
| S29 | 候选验证不断重跑、换 ID 或被拒绝 | 费用与清单仍累计，不能挑最好一次或重置总量 |
| S30 | L3 修改评分器、停止锁或测试清单 | 超出允许变更范围，候选拒绝 |
| S31 | 固定元 Agent 产生多代下游配置 | 标明自动化搜索，不据此记为递归自改进 |
| S32 | 第 15.3 节统计及零使用/零调用样例 | 分母/覆盖率正确；零分母 NA，不混合调用与 episode |

### 16.3 首个 Demo

保留五类成套轨迹：经验检索改善恢复；参数化技能迁移到新布局；相似技能因前提不符被拒；候选验证失败不发布；新版本发布后正常采用或撤销回退。

每套包含原始执行/观测、P 与 CoF 引用、检索包、绑定、实际使用版本、验证结果、发布或拒绝事件及费用。分别注明纯逻辑、轨迹回放、仿真与正式评测实际执行了哪些层级。

## 17. 参考工作与采用边界

以下依据前一轮方案讨论中已核对的论文摘要、方法正文和作者材料。接口、发布事务、权限路由和统计协议是本项目设计，不是对论文的原样复现。

| 工作 | 来源 | 本文采用的思路 | 适用边界 |
| --- | --- | --- | --- |
| Voyager，2023 | [论文及技能库方法](https://arxiv.org/html/2305.16291v2) | 可执行代码技能库、描述检索、反馈改进与技能组合；后续参考自动课程 | Minecraft 场景，需另补机器人分段契约、物理证据和依赖验证 |
| Reflexion，2023 | [论文](https://arxiv.org/abs/2303.11366) | 把任务反馈转换为文字反思并保存在经验记忆中 | 反思建议不自动成为真实原因或已验证技能；不需要权重更新 |
| ExpeL，AAAI 2024 | [论文方法](https://arxiv.org/html/2308.10144v3) | 成功/失败经历比较、跨任务经验抽象和成功轨迹检索 | 适合 L1；本文额外区分因果假设、证据时效和物理可执行条件 |
| ADAS，2024；2025 修订 | [论文](https://arxiv.org/abs/2408.08435) | 用代码表示 Agent，由元 Agent 搜索提示、工具使用与工作流 | 适合限定策略搜索；固定元 Agent 与更新后继续自改进分别评测 |
| Darwin Gödel Machine，2025；2026 修订 | [论文方法](https://arxiv.org/html/2505.22954v3) | 自修改代码、版本档案、候选评测和由新版本继续参与改进 | 论文侧重冻结基础模型下的编码 Agent，搜索过程仍有固定部分；不直接推断机器人能力或无限改进 |
| RoboCat，2023 | [论文](https://arxiv.org/abs/2306.11706) | 使用机器人策略生成经验，加入后续训练迭代 | 属于后续策略模型/数据训练路线，不包含在首版代码技能复用承诺中 |

首版重点参考 Voyager 的程序技能积累与 ExpeL 的跨任务经验学习；L3 再引入 ADAS/DGM 的候选搜索与谱系验证。各论文报告的收益不能直接作为本项目的预期数值。

## 18. 产物清单与首次协作对齐

每个 learning_run 保存解析后的协议、初始资产、任务/数据划分、允许反馈包、经验版本、全部候选及变换记录、实际验证清单、发布/撤销事件、探针结果、Agent 谱系与累计成本。

学习资产目录与评测私有数据按能力隔离；仅使用不同文件夹不足以阻止通过工具或缓存泄漏。运行报告包含数据暴露范围、学习预算、版本序列、S/F/U、缺失/协议偏差、迁移与回归结果，不只展示最终最优版本。

首次联调先对齐：

1. P 的经验索引、派生规则、快照读取和可用时间接口；Registry 的代码、契约与发布权限。
2. 实际执行 hash/call 与效果报告的关联；失败/未知、局部成功和反思假设的分别表示。
3. 手工技能与学习技能的统一目录适配、procedure 展开、版本绑定和撤销检查。
4. 一个任务族的参数角色、适用范围、独立成功条件、候选验证及冻结迁移清单。
5. 静态测试和持续学习各自的数据/反馈规则、更新边界、完整序列重置与全部成本。

本次交付仅为开发设计；代码实现、数据采集、技能发布、仿真与 RSI 效果均待后续实际完成和验收。
