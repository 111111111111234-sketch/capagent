# 第三阶段：模型生成计划和代码

2026-09-28：已接入模型提案接口、有限修正、工作进程与端到端测试。按本轮要求，没有调用真实模型服务。离线演示使用预设 JSON 回复和合成机器人状态；通过测试不代表模型规划质量或真实机器人成功率已验证。

后续进展：[第四阶段](stateful-closed-loop.md)已接上 CoF、P 处理确认和完整闭环；本页保留第三阶段的独立入口与历史测试范围。

## 当前流程

```text
任务 + 当前状态 + API/谓词目录
  → 模型生成初始计划 → 程序校验并提交
  → 选择子目标 → 模型生成当前动作段
  → 校验结构、状态版本、条件、代码、动作边界和输入
  → 独立代码进程 → 父进程 API 网关 → 后端
  → 原有条件验证 → 更新进度
  → 下一段 / 重试 / 模型局部修订 / 最终验证 / 有原因地停止
```

模型只能提出 `PlanProposal`、`PlanPatch` 和 `CodeProposal`，不能填写执行 ID、提高预算或改写进度。任务成功仍必须来自最终条件验证。代码正常返回、打印 SUCCESS 或写入 RESULT 都不能直接完成任务。

## 运行与配置

Python 3.10–3.12、Pydantic 2，无新增第三方依赖。以下命令在仓库根目录运行，输出目录必须为空。

先运行离线演示：

```bash
python -m capx.agents.stateful model-test-demo --scenario all --output outputs/model-test-demo
python -m capx.agents.stateful replay outputs/model-test-demo/drop_recovery
python -m unittest discover -s tests/stateful -v
```

当前机器可将 `python` 替换为 `/Users/agiuser/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`。

未来接入真实模型时，复制 [配置示例](stateful-models.example.json)，填写真实模型名和完整 completion 地址。接口沿用仓库已有的 chat-completions 消息格式，不自动选择模型、改写路由或调用旧客户端的无限重试。密钥只写入本地环境变量，配置只保存变量名；本地无鉴权服务可把 `api_key_env` 设为 null。

```bash
python -m capx.agents.stateful model-run --config model-config.json --output outputs/model-run
```

`model-run` 会发起真实网络请求，**本轮未运行**。这个命令当前仍使用合成 stacking 后端。失败或预算耗尽时返回退出码 1 并保存停止原因，不会退回固定计划或假定成功。原有 `demo`、`plan-demo`、`replay`、`schema` 继续可用。

根据服务能力选择 `max_tokens` 或 `max_completion_tokens`；只有服务明确支持时才打开 `json_object_mode`。`temperature: null` 表示不发送该参数。远程地址要求 HTTPS；HTTP 仅用于 localhost/回环地址。拒绝在 URL 中放凭据、查询参数或片段。

## 失败修正与预算

- 格式、schema、依赖/目标覆盖、非法代码、缺失输入、动作边界或状态版本不合格时，默认最多修正两次。下一次请求包含实际校验错误和刷新后的上下文。尚未派发的错误提案不增加动作尝试次数。
- 每次 HTTP 请求都在发出之前登记并预留输出 token 额度，包括瞬时错误后的重试。默认最多两次 HTTP 尝试；仅网络错误及 408/429/500/502/503/504 可重试。认证错误、重定向、空回复、截断、拒绝或 tool-call 回复明确停止。
- 每次请求有独立 HTTP 工作进程，默认 30 秒、最多可配置 60 秒。父进程超时后杀死并回收该进程，覆盖 DNS、TLS、响应头和响应体阻塞。关闭重定向，保留用户的代理和 CA 环境配置。密钥不进入命令行或日志。
- 默认总调用上限 32、预留输出额度 65536 token、上下文 128 KiB、回复 128 KiB、控制循环 100 步。可通过 `model-run --limits limits.json` 传入 `ModelLimits`，其 schema 可由 `schema` 命令导出。
- `reserved_output_tokens` 是按每次请求最大输出额度累计的保守预算，不是实际消耗。服务报告的输入/输出 token 单独累计；缺失用量和未知请求单独计数。离线预设回复不伪造 token 用量。
- 已派发动作的运行错误不会触发自动重放。继续沿用第二阶段的取消、预算、验证和停止策略；后端停止状态不明时阻止后续动作，等待显式对账。

