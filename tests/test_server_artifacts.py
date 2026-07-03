from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from kikai_lab.server.app import create_app
from kikai_lab.server.registry import ServerConfig
from tests.test_server_projects import make_project


def make_client_with_content(
    projects_root: Path, *, content_roots=(), path_map=None
) -> TestClient:
    config = ServerConfig(
        projects_root=projects_root,
        content_roots=tuple(Path(p) for p in content_roots),
        path_map=path_map or {},
    )
    return TestClient(create_app(config), raise_server_exceptions=False)


def write_ledger(project: Path, rows: list[dict]) -> None:
    ledger = project / "artifacts" / "example_run.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def artifact_row(artifact_id: str, path: str, *, kind="qc_video", location_kind="host_path"):
    return {
        "artifact_id": artifact_id,
        "run_name": "example_run",
        "kind": kind,
        "artifact_class": "visual_only_renderer_qc",
        "locations": [{"kind": location_kind, "path": path, "host_ref": "local"}],
    }


def test_artifacts_index_filters(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    write_ledger(
        project,
        [
            artifact_row("example_video", "/tmp/a.mp4"),
            artifact_row("example_ckpt", "/tmp/b.pt", kind="checkpoint"),
        ],
    )
    client = make_client_with_content(tmp_path)
    data = client.get("/v1/projects/example_a/artifacts").json()["data"]
    assert data["total"] == 2

    filtered = client.get(
        "/v1/projects/example_a/artifacts", params={"kind": "checkpoint"}
    ).json()["data"]
    assert [a["artifact_id"] for a in filtered["artifacts"]] == ["example_ckpt"]


def test_artifact_detail_and_missing(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    write_ledger(project, [artifact_row("example_video", "/tmp/a.mp4")])
    client = make_client_with_content(tmp_path)
    detail = client.get("/v1/projects/example_a/artifacts/example_video").json()
    assert detail["data"]["artifact"]["kind"] == "qc_video"

    missing = client.get("/v1/projects/example_a/artifacts/example_absent")
    assert missing.status_code == 404
    assert missing.json()["errors"][0]["code"] == "artifact.not_found"


def test_content_disabled_without_content_roots(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    payload_file = tmp_path / "store" / "a.mp4"
    payload_file.parent.mkdir(parents=True)
    payload_file.write_bytes(b"video-bytes")
    write_ledger(project, [artifact_row("example_video", str(payload_file))])

    client = make_client_with_content(tmp_path)  # no content roots -> fail closed
    response = client.get("/v1/projects/example_a/artifacts/example_video/content")
    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "artifact.content_root_forbidden"


def test_content_streams_inside_root_with_range(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    store = tmp_path / "store"
    store.mkdir()
    (store / "a.mp4").write_bytes(b"0123456789")
    write_ledger(project, [artifact_row("example_video", str(store / "a.mp4"))])

    client = make_client_with_content(tmp_path, content_roots=[store])
    full = client.get("/v1/projects/example_a/artifacts/example_video/content")
    assert full.status_code == 200
    assert full.content == b"0123456789"

    partial = client.get(
        "/v1/projects/example_a/artifacts/example_video/content",
        headers={"Range": "bytes=2-5"},
    )
    assert partial.status_code == 206
    assert partial.content == b"2345"


def test_content_outside_root_is_403(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    outside = tmp_path / "elsewhere" / "a.mp4"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"secret")
    store = tmp_path / "store"
    store.mkdir()
    write_ledger(project, [artifact_row("example_video", str(outside))])

    client = make_client_with_content(tmp_path, content_roots=[store])
    response = client.get("/v1/projects/example_a/artifacts/example_video/content")
    assert response.status_code == 403
    attempts = response.json()["errors"][0]["details"]["attempts"]
    assert attempts[0]["reason"] == "outside_content_roots"


def test_content_symlink_escape_is_403(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    secret = tmp_path / "elsewhere" / "secret.bin"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"secret")
    store = tmp_path / "store"
    store.mkdir()
    (store / "link.bin").symlink_to(secret)
    write_ledger(project, [artifact_row("example_video", str(store / "link.bin"))])

    client = make_client_with_content(tmp_path, content_roots=[store])
    response = client.get("/v1/projects/example_a/artifacts/example_video/content")
    assert response.status_code == 403


def test_content_container_path_uses_path_map(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    store = tmp_path / "store"
    (store / "runs").mkdir(parents=True)
    (store / "runs" / "a.mp4").write_bytes(b"mapped")
    write_ledger(
        project,
        [
            artifact_row(
                "example_video",
                "/workspace/training_runs/runs/a.mp4",
                location_kind="container_path",
            )
        ],
    )
    client = make_client_with_content(
        tmp_path,
        content_roots=[store],
        path_map={"/workspace/training_runs": str(store)},
    )
    response = client.get("/v1/projects/example_a/artifacts/example_video/content")
    assert response.status_code == 200
    assert response.content == b"mapped"


def test_path_map_respects_component_boundaries(tmp_path: Path) -> None:
    from kikai_lab.server.artifacts import apply_path_map

    mapping = {"/workspace/tr": "/host/tr"}
    assert apply_path_map("/workspace/tr/a.mp4", mapping) == "/host/tr/a.mp4"
    assert apply_path_map("/workspace/tr", mapping) == "/host/tr"
    # a sibling directory sharing the prefix text must NOT be rewritten
    assert apply_path_map("/workspace/tr_evil/a.mp4", mapping) == "/workspace/tr_evil/a.mp4"


def test_content_directory_location_is_403_not_500(tmp_path: Path) -> None:
    project = make_project(tmp_path, "example_a")
    store = tmp_path / "store"
    (store / "adir").mkdir(parents=True)
    write_ledger(project, [artifact_row("example_video", str(store / "adir"))])
    client = make_client_with_content(tmp_path, content_roots=[store])
    response = client.get("/v1/projects/example_a/artifacts/example_video/content")
    assert response.status_code == 403
    attempts = response.json()["errors"][0]["details"]["attempts"]
    assert attempts[0]["reason"] == "file_missing"


def test_artifact_detail_masks_absolute_paths(tmp_path: Path) -> None:
    import json as _json

    project = make_project(tmp_path, "example_a")
    write_ledger(project, [artifact_row("example_video", "/somewhere/private/a.mp4")])
    client = make_client_with_content(tmp_path)
    payload = client.get("/v1/projects/example_a/artifacts/example_video").json()
    assert "/somewhere/private" not in _json.dumps(payload)
    assert payload["data"]["artifact"]["locations"][0]["path"].endswith("a.mp4")
