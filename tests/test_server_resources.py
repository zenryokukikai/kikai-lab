from __future__ import annotations

from pathlib import Path

import yaml

from tests.test_server_projects import assert_envelope, make_client, make_project


def put_project(client, project_id: str, **body):
    return client.put(f"/v1/projects/{project_id}", json={"summary": "via api", **body})


# --------------------------------------------------------------------- project PUT
def test_project_put_creates_scaffolded_registry(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = put_project(client, "example_new")
    assert response.status_code == 201
    payload = response.json()
    assert_envelope(payload)
    assert payload["data"] == {"project_id": "example_new", "created": True}
    assert payload["next_actions"][0]["id"] == "register_experiment"

    root = tmp_path / "example_new"
    for name in ("experiments", "runs", "containers", "managed_runs", "ops"):
        assert (root / name).is_dir()
    record = yaml.safe_load((root / "project.yaml").read_text())
    assert record["project_id"] == "example_new"
    assert record["status"] == "active"
    assert record["created_by"] == "kikai-server"
    assert (root / "current.json").is_file()


def test_project_put_is_idempotent_then_updates(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    again = put_project(client, "example_new")
    assert again.status_code == 200
    assert again.json()["data"] == {"project_id": "example_new", "already_exists": True}

    changed = put_project(client, "example_new", summary_extra="x")
    assert changed.status_code == 200
    assert changed.json()["data"] == {"project_id": "example_new", "updated": True}
    record = yaml.safe_load((tmp_path / "example_new" / "project.yaml").read_text())
    assert record["created_at"]  # preserved across update
    assert record["summary_extra"] == "x"


def test_project_put_rejects_status_and_archived(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.put("/v1/projects/example_new", json={"status": "archived"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "project.record_invalid"

    put_project(client, "example_new")
    client.post("/v1/projects/example_new/archive")
    blocked = put_project(client, "example_new")
    assert blocked.status_code == 409
    assert blocked.json()["errors"][0]["code"] == "project.archived"


def test_project_archive_unarchive_cycle(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    archived = client.post("/v1/projects/example_new/archive").json()
    assert archived["data"]["status"] == "archived"
    again = client.post("/v1/projects/example_new/archive").json()
    assert again["data"]["already_exists"] is True
    listing = client.get("/v1/projects").json()
    assert listing["data"]["total"] == 0

    restored = client.post("/v1/projects/example_new/unarchive").json()
    assert restored["data"]["status"] == "active"
    record = yaml.safe_load((tmp_path / "example_new" / "project.yaml").read_text())
    assert "archived_at" not in record


# --------------------------------------------------------------------- experiments
def test_experiment_put_list_detail_roundtrip(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    created = client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"title": "Example", "summary": "s", "status": "active"},
    )
    assert created.status_code == 201
    assert created.json()["data"]["created"] is True

    same = client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"title": "Example", "summary": "s", "status": "active"},
    )
    assert same.json()["data"]["already_exists"] is True

    updated = client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"title": "Example", "summary": "s2", "status": "active"},
    )
    assert updated.json()["data"]["updated"] is True

    listing = client.get("/v1/projects/example_new/experiments").json()
    assert listing["data"]["total"] == 1
    assert listing["data"]["experiments"][0]["experiment_id"] == "example_exp"

    (tmp_path / "example_new" / "runs" / "example_run.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_name": "example_run",
                "experiment_id": "example_exp",
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    detail = client.get("/v1/projects/example_new/experiments/example_exp").json()
    assert detail["data"]["experiment"]["title"] == "Example"
    assert detail["data"]["runs"] == [
        {"run_name": "example_run", "status": "completed", "fresh_no_resume": None}
    ]


def test_experiment_put_rejects_id_mismatch_and_bad_schema(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    mismatch = client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"experiment_id": "other", "title": "x"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["errors"][0]["code"] == "experiment.record_invalid"

    bad = client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"schema_version": "not-an-int"},
    )
    assert bad.status_code == 422
    details = bad.json()["errors"][0]["details"]
    assert details["validation_errors"]


def test_experiment_detail_unknown_is_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = client.get("/v1/projects/example_new/experiments/example_missing")
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "experiment.not_found"


def test_experiment_put_on_archived_project_is_409(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    client.post("/v1/projects/example_new/archive")
    response = client.put(
        "/v1/projects/example_new/experiments/example_exp", json={"title": "x"}
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "project.archived"


# ----------------------------------------------------------------------- decisions
def test_decision_put_and_list(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    created = client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "Choose route", "status": "open"},
    )
    assert created.status_code == 201

    decided = client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "Choose route", "status": "decided", "decided_at": "2026-07-02T00:00:00Z"},
    )
    assert decided.json()["data"]["updated"] is True

    missing_title = client.put(
        "/v1/projects/example_new/decisions/example_bad", json={"status": "open"}
    )
    assert missing_title.status_code == 422

    bad_status = client.put(
        "/v1/projects/example_new/decisions/example_bad",
        json={"title": "x", "status": "wontfix"},
    )
    assert bad_status.status_code == 422

    listing = client.get("/v1/projects/example_new/decisions").json()
    assert listing["data"]["total"] == 1
    assert listing["data"]["decisions"][0]["status"] == "decided"