## 代码进程与动作范围

模型运行强制使用 `execution_mode="process"`。子进程只解释受限 AST，不执行任意 Python；每段拥有新变量空间和临时目录，不继承凭据环境。JSON RPC 只向父进程请求已启用 API。环境对象、账本和机器人调用留在父进程原线程，保持渲染/模拟器线程归属。

支持赋值、JSON 字面量、列表/字典、索引、负数、直接 API 调用和受限 print；不支持 import、属性访问、循环、函数定义或异常处理。限制代码、AST、RPC、输出和结果大小；序列化会提前拒绝过大的结构，避免共享列表引起指数级 JSON 展开。子进程有 CPU 上限；Linux 另有地址空间限制，macOS 依赖结构/输出限制和父进程时限。父进程网关继续管理 API 权限、调用次数、取消、执行事件和停止确认。

这提供进程分离与受限语言，**不是完整 OS 沙箱，也没有证明任意 Python 或真实机器人操作安全**。杀死代码进程不证明机器人已经停止。阻塞的后端 API 仍需支持协作式期限、取消和可靠停止，不能靠代码进程代替控制器看门狗。

当前 CLI 的可信输入提供器提供合成关节目标；合成阶段校验器限制抓取/短抬升、搬运、释放的允许调用序列，拒绝把三者串成一个没有中间验证的片段，也拒绝未知关节目标。这些合成数值不能用于真实机器人。真实几何、运动到达判定、长调用采样、P/CoF 与仿真适配仍待后续实现和验收。

## 接口与日志

`models/` 新增配置、传输、生成器、运行循环和显式离线 fixture。`PlanningManager.prepare_segment` 可只做校验；`execute_segment` 在正式派发前再次校验。通用 `run_model_loop(manager, client, inputs_provider, segment_validator)` 要求调用方提供可信输入和适合自身后端的动作段校验器。

提示词保存版本号和输出 schema；每次请求包含当前状态、计划/进度、预算使用情况和有大小上限的近期反馈。完整历史不无限拼入 prompt。模型提案仍需通过已有的目标覆盖、依赖、历史不可改和恢复预算继承规则。

所有模型、执行与验证事件进入同一个 SQLite 账本：

```text
events.sqlite3 / events.jsonl  权威事件 / 可读导出
models/model-N/request.json   脱敏请求、提示词版本、修正序号、预留额度
models/model-N/response.json  原始模型回复（有体积上限并脱敏）
models/model-N/result.json    服务报告用量、延迟
models/model-N/error.json     错误原因、延迟（失败时）
models/summary.json           终止状态、动作和模型调用计数
planning/                    计划版本、进度、验证状态快照
executions/                  冻结请求、代码、结果、逐 API 事件与合成帧
```

模型提案接受/拒绝事件带有关联的 model call ID。HTTP 错误响应体和鉴权头不写日志；服务输出中回显当前密钥的内容会被替换。计划生成前失败也保留 `ModelRunFinished`，可用 replay 查询。模型额度从账本恢复，未知请求不退还预留额度；CLI 本轮只启动新运行，恢复真实后端会话需要明确适配，不能新建一个 mock 冒充旧会话。

## 已验证范围

共 119 项测试通过：前两阶段 64 项回归，新增 55 项覆盖传输格式/鉴权/有限重试/总超时/预算、严格 JSON、计划与代码修正、状态变化、恢复预算、动作边界、工作进程 RPC/取消/输出限制、未知停止和防止模型自报成功。

离线演示共八种：正常、首次空抓、搬运掉落、非法代码修正、持续无效回复、目标初始已满足、恢复预算耗尽、停止状态未知。真实模型质量与真实机器人效果未测。
