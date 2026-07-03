import subprocess
import types

import pytest

from kikai_lab.operation import (
    OperationError,
    execute_remote_file_push_operation,
)


def _completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_remote_file_push_happy_path(monkeypatch, tmp_path):
    src = tmp_path / "payload.txt"
    src.write_text("hi", encoding="utf-8")
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _completed(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_file_push",
        "operation": "push_one",
        "ssh_host": "training-host.example",
        "local_paths": [str(src)],
        "remote_dest_dir": "/tmp/kikai_sync",
        "target_id": "target-1",
    }
    result = execute_remote_file_push_operation(request)

    assert result["execution_status"] == "remote_file_push_completed"
    assert result["remote_dest_dir"] == "/tmp/kikai_sync"
    assert result["pushed"] == [{"local_path": str(src), "is_dir": False}]

    # First call: mkdir -p with a shlex-quoted dest dir.
    mkdir_cmd = calls[0]
    assert mkdir_cmd[:2] == ["ssh", "training-host.example"]
    assert mkdir_cmd[2] == "mkdir -p /tmp/kikai_sync"
    # Second call: scp argv with the host:dest as a single argv element.
    scp_cmd = calls[1]
    assert scp_cmd[0] == "scp"
    assert scp_cmd[-1] == "training-host.example:/tmp/kikai_sync/"


@pytest.mark.parametrize(
    "bad_dir",
    ["relative/dir", "/tmp/x; rm -rf /", "/tmp/../etc"],
)
def test_remote_file_push_rejects_bad_dest_dir(monkeypatch, tmp_path, bad_dir):
    src = tmp_path / "payload.txt"
    src.write_text("hi", encoding="utf-8")

    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("subprocess.run must not run for a bad dest dir")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_file_push",
        "operation": "push_bad",
        "ssh_host": "training-host.example",
        "local_paths": [str(src)],
        "remote_dest_dir": bad_dir,
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_file_push_operation(request)
    assert exc.value.code == "operation.remote_file_push_invalid_dest"


def test_remote_file_push_rejects_option_like_ssh_host(monkeypatch, tmp_path):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("subprocess.run must not run for option-like ssh_host")

    monkeypatch.setattr(subprocess, "run", fake_run)

    request = {
        "adapter": "remote_file_push",
        "operation": "push_bad_host",
        "ssh_host": "-oProxyCommand=touch /tmp/pwned",
        "local_paths": [str(tmp_path)],
        "remote_dest_dir": "/tmp/kikai_sync",
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_file_push_operation(request)
    assert exc.value.code == "operation.remote_ssh_host_invalid"
