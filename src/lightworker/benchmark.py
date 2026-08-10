"""Fixed Phase 0 benchmark manifest loader."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class BenchmarkCase(BaseModel):
    case_id: str
    task_zh: str
    task_en: str
    fixture: str
    test_command: list[str]
    forbidden_paths: list[str] = Field(default_factory=list)


def load_cases(directory: Path | None = None) -> list[BenchmarkCase]:
    root = directory or benchmark_root() / "cases"
    cases = []
    for path in sorted(root.glob("*.yaml")):
        cases.append(BenchmarkCase.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
    if len(cases) != 5:
        raise ValueError(f"expected exactly 5 benchmark cases, found {len(cases)} in {root}")
    return cases


def benchmark_root() -> Path:
    source_root = Path(__file__).resolve().parents[2] / "benchmarks"
    if source_root.is_dir():
        return source_root
    return Path(__file__).resolve().parent / "benchmark_data"


def get_case(case_id: str) -> BenchmarkCase:
    for case in load_cases():
        if case.case_id == case_id:
            return case
    available = ", ".join(case.case_id for case in load_cases())
    raise ValueError(f"unknown benchmark case {case_id!r}; choose one of: {available}")


def materialize_case(case: BenchmarkCase, destination: Path) -> Path:
    source = benchmark_root() / "fixtures" / case.fixture
    if not source.is_dir():
        raise FileNotFoundError(f"benchmark fixture is missing: {source}")
    shutil.copytree(source, destination)
    environment = dict(os.environ)
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    commands = [
        ["git", "init", "--quiet"],
        ["git", "add", "--all"],
        [
            "git",
            "-c",
            "user.name=LightWorker Benchmark",
            "-c",
            "user.email=benchmark@lightworker.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "--message",
            "benchmark fixture",
        ],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=destination,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"failed to prepare benchmark: {command}")
    return destination
