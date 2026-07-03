import hashlib
import json
import subprocess
import sys


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def write_source_snapshot(root, source_snapshot_id="example_project_v1"):
    snapshot_root = root / "source_snapshots" / source_snapshot_id
    script = snapshot_root / "root" / "scripts" / "train.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('snapshot')\n")

    (snapshot_root / "snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_source_snapshot",
                "source_snapshot_id": source_snapshot_id,
                "immutable": True,
                "generated_by": {
                    "tool": "kikai source-snapshot create",
                    "schema_version": 1,
                },
                "files": [
                    {
                        "path": "root/scripts/train.py",
                        "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
        + "\n"
    )


def write_registry(
    root,
    *,
    mount_source="env:HOST_EXAMPLE_PROJECT_ROOT",
    source_kind=None,
    source_snapshot_id=None,
):
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    (root / "containers").mkdir(exist_ok=True)
    checkpoint = "${CONTAINER_TRAINING_RUNS_ROOT}/run1/checkpoints/checkpoint.pt"
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "demo",
                "current_experiment_id": "exp1",
                "current_run_name": "run1",
                "current_checkpoint": checkpoint,
                "current_model_arch": "arch1",
                "must_read_external_ref_ids": ["EXAMPLE-REF-001"],
                "verified_by": "test-agent",
                "last_verified_at": "2026-06-26T00:00:00Z",
                "staleness_warn_after_hours": 999999,
                "staleness_block_after_hours": 1000000,
                "established_by_decision_id": "decision-old",
                "next_decision_id": "decision-next",
                "next_decision_required": True,
                "required_container_ids": ["run1_training"],
            }
        )
    )
    (root / "experiments" / "exp1.yaml").write_text(
        """experiment_id: exp1
status: active
summary: Demo experiment
external_refs:
  - provider: example_design_registry
    id: EXAMPLE-REF-001
    kind: design
    required: true
next_actions:
  - id: review_next_checkpoint
    kind: human_review
    status: proposed
"""
    )
    (root / "runs" / "run1.yaml").write_text(
        f"""run_name: run1
experiment_id: exp1
status: completed
model_arch: arch1
checkpoint:
  latest: {checkpoint}
"""
    )
    source_kind_line = f"    source_kind: {source_kind}\n" if source_kind else ""
    source_snapshot_id_line = (
        f"    source_snapshot_id: {source_snapshot_id}\n" if source_snapshot_id else ""
    )
    (root / "containers" / "run1_training.yaml").write_text(
        f"""schema_version: 1
kind: docker_container
container_id: run1_training
role: training
status: desired_running
docker:
  name: example-run1-training
  image: env:EXAMPLE_TRAINING_IMAGE
workdir: /workspace/example_project
mounts:
  - source: {mount_source}
    target: /workspace/example_project
{source_kind_line}{source_snapshot_id_line}    mode: ro
  - source: env:HOST_TRAINING_RUNS_ROOT
    target: /workspace/training_runs
    mode: rw
"""
    )


def test_validate_rejects_live_source_repo_mount_for_code_paths(tmp_path):
    write_registry(tmp_path, mount_source="env:HOST_EXAMPLE_PROJECT_ROOT")

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "container.live_repo_mount_forbidden"


def test_validate_allows_registered_kikai_managed_source_snapshot_mount(tmp_path):
    write_source_snapshot(tmp_path, "example_project_v1")
    write_registry(
        tmp_path,
        mount_source="env:HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT",
        source_kind="kikai_managed_source_snapshot",
        source_snapshot_id="example_project_v1",
    )

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
