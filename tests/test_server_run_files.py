"""Run-dir inspection endpoints: listing, sandboxing, and small-file fetches.

These endpoints exist so agents can inspect a run's on-disk reality (checkpoints,
QC outputs, metrics tails) WITHOUT ssh — the sandbox tests are the contract that
makes that safe to expose."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_server_projects import make_client, make_project
from tests.test_server_runs import make_run_fixture


def make_files_fixture(tmp_path: Path) -> Path:
    project = make_run_fixture(tmp_path, qc_done=[200])
    run_dir = tmp_path / "run_dir"
    qc_dir = run_dir / "qc" / "step000200"
    qc_dir.mkdir(parents=True, exist_ok=True)
    (qc_dir / "preview.mp4").write_bytes(b"\x00\x00fakevideo")
    (qc_dir / "summary.json").write_text(json.dumps({"step": 200}), encoding="utf-8")
    (run_dir / "progress_note.txt").write_text("hello run\n", encoding="utf-8")
    return project


# ------------------------------------------------------------------- listing
def test_artifacts_lists_run_dir_root(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/runs/example_run/artifacts")
    assert response.status_code == 200
    data = response.json()["data"]
    paths = {e["path"]: e for e in data["entries"]}
    assert paths["checkpoints"]["is_dir"] is True
    assert paths["checkpoints"]["size"] is None
    assert paths["metrics.jsonl"]["is_dir"] is False
    assert paths["metrics.jsonl"]["size"] > 0
    assert "qc/step000200" not in paths  # depth=1 by default


def test_artifacts_subdir_and_depth(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts",
        params={"path": "checkpoints"},
    )
    assert response.status_code == 200
    names = [e["path"] for e in response.json()["data"]["entries"]]
    assert names == [
        "checkpoints/checkpoint_step_000200_loss9p9.pt",
        "checkpoints/checkpoint_step_000300_loss9p9.pt",
    ]
    deep = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts", params={"depth": 2}
    )
    deep_paths = [e["path"] for e in deep.json()["data"]["entries"]]
    assert "qc/step000200" in deep_paths


def test_artifacts_listing_a_file_is_422(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts",
        params={"path": "metrics.jsonl"},
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "run.artifact_path_invalid"


def test_artifacts_missing_run_dir_is_404(tmp_path):
    make_project(tmp_path, "example_a")  # run record exists, no managed_run/run_dir
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/runs/example_run/artifacts")
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "run.run_dir_missing"


# ------------------------------------------------------------------- sandbox
def test_artifacts_rejects_traversal_and_absolute_paths(tmp_path):
    make_files_fixture(tmp_path)
    (tmp_path / "outside_secret.txt").write_text("secret", encoding="utf-8")
    client = make_client(tmp_path)
    for bad in ("../outside_secret.txt", "..", "a/../../outside_secret.txt", "/etc"):
        response = client.get(
            "/v1/projects/example_a/runs/example_run/artifacts/file",
            params={"path": bad},
        )
        assert response.status_code == 403, bad
        assert response.json()["errors"][0]["code"] == "run.artifact_path_forbidden"
        listing = client.get(
            "/v1/projects/example_a/runs/example_run/artifacts", params={"path": bad}
        )
        assert listing.status_code == 403, bad


def test_artifacts_rejects_symlink_escape(tmp_path):
    make_files_fixture(tmp_path)
    (tmp_path / "outside_secret.txt").write_text("secret", encoding="utf-8")
    os.symlink(tmp_path / "outside_secret.txt", tmp_path / "run_dir" / "sneaky.txt")
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts/file",
        params={"path": "sneaky.txt"},
    )
    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "run.artifact_path_forbidden"


def test_artifacts_walk_does_not_follow_symlinked_dirs(tmp_path):
    make_files_fixture(tmp_path)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "leak.txt").write_text("leak", encoding="utf-8")
    os.symlink(outside, tmp_path / "run_dir" / "linked_dir")
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts", params={"depth": 3}
    )
    paths = [e["path"] for e in response.json()["data"]["entries"]]
    assert "linked_dir" in paths  # the link itself is visible...
    assert not any(p.startswith("linked_dir/") for p in paths)  # ...never entered


# ---------------------------------------------------------------- file fetch
def test_artifact_file_returns_text_content(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts/file",
        params={"path": "qc/step000200/summary.json"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["binary"] is False
    assert data["truncated"] is False
    assert json.loads(data["content"]) == {"step": 200}


def test_artifact_file_truncates_head_and_tail(tmp_path):
    make_files_fixture(tmp_path)
    big = tmp_path / "run_dir" / "big.log"
    big.write_text("A" * 100 + "Z" * 100, encoding="utf-8")
    client = make_client(tmp_path)
    url = "/v1/projects/example_a/runs/example_run/artifacts/file"
    head = client.get(url, params={"path": "big.log", "max_bytes": 100}).json()["data"]
    assert head["truncated"] is True and head["content"] == "A" * 100
    tail = client.get(
        url, params={"path": "big.log", "max_bytes": 100, "tail": "true"}
    ).json()["data"]
    assert tail["tail"] is True and tail["content"] == "Z" * 100


def test_artifact_file_binary_returns_metadata_only(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts/file",
        params={"path": "qc/step000200/preview.mp4"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["binary"] is True
    assert data["content"] is None
    assert data["size"] == len(b"\x00\x00fakevideo")


def test_artifact_file_on_directory_is_422(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts/file",
        params={"path": "checkpoints"},
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "run.artifact_path_invalid"


def test_artifact_file_missing_is_404(tmp_path):
    make_files_fixture(tmp_path)
    client = make_client(tmp_path)
    response = client.get(
        "/v1/projects/example_a/runs/example_run/artifacts/file",
        params={"path": "no/such/file.txt"},
    )
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "run.artifact_path_not_found"


def test_artifacts_respects_run_dir_roots_containment(tmp_path):
    """When run_dir_roots is configured and the run_dir is outside them, the
    endpoint fail-closes exactly like /metrics does."""
    from fastapi.testclient import TestClient

    from kikai_lab.server.app import create_app
    from kikai_lab.server.registry import ServerConfig

    make_files_fixture(tmp_path)
    contained = tmp_path / "elsewhere"
    contained.mkdir()
    app = create_app(
        ServerConfig(projects_root=tmp_path, run_dir_roots=(contained,))
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/v1/projects/example_a/runs/example_run/artifacts")
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "run.run_dir_missing"
