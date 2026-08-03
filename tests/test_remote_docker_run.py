import subprocess
import types

import pytest

from kikai_lab import operation
from kikai_lab.operation import (
    OperationError,
    execute_remote_docker_build_operation,
    execute_remote_docker_run_operation,
    execute_remote_docker_teardown_operation,
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


# --------------------------------------------------------------------------------------
# ssh_host="local": the kikai server host IS the docker host (single-machine install).
#
# The point of every test below is NEGATIVE: docker must be invoked as a direct argv,
# never through ssh. A mock that only records calls would happily accept a regression
# back to the ssh path, so each test pins KIKAI_SSH_BIN to a sentinel that cannot appear
# anywhere in a correct local-mode invocation, and asserts on the WHOLE argv (not just
# "some element is present"): if the local branch ever routes through ssh again, the argv
# becomes [SSH_SENTINEL, "local", "docker run ..."] and the assertions fail.
# --------------------------------------------------------------------------------------

SSH_SENTINEL = "ssh-must-not-be-used"


def _no_ssh(monkeypatch):
    """Make any accidental use of the ssh path detectable in the recorded argv."""
    monkeypatch.setenv("KIKAI_SSH_BIN", SSH_SENTINEL)


def _assert_no_ssh(argv):
    assert SSH_SENTINEL not in argv, f"local mode went through ssh: {argv}"
    assert argv[0] == "docker", f"local mode must exec docker directly, got {argv[0]!r}"
    # A shelled-out command would arrive as ONE string holding the whole command line.
    assert not any(" " in a and a.startswith("docker ") for a in argv), (
        f"local mode must not assemble a shell command string: {argv}"
    )


def _must_not_run(*_a, **_k):  # pragma: no cover - only reached on a regression
    raise AssertionError("subprocess.run must not be reached")


def test_remote_docker_run_local_mode_runs_argv_without_ssh(monkeypatch):
    """ssh_host=local は ssh を介さず docker argv を直接実行する。"""
    calls = {}

    def fake_run(argv, text, capture_output, timeout):
        calls["argv"] = argv
        calls["timeout"] = timeout
        return _completed(0, "abc123\n", "")

    _no_ssh(monkeypatch)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = execute_remote_docker_run_operation({
        "operation": "local-op",
        "ssh_host": "local",
        "image": "example-engine:dev",
        "detach": True,
        "name": "staging-engine",
        "gpus": "all",
        "ports": ["18080:8080"],
        "volumes": ["/h/data:/c/data:ro"],
        "env": {"MAX_SESSIONS": "16"},
        "workdir": "/workspace",
        "command": ["uvicorn", "app:api"],
        "timeout_sec": 60,
    })
    argv = calls["argv"]
    _assert_no_ssh(argv)
    # Pinned whole-argv: every validated field lands as its own element, in order.
    assert argv == [
        "docker", "run", "-d",
        "--gpus", "all",
        "--name", "staging-engine",
        "-w", "/workspace",
        "-e", "MAX_SESSIONS=16",
        "-v", "/h/data:/c/data:ro",
        "-p", "18080:8080",
        "example-engine:dev", "uvicorn", "app:api",
    ]
    # A detached service container must survive exit, so --rm must NOT be present.
    assert "--rm" not in argv
    assert calls["timeout"] == 60
    assert result["container_id"] == "abc123"
    assert result["container_name"] == "staging-engine"


def test_remote_docker_run_local_mode_one_off_uses_rm_and_no_shell_quoting(monkeypatch):
    """local の非 detach も ssh を介さない。argv 直接実行なので shlex 引用符は付かない
    (付けば引用符そのものが docker への literal 引数になって壊れる)。"""
    calls = {}

    def fake_run(argv, text, capture_output, timeout):
        calls["argv"] = argv
        return _completed(0, "", "")

    _no_ssh(monkeypatch)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = execute_remote_docker_run_operation({
        "operation": "local-op",
        "ssh_host": "local",
        "image": "example-engine:dev",
        "env": {"K2": "v 2"},
        "command": ["bash", "-lc", "echo hi; rm -rf /"],
    })
    argv = calls["argv"]
    _assert_no_ssh(argv)
    assert argv == [
        "docker", "run", "--rm",
        "--gpus", "all",
        "-e", "K2=v 2",
        "example-engine:dev", "bash", "-lc", "echo hi; rm -rf /",
    ]
    # No shell is involved, so nothing may be shlex-quoted on the way in.
    assert "'v 2'" not in argv
    assert "'echo hi; rm -rf /'" not in argv
    # And a one-off run reports no detach fields.
    assert "detach" not in result


def _shifting_registry(monkeypatch, first, later):
    """`${NAME}` resolves through the on-disk server registry, which is re-read on EVERY
    lookup. This makes the SECOND lookup of a name return a different value, so a code
    path that resolves a field again (instead of reusing the value that was validated)
    is caught: the argv would carry a string validation never saw."""
    seen = {}

    def fake_registry(name):
        seen[name] = seen.get(name, 0) + 1
        return first if seen[name] == 1 else later

    monkeypatch.setattr(operation, "resolve_registered_value", fake_registry)


def test_remote_docker_run_local_mode_argv_carries_the_validated_value(monkeypatch):
    """argv には「検証を通ったその値」が入る。経路ごとに再解決すると、レジストリが
    読み直される間に値が変わり、検証を通っていない文字列が docker に渡りうる。"""
    calls = {}

    def fake_run(argv, text, capture_output, timeout):
        calls["argv"] = argv
        return _completed(0, "", "")

    _no_ssh(monkeypatch)
    _shifting_registry(monkeypatch, "18080:8080", "19999:9999; rm -rf /")
    monkeypatch.setattr(subprocess, "run", fake_run)
    execute_remote_docker_run_operation({
        "operation": "local-op",
        "ssh_host": "local",
        "image": "example-engine:dev",
        "name": "staging-engine",
        "ports": ["${KIKAI_TEST_PORT}"],
        "env": {"ENDPOINT": "${KIKAI_TEST_ENDPOINT}"},
        "command": ["true"],
    })
    argv = calls["argv"]
    _assert_no_ssh(argv)
    assert argv[argv.index("-p") + 1] == "18080:8080"
    assert argv[argv.index("-e") + 1] == "ENDPOINT=18080:8080"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("ports", ["18080:8080; rm -rf /"], "operation.remote_docker_run_invalid_port"),
        ("ports", ["127.0.0.1:18080:8080"], "operation.remote_docker_run_invalid_port"),
        ("volumes", ["/h:/c; rm -rf /"], "operation.remote_docker_run_invalid_volume"),
        ("workdir", "/work/../etc", "operation.remote_docker_run_invalid_workdir"),
        ("name", "bad name", "operation.remote_docker_run_invalid_name"),
    ],
)
def test_remote_docker_run_local_mode_validates_like_remote(monkeypatch, field, value, code):
    """local モードは検証を一切スキップしない (argv 直接実行でもホスト側は保護する)。"""
    _no_ssh(monkeypatch)
    monkeypatch.setattr(subprocess, "run", _must_not_run)
    request = {
        "operation": "local-op",
        "ssh_host": "local",
        "image": "example-engine:dev",
        "command": ["true"],
        field: value,
    }
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation(request)
    assert exc.value.code == code


