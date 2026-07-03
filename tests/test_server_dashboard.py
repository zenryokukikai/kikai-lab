"""Dashboard delivery: GET / serves the SPA shell, /static serves its assets.

Static responses are plain files (no envelope — browsers, not agents, consume them);
the JSON API must stay envelope-shaped alongside the mounted static app.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kikai_lab.server.app import create_app
from kikai_lab.server.registry import ServerConfig

ENVELOPE_KEYS = {"ok", "schema_version", "data", "warnings", "errors", "next_actions"}


def make_client(projects_root: Path) -> TestClient:
    projects_root.mkdir(parents=True, exist_ok=True)
    app = create_app(ServerConfig(projects_root=projects_root))
    return TestClient(app, raise_server_exceptions=False)


def test_index_serves_dashboard_html(tmp_path: Path) -> None:
    client = make_client(tmp_path / "projects")
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "app.js" in response.text
    assert "style.css" in response.text
    assert "vendor/chart.umd.js" in response.text


def test_static_assets_served(tmp_path: Path) -> None:
    client = make_client(tmp_path / "projects")
    for asset in ("/static/app.js", "/static/style.css", "/static/vendor/chart.umd.js"):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert len(response.content) > 0, asset


def test_unknown_static_asset_is_404(tmp_path: Path) -> None:
    client = make_client(tmp_path / "projects")
    response = client.get("/static/nope.js")
    assert response.status_code == 404


def test_json_api_still_envelope_shaped(tmp_path: Path) -> None:
    client = make_client(tmp_path / "projects")
    response = client.get("/v1/projects")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["ok"] is True
    assert body["data"]["projects"] == []
    assert body["data"]["total"] == 0


def test_report_with_unquoted_yaml_timestamp_does_not_500(tmp_path):
    from tests.test_server_projects import make_client, make_project

    project = make_project(tmp_path, "example_a")
    (project / "decisions" / "example_d.yaml").write_text(
        "schema_version: 1\n"
        "kind: decision\n"
        "decision_id: example_d\n"
        "title: Example\n"
        "status: decided\n"
        "decided_at: 2026-01-01T00:00:00Z\n",  # unquoted -> datetime via yaml.safe_load
        encoding="utf-8",
    )
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/report")
    assert response.status_code == 200
    decided = response.json()["data"]["report"]["decisions"][0]["decided_at"]
    assert decided == "2026-01-01T00:00:00Z"


def test_skill_md_served_as_markdown(tmp_path):
    from tests.test_server_projects import make_client

    client = make_client(tmp_path)
    response = client.get("/v1/skill.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "Golden path" in response.text
    assert "already_exists" in response.text
