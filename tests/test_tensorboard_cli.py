import json
import subprocess
import sys
from datetime import UTC, datetime


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def write_registry(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    (root / "containers").mkdir(exist_ok=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "demo",
                "current_experiment_id": "exp1",
                "current_run_name": "run1",
                "current_checkpoint": "/workspace/training_runs/run1/checkpoints/checkpoint.pt",
                "current_model_arch": "arch1",
                "must_read_external_ref_ids": [],
                "verified_by": "test-agent",
                "last_verified_at": now,
                "observability": {"tensorboard": {"required": True, "port": 6123}},
            }
        )
    )
    (root / "experiments" / "exp1.yaml").write_text(
        """experiment_id: exp1
status: active
summary: Demo experiment
observability:
  tensorboard:
    required: true
    container_id: run1_tensorboard
external_refs: []
"""
    )
    (root / "runs" / "run1.yaml").write_text(
        """run_name: run1
experiment_id: exp1
status: running
model_arch: arch1
checkpoint:
  latest: /workspace/training_runs/run1/checkpoints/checkpoint.pt
outputs:
  tensorboard: /workspace/training_runs/run1/tensorboard
"""
    )
    (root / "containers" / "run1_tensorboard.yaml").write_text(
        """schema_version: 1
kind: docker_container
container_id: run1_tensorboard
role: tensorboard
docker:
  name: demo-run1-tensorboard
  image: tensorflow/tensorflow:2.16.1
network_mode: host
workdir: /workspace/training_runs
mounts:
  - source: env:HOST_TRAINING_RUNS_ROOT
    target: /workspace/training_runs
    mode: ro
related_runs:
  - run1
"""
    )


def test_tensorboard_ensure_current_emits_operation_from_project_policy(tmp_path):
    write_registry(tmp_path)

    result = run_cli("tensorboard", "ensure-current", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["required"] is True
    assert data["run_name"] == "run1"
    operation = data["operation"]
    assert operation["request"]["adapter"] == "tensorboard_service"
    assert operation["request"]["action"] == "ensure-running"
    assert operation["request"]["container_id"] == "run1_tensorboard"
    assert operation["request"]["port"] == 6123
    assert operation["request"]["logdir"] == "/workspace/training_runs/run1/tensorboard"


def test_tensorboard_ensure_current_writes_operation_file(tmp_path):
    write_registry(tmp_path)
    op_path = tmp_path / "ops" / "tb.json"

    result = run_cli(
        "tensorboard",
        "ensure-current",
        "--project-root",
        str(tmp_path),
        "--run-name",
        "run1",
        "--write-operation",
        str(op_path),
        "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["operation_file"] == str(op_path)
    saved = json.loads(op_path.read_text())
    assert saved["request"]["adapter"] == "tensorboard_service"


def test_tensorboard_ensure_current_fails_closed_when_required_but_no_logdir(tmp_path):
    write_registry(tmp_path)
    (tmp_path / "runs" / "run1.yaml").write_text(
        """run_name: run1
experiment_id: exp1
status: running
model_arch: arch1
checkpoint:
  latest: /workspace/training_runs/run1/checkpoints/checkpoint.pt
"""
    )

    result = run_cli("tensorboard", "ensure-current", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "tensorboard.logdir_missing"
