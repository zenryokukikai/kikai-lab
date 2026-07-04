from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.test_server_bundles import bundle_files, make_tar, upload
from tests.test_server_projects import make_client
from tests.test_server_resources import container_body, put_project


def write_fake_docker(tmp_path: Path):
    """Recording docker: `run` succeeds (control can force failure), `inspect`/`rm`
    honour a state control file. Every argv is appended to a log."""
    control = tmp_path / "docker_control.json"
    control.write_text(
        json.dumps({"run_fail": False, "exists": False}), encoding="utf-8"
    )
    log = tmp_path / "docker_argv.jsonl"
    fake = tmp_path / "fake_docker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"control_path = pathlib.Path({str(control)!r})\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "control = json.loads(control_path.read_text())\n"
        "with log.open('a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'run':\n"
        "    if control.get('run_fail'):\n"
        "        sys.stderr.write('docker: forced failure')\n"
        "        raise SystemExit(1)\n"
        "    control['exists'] = True\n"
        "    control_path.write_text(json.dumps(control))\n"
        "    print('example-container-id')\n"
        "    raise SystemExit(0)\n"
        "if cmd == 'inspect':\n"
        "    if not control.get('exists'):\n"
        "        sys.stderr.write('Error: No such object')\n"
        "        raise SystemExit(1)\n"
        "    print(json.dumps([{'State': {'Running': True, 'Status': 'running'}}]))\n"
        "    raise SystemExit(0)\n"
        "if cmd == 'rm':\n"
        "    control['exists'] = False\n"
        "    control_path.write_text(json.dumps(control))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    def read_argv():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def set_control(**kwargs):
        state = json.loads(control.read_text())
        state.update(kwargs)
        control.write_text(json.dumps(state))

    return fake, read_argv, set_control


def set_container_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXAMPLE_RUNS_ROOT", str(tmp_path / "host_runs"))
    monkeypatch.setenv("CONTAINER_RUNS_ROOT", "/workspace/runs")
    (tmp_path / "host_runs").mkdir(exist_ok=True)


def prepare_project(tmp_path: Path, client) -> None:
    put_project(client, "example_new")
    client.put(
        "/v1/projects/example_new/experiments/example_exp",
        json={"title": "Example"},
    )
    client.put(
        "/v1/projects/example_new/containers/example_training", json=container_body()
    )
    upload(client, "example_trainer_v1", make_tar(bundle_files()))


def submission_body(**overrides):
    body = {
        "experiment_id": "example_exp",
        "container_id": "example_training",
        "bundle_id": "example_trainer_v1",
        "entrypoint": "train",
        "args": ["--max-steps", "100"],
        "run_dir": "${EXAMPLE_RUNS_ROOT}/example_run_a/run",
        "managed": {"max_step": 100, "retention": {"keep_latest": 2, "keep_best": 1}},
    }
    body.update(overrides)
    return body


def submit(client, run_name="example_run_a", **overrides):
    return client.post(
        f"/v1/projects/example_new/runs/{run_name}/submit", json=submission_body(**overrides)
    )


