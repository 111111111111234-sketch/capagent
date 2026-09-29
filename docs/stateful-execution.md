# 第一阶段：基础接口与执行骨架

实现日期：2026-09-28。当前交付是固定脚本驱动的执行骨架，已完成本地逻辑验收；真实机器人、完整仿真和模型能力尚未验收。

后续进展：已在同一执行账本上加入[第二阶段任务规划与进度管理](stateful-planning.md)。本文保留独立执行模式的范围；规划模式另提供 attempt/恢复组预算、验证进度和局部修订。

第三阶段已新增[模型提案接口与独立代码进程](stateful-models.md)，并完成离线测试。本文的固定脚本演示仍使用原来的可信进程内模式；真实模型及真实机器人效果尚未验收。

## 现在能做什么

以“抓住红块并抬升”为首个任务，跑通：

```text
冻结任务 + 当前状态 + 固定脚本
    → 校验任务/状态/API/代码
    → 持久登记执行请求
    → 调用已注册 API，记录调用与观测
    → 确认执行/停止状态
    → 保存执行报告，独立评分 mock 结果
```

主要内容：

- 统一 TaskSpec、StateView、ExecutionRequest、ExecutionReport、SkillCatalog 和 FrameManifest；采用严格 Pydantic 模型，可导出 JSON Schema。
- 每段代码绑定 episode、任务/计划版本、子目标、attempt、segment、execution 和输入状态版本。第一阶段的这些关联值来自固定 fixture，尚无 Planner。
- 从实际 callable 注册表生成 API 目录；调用经过权限、参数签名、次数和时间检查。每段使用全新变量空间。
- 保存固定任务、请求、原始代码及 hash、API 参数/返回摘要、异常、标准输出、状态快照和观测帧索引。
- 使用 SQLite 持久事件账本。派发先登记，相同请求去重，不同内容不能复用同一 ID；未结束记录会阻止新动作。
- 提供段级/episode API 次数限制、episode 执行次数限制、取消信号、停止确认和显式对账。
- 提供正常、空抓、部分执行后异常、超时、停止未知、取消、观测缺失七个可复现 mock 场景。

`completed` 仅表示程序正常返回且后端已确认空闲。任务是否达成由独立证据判断；`RESULT` 自述不会更新进度。演示中的 `evaluation.json` 是 mock 真值评分，不是在线 Verifier，也不会写入 TaskProgress。

## 运行

在仓库根目录使用 Python 3.10–3.12，安装现有依赖 `pydantic>=2.7,<3` 即可运行这个子模块，无需安装整个 GPU/仿真依赖栈。测试使用标准库 unittest。

```bash
python -m capx.agents.stateful demo --scenario all --output outputs/stateful-demo
python -m capx.agents.stateful replay outputs/stateful-demo/missed_grasp
python -m capx.agents.stateful schema --output outputs/stateful-schemas
python -m unittest discover -s tests/stateful -v
```

`python` 必须指向上述版本并可导入 Pydantic。当前开发机器已验证的解释器为：

```text
/Users/agiuser/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
```

如果没有配置 `python`，可用该绝对路径替换命令开头。系统自带的 Python 3.9 不适用。上述 CLI 的 `demo --scenario all` 成功退出表示已产生测试轨迹，包含预期的 error/unknown；不代表七次机器人任务都成功。

演示需要空输出目录，避免把新建的 mock 世界混入旧记录。再次查看用 `replay`，重新演示换一个输出目录。也可以用 `--script /absolute/path/script.py` 执行自己编写的可信固定脚本。

支持的脚本是顺序赋值、直接调用已启用 API、基本 JSON 容器/索引和 print。第一版不支持 import、属性访问、循环、函数定义、异常处理或任意 NumPy 程序；这些需后续 worker 实现。

## 演示结果应如何理解

| 场景 | 执行状态 | mock 条件评分 | 含义 |
| --- | --- | --- | --- |
| normal | completed | pass | 程序正常结束，mock 夹持/抬升条件成立 |
| missed_grasp | completed | fail | API 正常返回，但未夹住红块 |
| partial_error | error | pass | 先产生抬升效果，再发生异常；异常记录不能被成功效果覆盖 |
| timed_out | timed_out | pass | 注入超时前 mock 条件已成立，停止得到确认；仍记录超时 |
| stop_unknown | outcome_unknown | unscorable | 停止未确认，阻止新动作，不接受场景成功判断 |
| cancelled | cancelled | pass | 注入取消前已产生效果，停止已确认；取消不会回滚环境 |
| observation_missing | completed | unscorable | 程序正常返回，执行后无法取得观测；不会用旧状态填充执行后状态 |

