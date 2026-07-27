from __future__ import annotations

import json
from pathlib import Path

import yaml

from kikai_lab.server.runs import derive_status
from tests.test_server_projects import make_client, make_project


def write_fake_docker(tmp_path: Path):
    """A controllable docker CLI: state/logs come from a JSON control file."""
    control = tmp_path / "docker_control.json"
    control.write_text(json.dumps({"state": None, "logs": ""}), encoding="utf-8")
    fake = tmp_path / "fake_docker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"control = json.loads(pathlib.Path({str(control)!r}).read_text())\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'inspect':\n"
        "    if control.get('state') is None:\n"
        "        sys.stderr.write('Error: No such object')\n"
        "        raise SystemExit(1)\n"
        "    print(json.dumps([{'State': control['state']}]))\n"
        "    raise SystemExit(0)\n"
        "if cmd == 'logs':\n"
        "    if control.get('state') is None:\n"
        "        sys.stderr.write('Error: No such container')\n"
        "        raise SystemExit(1)\n"
        "    sys.stdout.write(control.get('logs', ''))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    def set_control(state, logs=""):
        control.write_text(json.dumps({"state": state, "logs": logs}), encoding="utf-8")

    return fake, set_control


def write_metrics(run_dir: Path, steps: list[int], *, terminal: str | None = None) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for step in steps:
        lines.append(
            json.dumps(
                {
                    "event": "train_metrics",
                    "step": step,
                    "loss": float(1000 - step) / 10.0,
                    "lr": 0.0002,
                }
            )
        )
        lines.append(json.dumps({"event": "checkpoint", "step": step, "path": f"c{step}.pt"}))
    if terminal:
        lines.append(json.dumps({"event": terminal, "step": steps[-1] if steps else 0}))
    (run_dir / "metrics.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir / "metrics.jsonl"


def make_run_fixture(
    tmp_path: Path,
    *,
    steps: list[int] | None = None,
    terminal: str | None = None,
    qc_done: list[int] | None = None,
    checkpoints: list[int] | None = None,
) -> Path:
    """A project with one managed run whose run_dir lives under tmp_path/run_dir."""
    project = make_project(tmp_path, "example_a")
    run_dir = tmp_path / "run_dir"
    write_metrics(run_dir, steps or [100, 200, 300], terminal=terminal)
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    for step in checkpoints or [200, 300]:
        (checkpoint_dir / f"checkpoint_step_{step:06d}_loss9p9.pt").write_bytes(b"x")
    (project / "containers" / "example_training.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "kind": "docker_container",
                "container_id": "example_training",
                "docker": {"name": "example-training", "image": "example:latest"},
            }
        ),
        encoding="utf-8",
    )
    (project / "managed_runs" / "example_run.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "kind": "managed_run",
                "run_id": "example_run",
                "run_dir": str(run_dir),
                "training_container_id": "example_training",
                "max_step": 1000,
            }
        ),
        encoding="utf-8",
    )
    if qc_done is not None:
        (project / "managed_runs" / "example_run.progress.json").write_text(
            json.dumps(
                {
                    "run_id": "example_run",
                    "qc_done_steps": qc_done,
                    "lifecycle_state": "running",
                    "finalized": False,
                    "finalize_notified": False,
                    "seen_running": True,
                    "last_error": None,
                    "ticks": 5,
                }
            ),
            encoding="utf-8",
        )
    return project


# ----------------------------------------------------------------- derive_status
def test_derive_status_stopped_by_control() -> None:
    from kikai_lab.server.runs import derive_status

    gone = {"managed": True, "exists": False, "running": False}
    assert (
        derive_status(
            declared="running",
            container=gone,
            progress={"finalized": True},
            terminal_event="stopped_by_control",
        )
        == "stopped"
    )


