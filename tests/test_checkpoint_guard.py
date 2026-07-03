import json
import os
import subprocess
import sys


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
    )


def write_current(project_root):
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "example",
                "current_experiment_id": "exp1",
                "current_run_name": "example_run",
                "current_checkpoint": "/runs/example_run/checkpoints/checkpoint_step_003000.pt",
                "current_model_arch": "example_arch_v1",
                "artifact_class_allowed_next": [
                    "visual_only_renderer_qc",
                    "pose_sensitivity_renderer_qc",
                ],
                "artifact_class_forbidden_next": ["full_example_qc_claim_without_audio_to_pose"],
                "do_not_use_as_current": ["stale_renderer_run", "discarded_latent_route_run"],
                "verified_by": "test",
                "last_verified_at": "2026-06-25T00:00:00Z",
                "staleness_warn_after_hours": 999999,
                "staleness_block_after_hours": 999999,
            },
            indent=2,
        )
    )


def write_guard_operation(
    path,
    project_root,
    *,
    guard_id="guard1",
    run_name="example_run",
    checkpoint="/runs/example_run/checkpoints/checkpoint_step_003000.pt",
    model_arch="example_arch_v1",
    artifact_class="visual_only_renderer_qc",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "checkpoint_qc_guard",
                    "project_root": str(project_root),
                    "adapter": "checkpoint_guard",
                    "guard_id": guard_id,
                    "run_name": run_name,
                    "checkpoint": checkpoint,
                    "model_arch": model_arch,
                    "artifact_class": artifact_class,
                },
            },
            indent=2,
        )
    )


def test_checkpoint_guard_passes_current_checkpoint_model_and_artifact_class(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "guard.json"
    write_guard_operation(op, project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "checkpoint_guard_passed"
    assert payload["data"]["guard_id"] == "guard1"
    record_path = project_root / "guard_records" / "guard1.json"
    record = json.loads(record_path.read_text())
    assert record["status"] == "passed"
    assert record["run_name"] == "example_run"
    assert record["checkpoint"] == "/runs/example_run/checkpoints/checkpoint_step_003000.pt"
    assert record["model_arch"] == "example_arch_v1"
    assert record["artifact_class"] == "visual_only_renderer_qc"


def test_checkpoint_guard_fails_closed_on_wrong_checkpoint_without_record(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "guard.json"
    write_guard_operation(
        op,
        project_root,
        checkpoint="/runs/stale_renderer_run/checkpoints/checkpoint.pt",
    )
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.checkpoint_guard_mismatch"
    assert payload["errors"][0]["details"]["field"] == "checkpoint"
    assert not (project_root / "guard_records" / "guard1.json").exists()


def test_checkpoint_guard_fails_closed_on_forbidden_artifact_class(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "guard.json"
    write_guard_operation(
        op,
        project_root,
        artifact_class="full_example_qc_claim_without_audio_to_pose",
    )
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.checkpoint_guard_forbidden_artifact_class"
    assert not (project_root / "guard_records" / "guard1.json").exists()


def test_checkpoint_guard_fails_closed_on_do_not_use_run(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "guard.json"
    write_guard_operation(op, project_root, run_name="stale_renderer_run")
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.checkpoint_guard_forbidden_run"
    assert not (project_root / "guard_records" / "guard1.json").exists()