后三个故障场景是确定性故障注入，不是对真实控制器超时或停止能力的测量。另有测试验证实际取消信号和时间检查点。

## 产物与回放

每个场景单独保存：

```text
task.json / backend.json / catalog.json  冻结任务、能力与 API 目录
events.sqlite3                          唯一权威执行账本
events.jsonl                            方便阅读的事件导出
summary.json / evaluation.json          演示摘要与独立 mock 评分
executions/lift-1/
  request.json / code.py / report.json
  stdout.txt / stderr.txt / result.json
  state-*.json / frame-*.ppm / frames.json
```

事件按递增 seq 排列，含 UTC 时间、execution_id 和内容。API 调用的 call_id 在对应 execution 内唯一；帧记录携带相同 execution/call 关联。报告里的 `event:N` 指向当前账本的第 N 条事件；其他产物路径相对运行目录。mock 状态中的 `mock-sensor-vN` 是合成数据来源标识，实际快照保存在 state 文件中。

SQLite 事务和文件 fsync 保证先记录再动作；JSONL/报告文件是便于查看的导出物。中断后以 SQLite 为准，回放只读记录，不构造环境、不执行运动。

如果派发已登记而报告缺失，执行状态为 outcome_unknown，不自动重发。对仍存在的原后端会话调用 `Executor.reconcile(execution_id)`，确认停止并重新取状态；即便已确认停止，无法重建的 Python 结果仍不会被记成 completed。换一个新建/重置的后端会话不能接管旧账本。

## 如何连接已有 Cap-X

新增入口 `CodeExecutionEnvBase.api_functions()` 暴露当前启用的 API 注册表；`CapXBackend.from_env(...)` 复用这些 callable。旧 `_exec_user_code`、`env.step` 和原 CLI 行为保持原样。新模块有自己的 CLI，尚未加入原 `launch.py` 的 `agent_mode` 分支。

接入已有仿真环境时，由环境所有者提供：

1. 已初始化的 env 和 P/状态提供器。
2. 当前后端真实的 `motion_state()`、`stop()` 确认实现，返回 idle/running/unknown。
3. 每个已启用 API 的参数与操作范围校验函数。
4. 后端 ID、API 目录版本和会话标识。

适配器不会调用会注入原始 env/APIS 的旧代码执行入口。代码只获得 API 包装函数；原 API 的返回类型不变。API、状态读取和渲染都在原调用线程执行。此适配器已用替身 callable 验证接口，尚未通过 Robosuite 实际运行验收。

第一版依赖调用者独占后端，并为一个后端会话使用唯一运行目录；目录级文件锁防止多个写入者。它不提供跨目录、跨机器的设备租约。取消方法可以被其他线程发信号，执行与 SQLite 写入则应在创建 EventStore 的同一线程调用。

## 当前边界与下一步

- 当前是可信固定脚本的进程内模式，非不可信代码隔离环境；真实模型代码接入前需实现 worker/gateway 分离与资源限制。
- fake 后端支持合作检查点；Cap-X 旧阻塞 API 只能在调用前后检查时间与取消，不能承诺调用中途停止。能力文件明确将其标为不支持合作中断。
- 观测采集位于执行前、每次 API 结束和执行后；尚无长调用内部周期采样，不足以推断所有中间事件。
- 当前预算为段/episode 的 API 次数、episode 执行次数，以及合作式段时限。被拒绝 API 请求也累计；尚无控制步数、attempt 或恢复组账本，这些随后续规划/控制接入。
- 入口条件只做版本、UTC 时效、事实/来源及三值检查；P 的对象融合、完整谓词目录和线上 Verifier 尚未实现。
- mock 的关节目标仅用于离散状态转换，没有物理几何意义。PPM 帧为合成 fixture，不是机器人观测。

下一步可以在这些接口上实现任务规划与进度管理：由 Planner 提供当前子目标和执行请求，由独立验证结果推进进度。代码生成、真实 CoF、完整 P 和真实仿真联调继续按后续阶段推进。
