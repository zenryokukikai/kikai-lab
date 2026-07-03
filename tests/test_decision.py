import json
import os
import subprocess
import sys

import pytest

from kikai_lab.decision import DecisionError, create_decision, decision_ids, load_decisions
from kikai_lab.store import CurrentState
from kikai_lab.validation import validate_registry_links


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False, text=True, capture_output=True, env=os.environ.copy(),
    )


def test_create_and_load_decision(tmp_path):
    res = create_decision(tmp_path, "D-1", title="Pick renderer", summary="full-res",
                          status="decided", decided_at="2026-07-01T00:00:00Z",
                          links=[{"kind": "experiment", "id": "exp1"}])
    assert res["decision_id"] == "D-1"
    assert (tmp_path / "decisions" / "D-1.yaml").exists()
    decisions = load_decisions(tmp_path)
    assert len(decisions) == 1 and decisions[0]["status"] == "decided"
    assert decision_ids(tmp_path) == {"D-1"}


def test_create_decision_rejects_bad_input(tmp_path):
    with pytest.raises(DecisionError):
        create_decision(tmp_path, "bad id!", title="x", summary="y")
    with pytest.raises(DecisionError):
        create_decision(tmp_path, "D-2", title="x", summary="y", status="nope")
    create_decision(tmp_path, "D-3", title="x", summary="y")
    with pytest.raises(DecisionError):  # no overwrite
        create_decision(tmp_path, "D-3", title="x2", summary="y2")


def test_internal_decision_satisfies_must_read(tmp_path):
    # project where current.must_read references D-9, satisfied by an INTERNAL decision
    (tmp_path / "experiments").mkdir()
    (tmp_path / "runs").mkdir()
    (tmp_path / "current.json").write_text(json.dumps({
        "schema_version": 1, "current_experiment_id": "exp1", "current_run_name": "run1",
        "must_read_external_ref_ids": ["D-9"],
    }))
    (tmp_path / "experiments" / "exp1.yaml").write_text(
        "schema_version: 1\nkind: experiment\nexperiment_id: exp1\n")  # NO external_refs
    (tmp_path / "runs" / "run1.yaml").write_text(
        "schema_version: 1\nkind: run\nrun_name: run1\nexperiment_id: exp1\n")
    state = CurrentState(current=json.loads((tmp_path / "current.json").read_text()),
                         age_hours=0.0, staleness="fresh")
    # before the decision exists -> must_read is unsatisfied
    errs_before = validate_registry_links(tmp_path, state)
    assert any(e["code"] == "current.must_read_not_in_external_refs" for e in errs_before)
    # create the internal decision -> must_read now satisfied (no external system needed)
    create_decision(tmp_path, "D-9", title="d9", summary="s")
    errs_after = validate_registry_links(tmp_path, state)
    assert not any(e["code"] == "current.must_read_not_in_external_refs" for e in errs_after)


def test_decision_cli_create_and_list(tmp_path):
    c = run_cli("decision", "create", "D-7", "--project-root", str(tmp_path),
                "--title", "use full-res renderer", "--summary", "no bottleneck seam",
                "--status", "decided", "--link", "experiment:exp-001")
    assert c.returncode == 0, c.stdout + c.stderr
    listed = run_cli("decision", "list", "--project-root", str(tmp_path))
    data = json.loads(listed.stdout)["data"]
    assert data["count"] == 1
    assert data["decisions"][0]["decision_id"] == "D-7"
    assert data["decisions"][0]["links"][0] == {"kind": "experiment", "id": "exp-001"}
    bad = run_cli("decision", "create", "D-8", "--project-root", str(tmp_path),
                  "--title", "x", "--link", "noseparator")
    assert bad.returncode == 2 and "link_invalid" in bad.stdout
