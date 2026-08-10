"""LightWorker Phase 0 command-line interface."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import WorkerConfig, parse_verification_command
from .doctor import run_diagnostics
from .models import RunStatus, TaskSpec, VerificationCommand, VerificationKind
from .sandbox import DockerSandbox
from .storage import RunStore
from .workflow import CodingTaskRunner

app = typer.Typer(
    name="lightworker",
    help="能力驱动、可审计、Docker 隔离的 LightAgent 通用任务 Worker / Capability-routed, auditable, Docker-isolated task worker.",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="显示版本 / Show version", is_eager=True),
    ] = False,
) -> None:
    if version:
        console.print(f"LightWorker {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
    build_image: Annotated[
        bool,
        typer.Option("--build-image", help="构建缺失的基础镜像 / Build the sandbox image"),
    ] = False,
) -> None:
    """检查本机依赖，不读取模型密钥内容 / Diagnose the local environment."""
    settings = WorkerConfig.load(config)
    if build_image:
        if not DockerSandbox.daemon_available():
            console.print("[red]Docker daemon 未运行 / Docker daemon is not running.[/red]")
            raise typer.Exit(2)
        if not DockerSandbox.image_exists(settings.image):
            assert settings.dockerfile is not None
            assert settings.docker_context is not None
            with console.status("正在构建沙箱镜像 / Building sandbox image..."):
                DockerSandbox.build_image(settings.image, settings.dockerfile, settings.docker_context)

    table = Table(title="LightWorker Doctor")
    table.add_column("状态 / Status")
    table.add_column("检查 / Check")
    table.add_column("详情 / Detail")
    failed_required = False
    for diagnostic in run_diagnostics(settings):
        if diagnostic.ok:
            status = "[green]PASS[/green]"
        elif diagnostic.required:
            status = "[red]FAIL[/red]"
            failed_required = True
        else:
            status = "[yellow]WARN[/yellow]"
        table.add_row(status, diagnostic.name, diagnostic.message)
    console.print(table)
    if failed_required:
        raise typer.Exit(2)


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host", help="监听地址 / Listen address")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
) -> None:
    """启动本地单用户 Web 控制台 / Start the local Web console."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("the unauthenticated Phase 0 Web UI only supports loopback hosts")
    selected_config = config
    local_config = Path.cwd() / "lightworker.local.yaml"
    if selected_config is None and local_config.is_file():
        selected_config = local_config
    settings = WorkerConfig.load(selected_config, state_dir=state_dir)
    from .web import create_app

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency diagnostic
        raise RuntimeError("uvicorn is required for `lightworker serve`") from exc
    console.print(
        Panel.fit(
            f"Web UI: [bold]http://{host}:{port}[/bold]\n"
            f"State / 状态: {settings.state_dir}\n"
            "Local access only / 仅限本机访问",
            title="LightWorker",
        )
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command("run")
def run_task(
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True)],
    task: Annotated[str, typer.Option("--task", help="任务描述 / Task description")],
    test: Annotated[
        list[str] | None, typer.Option("--test", help="允许的测试命令，可重复 / Repeatable test command")
    ] = None,
    lint: Annotated[
        list[str] | None, typer.Option("--lint", help="允许的 lint 命令，可重复 / Repeatable lint command")
    ] = None,
    include_dirty: Annotated[
        bool,
        typer.Option("--include-dirty", help="包含原仓库未提交改动 / Include uncommitted source changes"),
    ] = False,
    image: Annotated[str | None, typer.Option("--image")] = None,
    max_repairs: Annotated[int | None, typer.Option("--max-repairs", min=0, max=3)] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
) -> None:
    """执行一个通用或工作区任务 / Run a general or workspace task."""
    overrides: dict[str, object] = {}
    if state_dir:
        overrides["state_dir"] = state_dir
    if image:
        overrides["image"] = image
    if max_repairs is not None:
        overrides["max_repairs"] = max_repairs
    if language:
        overrides["language"] = language
    settings = WorkerConfig.load(config, **overrides)
    verification = list(settings.verification)
    for index, raw in enumerate(test or [], start=1):
        verification.append(parse_verification_command(raw, kind=VerificationKind.TEST, index=index))
    for index, raw in enumerate(lint or [], start=1):
        verification.append(parse_verification_command(raw, kind=VerificationKind.LINT, index=index))
    if not verification:
        verification = autodetect_verification(repo)

    spec = TaskSpec(
        repo=repo,
        task=task,
        include_dirty=include_dirty,
        language=settings.language,
        verification=verification,
        max_repairs=settings.max_repairs,
        image=settings.image,
    )
    console.print(Panel.fit(f"Run: [bold]{spec.run_id}[/bold]\nTask / 任务: {task}", title="LightWorker"))
    runner = CodingTaskRunner(settings)
    with console.status("Agent 正在隔离环境中工作 / Agent is working in isolation..."):
        record = runner.run(spec)
    _print_record(record, runner.store)
    if record.status == RunStatus.SUCCEEDED:
        raise typer.Exit(0)
    if record.status == RunStatus.NEEDS_ATTENTION:
        raise typer.Exit(1)
    raise typer.Exit(2)


@app.command("show")
def show_run(
    run_id: str,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
) -> None:
    store = _store(state_dir)
    _print_record(store.load(run_id), store)


@app.command("list")
def list_runs(
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
) -> None:
    store = _store(state_dir)
    table = Table(title="LightWorker Runs")
    table.add_column("Run")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Updated")
    for record in store.list():
        table.add_row(record.run_id, record.status.value, record.task[:70], record.updated_at.isoformat())
    console.print(table)


