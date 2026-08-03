import subprocess
import types

import pytest

from kikai_lab.operation import (
    OperationError,
    execute_remote_docker_run_operation,
)


def _completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_remote_docker_run_runs_expected_command(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _completed(returncode=0, stdout="GPU 0: ok\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_smi",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["arg1", "arg2"],
        "gpus": "all",
        "env": {"K": "V"},
        "volumes": ["/h:/c"],
        "target_id": "target-1",
    }

    result = execute_remote_docker_run_operation(request)

    assert result["execution_status"] == "remote_docker_run_completed"
    assert result["image"] == "example-engine:dev"
    assert result["ssh_host"] == "training-host.example"
    assert result["returncode"] == 0
    assert result["stdout"] == "GPU 0: ok\n"
    assert result["target_id"] == "target-1"

    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    assert cmd[:2] == ["ssh", "training-host.example"]
    remote = cmd[2]
    assert remote == "docker run --rm --gpus all -e K=V -v /h:/c example-engine:dev arg1 arg2"
    # timeout is passed through.
    assert calls[0]["kwargs"].get("timeout") == 1800


def test_remote_docker_run_rejects_invalid_image(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for an invalid image")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_bad",
        "ssh_host": "training-host.example",
        "image": "bad image; rm -rf /",
        "command": ["nvidia-smi"],
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_invalid_image"


def test_remote_docker_run_rejects_unsafe_volume(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(returncode=0))

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_bad_vol",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["nvidia-smi"],
        "volumes": ["/h:/c; rm -rf /"],
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_invalid_volume"


def test_remote_docker_run_raises_on_nonzero(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        return _completed(returncode=125, stdout="starting\n", stderr="docker: no such image\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_fail",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["nvidia-smi"],
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_failed"
    assert exc.value.details["image"] == "example-engine:dev"
    assert exc.value.details["returncode"] == 125
    assert "no such image" in exc.value.details["stderr"]
    assert "starting" in exc.value.details["stdout_tail"]


@pytest.mark.parametrize("bad_gpus", ["all; rm -rf /", "$(touch x)", "device=0;reboot", "1 2"])
def test_remote_docker_run_rejects_invalid_gpus(monkeypatch, bad_gpus):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for invalid gpus")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_bad_gpus",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["nvidia-smi"],
        "gpus": bad_gpus,
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_invalid_gpus"


@pytest.mark.parametrize("good_gpus", ["all", "none", "0", "2", "device=0,1", '"device=0,1"'])
def test_remote_docker_run_accepts_valid_gpus(monkeypatch, good_gpus):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(returncode=0))

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_good_gpus",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["nvidia-smi"],
        "gpus": good_gpus,
    }

    result = execute_remote_docker_run_operation(request)
    assert result["execution_status"] == "remote_docker_run_completed"


def test_remote_docker_run_rejects_option_like_ssh_host(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for an option-like ssh_host")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_bad_host",
        "ssh_host": "-oProxyCommand=touch /tmp/pwned",
        "image": "example-engine:dev",
        "command": ["nvidia-smi"],
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_ssh_host_invalid"


def test_remote_docker_run_detached_service_command(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _completed(returncode=0, stdout="c0ffee1234567890\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "staging_engine",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["uvicorn", "app:api", "--port", "8080"],
        "gpus": "all",
        "name": "staging-engine",
        "detach": True,
        "ports": ["18080:8080", "19090:9090"],
        "volumes": ["/h:/c"],
        "timeout_sec": 60,
    }

    result = execute_remote_docker_run_operation(request)

    remote = calls[0]["cmd"][2]
    assert remote == (
        "docker run -d --gpus all --name staging-engine -v /h:/c "
        "-p 18080:8080 -p 19090:9090 example-engine:dev uvicorn app:api --port 8080"
    )
    # A detached service container must survive exit, so --rm must NOT be present.
    assert "--rm" not in remote
    # The timeout only bounds the start-up confirmation of the detached run.
    assert calls[0]["kwargs"].get("timeout") == 60
    assert result["detach"] is True
    assert result["container_id"] == "c0ffee1234567890"
    assert result["container_name"] == "staging-engine"


def test_remote_docker_run_detach_requires_name(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for a detached run without a name")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "staging_engine_noname",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["uvicorn", "app:api"],
        "detach": True,
        "ports": ["18080:8080"],
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_name_required"


@pytest.mark.parametrize(
    "bad_port",
    ["8080", "abc:80", "80:80:80", "", "127.0.0.1:18080:8080", "18080:8080; rm -rf /",
     "18080:8080\n", "1:80", "184080:8080"],
)
def test_remote_docker_run_rejects_invalid_port(monkeypatch, bad_port):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for an invalid port")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "staging_engine_bad_port",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["uvicorn", "app:api"],
        "name": "staging-engine",
        "detach": True,
        "ports": [bad_port],
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_invalid_port"
    assert exc.value.details["port"] == bad_port


def test_remote_docker_run_rejects_non_list_ports(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run must not run for non-list ports")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "staging_engine_bad_ports",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["uvicorn", "app:api"],
        "name": "staging-engine",
        "detach": True,
        "ports": "18080:8080",
    }

    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == "operation.remote_docker_run_invalid_ports"


def test_remote_docker_run_non_detached_command_is_unchanged(monkeypatch):
    """Regression guard: the argv of the pre-existing (one-off, --rm) path must not
    change by a single character now that detach/ports exist."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _completed(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_all_opts",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["nvidia-smi", "-L"],
        "gpus": "device=0,1",
        "network": "host",
        "name": "bench-1",
        "workdir": "/work",
        "env": {"K": "V", "K2": "v 2"},
        "volumes": ["/h:/c", "/h2:/c2:ro"],
    }

    result = execute_remote_docker_run_operation(request)

    assert calls[0]["cmd"][2] == (
        "docker run --rm --gpus device=0,1 --network host --name bench-1 -w /work "
        "-e K=V -e K2='v 2' -v /h:/c -v /h2:/c2:ro example-engine:dev nvidia-smi -L"
    )
    # No detach-only keys leak into the one-off result payload.
    assert "detach" not in result
    assert "container_id" not in result


def test_remote_docker_run_shell_quotes_command_argv(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _completed(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_docker_run",
        "operation": "bench_quote",
        "ssh_host": "training-host.example",
        "image": "example-engine:dev",
        "command": ["bash", "-lc", "echo hi; rm -rf /"],
    }

    execute_remote_docker_run_operation(request)

    remote = calls[0]["cmd"][2]
    # The dangerous argv element must be single-quoted, not passed raw.
    assert "'echo hi; rm -rf /'" in remote
    # And the raw (unquoted) injection must NOT appear verbatim outside the quotes.
    assert remote.endswith("example-engine:dev bash -lc 'echo hi; rm -rf /'")
