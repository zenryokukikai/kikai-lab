import subprocess
import types

import pytest

from kikai_lab.operation import (
    OperationError,
    execute_remote_docker_build_operation,
)


def _completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_remote_docker_build_runs_expected_command(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        # mkdir, Dockerfile write, docker build all succeed.
        return _completed(returncode=0, stdout="Successfully built abc123\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_build",
        "operation": "build_diffusers_image",
        "ssh_host": "training-host.example",
        "image_tag": "example-engine:dev-diffusers",
        "dockerfile_content": "FROM example-engine:dev\nRUN echo hi\n",
        "remote_build_dir": "/tmp/kikai_docker_build",
        "build_args": {"BASE_IMAGE": "example-engine:dev"},
        "no_cache": True,
        "target_id": "target-1",
    }

    result = execute_remote_docker_build_operation(request)

    assert result["execution_status"] == "remote_docker_build_completed"
    assert result["image_tag"] == "example-engine:dev-diffusers"
    assert result["ssh_host"] == "training-host.example"
    assert result["returncode"] == 0
    assert result["target_id"] == "target-1"

    # mkdir -p, cat > Dockerfile, docker build
    assert len(calls) == 3
    mkdir_cmd = calls[0]["cmd"]
    assert mkdir_cmd[:2] == ["ssh", "training-host.example"]
    assert mkdir_cmd[2] == "mkdir -p /tmp/kikai_docker_build"

    write_cmd = calls[1]["cmd"]
    assert write_cmd[2] == "cat > /tmp/kikai_docker_build/Dockerfile"
    assert calls[1]["kwargs"].get("input") == "FROM example-engine:dev\nRUN echo hi\n"

    build_cmd = calls[2]["cmd"]
    assert build_cmd[:2] == ["ssh", "training-host.example"]
    build_shell = build_cmd[2]
    assert "docker build" in build_shell
    assert "--no-cache" in build_shell
    assert "--build-arg BASE_IMAGE=example-engine:dev" in build_shell
    assert "-t example-engine:dev-diffusers" in build_shell
    assert "-f /tmp/kikai_docker_build/Dockerfile /tmp/kikai_docker_build" in build_shell


def test_remote_docker_build_rejects_invalid_image_tag(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for an invalid tag")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_build",
        "operation": "build_bad",
        "ssh_host": "training-host.example",
        "image_tag": "bad tag with spaces; rm -rf /",
        "dockerfile_content": "FROM scratch\n",
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_build_operation(request)
    assert exc.value.code == "operation.remote_docker_build_invalid_tag"


def test_remote_docker_build_rejects_invalid_remote_build_dir(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(returncode=0)
    )
    request = {
        "adapter": "remote_docker_build",
        "operation": "build_bad_dir",
        "ssh_host": "training-host.example",
        "image_tag": "example-engine:dev-diffusers",
        "dockerfile_content": "FROM scratch\n",
        "remote_build_dir": "relative/dir; rm -rf /",
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_build_operation(request)
    assert exc.value.code == "operation.remote_docker_build_invalid_dir"


def test_remote_docker_build_rejects_invalid_build_arg_key(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for an invalid build_arg key")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_build",
        "operation": "build_bad_arg_key",
        "ssh_host": "training-host.example",
        "image_tag": "example-engine:dev-diffusers",
        "dockerfile_content": "FROM scratch\n",
        "build_args": {"BAD KEY; rm -rf /": "x"},
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_build_operation(request)
    assert exc.value.code == "operation.remote_docker_build_invalid_build_arg_key"


def test_remote_docker_build_shell_quotes_build_arg_value(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _completed(returncode=0, stdout="Successfully built abc\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_build",
        "operation": "build_quote_arg",
        "ssh_host": "training-host.example",
        "image_tag": "example-engine:dev-diffusers",
        "dockerfile_content": "FROM scratch\n",
        "build_args": {"INJECT": "v; rm -rf /"},
    }
    execute_remote_docker_build_operation(request)

    build_shell = calls[2]["cmd"][2]
    # The dangerous value must be single-quoted as part of the k=v token, never raw.
    assert "--build-arg 'INJECT=v; rm -rf /'" in build_shell
    assert "INJECT=v; rm -rf /" not in build_shell.replace("'INJECT=v; rm -rf /'", "")


def test_remote_docker_build_raises_on_nonzero_build(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        shell = cmd[2]
        if shell.startswith("docker build"):
            return _completed(returncode=1, stdout="step 1/2\n", stderr="boom: pip failed\n")
        return _completed(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_build",
        "operation": "build_fail",
        "ssh_host": "training-host.example",
        "image_tag": "example-engine:dev-diffusers",
        "dockerfile_content": "FROM scratch\n",
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_build_operation(request)
    assert exc.value.code == "operation.remote_docker_build_failed"
    assert exc.value.details["image_tag"] == "example-engine:dev-diffusers"
    assert "boom: pip failed" in exc.value.details["stderr"]
    assert "step 1/2" in exc.value.details["stdout_tail"]
