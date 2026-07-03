import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def write_registry(root, *, stale=False):
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    last_verified = datetime.now(UTC) - timedelta(hours=200 if stale else 1)
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
                "last_verified_at": last_verified.isoformat().replace("+00:00", "Z"),
                "staleness_warn_after_hours": 72,
                "staleness_block_after_hours": 168,
                "established_by_decision_id": "decision-old",
                "next_decision_id": "decision-next",
                "next_decision_required": True,
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


def write_container(root, container_id="run1_training"):
    (root / "containers").mkdir(exist_ok=True)
    (root / "containers" / f"{container_id}.yaml").write_text(
        f"""schema_version: 1
kind: docker_container
container_id: {container_id}
role: training
status: desired_running
docker:
  name: example-run1-training
  image: env:EXAMPLE_TRAINING_IMAGE
"""
    )


def test_show_experiment_returns_record(tmp_path):
    write_registry(tmp_path)

    result = run_cli("show", "experiment", "exp1", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["experiment"]["experiment_id"] == "exp1"
    assert payload["data"]["experiment"]["summary"] == "Demo experiment"


def test_show_run_returns_record(tmp_path):
    write_registry(tmp_path)

    result = run_cli("show", "run", "run1", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["run"]["run_name"] == "run1"


def test_show_container_returns_record(tmp_path):
    write_registry(tmp_path)
    write_container(tmp_path, "run1_training")

    result = run_cli(
        "show",
        "container",
        "run1_training",
        "--project-root",
        str(tmp_path),
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["container"]["container_id"] == "run1_training"
    assert payload["data"]["container"]["docker"]["name"] == "example-run1-training"


def test_show_container_missing_returns_error(tmp_path):
    write_registry(tmp_path)

    result = run_cli("show", "container", "missing", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "show.container_missing"


def test_next_reports_validation_blocker_before_proposed_actions(tmp_path):
    write_registry(tmp_path, stale=True)

    result = run_cli("next", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["next_actions"][0]["id"] == "verify_current"
    assert payload["next_actions"][0]["blocking"] is True


def test_next_returns_proposed_experiment_actions_when_valid(tmp_path):
    write_registry(tmp_path)

    result = run_cli("next", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["next_actions"][0]["id"] == "review_next_checkpoint"
    assert payload["next_actions"][0]["blocking"] is False