def test_derive_status_matrix() -> None:
    running = {"managed": True, "exists": True, "running": True}
    exited0 = {"managed": True, "exists": True, "running": False, "state": "exited", "exit_code": 0}
    exited1 = {"managed": True, "exists": True, "running": False, "state": "exited", "exit_code": 1}
    gone = {"managed": True, "exists": False, "running": False}

    base = {"declared": "submitted", "progress": {}, "terminal_event": None}
    assert derive_status(**{**base, "container": running}) == "running"
    assert derive_status(**{**base, "container": exited0}) == "exited_pending_finalize"
    assert derive_status(**{**base, "container": exited1}) == "failed"
    assert (
        derive_status(
            declared=None, container=exited1, progress={}, terminal_event="early_stop"
        )
        == "exited_pending_finalize"
    )
    assert (
        derive_status(
            declared=None, container=gone, progress={"finalized": True}, terminal_event="done"
        )
        == "completed"
    )
    assert (
        derive_status(
            declared=None,
            container=gone,
            progress={"finalized": True},
            terminal_event="early_stop",
        )
        == "early_stopped"
    )
    # A crash has no terminal metrics row; the daemon still finalizes stably exited
    # containers — that must NOT read as success (reviewer H2).
    assert (
        derive_status(
            declared=None, container=gone, progress={"finalized": True}, terminal_event=None
        )
        == "failed"
    )
    created = {"managed": True, "exists": True, "running": False, "state": "created"}
    restarting = {"managed": True, "exists": True, "running": False, "state": "restarting"}
    assert derive_status(**{**base, "container": created}) == "submitted"
    assert derive_status(**{**base, "container": restarting}) == "running"
    assert derive_status(**{**base, "container": gone}) == "submitted"
    assert (
        derive_status(
            declared=None,
            container=gone,
            progress={"seen_running": True},
            terminal_event=None,
        )
        == "unknown"
    )
    assert (
        derive_status(
            declared="completed",
            container={"managed": False, "exists": False, "running": False},
            progress={},
            terminal_event=None,
        )
        == "completed"
    )


# ------------------------------------------------------------------------ routes
def test_runs_index_lists_declared_and_managed(tmp_path: Path) -> None:
    make_run_fixture(tmp_path, qc_done=[100])
    client = make_client(tmp_path)
    payload = client.get("/v1/projects/example_a/runs").json()
    assert payload["data"]["total"] == 1
    run = payload["data"]["runs"][0]
    assert run["run_name"] == "example_run"
    assert run["managed"] is True
    assert run["lifecycle_state"] == "running"

    filtered = client.get(
        "/v1/projects/example_a/runs", params={"experiment_id": "other"}
    ).json()
    assert filtered["data"]["total"] == 0


def test_runs_index_shows_terminal_status_for_finalized(tmp_path: Path) -> None:
    """Declared status freezes at 'running' forever (nothing rewrites the run
    yaml) — list surfaces must show the daemon-recorded terminal truth instead
    of phantom 'running' rows (dashboard incident 2026-07-23)."""
    project = make_run_fixture(tmp_path, qc_done=[100])
    progress_file = project / "managed_runs" / "example_run.progress.json"
    progress = json.loads(progress_file.read_text())
    progress.update(finalized=True, lifecycle_state="done", terminal_status="completed")
    progress_file.write_text(json.dumps(progress))

    client = make_client(tmp_path)
    run = client.get("/v1/projects/example_a/runs").json()["data"]["runs"][0]
    assert run["status"] == "completed"

    # pre-migration progress (finalized before terminal_status existed):
    # never show 'running'; 'finalized' until the daemon backfills
    progress.pop("terminal_status")
    progress_file.write_text(json.dumps(progress))
    run = client.get("/v1/projects/example_a/runs").json()["data"]["runs"][0]
    assert run["status"] == "finalized"

    brief = client.get("/v1/projects/example_a/brief").json()["data"]
    entry = next(r for r in brief["runs"] if r["run_name"] == "example_run")
    assert entry["status"] == "finalized"

    # experiment detail page (5th status surface) must agree
    client.put(
        "/v1/projects/example_a/experiments/example_exp", json={"title": "Example"}
    )
    detail = client.get("/v1/projects/example_a/experiments/example_exp").json()
    assert detail["ok"], detail
    row = next(r for r in detail["data"]["runs"] if r["run_name"] == "example_run")
    assert row["status"] == "finalized"


