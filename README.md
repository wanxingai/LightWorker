# LightWorker

LightWorker 是一个基于 [LightAgent](../LightAgent) 的本地统一任务 Worker。同一个执行流可以连续完成问答、检索、分析、写作和工作区文件修改；只有实际文件变更才输出可审核的补丁、命令日志与 Trace。

LightWorker is a local unified-task Agent Worker powered by LightAgent. A single workflow can combine conversation, research, analysis, writing, and workspace changes; auditable patches are produced only when files actually change.

## Phase 0 能力 / Capabilities

- 任意主题的问答、写作、翻译、规划、总结、比较、研究与分析；不再按领域拒绝任务。
- 统一任务流：研究、分析与文件修改可以发生在同一个任务和后续追问中。
- 通用代理可使用受控公共 HTTPS 请求；外部写操作仍要求对应工具和授权。
- 本地单 Git 仓库、Python 3.11 项目。
- `intake → context → plan → edit → verify → repair → review` LightFlow。
- 中英双语计划和最终报告。
- 非 root、只读根文件系统、资源受限、仅连接 internal 网络的 Docker 沙箱。
- 路径边界、命令白名单、敏感文件读写阻断、日志脱敏。
- 失败验证最多两轮修复，JSON checkpoint 支持 `resume` 和 `rerun`。
- 所有变更只发生在隔离 clone；原仓库不会被挂载或修改。
- 本地单用户 Web 控制台：任务创建、进度、计划、diff、日志、Trace 与人工审阅。

This local release intentionally excludes GitHub App, commits, pushes, pull requests, remote hosting, authentication, and multi-user features.

## 安装 / Installation

要求 Python 3.11、Git、Docker Desktop，以及相邻目录中的 LightAgent 0.9.7 源码：

```text
Langchain-Chatchat/
├── LightAgent/
└── LightWorker/
```

安装开发环境：

```bash
uv sync --python 3.11 --extra dev
uv run lightworker doctor --build-image
```

配置 OpenAI-compatible 模型。API Key 只保留在宿主机，不会传入任务容器：

```bash
export LIGHTWORKER_MODEL="your-model"
export LIGHTWORKER_BASE_URL="https://your-endpoint.example/v1"
export LIGHTWORKER_API_KEY="..."
```

`LIGHTWORKER_BASE_URL` 可省略以使用模型客户端默认地址。部分本地兼容服务不要求 API Key。
完整配置字段可参考 [`lightworker.example.yaml`](lightworker.example.yaml)，并通过
`lightworker run --config /path/to/config.yaml ...` 加载。请始终用环境变量保存 API Key。

如明确需要把密钥保存在 YAML，可使用 `model.api_key`；LightWorker 会以 `SecretStr`
加载并在序列化时遮蔽它。该文件仍是磁盘上的明文 Secret，必须加入 Git 忽略并设置为 `0600`。

## 使用 / Usage

### 本地 Web 界面

项目根目录存在 `lightworker.local.yaml` 时会自动加载，然后启动仅监听本机的控制台：

```bash
uv run lightworker serve
```

打开 `http://127.0.0.1:8787`。界面支持创建任务、查看 LightFlow 进度、计划、diff、验证日志、Trace，
以及记录人工通过/拒绝决定。审阅决定仅用于本地审计，不会写回原仓库。为避免无认证接口暴露仓库能力，
`serve` 拒绝监听非 loopback 地址。

新建任务默认从受管理的空 Git 仓库开始，无需填写路径，适合从零创建项目；切换到“已有仓库”后才需要
填写本机 Git 根目录。空目录模式允许创建 `pyproject.toml`，已有仓库仍执行默认依赖与部署文件保护策略。

### CLI

```bash
uv run lightworker run \
  --repo /absolute/path/to/repository \
  --task "修复 CSV 空单元格导致的导入错误 / Fix CSV empty-cell imports" \
  --test "pytest -q"
```

查看运行结果：

```bash
uv run lightworker list
uv run lightworker show RUN_ID
uv run lightworker diff RUN_ID
uv run lightworker logs RUN_ID
uv run lightworker resume RUN_ID
uv run lightworker rerun RUN_ID --from verify
```

查看 5 个固定 benchmark，或显式运行单个真实模型 smoke case：

```bash
uv run lightworker benchmark
uv run lightworker benchmark --live --case csv-empty-cell
```

`--live` 必须同时指定一个 `--case`，不会默认批量调用模型。

默认仅使用目标仓库 `HEAD`。如任务必须包含当前未提交文件，显式增加 `--include-dirty`；该模式会复制 tracked diff 和经过校验的未跟踪普通文件。

未传 `--test` 时，只有在检测到 `tests/` 和标准 pytest 配置后才自动运行 `pytest -q`。无法确定验证入口时不会猜测，结果标记为 `needs_attention`。

## pip 联网边界 / pip Network Boundary

普通任务容器只连接无外网路由的 per-run internal 网络。Coding Agent 可以调用专用 `pip_install` 工具；LightWorker 只在该调用期间为当前容器附加临时 egress bridge，随后强制断开并删除。包参数只允许 PyPI 名称和版本约束，不允许 URL、VCS、本地路径、editable 或 pip flags。

连接外网前会先在 internal 网络中执行 `--no-index --dry-run` 离线预检；镜像中已满足的依赖不会创建 egress 网络。

This is an explicit residual risk: package installation code can read the mounted isolated repository and use outbound network during the install window. The container receives no model key, host credential, Docker socket, or original repository mount.

## Artifacts

默认状态目录由操作系统的用户数据目录决定，也可用 `--state-dir` 指定。每个 run 保存：

- `run.json`：状态、任务和验证摘要。
- `plan.json` / `plan.md`：结构化双语计划。
- `changes.patch` / `git-status.txt`：最终变更证据。
- `verification-*.json` / `logs/*.log`：真实命令结果与完整日志。
- `summary.json` / `summary.md`：双语审核总结。
- `trace.jsonl` / `flow/*.json`：LightAgent Trace 与 LightFlow checkpoint。

## 安全默认值 / Security Defaults

- 禁止 shell、管道、重定向、后台命令和任意命令执行。
- 只允许 pytest、ruff check、mypy 和 `python -m build` 形态。
- 禁止删除/重命名/创建特殊文件，禁止修改 `.git`、`.env*`、CI/CD、Docker、部署配置和 Python 依赖/锁文件。
- `.env*`、私钥、包仓库凭据和 `secrets/` 默认不会被 Agent 列出、读取或搜索。
- LightAgent 的 Python executor、对象存储上传、自动技能、记忆和自学习均关闭或由 fail-closed policy 阻断。
- 计划和工具输出会做常见 Secret 脱敏；完整命令日志不会发送给模型，只返回有上限的摘要。

## 测试 / Tests

```bash
uv run pytest
uv run ruff check .
```

Docker、外网和真实模型测试分别使用 `docker`、`network`、`live` marker，默认测试集不调用外部模型。

## License

Apache-2.0
