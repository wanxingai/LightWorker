# LightWorker 0.5.0 — Next Runtime

本版一次接入统一运行时、持久 Job、可持续子 Agent、工具 Provider、执行策略、上下文压缩、证据工件、终端/LSP、调度和插件；不按“编码/分析”划分任务。模型运行时仍只有 LightAgent，没有引入 Agentica、Codex 或 DeepSeek Harness 运行时依赖。

## 运行时与版本

- 最低 LightAgent **0.11**，依赖范围 `>=0.11,<0.16`。相邻源码现有实际版本为 0.11；旧规划中的 0.15 里程碑不当作实际包版本。
- 默认 `agentic` 动态循环；可选 `workflow`、`code`、`minimal`、`ralph`。这是执行策略，不是领域分类。
- 保持原有 API 和 CLI；默认空目录，原仓库不挂载到容器。

## 唯一事实源

```text
LightAgent SQLite Session
  ├── 模型轮次、工具事件、Goal、审批、工作记忆、运行控制
  ├── 持久 Job、子 Agent 消息、工作流/代码检查点
  ├── 来源 Evidence、Artifact 哈希、插件锁和定时任务
  └── 投影：WebUI / SSE / JSON 兼容缓存 / Session 导出与 replay
```

`AtomicSessionStore` 保持 LightAgent 公共事件格式，事务使用 `BEGIN IMMEDIATE`。不同进程的事件追加、状态变更和队列领取串行提交；旧 Session 对象保存时只合并新事件，不删除已持久事件。支持顺序 CAS、幂等键和事务回滚。已有 JSON 状态首次读取迁移；存在 Session 投影时，损坏/删除的 JSON 不会覆盖真实状态。

Task Session ID 等于 run ID。对话消息队列仍使用 `lightworker-conversation-<root_run_id>`，子 Agent 使用 `<run_id>-child-<agent_id>`；模型历史不会因使用同一对话队列被重复注入。

## 持久任务与 Job

主任务、子 Agent 和后台终端都有持久描述符。Job 保存类型、输入、状态、owner、process ID、租约、递增 epoch、输出游标、结果和错误。

- 事务领取，执行期间定期续租。
- 只有当前 owner/epoch 且租约有效的执行器能发布输出或完成结果。
- 暂停/取消撤销 epoch；旧线程不能发布迟到结果。父任务控制状态也在每个安全边界检查。
- 服务重启或租约过期后标记 interrupted，不谎报任务成功；父任务恢复后重新绑定能力 Provider。
- 子 Agent 从独立 Session 和待处理消息恢复；固定流程跳过已完成步骤。
- 终端恢复会重新启动 argv，并不是恢复原 OS 进程或任意程序栈；命令可能产生重复副作用，需用户审阅后恢复。
- 外部 HTTP 操作等无法保证网络侧 exactly-once。系统保证领取/发布隔离，不宣称能撤回已发送的网络请求。

任务关闭时撤销仍活动的子 Job 并解绑沙箱相关 handler，不能通过过期对象重新执行已销毁的容器。冷恢复时先恢复父任务，再恢复其 Job Provider。

## 可持续子 Agent

`spawn_agent` 返回 agent ID 和 job ID；`send_message` 按 FIFO 追加后续轮次；`list_agents`、`interrupt_agent`、`resume_agent` 用于观察和控制。

内置 `spawn` 和 `fork` Provider。fork 继承模型历史，但不继承父任务审批、控制状态或后台 Job；权限快照只包含父级已授权的只读工具。模型请求使用全局 semaphore，子任务不会通过并行增加越权工具。写入继续由 Supervisor 负责。

Python 扩展可实现 `SubagentProvider(factory=...)` 并调用 `register_provider`，factory 提供 `specialist(role, allowed_tools=...)` 与 Agent `run(...)` 接口。现有实现没有默认连接外部 Codex/ACP 服务；外部服务必须另行实现适配 factory、配置凭据并遵守权限边界。不会仅凭服务名称自动获得宿主机 Shell 或登录态。

## Capability Provider 与工具加载

工具通过 LightAgent `CapabilityRegistry` 组合，按类别分组。只允许经过 `ToolCatalog` 包装的函数注册，避免绕过精确参数审批。Registry 负责沙箱边界，ToolCatalog 负责预算、取消检查、风险策略、审批、结果限长和审计。

