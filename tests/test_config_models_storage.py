from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lightworker.config import WorkerConfig, parse_verification_command
from lightworker.models import RunRecord, RunStatus, VerificationKind
from lightworker.storage import RunStore


def test_config_loads_yaml_and_overrides(tmp_path: Path):
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        "image: custom:latest\nlimits:\n  cpus: 1\n  memory: 2g\n",
        encoding="utf-8",
    )

    config = WorkerConfig.load(config_path, state_dir=tmp_path / "state")

    assert config.image == "custom:latest"
    assert config.limits.cpus == 1
    assert config.limits.memory == "2g"
    assert config.state_dir == tmp_path / "state"
    assert config.dockerfile and config.dockerfile.name == "Dockerfile.python311"
    assert config.dockerfile.is_file()
    assert config.docker_context and (config.docker_context / "sandbox_helper.py").is_file()


def test_configured_api_key_is_masked_in_serialization(tmp_path: Path):
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        "model:\n  model: test-model\n  api_key: test-secret-value\n",
        encoding="utf-8",
    )

    config = WorkerConfig.load(config_path)
    serialized = config.model.model_dump_json()

    assert config.model.resolved_api_key == "test-secret-value"
    assert "test-secret-value" not in serialized
    assert "**********" in serialized


def test_verification_command_uses_argv_not_shell():
    command = parse_verification_command(
        "pytest -q tests/test_unit.py",
        kind=VerificationKind.TEST,
        index=1,
    )
    assert command.argv == ["pytest", "-q", "tests/test_unit.py"]


def test_invalid_resource_limit_is_rejected():
    with pytest.raises(ValidationError):
        WorkerConfig.model_validate({"limits": {"cpus": 0}})


def test_run_store_round_trip_and_safe_artifact_names(tmp_path: Path):
    store = RunStore(tmp_path / "state")
    record = RunRecord(run_id="run-1", task="test", repo="/repo")
    store.create(record)
    store.write_text("run-1", "logs/test.log", "evidence")
    updated = store.update_status("run-1", RunStatus.RUNNING, current_step="plan")

    assert updated.status == RunStatus.RUNNING
    assert store.load("run-1").current_step == "plan"
    assert store.artifact_path("run-1", "logs/test.log").read_text() == "evidence"
    with pytest.raises(ValueError):
        store.artifact_path("run-1", "../outside")
    with pytest.raises(ValueError):
        store.load("../../unsafe")
