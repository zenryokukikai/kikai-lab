import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The two remote_kikai_exec operations below are authored inline (rather than read
# from checked-in fixtures) so the test stays self-contained and generic. Each mirrors
# the structure a real remote operation has: an `env` map of neutral env refs, a
# `remote_operation_template` path (resolved on the remote host, not materialized
# locally), and env-ref wiring for ssh_host / pipeline_run_id / uv_bin.
REMOTE_OP_NAME = "remote_example_run_resume_training_6000_to_20000.json"
REMOTE_QC_OP_NAME = "remote_example_run_checkpoint_qc_real_6000.json"

REMOTE_OP_REQUEST = {
    "adapter": "remote_kikai_exec",
    "env": {
        "CONTAINER_KIKAI_PROJECT_ROOT": "env:CONTAINER_KIKAI_PROJECT_ROOT",
        "CONTAINER_TRAINING_RUNS_ROOT": "env:CONTAINER_TRAINING_RUNS_ROOT",
        "CONTAINER_EXAMPLE_PROJECT_ROOT": "env:CONTAINER_EXAMPLE_PROJECT_ROOT",
        "HOST_TRAINING_RUNS_ROOT": "env:HOST_TRAINING_RUNS_ROOT",
        "HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT": (
            "env:HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT"
        ),
        "EXAMPLE_TRAINING_IMAGE": "env:EXAMPLE_TRAINING_IMAGE",
    },
    "operation": "remote_example_run_resume_training_6000_to_20000",
    "pipeline_run_id": "env:KIKAI_PIPELINE_RUN_ID",
    "remote_operation_template": (
        "examples/example_project/ops/example_run_resume_training_6000_to_20000.json"
    ),
    "remote_project_root": "env:KIKAI_REMOTE_PROJECT_ROOT",
    "ssh_host": "env:KIKAI_REMOTE_SSH_HOST",
    "string_replacements": [
        {
            "from": "example_run_resume_training_6000_to_20000_001",
            "to": "env:KIKAI_PIPELINE_RUN_ID",
        }
    ],
    "uv_bin": "env:KIKAI_REMOTE_UV_BIN",
}

REMOTE_QC_OP_REQUEST = {
    "adapter": "remote_kikai_exec",
    "env": {
        "CONTAINER_KIKAI_PROJECT_ROOT": "env:CONTAINER_KIKAI_PROJECT_ROOT",
        "CONTAINER_TRAINING_RUNS_ROOT": "env:CONTAINER_TRAINING_RUNS_ROOT",
        "CONTAINER_EXAMPLE_PROJECT_ROOT": "env:CONTAINER_EXAMPLE_PROJECT_ROOT",
        "HOST_TRAINING_RUNS_ROOT": "env:HOST_TRAINING_RUNS_ROOT",
        "HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT": (
            "env:HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT"
        ),
        "EXAMPLE_TRAINING_IMAGE": "env:EXAMPLE_TRAINING_IMAGE",
        "EXAMPLE_RUN_REAL_DIAGNOSTIC_PATH": "env:EXAMPLE_RUN_REAL_DIAGNOSTIC_PATH",
        "EXAMPLE_RUN_REAL_PREVIEW_PATH": "env:EXAMPLE_RUN_REAL_PREVIEW_PATH",
        "EXAMPLE_RUN_REAL_SUMMARY_PATH": "env:EXAMPLE_RUN_REAL_SUMMARY_PATH",
        "HOST_FACE_CACHE_ROOT": "env:HOST_FACE_CACHE_ROOT",
        "CONTAINER_FACE_CACHE_ROOT": "env:CONTAINER_FACE_CACHE_ROOT",
        "HOST_KIKAI_EXAMPLE_ENGINE_SOURCE_SNAPSHOT_ROOT": (
            "env:HOST_KIKAI_EXAMPLE_ENGINE_SOURCE_SNAPSHOT_ROOT"
        ),
        "CONTAINER_EXAMPLE_ENGINE_ROOT": "env:CONTAINER_EXAMPLE_ENGINE_ROOT",
    },
    "operation": "remote_example_run_checkpoint_qc_real_6000",
    "pipeline_run_id": "env:KIKAI_PIPELINE_RUN_ID",
    "remote_operation_template": (
        "examples/example_project/ops/example_run_checkpoint_qc_real.json"
    ),
    "remote_project_root": "env:KIKAI_REMOTE_PROJECT_ROOT",
    "ssh_host": "env:KIKAI_REMOTE_SSH_HOST",
    "string_replacements": [
        {
            "from": "example_run_checkpoint_qc_real_001",
            "to": "env:KIKAI_PIPELINE_RUN_ID",
        }
    ],
    "uv_bin": "env:KIKAI_REMOTE_UV_BIN",
}


def write_remote_operation(tmp_path, name, request):
    """Author a generic remote_kikai_exec operation inline in tmp_path.

    Mirrors the structure the checked-in fixtures had, but with neutral content and
    a concrete remote_project_root (so the operation does not depend on the
    KIKAI_REMOTE_PROJECT_ROOT env ref during dry-run)."""
    op = {"schema_version": 1, "kind": "kikai_operation", "request": dict(request)}
    op["request"]["remote_project_root"] = "/remote/kikai-lab"
    dest = tmp_path / name
    dest.write_text(json.dumps(op, indent=2) + "\n")
    return dest


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


def remote_exec_env(fake_ssh, pipeline_run_id):
    return {
        "CONTAINER_KIKAI_PROJECT_ROOT": "/workspace/kikai_project",
        "CONTAINER_TRAINING_RUNS_ROOT": "/workspace/training_runs",
        "CONTAINER_EXAMPLE_PROJECT_ROOT": "/workspace/example_project",
        "HOST_TRAINING_RUNS_ROOT": "/host/training_runs",
        "HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT": "/host/example-project-snapshot",
        "HOST_KIKAI_EXAMPLE_ENGINE_SOURCE_SNAPSHOT_ROOT": "/host/example-engine-snapshot",
        "KIKAI_PROGRESS_WEBHOOK_URL": "https://example.invalid/webhook/progress-token",
        "KIKAI_QC_WEBHOOK_URL": "https://example.invalid/webhook/qc-token",
        "KIKAI_REMOTE_SSH_HOST": "training-host.example",
        "KIKAI_REMOTE_UV_BIN": "/remote/bin/uv",
        "KIKAI_SSH_BIN": str(fake_ssh),
        "KIKAI_PIPELINE_RUN_ID": pipeline_run_id,
        "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
        "EXAMPLE_RUN_REAL_DIAGNOSTIC_PATH": "/host/training_runs/example_run/qc/diagnostic.mp4",
        "EXAMPLE_RUN_REAL_PREVIEW_PATH": "/host/training_runs/example_run/qc/preview.mp4",
        "EXAMPLE_RUN_REAL_SUMMARY_PATH": "/host/training_runs/example_run/qc/summary.json",
        "CONTAINER_FACE_CACHE_ROOT": "/workspace/face_cache",
        "CONTAINER_EXAMPLE_ENGINE_ROOT": "/workspace/example_engine",
        "HOST_FACE_CACHE_ROOT": "/host/face_cache",
    }


def test_remote_kikai_exec_runs_remote_template_through_structured_ssh(tmp_path):
    op = write_remote_operation(tmp_path, REMOTE_OP_NAME, REMOTE_OP_REQUEST)
    fake_ssh, argv_path, stdin_path = write_fake_ssh(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli(
        "exec",
        str(op),
        env=remote_exec_env(
            fake_ssh,
            "example_run_resume_training_6000_to_20000_test",
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["execution_status"] == "remote_kikai_exec_completed"
    assert data["pipeline_run_id"] == "example_run_resume_training_6000_to_20000_test"
    assert "secret" not in result.stdout
    assert json.loads(argv_path.read_text()) == ["training-host.example", "python3", "-"]
    stdin_payload = stdin_path.read_text()
    assert "remote_operation_template" in stdin_payload
    assert "example_run_resume_training_6000_to_20000.json" in stdin_payload
    assert "example_run_resume_training_6000_to_20000_test" in stdin_payload


def test_remote_kikai_exec_supports_real_checkpoint_qc_template(tmp_path):
    op = write_remote_operation(tmp_path, REMOTE_QC_OP_NAME, REMOTE_QC_OP_REQUEST)
    fake_ssh, argv_path, stdin_path = write_fake_ssh(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli(
        "exec",
        str(op),
        env=remote_exec_env(fake_ssh, "example_run_checkpoint_qc_real_6000_test"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["execution_status"] == "remote_kikai_exec_completed"
    assert data["pipeline_run_id"] == "example_run_checkpoint_qc_real_6000_test"
    assert "secret" not in result.stdout
    assert json.loads(argv_path.read_text()) == ["training-host.example", "python3", "-"]
    stdin_payload = stdin_path.read_text()
    assert "example_run_checkpoint_qc_real.json" in stdin_payload
    assert "example_run_checkpoint_qc_real_6000_test" in stdin_payload
    assert "delivery_id" in stdin_payload



def test_remote_kikai_exec_materializes_local_project_payload(tmp_path):
    project_root = tmp_path / "target_project"
    (project_root / "ops").mkdir(parents=True)
    (project_root / "containers").mkdir()
    (project_root / "script_bundles" / "bundle1" / "root" / "scripts").mkdir(parents=True)
    (project_root / "current.json").write_text(
        json.dumps({"schema_version": 1, "last_verified_at": "2026-06-26T00:00:00Z"})
    )
    (project_root / "containers" / "runner.yaml").write_text("container_id: runner\n")
    (project_root / "script_bundles" / "bundle1" / "bundle.json").write_text(
        json.dumps({"kind": "kikai_script_bundle", "bundle_id": "bundle1"})
    )
    (project_root / "script_bundles" / "bundle1" / "root" / "scripts" / "run.py").write_text(
        "print('ok')\n"
    )
    template = project_root / "ops" / "local_template.json"
    template.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "adapter": "script_bundle_run",
                    "operation": "bundle_gate",
                    "project_root": str(project_root),
                    "bundle_id": "bundle1",
                    "entrypoint": "run",
                    "container_id": "runner",
                },
            }
        )
    )
    remote_op = tmp_path / "remote_materialize.json"
    remote_op.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "adapter": "remote_kikai_exec",
                    "operation": "remote_bundle_gate",
                    "ssh_host": "training-host.example",
                    "remote_project_root": "/remote/kikai-lab",
                    "local_operation_template": str(template),
                    "local_project_root": str(project_root),
                    "local_project_payload_paths": [
                        "current.json",
                        "containers/runner.yaml",
                        "script_bundles/bundle1/bundle.json",
                        "script_bundles/bundle1/root/scripts/run.py",
                    ],
                    "remote_payload_project_root": "/tmp/kikai_payload_project",
                    "remote_operation_path": "/tmp/kikai_payload_project/ops/materialized.json",
                    "pipeline_run_id": "materialize_test_001",
                },
            }
        )
        + "\n"
    )
    fake_ssh, _argv_path, stdin_path = write_fake_ssh(tmp_path)
    dry_run = run_cli("target", "dry-run", str(remote_op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(remote_op), env={"KIKAI_SSH_BIN": str(fake_ssh)})

    assert result.returncode == 0, result.stdout + result.stderr
    stdin_payload = stdin_path.read_text()
    assert "operation_payload" in stdin_payload
    assert "project_payload_files" in stdin_payload
    assert "current.json" in stdin_payload
    assert "script_bundles/bundle1/root/scripts/run.py" in stdin_payload
    assert str(project_root) not in stdin_payload
    assert "/tmp/kikai_payload_project" in stdin_payload
