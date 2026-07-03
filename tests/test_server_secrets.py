import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A minimal, generic remote_kikai_exec operation authored inline (no on-disk
# fixture project). It exercises env-ref resolution from registered server
# settings (env:NAME) and from the stored secret: pipeline_run_id, every env
# value, the uv_bin, ssh_host and the string_replacement `to` are all env-refs
# that remote_kikai_exec resolves through the server settings + secret store
# before building the remote payload.
REMOTE_OP_NAME = "remote_example_run_resume.json"
REMOTE_OP_REQUEST = {
    "adapter": "remote_kikai_exec",
    "operation": "remote_example_run_resume",
    "ssh_host": "env:KIKAI_REMOTE_SSH_HOST",
    "remote_project_root": "env:KIKAI_REMOTE_PROJECT_ROOT",
    "uv_bin": "env:KIKAI_REMOTE_UV_BIN",
    "pipeline_run_id": "env:KIKAI_PIPELINE_RUN_ID",
    "remote_operation_template": "ops/example_run_resume.json",
    "env": {
        "CONTAINER_KIKAI_PROJECT_ROOT": "env:CONTAINER_KIKAI_PROJECT_ROOT",
        "CONTAINER_TRAINING_RUNS_ROOT": "env:CONTAINER_TRAINING_RUNS_ROOT",
        "CONTAINER_EXAMPLE_PROJECT_ROOT": "env:CONTAINER_EXAMPLE_PROJECT_ROOT",
        "HOST_TRAINING_RUNS_ROOT": "env:HOST_TRAINING_RUNS_ROOT",
        "HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT": (
            "env:HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT"
        ),
        "KIKAI_DISCORD_PROGRESS_WEBHOOK_URL": "env:KIKAI_DISCORD_PROGRESS_WEBHOOK_URL",
        "EXAMPLE_TRAINING_IMAGE": "env:EXAMPLE_TRAINING_IMAGE",
    },
    "string_replacements": [
        {"from": "example_run_resume_001", "to": "env:KIKAI_PIPELINE_RUN_ID"},
    ],
}
REMOTE_OP = {"schema_version": 1, "kind": "kikai_operation", "request": REMOTE_OP_REQUEST}


def run_cli(*args, env=None):
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
        env=run_env,
        cwd=REPO_ROOT,
    )


def write_fake_ssh(tmp_path):
    argv_path = tmp_path / "ssh_argv.json"
    stdin_path = tmp_path / "ssh_stdin.py"
    fake = tmp_path / "fake_ssh.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "payload = sys.stdin.read()\n"
        f"pathlib.Path({str(stdin_path)!r}).write_text(payload)\n"
        "print(json.dumps({'event': 'remote_kikai_dry_run', 'returncode': 0}))\n"
        "print(json.dumps({'event': 'remote_kikai_exec', 'returncode': 0}))\n"
    )
    fake.chmod(0o755)
    return fake, argv_path, stdin_path


def write_remote_operation(tmp_path):
    dest = tmp_path / REMOTE_OP_NAME
    dest.write_text(json.dumps(REMOTE_OP, indent=2) + "\n")
    return dest


def test_server_secret_set_stores_value_without_printing_it(tmp_path):
    env = {"KIKAI_SERVER_CONFIG_HOME": str(tmp_path / "server_config")}
    secret_value = "https://example.invalid/discord-webhook/test-token"

    result = run_cli(
        "server",
        "secret",
        "set",
        "KIKAI_DISCORD_PROGRESS_WEBHOOK_URL",
        "--value",
        secret_value,
        "--json",
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert secret_value not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"] == {
        "name": "KIKAI_DISCORD_PROGRESS_WEBHOOK_URL",
        "stored": True,
        "secret": True,
    }
    secret_file = tmp_path / "server_config" / "secrets.json"
    assert secret_file.exists()
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    stored = json.loads(secret_file.read_text())
    assert stored["KIKAI_DISCORD_PROGRESS_WEBHOOK_URL"] == secret_value


def test_remote_kikai_exec_resolves_env_refs_from_registered_server_settings_and_secrets(tmp_path):
    server_home = tmp_path / "server_config"
    fake_ssh, argv_path, stdin_path = write_fake_ssh(tmp_path)
    op = write_remote_operation(tmp_path)
    registrations = {
        "KIKAI_REMOTE_SSH_HOST": "training-host.example",
        "KIKAI_REMOTE_PROJECT_ROOT": "/remote/kikai-lab",
        "KIKAI_REMOTE_UV_BIN": "/remote/bin/uv",
        "KIKAI_PIPELINE_RUN_ID": "example_run_resume_secret_registry_test",
        "CONTAINER_KIKAI_PROJECT_ROOT": "/workspace/kikai_project",
        "CONTAINER_TRAINING_RUNS_ROOT": "/workspace/training_runs",
        "CONTAINER_EXAMPLE_PROJECT_ROOT": "/workspace/example_project",
        "HOST_TRAINING_RUNS_ROOT": "/host/training_runs",
        "HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT": "/host/example-project-snapshot",
        "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
    }
    env = {
        "KIKAI_SERVER_CONFIG_HOME": str(server_home),
        "KIKAI_SSH_BIN": str(fake_ssh),
    }
    for name, value in registrations.items():
        set_result = run_cli("server", "setting", "set", name, "--value", value, "--json", env=env)
        assert set_result.returncode == 0, set_result.stdout + set_result.stderr
    secret_value = "https://example.invalid/discord-webhook/test-token"
    set_secret = run_cli(
        "server",
        "secret",
        "set",
        "KIKAI_DISCORD_PROGRESS_WEBHOOK_URL",
        "--value",
        secret_value,
        "--json",
        env=env,
    )
    assert set_secret.returncode == 0, set_secret.stdout + set_secret.stderr

    dry_run = run_cli("target", "dry-run", str(op), env=env)
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    result = run_cli("exec", str(op), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert secret_value not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "remote_kikai_exec_completed"
    assert json.loads(argv_path.read_text()) == ["training-host.example", "python3", "-"]
    stdin_payload = stdin_path.read_text()
    assert "example_run_resume_secret_registry_test" in stdin_payload
    assert "/host/training_runs" in stdin_payload
    assert secret_value in stdin_payload