def test_remote_docker_run_local_mode_detach_still_requires_name(monkeypatch):
    """detach の name 必須は local でも同じ (teardown で必ず消せることを投入時に保証)。"""
    _no_ssh(monkeypatch)
    monkeypatch.setattr(subprocess, "run", _must_not_run)
    with pytest.raises(OperationError) as exc:
        execute_remote_docker_run_operation({
            "operation": "local-op",
            "ssh_host": "local",
            "image": "example-engine:dev",
            "command": ["true"],
            "detach": True,
        })
    assert exc.value.code == "operation.remote_docker_run_name_required"


def test_remote_docker_teardown_local_mode_lists_and_removes_without_ssh(monkeypatch):
    """teardown も ssh_host=local で docker argv を直接実行する。listing だけ local 化して
    rm -f が ssh に残る片側移行が起きうるので、両方の argv を個別に固定する。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "ps", "-a"]:
            return _completed(0, "staging-engine|exited|Exited (0) 1m|img|1m\n", "")
        return _completed(0, "", "")

    _no_ssh(monkeypatch)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = execute_remote_docker_teardown_operation({
        "operation": "teardown-op",
        "ssh_host": "local",
        "container_names": ["staging-engine"],
    })
    assert len(calls) == 2
    listing, removal = calls
    _assert_no_ssh(listing)
    assert listing == [
        "docker", "ps", "-a", "--format",
        "{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}|{{.RunningFor}}",
    ]
    # The removal is a separate subprocess call and regressed to ssh once before,
    # so it is pinned on its own rather than via "is somewhere in calls".
    _assert_no_ssh(removal)
    assert removal == ["docker", "rm", "-f", "staging-engine"]
    assert result["results"] == [{"name": "staging-engine", "returncode": 0,
                                  "removed": True, "stderr": ""}]


def test_remote_docker_teardown_remote_mode_still_uses_ssh(monkeypatch):
    """local 化が remote 経路を壊していないこと (ssh 経由の文字列組み立ては不変)。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "docker ps -a" in argv[-1]:
            return _completed(0, "orphan|exited|Exited (0) 1m|img|1m\n", "")
        return _completed(0, "", "")

    monkeypatch.setenv("KIKAI_SSH_BIN", "ssh-stub")
    monkeypatch.setattr(subprocess, "run", fake_run)
    execute_remote_docker_teardown_operation({
        "operation": "teardown-op",
        "ssh_host": "training-host.example",
        "container_names": ["orphan"],
    })
    assert calls[0][:2] == ["ssh-stub", "training-host.example"]
    assert calls[1] == ["ssh-stub", "training-host.example", "docker rm -f orphan"]