@app.command("diff")
def show_diff(
    run_id: str,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
) -> None:
    path = _store(state_dir).artifact_path(run_id, "changes.patch")
    console.print(path.read_text(encoding="utf-8"), markup=False)


@app.command("logs")
def show_logs(
    run_id: str,
    name: Annotated[str | None, typer.Option("--name", help="日志文件名 / Log file name")] = None,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
) -> None:
    store = _store(state_dir)
    logs_dir = store.artifact_path(run_id, "logs")
    if name:
        path = store.artifact_path(run_id, f"logs/{name}")
        console.print(path.read_text(encoding="utf-8"), markup=False)
        return
    table = Table(title=f"Logs: {run_id}")
    table.add_column("Name")
    table.add_column("Bytes", justify="right")
    for path in sorted(logs_dir.glob("*.log")):
        table.add_row(path.name, str(path.stat().st_size))
    console.print(table)


@app.command("resume")
def resume_run(
    run_id: str,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
) -> None:
    settings = WorkerConfig.load(config, state_dir=state_dir)
    runner = CodingTaskRunner(settings)
    with console.status("恢复任务 / Resuming task..."):
        record = runner.resume(run_id)
    _print_record(record, runner.store)
    _exit_for_record(record)


@app.command("rerun")
def rerun(
    run_id: str,
    from_step: Annotated[str, typer.Option("--from")] = "verify",
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
) -> None:
    if from_step != "verify":
        raise typer.BadParameter("Phase 0 only supports --from verify")
    settings = WorkerConfig.load(config, state_dir=state_dir)
    runner = CodingTaskRunner(settings)
    with console.status("重新验证 / Rerunning verification..."):
        record = runner.rerun_from_verify(run_id)
    _print_record(record, runner.store)
    _exit_for_record(record)


@app.command("benchmark")
def benchmark(
    live: Annotated[
        bool, typer.Option("--live", help="调用真实模型 / Invoke the configured real model")
    ] = False,
    case_id: Annotated[
        str | None, typer.Option("--case", help="单个 benchmark case ID / One benchmark case ID")
    ] = None,
    state_dir: Annotated[Path | None, typer.Option("--state-dir", file_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
) -> None:
    """检查固定 benchmark；真实运行必须显式指定 --live 和 --case。"""
    from .benchmark import get_case, load_cases, materialize_case

    cases = load_cases()
    table = Table(title="Python Benchmark Cases")
    table.add_column("ID")
    table.add_column("Task / 任务")
    table.add_column("Verification")
    for case in cases:
        table.add_row(case.case_id, case.task_zh, " ".join(case.test_command))
    console.print(table)
    if not live:
        if case_id:
            raise typer.BadParameter("--case requires --live")
        return
    if not case_id:
        raise typer.BadParameter("--live requires exactly one --case")
    try:
        selected = get_case(case_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    settings = WorkerConfig.load(config, state_dir=state_dir)
    settings.protected_patterns = sorted(set(settings.protected_patterns).union(selected.forbidden_paths))
    with tempfile.TemporaryDirectory(prefix="lightworker-benchmark-") as temporary:
        repo = materialize_case(selected, Path(temporary) / selected.fixture)
        spec = TaskSpec(
            repo=repo,
            task=f"{selected.task_zh}\n\n{selected.task_en}",
            language=settings.language,
            verification=[
                VerificationCommand(
                    name=f"benchmark-{selected.case_id}",
                    argv=selected.test_command,
                    kind=VerificationKind.TEST,
                )
            ],
            max_repairs=settings.max_repairs,
            image=settings.image,
        )
        console.print(Panel.fit(f"Live benchmark: [bold]{selected.case_id}[/bold]", title="LightWorker"))
        runner = CodingTaskRunner(settings)
        with console.status("运行真实 benchmark / Running live benchmark..."):
            record = runner.run(spec)
    _print_record(record, runner.store)
    _exit_for_record(record)


def autodetect_verification(repo: Path) -> list[VerificationCommand]:
    root = repo.resolve()
    if (root / "tests").is_dir() and any(
        (root / name).exists() for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
    ):
        return [
            VerificationCommand(
                name="auto-pytest",
                argv=["pytest", "-q"],
                kind=VerificationKind.TEST,
            )
        ]
    return []


def _store(state_dir: Path | None) -> RunStore:
    return RunStore(WorkerConfig.load(state_dir=state_dir).state_dir)


def _print_record(record: object, store: RunStore) -> None:
    from .models import RunRecord

    value = RunRecord.model_validate(record)
    color = {
        RunStatus.SUCCEEDED: "green",
        RunStatus.NEEDS_ATTENTION: "yellow",
        RunStatus.FAILED: "red",
        RunStatus.INTERRUPTED: "yellow",
    }.get(value.status, "cyan")
    table = Table(title=f"Run {value.run_id}")
    table.add_column("字段 / Field")
    table.add_column("值 / Value")
    table.add_row("Status", f"[{color}]{value.status.value}[/{color}]")
    table.add_row("Task / 任务", value.task)
    table.add_row("Repository", value.repo)
    table.add_row("Workspace", value.workspace or "-")
    table.add_row(
        "Verification", f"{sum(item.passed for item in value.verification)}/{len(value.verification)}"
    )
    table.add_row("Artifacts", str(store.run_dir(value.run_id)))
    if value.error:
        table.add_row("Error / 错误", value.error)
    console.print(table)


def _exit_for_record(record: object) -> None:
    from .models import RunRecord

    value = RunRecord.model_validate(record)
    if value.status == RunStatus.SUCCEEDED:
        return
    if value.status == RunStatus.NEEDS_ATTENTION:
        raise typer.Exit(1)
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
