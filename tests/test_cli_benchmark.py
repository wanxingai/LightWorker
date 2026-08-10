from __future__ import annotations

from pathlib import Path

from conftest import run_git
from typer.testing import CliRunner

from lightworker.benchmark import get_case, load_cases, materialize_case
from lightworker.cli import app, autodetect_verification


def test_version_flag_works_without_command():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "LightWorker 0.2.0" in result.stdout


def test_no_args_prints_help():
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "Docker" in result.stdout
    assert "serve" in result.stdout


def test_web_server_rejects_non_loopback_binding():
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "loopback" in result.stderr


def test_benchmark_has_exactly_five_existing_fixtures():
    cases = load_cases()
    assert len(cases) == 5
    root = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
    assert all((root / case.fixture / "pyproject.toml").is_file() for case in cases)


def test_materialized_benchmark_is_a_clean_git_repository(tmp_path: Path):
    destination = materialize_case(get_case("csv-empty-cell"), tmp_path / "case")

    assert (destination / "importer.py").is_file()
    assert (destination / ".git").is_dir()
    assert run_git(destination, "status", "--porcelain") == ""


def test_live_benchmark_requires_exactly_one_case():
    runner = CliRunner()

    missing = runner.invoke(app, ["benchmark", "--live"])
    without_live = runner.invoke(app, ["benchmark", "--case", "csv-empty-cell"])

    assert missing.exit_code == 2
    assert "requires exactly one --case" in missing.stderr
    assert without_live.exit_code == 2
    assert "requires --live" in without_live.stderr


def test_autodetect_pytest(git_repo):
    commands = autodetect_verification(git_repo)
    assert len(commands) == 1
    assert commands[0].argv == ["pytest", "-q"]