# ---------------------------------------------------------------------- containers
def container_body(**overrides):
    body = {
        "host_id": "training_host",
        "role": "training",
        "status": "ephemeral_run",
        "summary": "example trainer",
        "docker": {"name": "example-training", "image": "example-image:latest"},
        "mounts": [
            {
                "source": "env:EXAMPLE_RUNS_ROOT",
                "target": "env:CONTAINER_RUNS_ROOT",
                "mode": "rw",
            }
        ],
    }
    body.update(overrides)
    return body


def test_container_put_list_detail(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    created = client.put(
        "/v1/projects/example_new/containers/example_training", json=container_body()
    )
    assert created.status_code == 201

    listing = client.get("/v1/projects/example_new/containers").json()
    assert listing["data"]["containers"][0]["name"] == "example-training"

    detail = client.get("/v1/projects/example_new/containers/example_training").json()
    assert detail["data"]["container"]["kind"] == "docker_container"

    missing = client.get("/v1/projects/example_new/containers/example_missing")
    assert missing.status_code == 404


def test_container_put_rejects_live_worktree_mount(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    body = container_body(
        mounts=[
            {
                "source": "env:HOST_EXAMPLE_WORKTREE",
                "target": "/workspace/example_engine",
                "mode": "rw",
            }
        ]
    )
    response = client.put("/v1/projects/example_new/containers/example_training", json=body)
    assert response.status_code == 422
    payload = response.json()
    assert_envelope(payload)
    assert payload["errors"]


# -------------------------------------------------------------------- data sources
def test_data_sources_list_and_detail(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    listing = client.get("/v1/projects/example_new/data-sources").json()
    assert listing["data"] == {"data_sources": [], "total": 0}

    record = {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": "example_manifest",
        "source_type": "dataset_manifest",
        "status": "active",
        "summary": "example",
        "storage": {"storage_kind": "host_path", "host_ref": "h1", "path": "/tmp/x"},
        "immutability": {"mode": "immutable"},
        "integrity": {"strategy": "not_available", "reason": "example fixture"},
        "contract": {"role_compatibility": ["train_manifest"]},
    }
    (tmp_path / "example_new" / "data_sources" / "example_manifest.yaml").write_text(
        yaml.safe_dump(record), encoding="utf-8"
    )
    listing = client.get("/v1/projects/example_new/data-sources").json()
    assert listing["data"]["total"] == 1
    assert listing["data"]["data_sources"][0]["roles"] == ["train_manifest"]

    detail = client.get("/v1/projects/example_new/data-sources/example_manifest").json()
    assert detail["data"]["data_source"]["source_type"] == "dataset_manifest"


# ------------------------------------------------------- review-finding regressions
def test_put_retry_safe_with_hand_edited_unquoted_timestamp(tmp_path: Path) -> None:
    """yaml.safe_load turns unquoted ISO timestamps into datetime objects; the
    canonical hash must absorb them instead of 500ing (H1)."""
    client = make_client(tmp_path)
    put_project(client, "example_new")
    record_path = tmp_path / "example_new" / "decisions" / "example_decision.yaml"
    record_path.write_text(
        "schema_version: 1\n"
        "kind: decision\n"
        "decision_id: example_decision\n"
        "title: Choose route\n"
        "status: decided\n"
        "decided_at: 2026-07-02T00:00:00Z\n",  # unquoted -> datetime
        encoding="utf-8",
    )
    response = client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "Choose route", "status": "decided", "decided_at": "2026-07-02T00:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["already_exists"] is True


def test_put_null_kind_cannot_create_ghost_records(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"kind": None, "title": "ghost"},
    )
    # An explicit null contradicting the enforced kind is a loud 422, never a record
    # that exists-but-lists-nowhere.
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "experiment.record_invalid"
    listing = client.get("/v1/projects/example_new/experiments").json()
    assert listing["data"]["total"] == 0
    assert not (tmp_path / "example_new" / "experiments" / "example_exp.yaml").exists()


def test_project_put_with_null_values_converges(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    first = client.put(
        "/v1/projects/example_new", json={"summary": "via api", "extra": None}
    ).json()
    second = client.put(
        "/v1/projects/example_new", json={"summary": "via api", "extra": None}
    ).json()
    assert second["data"]["already_exists"] is True, (first["data"], second["data"])


def test_body_cannot_inject_server_managed_fields(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.put(
        "/v1/projects/example_new",
        json={"summary": "s", "archived_at": "2026-01-01T00:00:00Z"},
    )
    record = yaml.safe_load((tmp_path / "example_new" / "project.yaml").read_text())
    assert "archived_at" not in record


def test_kindless_experiment_listed_as_invalid_stub(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    (tmp_path / "example_new" / "experiments" / "example_legacy.yaml").write_text(
        yaml.safe_dump({"experiment_id": "example_legacy", "title": "legacy"}),
        encoding="utf-8",
    )
    listing = client.get("/v1/projects/example_new/experiments").json()
    assert listing["data"]["total"] == 1
    stub = listing["data"]["experiments"][0]
    assert stub["_invalid"] is True
    assert stub["_error_code"] == "experiment.kind_missing"
    counts = client.get("/v1/projects/example_new").json()["data"]["counts"]
    assert counts["experiment_count"] == 1  # listing total matches project counts


def test_decision_title_rewrite_is_409_with_diff_keys(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "Original", "status": "open"},
    )
    rewrite = client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "Rewritten", "status": "open"},
    )
    assert rewrite.status_code == 409
    payload = rewrite.json()
    assert payload["errors"][0]["code"] == "decision.exists"
    assert payload["errors"][0]["details"]["diff_keys"] == ["title"]
    assert "Rewritten" not in str(payload["errors"][0]["details"])  # no document echo

    backward = client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "Original", "status": "open"},
    )
    assert backward.json()["data"]["already_exists"] is True