`tool_search` 根据用途/工具名/类别返回 schema 并激活工具；下一次模型请求呈现新增 schema。隐藏 schema 是降低 Token 成本的措施，**不是**授权机制，工具实际权限始终由策略控制。

普通读写、Web、Browser、Memory、Skills、MCP、RAG 能力沿用已有实现。新增加：持久 Agent/Job、工作流、Docker 代码桥、终端、只读 LSP 和定时任务。

## 五种执行策略

| 策略 | 行为 |
|---|---|
| agentic | 默认动态循环，按需发现能力，支持跨领域混合任务 |
| workflow | 保留固定 LightFlow，并使用 SessionFlowStore 持久步骤 |
| code | 动态循环可调用 `run_code`，在 Docker 内执行有界 Python 工具编排 |
| minimal | 只提供小型仓库/Shell 工具集，无自动新增研究工具 |
| ralph | 动态执行后按验收命令进行有限轮次修复，不削弱测试 |

`workflow_run(workflow_id, steps)` 可在动态任务中调用固定能力步骤；同一 ID 的步骤内容不可修改。完成结果按 step ID 持久化。

`run_code(source)` 的 Python 子集提供 `call('tool_name', {...})`、基本计算和有限循环；不开放 import、属性访问、裸文件/网络调用、函数定义和无限 while。每个子调用仍经过原审批与工具策略；完成结果持久化用于重放，不重复执行已经完成的子调用。若进程在可能有副作用的调用中断开，保留不确定检查点并要求审查，不自动重放该调用。该限制语言不是宿主机安全沙箱，真正隔离仍依赖 Docker。

Docker 不可用时只读降级；不会把 Code Mode、Shell、terminal、LSP server 或 Skill 脚本放到宿主机执行。

## 上下文、记忆和证据

自动压缩保留系统指令、最新用户输入、完整 assistant-tool 组及 Goal/验收、审批哈希/状态、实时引导、工作记忆、来源索引、后台 Job 状态。长工具输出保存在日志工件，压缩前上下文保存在脱敏 checkpoint。必需内容自身超过预算时显式失败，不静默丢弃审批或拆散工具调用组。

WorkingMemory、Workspace Memory、用户/项目/嵌套 AGENTS.md、Markdown Skills 和 FTS5 RAG 沿用已有实现。Embedding 为可选项，默认不要求模型服务支持 embedding API。

`EvidenceStore` 在工具输出限长之前保存完整来源正文，记录 `E1/E2/...`、URL/RAG citation、内容哈希、访问时间及 search_lead/retrieved 类型；搜索摘要不会升级为已核实正文。用户提供的资料仍不能冒充独立检索证据。

`ArtifactRegistry` 保存工件路径、类型、长度、内容哈希、上一版本哈希和 evidence ID。只有实际文件变更才展示 diff。引用标注支持 `[E1]`、`[1]` 和已访问的 Markdown 来源链接；点击可以查看摘要、已捕获正文和链接。

## 终端和 LSP

`terminal_start(argv)` 经精确审批后在 Docker 中启动进程，作为 Job 提供增量输出；`terminal_send(job_id,text)` 写入 owned stdin。暂停/取消清理容器里的进程组。当前是管道 stdin/stdout，不是完整交互式 PTY/TUI；不持久化 OS 进程到容器之外。

`code_symbols(path)` 使用文件 AST 返回符号/语法诊断；`lsp_request(argv,method,params)` 在 Docker 中启动 JSON-RPC LSP server，支持初始化后的只读查询。需要在镜像里提供对应 server 并配置 allowed_programs；不在宿主机安装/运行 server，不支持任意 LSP 写命令。

## 调度与插件

本机 Web 服务运行期间处理一次性和间隔定时任务（最小间隔 60 秒）。schedule occurrence 对应确定性 run ID；租约 epoch 阻止过期 dispatcher 重复提交完成状态。服务离线期间不执行任务，重启后继续检查到期任务；来源对话仍活动时延迟执行。没有后台系统 daemon 或默认启用 cron shell。

模型调用 `schedule_create` 必须审批；用户在 UI/API 创建则是直接明确操作。可暂停、恢复或软删除，记录保留在 Session。

插件为声明式 `plugin.json`，允许 `name/version/description/skills/mcp/workflows`。不加载宿主机 Python/JS 插件代码。整个目录逐文件哈希后由用户锁定，任何文件变化重新禁用，拒绝 symlink 和目录逃逸。MCP/Workflow 使用插件命名空间。配置受信任 Ed25519 公钥后签名为强制项；未安装签名依赖会 fail closed。

