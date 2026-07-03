from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from kikai_lab.server.app import create_app
from kikai_lab.server.registry import PROJECT_DIRS, ServerConfig

ENVELOPE_KEYS = {"ok", "schema_version", "data", "warnings", "errors", "next_actions"}


def utc_now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_project(
    projects_root: Path,
    project_id: str,
    *,
    status: str = "active",
    with_current: bool = True,
) -> Path:
    root = projects_root / project_id
    for name in PROJECT_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    project_yaml = {
        "schema_version": 1,
        "project_id": project_id,
        "summary": f"{project_id} example project",
        "status": status,
        "created_at": utc_now_text(),
        "updated_at": utc_now_text(),
    }
    (root / "project.yaml").write_text(yaml.safe_dump(project_yaml), encoding="utf-8")
    if with_current:
        current = {
            "schema_version": 1,
            "project_id": project_id,
            "current_experiment_id": "example_exp",
            "current_run_name": "example_run",
            "last_verified_at": utc_now_text(),
            "verified_by": "test",
        }
        (root / "current.json").write_text(json.dumps(current), encoding="utf-8")
    (root / "runs" / "example_run.yaml").write_text(
        yaml.safe_dump(
            {"schema_version": 1, "run_name": "example_run", "experiment_id": "example_exp"}
        ),
        encoding="utf-8",
    )
    (root / "experiments" / "example_exp.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "kind": "experiment",
                "experiment_id": "example_exp",
                "title": "Example experiment",
                "summary": "example",
            }
        ),
        encoding="utf-8",
    )
    return root


def make_client(projects_root: Path) -> TestClient:
    app = create_app(ServerConfig(projects_root=projects_root))
    return TestClient(app, raise_server_exceptions=False)


def assert_envelope(payload: dict) -> None:
    assert set(payload.keys()) == ENVELOPE_KEYS
    assert payload["schema_version"] == 1


