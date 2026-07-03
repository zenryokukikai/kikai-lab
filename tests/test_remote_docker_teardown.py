import subprocess
import types

import pytest

from kikai_lab.operation import (
    OperationError,
    execute_remote_docker_teardown_operation,
)

# `docker ps -a` rows: Names|State|Status|Image|RunningFor
_PS_OUTPUT = (
    "job-alpha|running|Up 2 hours|engine:dev|2 hours ago\n"
    "job-beta|exited|Exited (0)|engine:dev|3 hours ago\n"
    "unrelated-db|running|Up 1 day|postgres:16|1 day ago\n"
)


def _completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _ps_runner(rm_calls):
    def fake_run(cmd, *args, **kwargs):
        shell = cmd[2]
        if shell.startswith("docker ps -a"):
            return _completed(returncode=0, stdout=_PS_OUTPUT)
        if shell.startswith("docker rm -f"):
            rm_calls.append(shell)
            return _completed(returncode=0)
        return _completed(returncode=0)

    return fake_run


def test_teardown_pattern_is_anchored_not_substring(monkeypatch):
    rm_calls = []
    monkeypatch.setattr(subprocess, "run", _ps_runner(rm_calls))

    # "job-beta" as a substring pattern would, under the old `.search`, match the
    # whole "job-beta" name; but a partial pattern like "job-be" must NOT fullmatch
    # any container, so nothing is selected/removed.
    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "training-host.example",
        "name_pattern": "job-be",
    }
    result = execute_remote_docker_teardown_operation(request)
    assert result["selected"] == []
    assert rm_calls == []


def test_teardown_dot_pattern_does_not_select_everything(monkeypatch):
    rm_calls = []
    monkeypatch.setattr(subprocess, "run", _ps_runner(rm_calls))

    # "." with the old unanchored search matched EVERY container name (each has at
    # least one char) and removed all of them. With fullmatch, "." matches only a
    # single-character name — none here — so nothing is removed.
    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "training-host.example",
        "name_pattern": ".",
    }
    result = execute_remote_docker_teardown_operation(request)
    assert result["selected"] == []
    assert rm_calls == []


def test_teardown_full_pattern_selects_and_removes(monkeypatch):
    rm_calls = []
    monkeypatch.setattr(subprocess, "run", _ps_runner(rm_calls))

    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "training-host.example",
        "name_pattern": "job-.*",
    }
    result = execute_remote_docker_teardown_operation(request)
    assert result["selected"] == ["job-alpha", "job-beta"]
    # The argv shape is [ssh_bin, ssh_host, "docker rm -f <name>"].
    assert rm_calls == ["docker rm -f job-alpha", "docker rm -f job-beta"]


def test_teardown_list_only_does_not_remove(monkeypatch):
    rm_calls = []
    monkeypatch.setattr(subprocess, "run", _ps_runner(rm_calls))

    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "training-host.example",
        "name_pattern": "job-.*",
        "list_only": True,
    }
    result = execute_remote_docker_teardown_operation(request)
    assert result["list_only"] is True
    assert result["selected"] == ["job-alpha", "job-beta"]
    assert rm_calls == []


def test_teardown_rejects_too_long_pattern(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _ps_runner([]))
    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "training-host.example",
        "name_pattern": "a" * 201,
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_teardown_operation(request)
    assert exc.value.code == "operation.remote_docker_teardown_invalid_pattern"


def test_teardown_rejects_invalid_regex(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _ps_runner([]))
    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "training-host.example",
        "name_pattern": "job(",
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_teardown_operation(request)
    assert exc.value.code == "operation.remote_docker_teardown_invalid_pattern"


def test_teardown_rejects_option_like_ssh_host(monkeypatch):
    def fake_run(cmd, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("subprocess.run must not run for an option-like ssh_host")

    monkeypatch.setattr(subprocess, "run", fake_run)
    request = {
        "adapter": "remote_docker_teardown",
        "ssh_host": "-oProxyCommand=touch /tmp/pwned",
        "name_pattern": "job-.*",
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_teardown_operation(request)
    assert exc.value.code == "operation.remote_ssh_host_invalid"
