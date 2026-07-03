import subprocess
import types

import pytest

from kikai_lab.operation import (
    OperationError,
    execute_remote_docker_logs_operation,
)


def _completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_remote_docker_logs_builds_expected_ssh_command(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _completed(returncode=0, stdout="step 100 loss 0.5\n", stderr="warn\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_logs",
        "operation": "tail_example_run",
        "ssh_host": "training-host.example",
        "container_name": "example-run-train",
        "tail": 50,
        "target_id": "target-1",
    }

    result = execute_remote_docker_logs_operation(request)

    assert result["execution_status"] == "remote_docker_logs_completed"
    assert result["container_name"] == "example-run-train"
    assert result["ssh_host"] == "training-host.example"
    assert result["tail"] == 50
    assert result["returncode"] == 0
    assert result["target_id"] == "target-1"
    # Combined stdout + stderr (training often logs to stderr).
    assert "step 100 loss 0.5" in result["logs"]
    assert "warn" in result["logs"]

    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    assert cmd == ["ssh", "training-host.example", "docker logs --tail 50 example-run-train"]


def test_remote_docker_logs_defaults_tail_to_200(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _completed(returncode=0, stdout="ok\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_logs",
        "operation": "tail_default",
        "ssh_host": "training-host.example",
        "container_name": "example-run-train",
    }

    result = execute_remote_docker_logs_operation(request)

    assert result["tail"] == 200
    assert calls[0] == ["ssh", "training-host.example", "docker logs --tail 200 example-run-train"]


def test_remote_docker_logs_rejects_unsafe_name(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("subprocess.run must not run for an unsafe name")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_logs",
        "operation": "tail_bad",
        "ssh_host": "training-host.example",
        "container_name": "example; rm -rf /",
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_logs_operation(request)
    assert exc.value.code == "operation.remote_docker_logs_invalid_name"


def test_remote_docker_logs_truncates_to_20000_chars(monkeypatch):
    big = "x" * 50000

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(returncode=0, stdout=big)
    )

    request = {
        "adapter": "remote_docker_logs",
        "operation": "tail_big",
        "ssh_host": "training-host.example",
        "container_name": "example-run-train",
    }

    result = execute_remote_docker_logs_operation(request)
    assert len(result["logs"]) == 20000