def test_healthz_and_version_report_config(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    health = client.get("/healthz")
    assert health.status_code == 200
    payload = health.json()
    assert_envelope(payload)
    assert payload["ok"] is True
    assert payload["data"]["projects_root"] == str(tmp_path)
    assert payload["data"]["host_id"] == "local"
    assert payload["data"]["reconciler"] == {"enabled": False}

    version = client.get("/v1/version").json()
    assert_envelope(version)
    assert version["data"]["version"]


def test_projects_list_empty_root(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payload = client.get("/v1/projects").json()
    assert_envelope(payload)
    assert payload["ok"] is True
    assert payload["data"] == {"projects": [], "total": 0, "offset": 0, "limit": 100}


def test_projects_list_skips_archived_and_non_projects(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a")
    make_project(tmp_path, "example_b", status="archived")
    (tmp_path / "not_a_project").mkdir()

    client = make_client(tmp_path)
    payload = client.get("/v1/projects").json()
    ids = [p["project_id"] for p in payload["data"]["projects"]]
    assert ids == ["example_a"]
    assert payload["data"]["total"] == 1

    both = client.get("/v1/projects", params={"include_archived": "true"}).json()
    ids = [p["project_id"] for p in both["data"]["projects"]]
    assert ids == ["example_a", "example_b"]


def test_projects_list_fields_and_pagination(tmp_path: Path) -> None:
    for name in ("example_a", "example_b", "example_c"):
        make_project(tmp_path, name)
    client = make_client(tmp_path)
    payload = client.get(
        "/v1/projects", params={"fields": "project_id,status", "limit": 2, "offset": 1}
    ).json()
    assert payload["data"]["total"] == 3
    assert payload["data"]["projects"] == [
        {"project_id": "example_b", "status": "active"},
        {"project_id": "example_c", "status": "active"},
    ]


def test_project_detail_counts_and_summary(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a")
    client = make_client(tmp_path)
    payload = client.get("/v1/projects/example_a").json()
    assert_envelope(payload)
    assert payload["data"]["project"]["project_id"] == "example_a"
    assert payload["data"]["project"]["status"] == "active"
    assert payload["data"]["counts"]["experiment_count"] == 1


def test_project_detail_unknown_is_404_envelope(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_missing")
    assert response.status_code == 404
    payload = response.json()
    assert_envelope(payload)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "project.not_found"


def test_project_id_traversal_is_rejected(tmp_path: Path) -> None:
    import pytest

    from kikai_lab.operation import OperationError
    from kikai_lab.server.registry import require_safe_id

    # The guard itself must refuse traversal and hidden-file ids.
    for bad in ("../example_a", "a/b", ".hidden", "", "-dash"):
        with pytest.raises(OperationError) as excinfo:
            require_safe_id(bad, kind="project")
        assert excinfo.value.code == "project.id_invalid"

    # Over HTTP, an id that survives routing but fails the guard is a 422 envelope;
    # an encoded slash is normalized away by routing and safely dead-ends as 404.
    make_project(tmp_path, "example_a")
    client = make_client(tmp_path)
    hidden = client.get("/v1/projects/.hidden")
    assert hidden.status_code == 422
    assert hidden.json()["errors"][0]["code"] == "project.id_invalid"
    encoded = client.get("/v1/projects/..%2Fexample_a")
    assert encoded.status_code == 404
    payload = encoded.json()
    assert_envelope(payload)
    assert payload["errors"][0]["code"] in ("route.not_found", "project.not_found")
    assert "example_a example project" not in json.dumps(payload)


def test_project_report_uses_build_project_report(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a")
    client = make_client(tmp_path)
    payload = client.get("/v1/projects/example_a/report").json()
    assert_envelope(payload)
    report = payload["data"]["report"]
    assert report["kind"] == "kikai_project_report"
    assert report["project"]["project_id"] == "example_a"
    assert report["experiment_count"] == 1


def test_project_validate_fresh_project_is_ok(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a")
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/validate")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["staleness"] == "fresh"


def test_project_validate_missing_current_is_404(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a", with_current=False)
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/validate")
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "project.current_missing"


def test_http_status_for_code_table() -> None:
    from kikai_lab.server.app import http_status_for_code

    table = {
        "project.not_found": 404,
        "project.current_missing": 404,
        "data_source.missing": 404,
        "registry.project_root_missing": 404,
        "data_source.exists": 409,
        "decision.exists": 409,
        "operation.script_bundle_run_name_in_use": 409,
        "project.archived": 409,
        "operation.host_not_local": 409,
        "project.id_invalid": 422,
        "project.record_invalid": 422,
        "data_source.integrity_unverified": 422,
        "data_source.role_unknown": 422,
        "data_source.role_incompatible": 422,
        "operation.adapter_not_implemented": 400,
    }
    for code, expected in table.items():
        assert http_status_for_code(code) == expected, code


def test_bad_query_params_return_envelope(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/v1/projects", params={"limit": 0})
    assert response.status_code == 422
    payload = response.json()
    assert_envelope(payload)
    assert payload["errors"][0]["code"] == "request.params_invalid"
    assert payload["errors"][0]["details"]["validation_errors"]


def test_unknown_route_returns_envelope(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/v1/nonexistent")
    assert response.status_code == 404
    payload = response.json()
    assert_envelope(payload)
    assert payload["errors"][0]["code"] == "route.not_found"


def test_corrupt_project_yaml_degrades_gracefully(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a")
    broken = make_project(tmp_path, "example_broken")
    (broken / "project.yaml").write_text("status: [unclosed", encoding="utf-8")

    client = make_client(tmp_path)
    listing = client.get("/v1/projects").json()
    by_id = {p["project_id"]: p for p in listing["data"]["projects"]}
    assert by_id["example_a"]["status"] == "active"
    assert by_id["example_broken"]["status"] == "invalid"
    assert by_id["example_broken"]["error_code"] == "project.record_invalid"

    detail = client.get("/v1/projects/example_broken")
    assert detail.status_code == 422
    assert detail.json()["errors"][0]["code"] == "project.record_invalid"


def test_report_without_current_is_404_not_500(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a", with_current=False)
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/report")
    assert response.status_code == 404
    payload = response.json()
    assert_envelope(payload)
    assert payload["errors"][0]["code"] == "project.current_missing"


def test_project_detail_warns_on_project_id_mismatch(tmp_path: Path) -> None:
    root = make_project(tmp_path, "example_a")
    record = yaml.safe_load((root / "project.yaml").read_text())
    record["project_id"] = "example_other"
    (root / "project.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")

    client = make_client(tmp_path)
    payload = client.get("/v1/projects/example_a").json()
    assert payload["data"]["project"]["project_id"] == "example_a"
    assert payload["warnings"][0]["code"] == "project.id_mismatch"
    assert payload["warnings"][0]["blocking"] is False


def test_unexpected_error_is_envelope_without_message_leak(
    tmp_path: Path, monkeypatch
) -> None:
    import kikai_lab.server.projects as projects_module

    def boom(*args, **kwargs):
        raise RuntimeError("secret host path /somewhere/private")

    monkeypatch.setattr(projects_module, "list_projects", boom)
    client = make_client(tmp_path)
    response = client.get("/v1/projects")
    assert response.status_code == 500
    payload = response.json()
    assert_envelope(payload)
    assert payload["errors"][0]["code"] == "server.internal_error"
    assert payload["errors"][0]["details"] == {"type": "RuntimeError"}
    assert "/somewhere/private" not in json.dumps(payload)


def test_symlinked_project_dir_is_listed(tmp_path: Path) -> None:
    real = make_project(tmp_path / "elsewhere", "example_real")
    (tmp_path / "example_link").symlink_to(real)
    client = make_client(tmp_path)
    ids = [p["project_id"] for p in client.get("/v1/projects").json()["data"]["projects"]]
    assert ids == ["example_link"]


def test_cli_server_start_missing_projects_root(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "kikai_lab.cli",
            "server",
            "start",
            "--projects-root",
            str(tmp_path / "missing"),
        ],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "server.projects_root_missing"
    assert payload["next_actions"][0]["kind"] == "create_directory"


def test_cli_server_start_boots_real_uvicorn(tmp_path: Path) -> None:
    import socket
    import time
    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make_project(tmp_path, "example_a")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "kikai_lab.cli",
            "server",
            "start",
            "--projects-root",
            str(tmp_path),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        payload = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1
                ) as response:
                    payload = json.loads(response.read())
                break
            except OSError:
                time.sleep(0.2)
        assert payload is not None, "server did not come up"
        assert payload["ok"] is True
        assert payload["data"]["projects_root"] == str(tmp_path)
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_cli_server_start_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", "server", "start", "--help"],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0
    assert "--projects-root" in completed.stdout


def test_journal_appends_and_filters(tmp_path: Path) -> None:
    from kikai_lab.server.registry import append_journal

    root = make_project(tmp_path, "demo")
    append_journal(root, "run_submitted", {"run_name": "r1"})
    append_journal(root, "conclusion", {"run_name": "r1", "verdict": "adopted"})
    lines = (root / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "run_submitted" and first["run_name"] == "r1" and first["at"]

    client = make_client(tmp_path)
    resp = client.get("/v1/projects/demo/journal")
    assert resp.status_code == 200
    events = resp.json()["data"]["events"]
    assert [e["kind"] for e in events] == ["run_submitted", "conclusion"]

    # since is at-least-once: at >= since is included, strictly-older excluded
    resp = client.get("/v1/projects/demo/journal", params={"since": first["at"]})
    kinds = [e["kind"] for e in resp.json()["data"]["events"]]
    assert "run_submitted" in kinds  # same-second event NOT lost
    resp = client.get("/v1/projects/demo/journal", params={"since": "2999-01-01T00:00:00Z"})
    assert resp.json()["data"]["events"] == []

    # limit takes the newest tail
    resp = client.get("/v1/projects/demo/journal", params={"limit": 1})
    data = resp.json()["data"]
    assert len(data["events"]) == 1 and data["events"][0]["kind"] == "conclusion"
    assert data["total"] == 2

    # corrupt line is skipped, not fatal
    with (root / "journal.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    resp = client.get("/v1/projects/demo/journal")
    assert resp.status_code == 200 and resp.json()["data"]["total"] == 2


def test_project_brief_digest(tmp_path: Path) -> None:
    from kikai_lab.server.registry import append_journal

    root = make_project(tmp_path, "demo")
    # a finalized managed run without a conclusion -> attention
    (root / "runs" / "done_run.yaml").write_text(
        yaml.safe_dump(
            {"schema_version": 1, "run_name": "done_run", "experiment_id": "example_exp"}
        ),
        encoding="utf-8",
    )
    (root / "managed_runs").mkdir(exist_ok=True)
    (root / "managed_runs" / "done_run.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "run_id": "done_run"}), encoding="utf-8"
    )
    (root / "managed_runs" / "done_run.progress.json").write_text(
        json.dumps(
            {
                "run_id": "done_run",
                "qc_done_steps": [500, 1000],
                "check_verdicts": {"loss_down": "fail"},
                "lifecycle_state": "done",
                "finalized": True,
                "ticks": 12,
            }
        ),
        encoding="utf-8",
    )
    append_journal(root, "run_finalized", {"run_name": "done_run"})

    client = make_client(tmp_path)
    resp = client.get("/v1/projects/demo/brief")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["project"]["project_id"] == "demo"
    assert data["project"]["current_run_name"] == "example_run"
    by_name = {r["run_name"]: r for r in data["runs"]}
    assert by_name["done_run"]["lifecycle_state"] == "done"
    assert by_name["done_run"]["qc_max_step"] == 1000
    assert by_name["done_run"]["check_verdicts"] == {"loss_down": "fail"}
    # unmanaged run carries no lifecycle noise
    assert by_name["example_run"]["lifecycle_state"] is None
    reasons = {(a["run_name"], a["reason"]) for a in data["attention"]}
    assert ("done_run", "finalized_without_conclusion") in reasons
    assert ("done_run", "metric_check_fail:loss_down") in reasons
    assert data["recent_events"][-1]["kind"] == "run_finalized"


def test_bearer_auth_gate(tmp_path: Path) -> None:
    from kikai_lab.server.app import create_app

    make_project(tmp_path, "demo")
    config = ServerConfig(projects_root=tmp_path, auth_token="example-secret")
    client = TestClient(create_app(config))

    # liveness stays open for probes
    assert client.get("/healthz").status_code == 200
    # everything else is 401 without the token
    resp = client.get("/v1/projects")
    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "server.unauthorized"
    assert resp.headers["www-authenticate"] == "Bearer"
    # wrong token, wrong scheme -> still 401
    assert client.get(
        "/v1/projects", headers={"Authorization": "Bearer nope"}
    ).status_code == 401
    assert client.get(
        "/v1/projects", headers={"Authorization": "Basic example-secret"}
    ).status_code == 401
    # right token passes
    ok = client.get(
        "/v1/projects", headers={"Authorization": "Bearer example-secret"}
    )
    assert ok.status_code == 200

    # no token configured -> open (backward compatible)
    open_client = TestClient(create_app(ServerConfig(projects_root=tmp_path)))
    assert open_client.get("/v1/projects").status_code == 200