def test_reconcile_tick_backfills_terminal_status(tmp_path: Path) -> None:
    from kikai_lab.reconcile import tick

    project = make_run_fixture(tmp_path, terminal="done", qc_done=[100])
    managed = yaml.safe_load(
        (project / "managed_runs" / "example_run.yaml").read_text()
    )
    progress = {"run_id": "example_run", "finalized": True, "lifecycle_state": "done"}
    tick(project, managed, progress, execute=lambda op: {}, inspect=None)
    assert progress["terminal_status"] == "completed"

    # a pre-upgrade FORCE-finalized run (no terminal metrics row, control.json
    # still carries the request) must backfill as 'stopped', not 'failed'
    project2 = make_run_fixture(tmp_path / "b", qc_done=[100])
    (project2 / "managed_runs" / "example_run.control.json").write_text(
        json.dumps({"force_finalize": True})
    )
    managed2 = yaml.safe_load(
        (project2 / "managed_runs" / "example_run.yaml").read_text()
    )
    progress2 = {"run_id": "example_run", "finalized": True, "lifecycle_state": "done"}
    tick(project2, managed2, progress2, execute=lambda op: {}, inspect=None)
    assert progress2["terminal_status"] == "stopped"


def test_run_detail_merges_all_sources(tmp_path: Path, monkeypatch) -> None:
    make_run_fixture(tmp_path, steps=[100, 200, 300], qc_done=[100, 200])
    fake, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_control({"Running": True, "Status": "running", "StartedAt": "2026-01-01T00:00:00Z"})

    client = make_client(tmp_path)
    payload = client.get("/v1/projects/example_a/runs/example_run").json()
    data = payload["data"]
    assert data["derived_status"] == "running"
    assert data["container"]["running"] is True
    assert [c["step"] for c in data["checkpoints"]] == [200, 300]
    assert data["latest_metrics"]["step"] == 300
    assert data["progress"]["qc_done_steps"] == [100, 200]


def test_run_status_is_small_and_fresh(tmp_path: Path, monkeypatch) -> None:
    make_run_fixture(tmp_path, steps=[100, 200, 300], qc_done=[100])
    fake, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_control({"Running": True, "Status": "running"})

    client = make_client(tmp_path)
    payload = client.get("/v1/projects/example_a/runs/example_run/status").json()
    data = payload["data"]
    assert data["derived_status"] == "running"
    assert data["latest_step"] == 300
    assert data["latest_loss"] == 70.0
    assert data["qc_done_steps"] == [100]
    assert data["terminal_event"] is None


def test_run_status_after_finalize(tmp_path: Path, monkeypatch) -> None:
    project = make_run_fixture(tmp_path, steps=[100, 200], terminal="early_stop")
    progress = project / "managed_runs" / "example_run.progress.json"
    progress.write_text(
        json.dumps(
            {
                "run_id": "example_run",
                "qc_done_steps": [100, 200],
                "lifecycle_state": "done",
                "finalized": True,
                "seen_running": True,
                "ticks": 9,
            }
        ),
        encoding="utf-8",
    )
    fake, _ = write_fake_docker(tmp_path)  # container gone
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    client = make_client(tmp_path)
    data = client.get("/v1/projects/example_a/runs/example_run/status").json()["data"]
    assert data["derived_status"] == "early_stopped"
    assert data["terminal_event"] == "early_stop"


def test_run_logs_via_fake_docker(tmp_path: Path, monkeypatch) -> None:
    make_run_fixture(tmp_path)
    fake, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_control({"Running": True}, logs="step 100\nstep 200\n")

    client = make_client(tmp_path)
    data = client.get(
        "/v1/projects/example_a/runs/example_run/logs", params={"tail": 10}
    ).json()["data"]
    assert data["lines"] == ["step 100", "step 200"]

    set_control(None)
    gone = client.get("/v1/projects/example_a/runs/example_run/logs")
    assert gone.status_code == 404
    assert gone.json()["errors"][0]["code"] == "run.container_not_found"


def test_run_metrics_columnar_and_downsampled(tmp_path: Path) -> None:
    make_run_fixture(tmp_path, steps=list(range(0, 1000, 10)))
    client = make_client(tmp_path)
    data = client.get(
        "/v1/projects/example_a/runs/example_run/metrics",
        params={"keys": "loss", "max_points": 10},
    ).json()["data"]
    assert data["keys"] == ["loss"]
    assert data["downsampled"] is True
    assert data["points"] <= 11
    assert data["step"][-1] == 990  # freshest point always survives
    assert data["last_row"]["step"] == 990
    assert "loss" in data["available_keys"] and "lr" in data["available_keys"]
    assert len(data["series"]["loss"]) == len(data["step"])


