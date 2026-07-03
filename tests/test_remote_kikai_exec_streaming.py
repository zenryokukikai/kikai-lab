import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, env=None):
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False, text=True, capture_output=True, env=run_env, cwd=REPO_ROOT,
    )


def write_streaming_fake_ssh(tmp_path):
    """A fake ssh that emits incremental @@STREAM@@ progress lines (as the real
    remote driver now does) followed by the structured dry-run / exec events."""
    fake = tmp_path / "fake_ssh_stream.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'event': 'remote_kikai_dry_run', 'returncode': 0}), flush=True)\n"
        "print('@@STREAM@@ remote training step 1 alpha', flush=True)\n"
        "print('@@STREAM@@ remote training step 2 beta', flush=True)\n"
        "print(json.dumps({'event': 'remote_kikai_exec', 'returncode': 0}), flush=True)\n"
    )
    fake.chmod(0o755)
    return fake


def write_op(tmp_path):
    """A minimal generic remote_kikai_exec operation. The exec adapter only needs a
    handful of request fields (ssh_host, remote_project_root, pipeline_run_id, and
    exactly one of remote/local operation_template); everything else the streaming
    behaviour under test does not depend on. Env-ref placeholders are resolved from
    env_for() at exec time, matching how real ops reference their environment."""
    op = {
        "request": {
            "operation": "remote_example_run_exec",
            "adapter": "remote_kikai_exec",
            "ssh_host": "${KIKAI_REMOTE_SSH_HOST}",
            "remote_project_root": "/remote/kikai-lab",
            "remote_operation_template": "ops/example_run_exec.json",
            "pipeline_run_id": "${KIKAI_PIPELINE_RUN_ID}",
            "uv_bin": "${KIKAI_REMOTE_UV_BIN}",
        }
    }
    dest = tmp_path / "remote_example_run_exec.json"
    dest.write_text(json.dumps(op, indent=2) + "\n")
    return dest


def env_for(fake_ssh):
    return {
        "KIKAI_REMOTE_SSH_HOST": "training-host.example",
        "KIKAI_REMOTE_UV_BIN": "/remote/bin/uv",
        "KIKAI_SSH_BIN": str(fake_ssh),
        "KIKAI_PIPELINE_RUN_ID": "stream_test",
    }


def test_remote_kikai_exec_streams_progress_lines_to_stderr_not_result(tmp_path):
    op = write_op(tmp_path)
    fake = write_streaming_fake_ssh(tmp_path)
    assert run_cli("target", "dry-run", str(op)).returncode == 0

    result = run_cli("exec", str(op), env=env_for(fake))
    assert result.returncode == 0, result.stdout + result.stderr

    # incremental progress was forwarded live to the caller's stderr
    assert "remote training step 1 alpha" in result.stderr
    assert "remote training step 2 beta" in result.stderr

    # the structured result keeps the final events but NOT the raw @@STREAM@@ lines
    payload = json.loads(result.stdout)
    tail = payload["data"]["stdout_tail"]
    assert "@@STREAM@@" not in tail
    assert "remote_kikai_exec" in tail