def test_decision_backward_status_transition_is_409(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "T", "status": "decided", "decided_at": "2026-07-02T00:00:00Z"},
    )
    backward = client.put(
        "/v1/projects/example_new/decisions/example_decision",
        json={"title": "T", "status": "open"},
    )
    assert backward.status_code == 409


def test_archived_project_blocks_decision_and_container_puts(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    client.post("/v1/projects/example_new/archive")
    for url, body in (
        ("/v1/projects/example_new/decisions/example_d", {"title": "x"}),
        ("/v1/projects/example_new/containers/example_c", container_body()),
    ):
        response = client.put(url, json=body)
        assert response.status_code == 409
        assert response.json()["errors"][0]["code"] == "project.archived"


def test_error_details_do_not_leak_absolute_paths(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = client.get("/v1/projects/example_new/data-sources/example_absent")
    assert response.status_code == 404
    details = response.json()["errors"][0]["details"]
    for value in details.values():
        assert not (isinstance(value, str) and value.startswith("/")), details


def test_upsert_yaml_record_immutable_semantics(tmp_path: Path) -> None:
    from kikai_lab.operation import OperationError
    from kikai_lab.server.registry import upsert_yaml_record

    target = tmp_path / "record.yaml"
    first = upsert_yaml_record(target, {"a": 1}, kind="thing", mutable=False)
    assert first == {"created": True}
    same = upsert_yaml_record(target, {"a": 1}, kind="thing", mutable=False)
    assert same == {"already_exists": True}
    try:
        upsert_yaml_record(target, {"a": 2}, kind="thing", mutable=False)
    except OperationError as exc:
        assert exc.code == "thing.exists"
        assert exc.details["diff_keys"] == ["a"]
    else:
        raise AssertionError("divergent immutable content must raise")


def test_new_project_end_to_end_report(tmp_path: Path) -> None:
    """A project created purely over the API renders a report without errors."""
    client = make_client(tmp_path)
    put_project(client, "example_new")
    client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"title": "Example", "summary": "s"},
    )
    report = client.get("/v1/projects/example_new/report")
    assert report.status_code == 200
    body = report.json()["data"]["report"]
    assert body["experiment_count"] == 1


def test_make_project_helper_still_compatible(tmp_path: Path) -> None:
    """The read-plane fixtures from test_server_projects keep working via the routers."""
    make_project(tmp_path, "example_a")
    client = make_client(tmp_path)
    listing = client.get("/v1/projects/example_a/experiments").json()
    assert listing["data"]["total"] == 1
