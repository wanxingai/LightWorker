# LightWorker

LightWorker 是基于 LightAgent 0.9.7 的本地通用 Agent Worker。它不再把任务拆成“编码任务”和“通用任务”两个入口：检索、分析、写作、浏览器操作、RAG、Shell、代码修改和后续追问都可以在同一个动态 Agentic Loop 中按需组合。只有发生文件编辑时才展示 diff。

LightWorker is a local universal Agent Worker powered by LightAgent 0.9.7. Research, analysis, writing, browser automation, RAG, Docker shell, code changes, and follow-up messages share one dynamic Agentic Loop. A diff is shown only when files actually changed.

Agentica 只作为能力设计参考，不是运行时依赖。核心模型循环、Trace、Hook、LightFlow 和 MCP 基础仍来自 LightAgent。

## 核心能力

- 默认动态 Agentic Loop；固定 LightFlow Workflow 作为显式可选模式保留。
- 任意任务类型共享同一个入口，同一任务可以先研究、再分析、最后修改代码。
- 统一工具元数据：类别、读写/破坏性、外部副作用、并发安全、沙箱/网络要求、凭据范围、审批策略、超时和输出上限。
- 精确参数审批：Shell、外部 POST、MCP 写工具、删除/重命名、技能脚本和持久记忆变更会生成可恢复的审批请求；参数变化后必须重新审批。
- Shell 只在 Docker 容器内以 argv 执行，绝不直接执行宿主机 Shell；禁用破坏性 Git、inline Python 和直接 pip install。
- 公共 Web 搜索、HTTPS GET，以及需要审批的外部 HTTP action。
- Playwright 默认浏览器后端；DrissionPage 可选。每个 run 使用临时 profile，私网目标和下载默认阻断。
- Supervisor + Explore/Research/Code/Test/Review/RAG 子 Agent；最多 4 个只读子任务并行、深度最多 2、整棵 Agent 树默认最多 8 个子 Agent，写入由 Supervisor 串行完成。
- Goal 模式：目标、验收标准、子目标、状态、轮次/工具/模型/Token/时间预算、无进展检测、暂停/恢复/取消和实时 steering。
- 自动上下文压缩：达到上下文窗口阈值后保留目标、验收标准、审批、决策和最近轮次，并压缩较早轮次。
- WorkingMemory、持久 Workspace Memory，以及用户级、项目级和嵌套 `AGENTS.md`。
- Markdown Skills：项目级、用户级、托管目录和内置目录；脚本只能经审批后在 Docker 中运行。
- MCP：stdio、SSE、Streamable HTTP，工具自动命名空间；stdio 被改写为 `docker exec`，远程服务只允许公共 HTTPS。
- 内置小型 RAG：文件增量哈希、SQLite FTS5/BM25、来源 chunk 引用，可选 PDF/DOCX reader 和 Embedding + RRF 混合检索。
- 多任务调度：任务、模型请求、浏览器和容器分别限流；服务重启后的活动任务会转成可恢复 checkpoint。
- 本机 Chat UI：SSE 实时事件、工具调用、审批、Goal、Agent 树、浏览器截图、Memory/Skills/RAG/MCP 管理和按需 diff。

## 运行架构

```text
Chat / CLI
    │
    ├─ Dynamic Agentic Loop (default)
    │    ├─ Goal + WorkingMemory + automatic compression
    │    ├─ Tool policy + exact approval + EventLog
    │    ├─ Web / Browser / RAG / Memory / Skills / MCP
    │    ├─ bounded parallel subagents
    │    └─ Docker workspace + Shell + verification
    │
    └─ Fixed LightFlow Workflow (optional)

All file edits → isolated workspace → deterministic verification → diff artifact
No file edit   → direct answer, no empty diff card
```

原仓库从不挂载进任务容器。已有仓库会先复制为隔离快照；新任务默认从受管理的空 Git 目录开始。