def test_run_metrics_since_step_and_missing(tmp_path: Path) -> None:
    make_run_fixture(tmp_path, steps=[100, 200, 300])
    client = make_client(tmp_path)
    data = client.get(
        "/v1/projects/example_a/runs/example_run/metrics",
        params={"keys": "loss", "since_step": 250},
    ).json()["data"]
    assert data["step"] == [300]

    (tmp_path / "run_dir" / "metrics.jsonl").unlink()
    missing = client.get("/v1/projects/example_a/runs/example_run/metrics")
    assert missing.status_code == 404
    assert missing.json()["errors"][0]["code"] == "run.metrics_missing"


def test_run_metrics_resolves_env_ref_run_dir(tmp_path: Path, monkeypatch) -> None:
    project = make_run_fixture(tmp_path, steps=[100])
    managed = project / "managed_runs" / "example_run.yaml"
    record = yaml.safe_load(managed.read_text())
    record["run_dir"] = "${EXAMPLE_RUNS_ROOT}/run_dir"
    managed.write_text(yaml.safe_dump(record), encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_RUNS_ROOT", str(tmp_path))

    client = make_client(tmp_path)
    data = client.get(
        "/v1/projects/example_a/runs/example_run/metrics", params={"keys": "loss"}
    ).json()["data"]
    assert data["step"] == [100]


def test_experiment_metrics_comparison(tmp_path: Path) -> None:
    make_run_fixture(tmp_path, steps=[100, 200])
    client = make_client(tmp_path)
    data = client.get(
        "/v1/projects/example_a/experiments/example_exp/metrics", params={"keys": "loss"}
    ).json()["data"]
    assert data["experiment_id"] == "example_exp"
    assert data["runs"]["example_run"]["step"] == [100, 200]


def test_run_events_structural_seqs_survive_late_qc(tmp_path: Path) -> None:
    """Seqs are structural (step / sentinels), so QC events landing AFTER the terminal
    metrics row neither duplicate the terminal event nor get lost for a poller."""
    project = make_run_fixture(tmp_path, steps=[100, 200], terminal="done", qc_done=[100])
    client = make_client(tmp_path)
    data = client.get("/v1/projects/example_a/runs/example_run/events").json()["data"]
    assert [(e["seq"], e["kind"]) for e in data["events"]] == [
        (100, "qc_delivered"),
        (10**9 + 1, "terminal"),
    ]
    # The endpoint's OWN cursor must be safe to resume from: while the terminal row
    # exists but finalize hasn't happened, later QC events still arrive below the
    # terminal sentinel, so last_seq stays at the highest QC step.
    cursor = data["last_seq"]
    assert cursor == 100

    # The reconciler QCs the final checkpoint after the trainer wrote 'done'.
    progress = project / "managed_runs" / "example_run.progress.json"
    state = json.loads(progress.read_text())
    state["qc_done_steps"] = [100, 200]
    progress.write_text(json.dumps(state), encoding="utf-8")

    fresh = client.get(
        "/v1/projects/example_a/runs/example_run/events", params={"since_seq": cursor}
    ).json()["data"]
    assert [(e["seq"], e["kind"]) for e in fresh["events"]] == [
        (200, "qc_delivered"),  # late QC delivered exactly once
        (10**9 + 1, "terminal"),  # terminal not duplicated for cursor >= its seq
    ]
    # While not finalized the cursor stays below the terminal sentinel, so the
    # terminal event is re-delivered (at-least-once, idempotent kind) — but never lost.
    pending = client.get(
        "/v1/projects/example_a/runs/example_run/events",
        params={"since_seq": fresh["last_seq"]},
    ).json()["data"]
    assert [e["kind"] for e in pending["events"]] == ["terminal"]

    state = json.loads(progress.read_text())
    state["finalized"] = True
    progress.write_text(json.dumps(state), encoding="utf-8")
    finalized = client.get(
        "/v1/projects/example_a/runs/example_run/events",
        params={"since_seq": pending["last_seq"]},
    ).json()["data"]
    assert [e["kind"] for e in finalized["events"]] == ["terminal", "finalized"]
    drained = client.get(
        "/v1/projects/example_a/runs/example_run/events",
        params={"since_seq": finalized["last_seq"]},
    ).json()["data"]
    assert drained["events"] == []


def test_run_unknown_is_404(tmp_path: Path) -> None:
    make_project(tmp_path, "example_a")
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/runs/example_missing")
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "run.not_found"


def test_run_dir_containment_when_roots_configured(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from kikai_lab.server.app import create_app
    from kikai_lab.server.registry import ServerConfig

    make_run_fixture(tmp_path, steps=[100])
    allowed = ServerConfig(projects_root=tmp_path, run_dir_roots=(tmp_path / "run_dir",))
    client = TestClient(create_app(allowed), raise_server_exceptions=False)
    ok = client.get(
        "/v1/projects/example_a/runs/example_run/metrics", params={"keys": "loss"}
    )
    assert ok.status_code == 200

    elsewhere = ServerConfig(
        projects_root=tmp_path, run_dir_roots=(tmp_path / "somewhere_else",)
    )
    client = TestClient(create_app(elsewhere), raise_server_exceptions=False)
    blocked = client.get(
        "/v1/projects/example_a/runs/example_run/metrics", params={"keys": "loss"}
    )
    assert blocked.status_code == 404
    assert blocked.json()["errors"][0]["code"] == "run.run_dir_missing"


def test_metrics_discovers_keys_appearing_mid_run(tmp_path: Path) -> None:
    import json as _json

    make_run_fixture(tmp_path, steps=[100])
    metrics_path = tmp_path / "run_dir" / "metrics.jsonl"
    rows = [
        {"event": "train_metrics", "step": 100, "loss": 9.0},
        {"event": "train_metrics", "step": 200, "loss": 8.0, "val_loss": 7.5},
    ]
    metrics_path.write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    client = make_client(tmp_path)
    data = client.get("/v1/projects/example_a/runs/example_run/metrics").json()["data"]
    assert data["series"]["val_loss"] == [None, 7.5]  # back-filled, not silently absent


# ----------------------------------------------------------------- long-poll + compare
def test_run_status_longpoll_returns_on_change_and_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    make_run_fixture(tmp_path, steps=[100], qc_done=[100])
    fake, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_control({"Running": True, "Status": "running"})
    client = make_client(tmp_path)

    # baseline mismatch -> returns immediately with changed=true
    resp = client.get(
        "/v1/projects/example_a/runs/example_run/status",
        params={"wait": "state_change", "timeout": 30, "from": "submitted"},
    )
    data = resp.json()["data"]
    assert data["changed"] is True
    assert data["baseline"] == "submitted"
    assert data["derived_status"] == "running"
    assert data["waited_sec"] == 0.0

    # stable status -> waits out the (tiny) timeout, changed=false
    resp = client.get(
        "/v1/projects/example_a/runs/example_run/status",
        params={"wait": "state_change", "timeout": 1},
    )
    data = resp.json()["data"]
    assert data["changed"] is False
    assert data["baseline"] == "running"
    assert data["waited_sec"] >= 1.0

    # unknown wait mode -> 422 envelope
    resp = client.get(
        "/v1/projects/example_a/runs/example_run/status",
        params={"wait": "bogus"},
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "run.wait_invalid"

    # typo'd baseline would silently degrade to busy-polling -> 422
    resp = client.get(
        "/v1/projects/example_a/runs/example_run/status",
        params={"wait": "state_change", "from": "runnign"},
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "run.from_invalid"


def test_runs_compare_diffs_flags_and_normalizes_names(
    tmp_path: Path, monkeypatch
) -> None:
    project = make_run_fixture(tmp_path, steps=[100, 200])
    fake, _ = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    def write_run(name: str, vgg: str, extra: list[str], verdict: str | None) -> None:
        record = {
            "schema_version": 1,
            "run_name": name,
            "experiment_id": "example_exp",
            "status": "completed",
            "submission": {
                "experiment_id": "example_exp",
                "container_id": "example_training",
                "bundle_id": "example_bundle_v1",
                "entrypoint": "train",
                "args": ["--vgg-weight", vgg, "--fresh", *extra],
                "env": {"RUN_TAG": name},
                "run_dir": f"/runs/{name}/run",
                # server-managed metadata DIFFERS between the runs, so the
                # no-noise assertions below actually bite
                "at": f"2026-07-02T00:00:0{vgg[0]}Z",
                "request_sha256": f"sha-{name}",
                "started_container_id": f"ctr-{name}",
            },
        }
        if verdict:
            record["verdict"] = verdict
        (project / "runs" / f"{name}.yaml").write_text(
            yaml.safe_dump(record), encoding="utf-8"
        )

    write_run("example_run_a", "50", [], "rejected")
    write_run("example_run_b", "10", ["--mask-fill-mean"], None)

    client = make_client(tmp_path)
    resp = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_b"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["runs"]["example_run_a"]["verdict"] == "rejected"
    diff = data["config_diff"]
    assert diff["args"]["--vgg-weight"] == {
        "example_run_a": ["50"],
        "example_run_b": ["10"],
    }
    assert diff["args"]["--mask-fill-mean"] == {
        "example_run_a": None,
        "example_run_b": [],
    }
    assert "--fresh" not in diff["args"]  # identical -> not a diff
    # run_dir differs only by the run's own name -> normalized away
    assert "run_dir" not in diff["submission"]
    # server-managed metadata (sha, timestamps) never shows up as a diff
    assert "request_sha256" not in diff["submission"]
    assert "at" not in diff["submission"]
    assert "started_container_id" not in diff["submission"]
    # env RUN_TAG embeds the run name -> also normalized away
    assert "RUN_TAG" not in diff["env"]

    # identical raw values embedding one run's name are NOT a phantom diff
    for name in ("example_run_a", "example_run_b"):
        rec = yaml.safe_load((project / "runs" / f"{name}.yaml").read_text())
        rec["submission"]["env"] = {"CKPT": "/ckpts/example_run_a/best.pt"}
        (project / "runs" / f"{name}.yaml").write_text(
            yaml.safe_dump(rec), encoding="utf-8"
        )
    diff2 = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_b"},
    ).json()["data"]["config_diff"]
    assert "CKPT" not in diff2["env"]

    # join-ambiguous argv values stay distinguishable as lists
    rec_a = yaml.safe_load((project / "runs" / "example_run_a.yaml").read_text())
    rec_a["submission"]["args"] = ["--extra", "foo bar"]
    (project / "runs" / "example_run_a.yaml").write_text(
        yaml.safe_dump(rec_a), encoding="utf-8"
    )
    rec_b = yaml.safe_load((project / "runs" / "example_run_b.yaml").read_text())
    rec_b["submission"]["args"] = ["--extra", "foo", "bar"]
    (project / "runs" / "example_run_b.yaml").write_text(
        yaml.safe_dump(rec_b), encoding="utf-8"
    )
    diff3 = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_b"},
    ).json()["data"]["config_diff"]
    assert diff3["args"]["--extra"] == {
        "example_run_a": ["foo bar"],
        "example_run_b": ["foo", "bar"],
    }

    # cosmetic own-name-only argv difference must NOT resurface via _raw
    rec_a = yaml.safe_load((project / "runs" / "example_run_a.yaml").read_text())
    rec_b = yaml.safe_load((project / "runs" / "example_run_b.yaml").read_text())
    rec_a["submission"]["args"] = ["--run-dir", "/runs/example_run_a"]
    rec_b["submission"]["args"] = ["--run-dir", "/runs/example_run_b"]
    (project / "runs" / "example_run_a.yaml").write_text(
        yaml.safe_dump(rec_a), encoding="utf-8"
    )
    (project / "runs" / "example_run_b.yaml").write_text(
        yaml.safe_dump(rec_b), encoding="utf-8"
    )
    cosmetic = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_b"},
    ).json()["data"]["config_diff"]
    assert cosmetic["args"] == {}

    # a lossy parse (repeated flag) with a REAL diff elsewhere still exposes _raw
    rec_a["submission"]["args"] = ["--vgg-weight", "50", "--lr", "1", "--lr", "2"]
    rec_b["submission"]["args"] = ["--vgg-weight", "10", "--lr", "1", "--lr", "3"]
    (project / "runs" / "example_run_a.yaml").write_text(
        yaml.safe_dump(rec_a), encoding="utf-8"
    )
    (project / "runs" / "example_run_b.yaml").write_text(
        yaml.safe_dump(rec_b), encoding="utf-8"
    )
    lossy = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_b"},
    ).json()["data"]["config_diff"]
    assert lossy["args"]["--vgg-weight"] == {
        "example_run_a": ["50"],
        "example_run_b": ["10"],
    }
    assert "_raw" in lossy["args"]  # repeated-flag diff not silently masked

    # differences the flag parse cannot represent fall back to raw argv
    rec_a["submission"]["args"] = ["--lr", "1", "--lr", "2"]
    rec_b["submission"]["args"] = ["--lr", "1", "--lr", "3"]
    (project / "runs" / "example_run_a.yaml").write_text(
        yaml.safe_dump(rec_a), encoding="utf-8"
    )
    (project / "runs" / "example_run_b.yaml").write_text(
        yaml.safe_dump(rec_b), encoding="utf-8"
    )
    diff4 = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_b"},
    ).json()["data"]["config_diff"]
    assert diff4["args"]["_raw"] == {
        "example_run_a": ["--lr", "1", "--lr", "2"],
        "example_run_b": ["--lr", "1", "--lr", "3"],
    }

    too_few = client.get(
        "/v1/projects/example_a/compare", params={"runs": "example_run_a"}
    )
    assert too_few.status_code == 422
    dup = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_a"},
    )
    assert dup.status_code == 422
    missing = client.get(
        "/v1/projects/example_a/compare",
        params={"runs": "example_run_a,example_run_zz"},
    )
    assert missing.status_code == 404


