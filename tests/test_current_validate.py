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


def write_current(root, last_verified_at, warn=72, block=168):
    root.mkdir(parents=True, exist_ok=True)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "demo",
                "current_experiment_id": "exp1",
                "current_run_name": "run1",
                "current_checkpoint": (
                    "${CONTAINER_TRAINING_RUNS_ROOT}/run1/checkpoints/checkpoint.pt"
                ),
                "current_model_arch": "arch1",
                "must_read_external_ref_ids": ["EXAMPLE-REF-001"],
                "verified_by": "test-agent",
                "last_verified_at": last_verified_at,
                "staleness_warn_after_hours": warn,
                "staleness_block_after_hours": block,
                "established_by_decision_id": "decision-old",
                "next_decision_id": "decision-next",
                "next_decision_required": True,
            }
        )
    )


def test_current_reports_fresh_state(tmp_path):
    write_current(tmp_path, datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    result = run_cli("current", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["current"]["current_experiment_id"] == "exp1"
    assert payload["data"]["staleness"] == "fresh"
    assert payload["data"]["age_hours"] >= 0


def test_current_reports_warn_state(tmp_path):
    old = datetime.now(UTC) - timedelta(hours=80)
    write_current(tmp_path, old.isoformat().replace("+00:00", "Z"))

    result = run_cli("current", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["staleness"] == "warn"
    assert payload["warnings"][0]["code"] == "current.staleness_warn"


def test_validate_blocks_stale_current(tmp_path):
    old = datetime.now(UTC) - timedelta(hours=200)
    write_current(tmp_path, old.isoformat().replace("+00:00", "Z"))

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "current.stale"
    assert payload["errors"][0]["blocking"] is True