def test_remote_docker_build_local_mode_writes_dockerfile_and_builds_without_ssh(
    monkeypatch, tmp_path
):
    """ssh_host=local の build は Dockerfile をローカルに書き、docker build を argv 直実行する。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _completed(0, "built\n", "")

    _no_ssh(monkeypatch)
    monkeypatch.setattr(subprocess, "run", fake_run)
    build_dir = tmp_path / "build"
    result = execute_remote_docker_build_operation({
        "operation": "build-op",
        "ssh_host": "local",
        "image_tag": "example-engine:dev",
        "dockerfile_content": "FROM scratch\n",
        "remote_build_dir": str(build_dir),
        "build_args": {"VERSION": "1.2.3"},
        "no_cache": True,
    })
    # The Dockerfile is written through the filesystem, NOT piped over ssh, so there is
    # exactly one subprocess call (the build) instead of mkdir + cat + build.
    assert len(calls) == 1
    _assert_no_ssh(calls[0])
    assert calls[0] == [
        "docker", "build", "--no-cache",
        "--build-arg", "VERSION=1.2.3",
        "-t", "example-engine:dev",
        "-f", str(build_dir / "Dockerfile"), str(build_dir),
    ]
    assert (build_dir / "Dockerfile").read_text() == "FROM scratch\n"
    assert result["image_tag"] == "example-engine:dev"


def test_remote_docker_build_local_mode_argv_carries_the_validated_value(
    monkeypatch, tmp_path
):
    """build_args も remote 用文字列と local argv で解決済みの値を共用する
    (再解決すると docker build に渡る値が検証時の値とずれる)。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _completed(0, "", "")

    _no_ssh(monkeypatch)
    _shifting_registry(monkeypatch, "1.2.3", "9.9.9-unvalidated")
    monkeypatch.setattr(subprocess, "run", fake_run)
    execute_remote_docker_build_operation({
        "operation": "build-op",
        "ssh_host": "local",
        "image_tag": "example-engine:dev",
        "dockerfile_content": "FROM scratch\n",
        "remote_build_dir": str(tmp_path / "build"),
        "build_args": {"VERSION": "${KIKAI_TEST_VERSION}"},
    })
    argv = calls[0]
    assert argv[argv.index("--build-arg") + 1] == "VERSION=1.2.3"


def test_remote_docker_build_remote_mode_still_pipes_over_ssh(monkeypatch):
    """local 化が remote build 経路を壊していないこと (mkdir + cat + build の3手)。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _completed(0, "", "")

    monkeypatch.setenv("KIKAI_SSH_BIN", "ssh-stub")
    monkeypatch.setattr(subprocess, "run", fake_run)
    execute_remote_docker_build_operation({
        "operation": "build-op",
        "ssh_host": "training-host.example",
        "image_tag": "example-engine:dev",
        "dockerfile_content": "FROM scratch\n",
        "remote_build_dir": "/tmp/kikai_docker_build",
        "build_args": {"VERSION": "1.2.3"},
    })
    assert [c[:2] for c in calls] == [["ssh-stub", "training-host.example"]] * 3
    assert calls[0][2] == "mkdir -p /tmp/kikai_docker_build"
    assert calls[1][2] == "cat > /tmp/kikai_docker_build/Dockerfile"
    assert calls[2][2] == (
        "docker build  --build-arg VERSION=1.2.3 -t example-engine:dev "
        "-f /tmp/kikai_docker_build/Dockerfile /tmp/kikai_docker_build"
    )