def test_run_status_longpoll_sees_record_rewrite_mid_poll(tmp_path: Path) -> None:
    """Status is derived per POLL: a record rewritten while the request is held
    (resubmit, conclusion of a crash window) must fire the state change."""
    import threading
    import time

    project = make_project(tmp_path, "example_a")
    run_path = project / "runs" / "example_run.yaml"
    run_path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "run_name": "example_run", "status": "submitted"}
        ),
        encoding="utf-8",
    )
    client = make_client(tmp_path)
    result: dict = {}

    def poll() -> None:
        result["resp"] = client.get(
            "/v1/projects/example_a/runs/example_run/status",
            params={"wait": "state_change", "timeout": 12},
        )

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(1.0)  # request is now held on its first 5s sleep
    run_path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "run_name": "example_run", "status": "completed"}
        ),
        encoding="utf-8",
    )
    thread.join(timeout=15)
    assert not thread.is_alive()
    data = result["resp"].json()["data"]
    assert data["changed"] is True
    assert data["baseline"] == "submitted"
    assert data["derived_status"] == "completed"
    assert data["waited_sec"] <= 6.0  # fired on the first recompute, not the timeout


def test_stopped_by_control_is_terminal_everywhere(tmp_path: Path, monkeypatch) -> None:
    """status AND metrics endpoints must agree a control-stopped run terminated."""
    project = make_run_fixture(tmp_path, steps=[100, 200], terminal="stopped_by_control")
    progress = project / "managed_runs" / "example_run.progress.json"
    progress.write_text(
        json.dumps({
            "run_id": "example_run", "qc_done_steps": [100, 200],
            "lifecycle_state": "done", "finalized": True,
            "seen_running": True, "ticks": 4,
        }),
        encoding="utf-8",
    )
    fake, _ = write_fake_docker(tmp_path)  # container gone
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    client = make_client(tmp_path)

    status = client.get("/v1/projects/example_a/runs/example_run/status").json()["data"]
    assert status["derived_status"] == "stopped"
    assert status["terminal_event"] == "stopped_by_control"

    metrics = client.get(
        "/v1/projects/example_a/runs/example_run/metrics?keys=loss"
    ).json()["data"]
    # was null pre-fix (metrics.py had its own stale TERMINAL_EVENTS copy)
    assert metrics["terminal_event"]["event"] == "stopped_by_control"