```yaml
runtime:
  mode: agentic
  deferred_tools: true
  ralph_rounds: 3
  background_wait_seconds: 900
  workflow_presets: {}
plugins:
  directories: [~/.lightworker/plugins]
  trusted_public_keys: []
```

```json
{"name":"research-kit","version":"1.0","skills":["skills"],"mcp":{},"workflows":{"inspect":[{"id":"status","tool":"git_status","arguments":{}}]}}
```

启用签名：`uv sync --extra signing`。signature.json 使用 `public_key`、`signature` 字段（base64）；签名载荷为 UI/inspect 返回的十六进制 `digest` 字符串的 UTF-8 字节。该签名文件不参与目录 digest，但仍必须匹配配置公钥和当前 digest。

## Web/API

WebUI 保留执行中逐行显示、成功后收起过程、耗时/最终 Markdown、hover 操作、侧栏折叠、排队和审批弹窗。新增后台 Job/子 Agent 面板、输出、子 Agent 追问、工件/轨迹，以及运行资源中的指标、调度和插件信任入口。Agentic 耗时累加各次执行区间，不包含已完成到下次续问之间的空闲时间。

| API | 用途 |
|---|---|
| GET `/api/runs/{id}/session?replay=true` | 事件导出/恢复视图 |
| POST `/api/runs/{id}/checkpoint` | 添加检查点 |
| POST `/api/runs/{id}/fork` | 分叉模型历史为独立任务 |
| GET `/api/runs/{id}/jobs` | Job 状态、游标输出 |
| POST `/api/runs/{id}/jobs/{job}` | pause/resume/cancel |
| POST `/api/runs/{id}/agents/{agent}/message` | 子 Agent FIFO 追问 |
| GET `/api/runs/{id}/evidence/{E1}` | 完整捕获来源 |
| GET `/api/runs/{id}/artifact-manifest` | 工件哈希和版本 |
| GET `/api/runs/{id}/metrics` | 工具时间/失败、Job、证据、压缩和预算统计 |
| GET/POST `/api/schedules` | 查询/创建定时任务 |
| POST `/api/schedules/{id}` | 暂停/恢复/软删除 |
| GET `/api/plugins`，POST `/api/plugins/trust` | 查看/锁定插件 |
| DELETE `/api/runs/{id}/queue/{item}` | 移除待执行消息 |
| POST `/api/runs/{id}/queue/order` | 待执行消息重排 |

API 仍为本机单用户 loopback 服务；写请求拒绝非同源 Origin，不能当作多用户公网服务直接部署。不要通过反向代理开放未认证 API。

已结束的子 Agent 会话接到 UI 追问后可自动重新激活父任务，以同一子 Session 执行下一轮；暂停/审批中的父任务仍等待明确恢复，取消的父任务不重新启动。该续问不会借机重新创建同一个子 Agent 或重复执行原始任务。

## 验收

新增 `tests/test_next_runtime.py` 验证跨进程队列/Session、陈旧保存合并、CAS/幂等、损坏缓存、租约恢复与 fencing、父取消、Provider 审批不绕过、长 JSON/完整来源、工作流和代码重放、代码语言逃逸拒绝、独立子 Session、调度 fencing、插件哈希和控制 API。

```bash
uv run ruff check src tests
uv run pytest -q
uv run lightworker doctor --rebuild-image  # 升级旧镜像中的 sandbox helper
uv run pytest -q -m docker  # 需要可运行的 Docker 与 lightworker-python:3.11 镜像
uv run lightworker serve
```

Docker、真实 LSP server、可选 DrissionPage/远程 MCP/Embedding 和外部 Subagent factory 的端到端结果取决于本机依赖与服务；缺失能力会明确报错或只读降级，不编造执行成功。

本次开发验收：130 项测试通过，3 项 Docker 集成测试因本机 Docker 安装缺少可执行文件而跳过；Ruff 和 diff 检查通过，wheel/sdist 构建成功。已配置模型的实际浏览器测试覆盖 Markdown 完成态、tool_search→spawn_agent→Job 结果收集，以及服务重启后同一子 Session 的第二轮追问。普通任务耗时 6 秒，首次子 Agent 任务耗时 25 秒；第二轮返回“**不确定性可能带来损失续问成功**”。这些耗时仅是本次测试记录，不是性能保证。