Docker 不可用时，动态运行时会安全降级为纯 Python 只读工作区：Web 检索、分析、浏览器、Memory 和 RAG 仍可使用，但不会提供文件写入、Shell、技能脚本或 stdio MCP，也不会回退到宿主机命令。

## 安装

要求 Python 3.11、Git、Docker Desktop，以及相邻目录中的 LightAgent 0.9.7 源码：

```text
Langchain-Chatchat/
├── LightAgent/
└── LightWorker/
```

安装基础开发环境和默认 Playwright 后端：

```bash
uv sync --python 3.11 --extra dev
uv run playwright install chromium
uv run lightworker doctor --build-image
```

可选文档 reader：

```bash
uv sync --extra documents
```

可选 DrissionPage 后端：

```bash
uv sync --extra drissionpage
```

DrissionPage 不是默认依赖。其商业使用可能需要单独授权；商业用户需自行确认并取得符合用途的授权，详见 [DrissionPage 官网](https://www.drissionpage.cn/) 和项目许可说明。

## 模型配置

配置 OpenAI-compatible 模型：

```bash
export LIGHTWORKER_MODEL="your-model"
export LIGHTWORKER_BASE_URL="https://your-endpoint.example/v1"
export LIGHTWORKER_API_KEY="..."
```

`LIGHTWORKER_BASE_URL` 可省略。完整配置参考 [`lightworker.example.yaml`](lightworker.example.yaml)。API Key 默认只存在宿主机进程中，不传入任务容器。

如确需在本机 YAML 中保存 Key，可使用 `model.api_key`。该文件仍是明文 Secret，必须加入 Git 忽略并设置为 `0600`；Web、Trace、事件和日志会做常见 Secret 脱敏。

## 使用

### Web Chat

```bash
uv run lightworker serve
```

打开 `http://127.0.0.1:8787`。`serve` 只允许监听 loopback 地址。

界面中可以：

- 从空目录或已有 Git 仓库快照开始；
- 选择动态 Agentic Loop 或固定 Workflow；
- 执行中继续发送资料或 steering；
- 暂停、恢复或取消任务；
- 查看工具输入/输出、审批、Goal、Agent 树、验证日志、浏览器截图和 Trace；
- 管理 Workspace Memory、Skills、RAG 文档和 MCP 状态；
- 仅在有文件编辑时查看 diff。

### CLI

```bash
uv run lightworker run \
  --repo /absolute/path/to/repository \
  --task "检索相关规范，分析失败测试并完成最小修复" \
  --test "pytest -q"
```

```bash
uv run lightworker list
uv run lightworker show RUN_ID
uv run lightworker diff RUN_ID
uv run lightworker logs RUN_ID
uv run lightworker resume RUN_ID
uv run lightworker rerun RUN_ID --from verify
```

## 权限与审批

| 等级 | 示例 | 默认行为 |
|---|---|---|
| L0 只读 | 读文件、搜索、HTTP GET、RAG search | 自动执行并记录事件 |
| L1 隔离写入 | 普通 `apply_patch`、WorkingMemory、RAG ingest | 自动执行；有文件编辑才输出 diff |
| L2 敏感操作 | Docker Shell、pip、HTTP POST、MCP 写工具、技能脚本 | 精确参数审批 |
| L3 破坏/持久操作 | 删除/重命名、Memory 提升/删除、RAG 删除 | 强制审批或显式 UI 操作 |

审批只授权工具名和规范化参数的哈希。模型改变命令、URL、请求体或 patch 后，旧审批不会复用。

## Docker Shell 边界

- `shell_exec` 只调用 Docker sandbox helper，使用 `subprocess.Popen(argv, shell=False)`。
- 不挂载 Docker socket、宿主机凭据或原仓库。
- 容器 root filesystem 只读、非 root、drop all capabilities、`no-new-privileges`、资源和超时受限。
- 普通容器网络为 internal；`pip_install` 只在审计调用期间临时连接 egress，完成后立即断开。
- `uv run`、Python workspace script 等可在审批后运行；宿主机 Shell 永远不会作为降级方案。

## Browser Tool

默认 Playwright，配置 `browser.backend: drissionpage` 可切换可选后端。当前版本只使用 run 级临时 profile；持久登录态尚未开放，即使配置为 true 也会 fail closed。浏览器导航会校验公共地址，Playwright 还会拦截私网子资源；下载默认关闭。

主要工具：`browser_open`、`browser_click`、`browser_type`、`browser_select`、`browser_extract`、`browser_tabs`、`browser_screenshot`。

## Memory、AGENTS.md 与 Skills

指令优先级：

1. `~/.lightworker/AGENTS.md`
2. 项目根 `AGENTS.md`
3. 目标子目录中的嵌套 `AGENTS.md`

Skill 发现优先级：

1. `<project>/.lightworker/skills`
2. `~/.lightworker/skills`
3. 配置的托管只读目录
4. LightWorker 内置目录

同名 Skill 不会静默覆盖；高优先级版本生效，冲突会显示在 manifest 和 UI 中。自动提取的持久记忆先作为有 TTL 的 candidate，只有用户提升后才参与检索。

## MCP

MCP 工具名统一为 `mcp__<server>__<tool>`。远程 SSE/Streamable HTTP 只允许公共 HTTPS 和可选 host allowlist；header/stdio env 可写成 `${ENV_VAR}`，值不会进入模型参数或 Web 配置响应。未声明为 `read_only_tools` 的 MCP 工具默认需要审批。

stdio MCP server 不会直接作为宿主机子进程执行。LightWorker 只在 Docker 已启动时将其改写为：

```text
docker exec -i <task-container> <configured-command> <args...>
```

## 小型 RAG

支持 `.txt`、`.md`、`.rst`、`.html`、`.json`、`.csv`、`.tsv`；安装 `documents` extra 后支持 `.pdf` 和 `.docx`。默认 chunk 约 1000 tokens、overlap 120 tokens，使用内容哈希增量更新和 SQLite FTS5 BM25 检索。返回结果包含 `[path#chunk-N]` 引用。

`rag.embeddings_enabled` 默认关闭，因此当前模型服务无需提供 Embedding API。启用后，LightWorker 使用 OpenAI-compatible Embedding API 生成向量，并用 Reciprocal Rank Fusion (RRF) 合并 FTS5 与余弦相似度结果；服务不可用或未配置密钥时自动回退到 FTS5。密钥只从 `rag.embedding_api_key_env` 指定的环境变量读取，默认是 `LIGHTWORKER_EMBEDDING_API_KEY`，不会写入索引、事件或任务容器。

```bash
export LIGHTWORKER_EMBEDDING_API_KEY="..."
```

端点、模型和批大小分别由 `rag.embedding_base_url`、`rag.embedding_model` 和 `rag.embedding_batch_size` 配置。RAG 知识与持久 Memory 分库存储，避免把检索资料误当成用户偏好或项目规则。

## Artifacts

每个 run 的状态目录包含：

- `run.json`、`goal.json`、`working-memory.json`、`control.json`；
- `events.jsonl`、`trace.jsonl`、`agent-tree.json`、`tool-manifest.json`；
- `summary.md`；
- 仅有文件变更时使用的 `changes.patch` 和 `git-status.txt`；
- `verification-*.json`、`logs/*.log`；
- `approvals.json`；
- `browser/screenshot-*.png`；
- 可选固定 Workflow 的 `flow/*.json`。

SQLite 数据库存储在 state directory：`memory.sqlite3` 和 `rag.sqlite3`。

## 测试

```bash
uv run pytest
uv run ruff check .
```

Docker、外网和真实模型测试分别使用 `docker`、`network`、`live` marker。默认测试集不调用外部模型。

## License

Apache-2.0
