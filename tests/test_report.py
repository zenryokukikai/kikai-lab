import json
import os
import subprocess
import sys
from pathlib import Path

from kikai_lab.report import build_project_report, render_report_html


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False, text=True, capture_output=True, env=os.environ.copy(),
    )


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "experiments").mkdir(parents=True)
    (root / "containers").mkdir()
    root.joinpath("current.json").write_text(json.dumps({
        "schema_version": 1, "project_id": "lipsync",
        "current_experiment_id": "exp1", "current_run_name": "run9",
        "current_stage": "stage_a", "summary": "the project concept",
    }))
    root.joinpath("experiments", "exp1.yaml").write_text(
        "schema_version: 1\nkind: experiment\nexperiment_id: exp1\n"
        "title: First experiment\nsummary: does the thing\n"
        "external_refs:\n  - id: D-1\n    title: a decision\n"
    )
    root.joinpath("experiments", "exp2.yaml").write_text(
        "schema_version: 1\nkind: experiment\nexperiment_id: exp2\ntitle: Second\nsummary: also\n"
    )
    root.joinpath("containers", "run9_train.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: run9_train\n"
        "role: trainer\nstatus: ephemeral_run\nsummary: a run\n"
        "docker:\n  name: run9-train\n  image: engine:dev\n"
    )
    # a non-container yaml that must be ignored
    root.joinpath("containers", "notes.yaml").write_text("kind: other\n")
    return root


def test_build_project_report_aggregates_records(tmp_path):
    report = build_project_report(_make_project(tmp_path))
    assert report["kind"] == "kikai_project_report"
    assert report["project"]["project_id"] == "lipsync"
    assert report["project"]["summary"] == "the project concept"
    assert report["experiment_count"] == 2
    exp1 = next(e for e in report["experiments"] if e["experiment_id"] == "exp1")
    assert exp1["is_current"] is True
    assert exp1["title"] == "First experiment"
    assert exp1["external_refs"][0]["id"] == "D-1"
    assert report["run_count"] == 1  # the non-container yaml is ignored
    assert report["runs"][0]["container_id"] == "run9_train"
    assert report["runs"][0]["image"] == "engine:dev"
    assert report["runs"][0]["metrics"] is None


def test_render_report_html_is_self_contained_and_escapes_script(tmp_path):
    report = build_project_report(_make_project(tmp_path))
    html = render_report_html(report)
    assert html.startswith("<!doctype html>")
    assert "const REPORT=" in html
    assert "lipsync" in html
    # no raw </script> from the payload could break out
    assert "<\\/" in html or "</script>" not in html.split("const REPORT=")[1].split("</script>")[0]


def test_report_cli_writes_json_and_html(tmp_path):
    root = _make_project(tmp_path)
    out_json = tmp_path / "report.json"
    out_html = tmp_path / "dash.html"
    result = run_cli("report", "--project-root", str(root),
                     "--out", str(out_json), "--html", str(out_html))
    assert result.returncode == 0, result.stdout + result.stderr
    assert out_json.exists() and out_html.exists()
    data = json.loads(out_json.read_text())
    assert data["run_count"] == 1
    assert "<!doctype html>" in out_html.read_text()