def test_submit_dry_run_validates_without_docker(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    response = submit(client, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["request_sha256"]
    assert "project_root" not in data["op_request"]
    assert data["op_request"]["adapter"] == "script_bundle_run"
    assert read_argv() == []  # nothing executed
    assert not (tmp_path / "example_new" / "runs" / "example_run_a.yaml").exists()


def test_submit_launches_and_records_everything(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    response = submit(client)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["submitted"] is True
    assert data["managed_run_created"] is True

    project = tmp_path / "example_new"
    run_record = yaml.safe_load((project / "runs" / "example_run_a.yaml").read_text())
    assert run_record["status"] == "running"
    assert run_record["submission"]["request_sha256"] == data["request_sha256"]
    assert run_record["experiment_id"] == "example_exp"

    managed = yaml.safe_load((project / "managed_runs" / "example_run_a.yaml").read_text())
    assert managed["run_id"] == "example_run_a"
    assert managed["training_container_id"] == "example_training"
    assert managed["max_step"] == 100
    assert managed["retention"] == {"keep_latest": 2, "keep_best": 1}
    assert managed["experiment_id"] == "example_exp"  # retention inheritance path

    audit = json.loads((project / "ops" / "example_run_a_submit.json").read_text())
    assert audit["request"]["adapter"] == "script_bundle_run"

    run_argv = [argv for argv in read_argv() if argv and argv[0] == "run"]
    assert run_argv, "docker run must have been invoked"
    joined = " ".join(run_argv[0])
    assert "--name example-training" in joined
    assert "script_bundles/example_trainer_v1/root/scripts/train.py" in joined


def test_submit_is_idempotent_and_conflicts_on_divergence(
    tmp_path: Path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    submit(client)
    runs_before = len([a for a in read_argv() if a and a[0] == "run"])

    again = submit(client)
    assert again.status_code == 200
    assert again.json()["data"]["already_exists"] is True
    assert len([a for a in read_argv() if a and a[0] == "run"]) == runs_before

    diverged = submit(client, args=["--max-steps", "999"])
    assert diverged.status_code == 409
    assert diverged.json()["errors"][0]["code"] == "run.exists"


def test_submit_failure_is_recorded_and_retryable(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    set_control(run_fail=True)
    failed = submit(client)
    assert failed.status_code >= 400
    record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_a.yaml").read_text()
    )
    assert record["status"] == "submit_failed"
    # no orphan managed_run: the daemon must not tick a ghost, and the read plane
    # must report the declared failure rather than 'submitted'
    assert not (
        tmp_path / "example_new" / "managed_runs" / "example_run_a.yaml"
    ).exists()
    status = client.get("/v1/projects/example_new/runs/example_run_a/status").json()
    assert status["data"]["derived_status"] == "submit_failed"

    set_control(run_fail=False)
    retried = submit(client)
    assert retried.status_code == 201
    record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_a.yaml").read_text()
    )
    assert record["status"] == "running"


def test_submit_validation_failures(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    unknown_bundle = submit(client, bundle_id="example_absent")
    assert unknown_bundle.status_code == 404

    unknown_entrypoint = submit(client, entrypoint="evaluate")
    assert unknown_entrypoint.status_code == 404

    unknown_experiment = submit(client, experiment_id="example_absent")
    assert unknown_experiment.status_code == 404

    other_host = submit(client, host_ref="example_other_host")
    assert other_host.status_code == 409
    assert other_host.json()["errors"][0]["code"] == "operation.host_not_local"

    typo = submit(client, maxx_steps=1)
    assert typo.status_code == 422  # additionalProperties: false catches typos loudly

    managed_without_dir = submit(client, run_dir=None)
    assert managed_without_dir.status_code == 422

    assert read_argv() == []  # none of these reached docker


def test_stop_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)

    stopped = client.post("/v1/projects/example_new/runs/example_run_a/stop")
    assert stopped.status_code == 200
    assert stopped.json()["data"]["stopped"] is True
    assert any(a[:2] == ["rm", "-f"] for a in read_argv())

    again = client.post("/v1/projects/example_new/runs/example_run_a/stop")
    assert again.json()["data"]["already_stopped"] is True

    missing = client.post("/v1/projects/example_new/runs/example_absent/stop")
    assert missing.status_code == 404


def test_operations_escape_hatch_noop_and_project_root_pinning(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    dry = client.post(
        "/v1/projects/example_new/operations",
        params={"dry_run": "true"},
        json={"adapter": "noop", "operation": "example_probe", "project_root": "/evil"},
    )
    assert dry.status_code == 200
    assert "project_root" not in dry.json()["data"]["op_request"]

    executed = client.post(
        "/v1/projects/example_new/operations",
        json={"adapter": "noop", "operation": "example_probe", "project_root": "/evil"},
    )
    assert executed.status_code == 200, executed.text
    data = executed.json()["data"]
    audit_files = list((tmp_path / "example_new" / "ops").glob("example_probe_*.json"))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text())
    assert audit["request"]["project_root"] == str(tmp_path / "example_new")
    assert data["request_sha256"]

    bad = client.post("/v1/projects/example_new/operations", json={"operation": "x"})
    assert bad.status_code == 422


def test_background_reconciler_pass_and_healthz(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from kikai_lab.server.app import create_app
    from kikai_lab.server.reconciler import BackgroundReconciler, reconcile_all
    from kikai_lab.server.registry import ServerConfig
    from tests.test_server_projects import make_project

    make_project(tmp_path, "example_a")
    make_project(tmp_path, "example_archived", status="archived")

    seen: list[str] = []

    def fake_once(project_path):
        seen.append(Path(project_path).name)
        return {"managed_runs": 0, "results": []}

    config = ServerConfig(
        projects_root=tmp_path, with_reconciler=True, reconcile_interval=1
    )
    results = reconcile_all(config, once_fn=fake_once)
    assert seen == ["example_a"]  # archived projects are skipped
    assert results["example_a"]["managed_runs"] == 0

    reconciler = BackgroundReconciler(config, once_fn=fake_once)
    reconciler.tick()
    assert reconciler.last_tick_at is not None

    app = create_app(config)
    with TestClient(app) as client:  # context manager triggers lifespan start/stop
        health = client.get("/healthz").json()
        assert health["data"]["reconciler"]["enabled"] is True


def test_submit_preflights_data_source_refs_before_docker(
    tmp_path: Path, monkeypatch
) -> None:
    """Unknown data_source ids must die BEFORE docker (the in-process path skips the
    guard-receipt machinery where the preflight normally lives) — reviewer H1."""
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    bogus = submit(
        client,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_bogus"}],
    )
    assert bogus.status_code >= 400, bogus.text
    assert read_argv() == []  # never reached docker

    payload = tmp_path / "inputs" / "manifest.jsonl"
    payload.parent.mkdir(parents=True)
    payload.write_text("{}\n", encoding="utf-8")
    client.put(
        "/v1/projects/example_new/data-sources/example_manifest",
        json={
            "kind": "file",
            "source_type": "dataset_manifest",
            "path": str(payload),
            "host_ref": "local",
            "roles": ["train_manifest"],
            "summary": "example",
        },
    )
    payload.write_text("tampered\n", encoding="utf-8")
    tampered = submit(
        client,
        data_source_refs=[
            {"role": "train_manifest", "data_source_id": "example_manifest"}
        ],
    )
    assert tampered.status_code >= 400
    assert read_argv() == []  # integrity mismatch also dies pre-docker


def test_submit_refs_hash_stable_between_omitted_and_empty(
    tmp_path: Path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    submit(client)  # omitted data_source_refs
    again = submit(client, data_source_refs=[])
    assert again.status_code == 200
    assert again.json()["data"]["already_exists"] is True


def test_submit_crash_window_is_adoptable(tmp_path: Path, monkeypatch) -> None:
    """A record stuck at 'submitting' with the container actually started (crash
    between docker run and the final write) must be adoptable, observable, and never
    wedged — reviewer H2."""
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    # Simulate the crash window: submitting record + managed_run + live container,
    # but no final 'running' write.
    import yaml as _yaml

    from kikai_lab.operation import request_sha256
    from kikai_lab.server.submit import (
        build_submit_op,
        managed_run_record,
        submission_record,
    )

    body = submission_body()
    sha = request_sha256(build_submit_op(tmp_path / "example_new", "example_run_a", body))
    (tmp_path / "example_new" / "runs" / "example_run_a.yaml").write_text(
        _yaml.safe_dump(submission_record("example_run_a", body, sha, status="submitting")),
        encoding="utf-8",
    )
    (tmp_path / "example_new" / "managed_runs" / "example_run_a.yaml").write_text(
        _yaml.safe_dump(managed_run_record("example_run_a", body)), encoding="utf-8"
    )
    set_control(exists=True)  # the container IS running

    # Observable: the read plane sees the live container, not a phantom.
    status = client.get("/v1/projects/example_new/runs/example_run_a/status").json()
    assert status["data"]["container"]["exists"] is True

    # Recoverable: an identical resubmit adopts the container instead of wedging.
    recovered = submit(client)
    assert recovered.status_code == 201, recovered.text
    data = recovered.json()["data"]
    assert data["adopted_existing_container"] is True
    record = _yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_a.yaml").read_text()
    )
    assert record["status"] == "running"
    # adoption never issued a second docker run
    assert [a for a in read_argv() if a and a[0] == "run"] == []


def test_fresh_submit_name_collision_points_at_stop(tmp_path: Path, monkeypatch) -> None:
    """A FIRST-time submit hitting a name collision (another run holds the container
    profile) fails with an actionable hint, and stays retryable — reviewer M1."""
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    set_control(exists=True)  # someone else's container already holds the name

    import json as _json

    control = tmp_path / "docker_control.json"
    state = _json.loads(control.read_text())
    state["run_fail"] = True  # docker run would fail; but adapter checks name first
    control.write_text(_json.dumps(state))

    response = submit(client)
    assert response.status_code == 409
    error = response.json()["errors"][0]
    assert error["code"] == "operation.script_bundle_run_name_in_use"
    assert "/stop" in error["details"]["next"]


def test_run_conclusion_appends_and_badges(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)

    first = client.post(
        "/v1/projects/example_new/runs/example_run_a/conclusion",
        json={
            "verdict": "rejected",
            "summary": "texture terms flattened after 6000",
            "evidence": ["metric_check highpass: fail"],
            "next_run": "example_run_b",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["conclusion_count"] == 1

    second = client.post(
        "/v1/projects/example_new/runs/example_run_a/conclusion",
        json={"verdict": "superseded", "summary": "replaced by example_run_b"},
    )
    assert second.json()["data"]["conclusion_count"] == 2  # append-only history

    record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_a.yaml").read_text()
    )
    assert [c["verdict"] for c in record["conclusions"]] == ["rejected", "superseded"]
    assert record["verdict"] == "superseded"  # latest wins for badges
    assert record["conclusions"][0]["evidence"] == ["metric_check highpass: fail"]

    listing = client.get("/v1/projects/example_new/runs").json()["data"]["runs"]
    assert listing[0]["verdict"] == "superseded"

    bad = client.post(
        "/v1/projects/example_new/runs/example_run_a/conclusion",
        json={"verdict": "maybe", "summary": "x"},
    )
    assert bad.status_code == 422

    # every mutation left a journal entry (submit + 2 conclusions; bad verdict did not)
    journal = [
        json.loads(line)
        for line in (tmp_path / "example_new" / "journal.jsonl").read_text().splitlines()
    ]
    kinds = [e["kind"] for e in journal]
    assert kinds == ["run_submitted", "conclusion", "conclusion"]
    assert journal[0]["run_name"] == "example_run_a"
    assert journal[0]["parent_run"] is None
    assert journal[1]["verdict"] == "rejected" and journal[1]["summary"]
    missing = client.post(
        "/v1/projects/example_new/runs/example_absent/conclusion",
        json={"verdict": "adopted", "summary": "x"},
    )
    assert missing.status_code == 404


def test_conclusions_survive_submit_retry(tmp_path: Path, monkeypatch) -> None:
    """The reviewer's exact replay: failed launch -> conclusion recorded -> identical
    resubmit. The rebuilt record must carry the analysis trail, never erase it."""
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)

    set_control(run_fail=True)
    submit(client)  # -> submit_failed
    concluded = client.post(
        "/v1/projects/example_new/runs/example_run_a/conclusion",
        json={"verdict": "rejected", "summary": "launch env was broken"},
    )
    assert concluded.status_code == 201

    set_control(run_fail=False)
    retried = submit(client)
    assert retried.status_code == 201
    record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_a.yaml").read_text()
    )
    assert record["status"] == "running"
    assert [c["verdict"] for c in record["conclusions"]] == ["rejected"]
    assert record["verdict"] == "rejected"


def test_conclusion_blocked_on_archived_project(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)
    client.post("/v1/projects/example_new/archive")
    response = client.post(
        "/v1/projects/example_new/runs/example_run_a/conclusion",
        json={"verdict": "adopted", "summary": "x"},
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "project.archived"


def test_submit_from_inherits_rebinds_and_records_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)  # parent: example_run_a
    client.post("/v1/projects/example_new/runs/example_run_a/stop")
    set_control(exists=False)

    child = client.post(
        "/v1/projects/example_new/runs/example_run_b/submit-from/example_run_a",
        json={"overrides": {"args_set": {"--vgg-weight": "5.0", "--max-steps": None}}},
    )
    assert child.status_code == 201, child.text
    record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_b.yaml").read_text()
    )
    sub = record["submission"]
    # the docker id from `docker run -d` stdout, NOT the profile id (container_id)
    assert sub["started_container_id"] == "example-container-id"
    assert sub["container_id"] == "example_training"
    assert sub["parent_run"] == "example_run_a"
    assert sub["overrides"]["args_set"]["--vgg-weight"] == "5.0"
    args = sub["args"]
    assert args[args.index("--vgg-weight") + 1] == "5.0"  # upserted
    assert "--max-steps" not in args  # null removes the flag
    # run_dir and every path rebound from parent name to child name
    assert "example_run_b" in sub["run_dir"] and "example_run_a" not in sub["run_dir"]
    managed = yaml.safe_load(
        (tmp_path / "example_new" / "managed_runs" / "example_run_b.yaml").read_text()
    )
    assert managed["retention"] == {"keep_latest": 2, "keep_best": 1}  # inherited
    assert "example_run_a" not in json.dumps(managed.get("qc_op", {}), default=str)
    # journal recorded the lineage
    journal = [
        json.loads(line)
        for line in (tmp_path / "example_new" / "journal.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["kind"] == "run_submitted"
    assert journal[-1]["run_name"] == "example_run_b"
    assert journal[-1]["parent_run"] == "example_run_a"

    # dry_run form validates without docker
    runs_before = len([a for a in read_argv() if a and a[0] == "run"])
    preview = client.post(
        "/v1/projects/example_new/runs/example_run_c/submit-from/example_run_a",
        json={"dry_run": True},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["dry_run"] is True
    assert len([a for a in read_argv() if a and a[0] == "run"]) == runs_before

    missing = client.post(
        "/v1/projects/example_new/runs/example_run_d/submit-from/example_absent",
        json={},
    )
    assert missing.status_code == 404


def test_rebind_run_name_boundaries_and_sibling_guard() -> None:
    from kikai_lab.operation import OperationError
    from kikai_lab.server.submit import rebind_run_name

    # reviewer's confirmed corruption case: run_1 inside run_10's path
    assert (
        rebind_run_name("/data/runs/run_10/best.pt", "run_1", "run_2")
        == "/data/runs/run_10/best.pt"
    )
    # derived names still rebind across _ boundaries
    assert (
        rebind_run_name("run_1_qc_{step6} out=/r/run_1/qc", "run_1", "run_2")
        == "run_2_qc_{step6} out=/r/run_2/qc"
    )
    assert rebind_run_name(["run_1", {"x": "a_run_1_b"}], "run_1", "run_2") == [
        "run_2",
        {"x": "a_run_2_b"},
    ]
    # a referenced SIBLING run extending the parent's name is fail-closed
    try:
        rebind_run_name(
            "/runs/example_run_v2/ckpt.pt",
            "example_run",
            "example_run_b",
            sibling_runs=frozenset({"example_run_v2"}),
        )
        raise AssertionError("expected run.rebind_invalid")
    except OperationError as exc:
        assert exc.code == "run.rebind_invalid"
        assert exc.details["token"] == "example_run_v2"
    # a sibling's DERIVED artifact token is protected too
    try:
        rebind_run_name(
            "/r/example_run_v2_ckpt.pt",
            "example_run",
            "example_run_b",
            sibling_runs=frozenset({"example_run_v2"}),
        )
        raise AssertionError("expected run.rebind_invalid")
    except OperationError as exc:
        assert exc.code == "run.rebind_invalid"
    # non-embedding siblings never trip the guard
    assert (
        rebind_run_name(
            "/r/example_run/x", "example_run", "example_run_b",
            sibling_runs=frozenset({"other_run"}),
        )
        == "/r/example_run_b/x"
    )


def test_apply_overrides_supersedes_equals_form() -> None:
    from kikai_lab.server.submit import apply_overrides

    base = {"args": ["--max-steps=100", "--lr=1e-4", "--amp"]}
    out = apply_overrides(base, {"args_set": {"--max-steps": "85"}})
    # no duplicate flag left for a first-wins parser to prefer
    assert out["args"] == ["--lr=1e-4", "--amp", "--max-steps", "85"]
    out = apply_overrides(base, {"args_set": {"--lr": None}})
    assert out["args"] == ["--max-steps=100", "--amp"]


def test_apply_overrides_nargs_and_bool_flags() -> None:
    from kikai_lab.server.submit import apply_overrides

    base = {"args": ["--milestones", "1000", "2000", "3000", "--lr", "1e-4", "--amp"]}
    # null removes flag AND all its values (no orphaned positionals)
    out = apply_overrides(base, {"args_set": {"--milestones": None}})
    assert out["args"] == ["--lr", "1e-4", "--amp"]
    # scalar upsert replaces ALL values of an nargs flag
    out = apply_overrides(base, {"args_set": {"--milestones": "500"}})
    assert out["args"] == ["--milestones", "500", "--lr", "1e-4", "--amp"]
    # list value sets a multi-value flag
    out = apply_overrides(base, {"args_set": {"--milestones": ["100", "200"]}})
    assert out["args"][:3] == ["--milestones", "100", "200"]
    # "" strips values down to a bare flag; booleans stringify lowercase
    out = apply_overrides(base, {"args_set": {"--lr": "", "--fused": True}})
    assert out["args"] == ["--milestones", "1000", "2000", "3000", "--lr", "--amp", "--fused", "true"]


def test_submit_from_env_era_warning_and_lineage_survives_plain_retry(
    tmp_path: Path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)  # parent example_run_a
    client.post("/v1/projects/example_new/runs/example_run_a/stop")
    set_control(exists=False)

    # simulate a pre-env-persistence parent record
    parent_path = tmp_path / "example_new" / "runs" / "example_run_a.yaml"
    parent = yaml.safe_load(parent_path.read_text())
    parent["submission"].pop("env", None)
    parent_path.write_text(yaml.safe_dump(parent), encoding="utf-8")

    child = client.post(
        "/v1/projects/example_new/runs/example_run_b/submit-from/example_run_a",
        json={"overrides": {"args_set": {"--vgg-weight": "5.0"}}},
    )
    assert child.status_code == 201, child.text
    warnings = child.json()["warnings"]
    assert any(w["code"] == "run.parent_env_unrecorded" for w in warnings)
    assert warnings[0]["details"] == {"parent_run": "example_run_a"}

    # identical-body plain-submit retry must NOT erase lineage
    child_record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_b.yaml").read_text()
    )
    assert child_record["submission"]["parent_run"] == "example_run_a"
    child_record["status"] = "submitting"  # reopen the retry window
    (tmp_path / "example_new" / "runs" / "example_run_b.yaml").write_text(
        yaml.safe_dump(child_record), encoding="utf-8"
    )
    from kikai_lab.server.submit import reconstruct_submit_body

    set_control(exists=False)  # free the profile-named container for the retry
    body, _ = reconstruct_submit_body(tmp_path / "example_new", "example_run_b")
    retry = client.post(
        "/v1/projects/example_new/runs/example_run_b/submit", json=body
    )
    assert retry.status_code == 201, retry.text
    rewritten = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_run_b.yaml").read_text()
    )
    assert rewritten["submission"]["parent_run"] == "example_run_a"
    assert rewritten["submission"]["overrides"] == {
        "args_set": {"--vgg-weight": "5.0"}
    }


def test_arg_value_after_handles_both_forms() -> None:
    from kikai_lab.server.submit import arg_value_after

    assert arg_value_after(["--run-dir", "/a/b"], "--run-dir") == "/a/b"
    assert arg_value_after(["--run-dir=/a/b"], "--run-dir") == "/a/b"
    assert arg_value_after(["--run-dirx=/a"], "--run-dir") is None
    assert arg_value_after(["--run-dir", "--next"], "--run-dir") is None
    assert arg_value_after([], "--run-dir") is None


def test_probe_from_warm_starts_from_parent_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, read_argv, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(
        client,
        args=["--run-dir", "${CONTAINER_RUNS_ROOT}/example_run_a/run", "--max-steps", "100"],
    )
    client.post("/v1/projects/example_new/runs/example_run_a/stop")
    set_control(exists=False)
    ckpt_dir = tmp_path / "host_runs" / "example_run_a" / "run" / "checkpoints"
    ckpt_dir.mkdir(parents=True)
    (ckpt_dir / "checkpoint_step_000080_loss2p0.pt").write_bytes(b"x")
    (ckpt_dir / "best_step_000060_loss1p5.pt").write_bytes(b"x")

    resp = client.post(
        "/v1/projects/example_new/runs/example_probe_a/probe-from/example_run_a",
        json={
            "question": "does adv pressure move mouth texture at all?",
            "probe_steps": 25,
            "overrides": {"args_set": {"--adv-weight": "0.05"}},
        },
    )
    assert resp.status_code == 201, resp.text
    record = yaml.safe_load(
        (tmp_path / "example_new" / "runs" / "example_probe_a.yaml").read_text()
    )
    probe = record["probe"]
    assert probe["parent_run"] == "example_run_a"
    assert probe["question"].startswith("does adv")
    assert probe["budget_steps"] == 25
    assert probe["resume_step"] == 60  # "best" = newest best_step_*
    args = record["submission"]["args"]
    # resume points INTO THE PARENT's container dir (not rebound to the probe)
    assert args[args.index("--resume-checkpoint") + 1] == (
        "${CONTAINER_RUNS_ROOT}/example_run_a/run/checkpoints/best_step_000060_loss1p5.pt"
    )
    assert args[args.index("--max-steps") + 1] == "85"  # resume 60 + budget 25
    assert args[args.index("--adv-weight") + 1] == "0.05"  # caller override applied
    # the probe's own run_dir was rebound away from the parent
    assert "example_probe_a" in record["submission"]["run_dir"]
    managed = yaml.safe_load(
        (tmp_path / "example_new" / "managed_runs" / "example_probe_a.yaml").read_text()
    )
    assert managed["max_step"] == 85
    assert managed["retention"] == {"keep_latest": 1, "keep_best": 1}  # probe default

    # checkpoint="latest" prefers the periodic family
    set_control(exists=False)
    resp = client.post(
        "/v1/projects/example_new/runs/example_probe_b/probe-from/example_run_a",
        json={"question": "latest?", "probe_steps": 10, "checkpoint": "latest",
              "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    argv = resp.json()["data"]["op_request"]["args"]
    assert any("checkpoint_step_000080" in a for a in argv)

    # fail-closed validation
    no_question = client.post(
        "/v1/projects/example_new/runs/example_probe_c/probe-from/example_run_a",
        json={"probe_steps": 10},
    )
    assert no_question.status_code == 422
    too_long = client.post(
        "/v1/projects/example_new/runs/example_probe_c/probe-from/example_run_a",
        json={"question": "x?", "probe_steps": 50000},
    )
    assert too_long.status_code == 422
    missing_step = client.post(
        "/v1/projects/example_new/runs/example_probe_c/probe-from/example_run_a",
        json={"question": "x?", "probe_steps": 10, "checkpoint": 999},
    )
    assert missing_step.status_code == 404

    # brief surfaces the probe question
    brief = client.get("/v1/projects/example_new/brief").json()["data"]
    by_name = {r["run_name"]: r for r in brief["runs"]}
    assert by_name["example_probe_a"]["probe"].startswith("does adv")
    assert by_name["example_run_a"]["probe"] is None

    # non-dict overrides.managed fails closed (422, not 500)
    bad_managed = client.post(
        "/v1/projects/example_new/runs/example_probe_c/probe-from/example_run_a",
        json={"question": "x?", "probe_steps": 10, "overrides": {"managed": "oops"}},
    )
    assert bad_managed.status_code == 422

    # identical-sha crash-window retry must NOT erase the probe metadata
    probe_path = tmp_path / "example_new" / "runs" / "example_probe_a.yaml"
    rec = yaml.safe_load(probe_path.read_text())
    rec["status"] = "submitting"  # reopen the retry window
    probe_path.write_text(yaml.safe_dump(rec), encoding="utf-8")
    from kikai_lab.server.submit import reconstruct_submit_body

    retry_body, _ = reconstruct_submit_body(tmp_path / "example_new", "example_probe_a")
    set_control(exists=False)
    retry = client.post(
        "/v1/projects/example_new/runs/example_probe_a/submit", json=retry_body
    )
    assert retry.status_code == 201, retry.text
    rewritten = yaml.safe_load(probe_path.read_text())
    assert rewritten["probe"]["question"].startswith("does adv")
    assert rewritten["probe"]["resume_step"] == 60
    assert rewritten["submission"]["parent_run"] == "example_run_a"


def test_run_control_write_read_and_sync(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)  # managed run with run_dir under host_runs

    run_dir = tmp_path / "host_runs" / "example_run_a" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    resp = client.post(
        "/v1/projects/example_new/runs/example_run_a/control",
        json={"max_steps": 120000, "early_stop_patience": 15,
              "early_stop_min_delta": 0.0005},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["managed_max_step_synced"] is True
    written = json.loads((run_dir / "control.json").read_text())
    assert written == {"max_steps": 120000, "early_stop_patience": 15,
                       "early_stop_min_delta": 0.0005}
    managed = yaml.safe_load(
        (tmp_path / "example_new" / "managed_runs" / "example_run_a.yaml").read_text()
    )
    assert managed["max_step"] == 120000  # daemon lifecycle follows the new cap

    # journaled
    journal = [
        json.loads(line)
        for line in (tmp_path / "example_new" / "journal.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["kind"] == "run_control"
    assert journal[-1]["max_steps"] == 120000

    # GET: requested visible; not yet applied (no trainer event)
    view = client.get("/v1/projects/example_new/runs/example_run_a/control").json()["data"]
    assert view["requested"]["max_steps"] == 120000
    assert view["applied"] is None

    # trainer writes a control_applied event -> GET reflects it
    with (run_dir / "metrics.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "control_applied", "step": 22300,
                            "applied": {"max_steps": 120000}, "ignored": []}) + "\n")
    view = client.get("/v1/projects/example_new/runs/example_run_a/control").json()["data"]
    assert view["applied"]["step"] == 22300
    assert view["applied"]["applied"] == {"max_steps": 120000}

    # fail-closed validation
    for bad in (
        {},                                  # empty
        {"max_steps": 0},
        {"max_steps": True},
        {"early_stop_min_delta": -1},
        {"stop": "hard"},                    # hard kill is POST .../stop
        {"max_step": 100},                   # typo'd key rejected, not dropped
    ):
        r = client.post(
            "/v1/projects/example_new/runs/example_run_a/control", json=bad
        )
        assert r.status_code == 422, (bad, r.text)
    # nothing above overwrote the good control file
    assert json.loads((run_dir / "control.json").read_text())["max_steps"] == 120000

    # graceful stop request writes through
    r = client.post(
        "/v1/projects/example_new/runs/example_run_a/control",
        json={"stop": "graceful"},
    )
    assert r.status_code == 201
    assert json.loads((run_dir / "control.json").read_text()) == {"stop": "graceful"}


def test_submit_clears_stale_control_and_warns_on_truncating_cap(
    tmp_path: Path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    submit(client)
    run_dir = tmp_path / "host_runs" / "example_run_a" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    # a graceful stop left a control file; a relaunch must not inherit it
    (run_dir / "control.json").write_text('{"stop": "graceful"}', encoding="utf-8")
    rec_path = tmp_path / "example_new" / "runs" / "example_run_a.yaml"
    rec = yaml.safe_load(rec_path.read_text())
    rec["status"] = "submitting"  # reopen the retry window
    rec_path.write_text(yaml.safe_dump(rec), encoding="utf-8")
    from kikai_lab.server.submit import reconstruct_submit_body

    body, _ = reconstruct_submit_body(tmp_path / "example_new", "example_run_a")
    set_control(exists=False)
    retry = client.post("/v1/projects/example_new/runs/example_run_a/submit", json=body)
    assert retry.status_code == 201, retry.text
    assert not (run_dir / "control.json").exists()  # stale control cleared

    # truncating cap: max_steps at/below the last known step warns
    (run_dir / "metrics.jsonl").write_text(
        json.dumps({"event": "train_metrics", "step": 500, "loss": 1.0}) + "\n",
        encoding="utf-8",
    )
    resp = client.post(
        "/v1/projects/example_new/runs/example_run_a/control",
        json={"max_steps": 400},
    )
    assert resp.status_code == 201
    warnings = resp.json()["warnings"]
    assert any(w["code"] == "run.control_truncates" for w in warnings)

    # UNREMOVABLE stale control fails the launch WITH full submit_failed cleanup
    # (record + managed_run unlink + journal) — never a ghost managed run
    import os
    import stat as stat_mod

    (run_dir / "control.json").write_text('{"stop": "graceful"}', encoding="utf-8")
    rec = yaml.safe_load(rec_path.read_text())
    rec["status"] = "submitting"
    rec_path.write_text(yaml.safe_dump(rec), encoding="utf-8")
    set_control(exists=False)
    os.chmod(run_dir, stat_mod.S_IRUSR | stat_mod.S_IXUSR)  # unlink now EACCES
    try:
        blocked = client.post(
            "/v1/projects/example_new/runs/example_run_a/submit", json=body
        )
    finally:
        os.chmod(run_dir, 0o755)
    assert blocked.status_code != 201
    assert blocked.json()["errors"][0]["code"] == "run.control_stale_unremovable"
    cleaned = yaml.safe_load(rec_path.read_text())
    assert cleaned["status"] == "submit_failed"
    assert cleaned["submit_error"] == "run.control_stale_unremovable"
    assert not (
        tmp_path / "example_new" / "managed_runs" / "example_run_a.yaml"
    ).exists()  # no ghost for the daemon to tick
    journal = [
        json.loads(line)
        for line in (tmp_path / "example_new" / "journal.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["kind"] == "run_submit_failed"
    (run_dir / "control.json").unlink()  # tidy for the assertions below
    # relaunch cleanly for the control-endpoint checks below
    rec = yaml.safe_load(rec_path.read_text())
    rec["status"] = "submitting"
    rec_path.write_text(yaml.safe_dump(rec), encoding="utf-8")
    set_control(exists=False)
    relaunch = client.post(
        "/v1/projects/example_new/runs/example_run_a/submit", json=body
    )
    assert relaunch.status_code == 201, relaunch.text

    # absent run_dir is a 404 (run_dir_missing), not a chown hint
    other = tmp_path / "example_new" / "runs" / "example_run_ghost.yaml"
    other.write_text(
        yaml.safe_dump({
            "schema_version": 1, "run_name": "example_run_ghost",
            "submission": {"run_dir": "${EXAMPLE_RUNS_ROOT}/example_run_ghost/run",
                           "bundle_id": "x", "container_id": "y", "entrypoint": "t",
                           "args": [], "env": {}},
        }),
        encoding="utf-8",
    )
    resp = client.post(
        "/v1/projects/example_new/runs/example_run_ghost/control",
        json={"max_steps": 10},
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["code"] == "run.run_dir_missing"


def test_probe_from_refuses_unrelocated_run_dir_endpoint(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a parent whose run_dir naming differs from its run name must be
    refused at the endpoint with 422 — locks in that the guard is WIRED, not just
    that the helper works."""
    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    # parent run_dir uses a DIFFERENT token than the run name -> rebind can't relocate
    submit(
        client,
        args=["--run-dir", "${CONTAINER_RUNS_ROOT}/legacy_renderer/run", "--max-steps", "100"],
        run_dir="${EXAMPLE_RUNS_ROOT}/legacy_renderer/run",
    )
    client.post("/v1/projects/example_new/runs/example_run_a/stop")
    set_control(exists=False)
    ckpt = tmp_path / "host_runs" / "legacy_renderer" / "run" / "checkpoints"
    ckpt.mkdir(parents=True)
    (ckpt / "best_step_000080_loss1p0.pt").write_bytes(b"x")

    resp = client.post(
        "/v1/projects/example_new/runs/example_probe_x/probe-from/example_run_a",
        json={"question": "would this corrupt the parent?", "probe_steps": 10},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "run.run_dir_relocation_invalid"

    # with an explicit fresh run_dir + matching --run-dir, it launches
    ok = client.post(
        "/v1/projects/example_new/runs/example_probe_y/probe-from/example_run_a",
        json={"question": "fresh dir", "probe_steps": 10,
              "overrides": {"run_dir": "${EXAMPLE_RUNS_ROOT}/example_probe_y/run",
                            "args_set": {"--run-dir": "${CONTAINER_RUNS_ROOT}/example_probe_y/run"}}},
    )
    assert ok.status_code == 201, ok.text


def test_submit_from_refuses_unrelocated_run_dir(tmp_path: Path, monkeypatch) -> None:
    """A parent whose run_dir naming differs from its run name cannot be rebound;
    the child must be refused, not silently pointed at the parent's dir."""
    import pytest

    from kikai_lab.operation import OperationError
    from kikai_lab.server.submit import ensure_run_dir_relocated

    # run_dir uses a DIFFERENT token than the run name -> rebind is a no-op
    parent = {
        "run_dir": "${HOST}/legacy_renderer_dir/run",
        "args": ["--run-dir", "${C}/legacy_renderer_dir/run", "--max-steps", "100"],
    }
    # child inherited the parent's run_dir unchanged (rebind couldn't touch it)
    with pytest.raises(OperationError) as exc:
        ensure_run_dir_relocated(parent, dict(parent), "child_run")
    assert exc.value.code == "run.run_dir_relocation_invalid"

    # only the trainer arg collides (managed run_dir was relocated) -> still refused
    child_arg_only = {
        "run_dir": "${HOST}/child_run/run",  # relocated
        "args": ["--run-dir", "${C}/legacy_renderer_dir/run"],  # NOT relocated
    }
    with pytest.raises(OperationError) as exc:
        ensure_run_dir_relocated(parent, child_arg_only, "child_run")
    assert exc.value.code == "run.run_dir_relocation_invalid"

    # a genuinely relocated child passes
    good = {
        "run_dir": "${HOST}/child_run/run",
        "args": ["--run-dir", "${C}/child_run/run"],
    }
    ensure_run_dir_relocated(parent, good, "child_run")  # no raise

    # a parent with no run_dir (unmanaged) is a no-op
    ensure_run_dir_relocated({"args": []}, {"args": []}, "child_run")


def test_submit_posts_start_notification(tmp_path: Path, monkeypatch) -> None:
    """A managed submit with a delivery target announces itself; failure to
    deliver is a warning, never a failed submit (the container already runs)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(204)
            self.end_headers()
        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_WEBHOOK_URL", f"http://127.0.0.1:{server.server_port}/hook")

    client = make_client(tmp_path)
    prepare_project(tmp_path, client)
    fake, _, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_container_env(monkeypatch, tmp_path)
    # register a delivery target the notification can resolve
    (tmp_path / "example_new" / "delivery_targets").mkdir(exist_ok=True)
    (tmp_path / "example_new" / "delivery_targets" / "example_hook.json").write_text(
        json.dumps({"schema_version": 1, "kind": "discord_webhook",
                    "target_id": "example_hook",
                    "webhook_url": "env:TEST_WEBHOOK_URL"}), encoding="utf-8")

    resp = submit(client, managed={"max_step": 100,
                                   "retention": {"keep_latest": 2, "keep_best": 1},
                                   "delivery_target_id": "example_hook"})
    assert resp.status_code == 201, resp.text
    assert received, "start notification was not posted"
    payload = received[0].decode()
    assert "example_run_a" in payload and "training started" in payload
    assert "fresh" in payload  # no --resume-checkpoint in args
    server.shutdown()

    # webhook unreachable -> submit still succeeds, with a non-blocking warning
    monkeypatch.setenv("TEST_WEBHOOK_URL", "http://127.0.0.1:9/unreachable")
    set_control(exists=False)
    resp2 = client.post(
        "/v1/projects/example_new/runs/example_run_n2/submit",
        json=submission_body(
            args=["--run-dir", "${CONTAINER_RUNS_ROOT}/example_run_n2/run"],
            run_dir="${EXAMPLE_RUNS_ROOT}/example_run_n2/run",
            managed={"max_step": 100, "retention": {"keep_latest": 1, "keep_best": 1},
                     "delivery_target_id": "example_hook"}),
    )
    assert resp2.status_code == 201, resp2.text
    codes = [w.get("code") for w in resp2.json()["warnings"]]
    assert "run.start_notification_failed" in codes
