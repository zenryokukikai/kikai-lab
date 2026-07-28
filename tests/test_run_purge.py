"""run 単位の purge: 何を消し、何を残し、いつ拒否し、何を記録するか。

purge は kikai が持つ唯一の破壊的ファイル操作なので、ここのテストがそのまま契約。
「daemon がまだ所有している run は消えない」「証跡は残る」「外向き symlink は
たどらない」「消した事実が台帳と journal に残る」の 4 点が守られている限り、生の
delete API を外から叩くより安全である、という主張が成立する。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from kikai_lab import remote_client as rc
from tests.test_server_projects import make_client, make_project
from tests.test_server_runs import make_run_fixture, write_fake_docker

PURGE_URL = "/v1/projects/example_a/runs/example_run/artifacts/purge"


def make_purge_fixture(tmp_path: Path, *, qc_done: list[int] | None = None) -> Path:
    """終端まで終わった managed run: checkpoints/qc が残り、QC も消化済み。"""
    project = make_run_fixture(tmp_path, terminal="done", qc_done=qc_done or [200, 300])
    run_dir = tmp_path / "run_dir"
    qc_dir = run_dir / "qc" / "step000200"
    qc_dir.mkdir(parents=True, exist_ok=True)
    (qc_dir / "preview.mp4").write_bytes(b"video" * 10)
    tb = run_dir / "tensorboard"
    tb.mkdir(parents=True, exist_ok=True)
    (tb / "events.out.tfevents.1").write_bytes(b"tb" * 5)
    (run_dir / "stray.log").write_text("noise\n", encoding="utf-8")
    progress = project / "managed_runs" / "example_run.progress.json"
    record = json.loads(progress.read_text(encoding="utf-8"))
    record["finalized"] = True
    record["terminal_status"] = "completed"
    progress.write_text(json.dumps(record), encoding="utf-8")
    return project


def ledger_rows(project: Path) -> list[dict]:
    path = project / "artifacts" / "example_run.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def journal_rows(project: Path) -> list[dict]:
    path = project / "journal.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ------------------------------------------------------------------- dry run
def test_dry_run_lists_targets_and_deletes_nothing(tmp_path):
    make_purge_fixture(tmp_path)
    run_dir = tmp_path / "run_dir"
    client = make_client(tmp_path)
    response = client.post(PURGE_URL, json={})  # dry_run defaults to true
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["dry_run"] is True
    targets = {e["path"]: e for e in data["would_delete"]}
    assert set(targets) == {"checkpoints", "qc", "stray.log"}
    assert targets["checkpoints"]["is_dir"] is True
    assert targets["checkpoints"]["file_count"] == 2
    assert targets["qc"]["bytes"] == len(b"video" * 10)
    assert data["total_bytes"] == sum(e["bytes"] for e in data["would_delete"])
    assert data["keep"] == ["metrics.jsonl", "tensorboard"]
    # nothing moved
    assert (run_dir / "checkpoints").is_dir()
    assert (run_dir / "stray.log").is_file()
    assert ledger_rows(tmp_path / "example_a") == []


# --------------------------------------------------------------- real purge
def test_purge_keeps_evidence_and_removes_the_rest(tmp_path):
    make_purge_fixture(tmp_path)
    run_dir = tmp_path / "run_dir"
    client = make_client(tmp_path)
    response = client.post(PURGE_URL, json={"dry_run": False})
    assert response.status_code == 200
    data = response.json()["data"]
    assert {e["path"] for e in data["deleted"]} == {"checkpoints", "qc", "stray.log"}
    assert data["total_bytes"] > 0
    assert data["failed"] == []
    assert (run_dir / "metrics.jsonl").is_file()
    assert (run_dir / "tensorboard" / "events.out.tfevents.1").is_file()
    assert not (run_dir / "checkpoints").exists()
    assert not (run_dir / "qc").exists()
    assert not (run_dir / "stray.log").exists()


def test_purge_honours_explicit_keep(tmp_path):
    make_purge_fixture(tmp_path)
    run_dir = tmp_path / "run_dir"
    client = make_client(tmp_path)
    response = client.post(PURGE_URL, json={"dry_run": False, "keep": ["checkpoints"]})
    assert response.status_code == 200
    assert (run_dir / "checkpoints").is_dir()
    assert not (run_dir / "qc").exists()
    assert not (run_dir / "metrics.jsonl").exists()  # keep is a REPLACEMENT, not an addition


def test_keep_rejects_paths_not_directly_under_the_run_dir(tmp_path):
    make_purge_fixture(tmp_path)
    client = make_client(tmp_path)
    for bad in ("qc/step000200", "../outside", ".."):
        response = client.post(PURGE_URL, json={"dry_run": False, "keep": [bad]})
        assert response.status_code == 422, bad
        assert response.json()["errors"][0]["code"] == "run.purge_keep_invalid"


# ------------------------------------------------------------------- records
def test_purge_appends_a_ledger_event_leaving_existing_rows_intact(tmp_path):
    project = make_purge_fixture(tmp_path)
    ledger = project / "artifacts" / "example_run.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "schema_version": 1,
        "artifact_id": "example_run_qc_step000200_preview.mp4",
        "run_name": "example_run",
        "kind": "qc_video",
    }
    ledger.write_text(json.dumps(existing, sort_keys=True) + "\n", encoding="utf-8")
    client = make_client(tmp_path)
    data = client.post(PURGE_URL, json={"dry_run": False}).json()["data"]

    rows = ledger_rows(project)
    assert len(rows) == 2
    assert rows[0] == existing  # append-only: the qc row is untouched
    purge_row = rows[1]
    assert purge_row["kind"] == "purge"
    assert purge_row["artifact_id"] == data["artifact_id"]
    assert purge_row["run_name"] == "example_run"
    assert set(purge_row["purged_paths"]) == {"checkpoints", "qc", "stray.log"}
    assert purge_row["purged_count"] == 3
    assert purge_row["total_bytes"] == data["total_bytes"]


def test_purge_is_written_to_the_journal(tmp_path):
    project = make_purge_fixture(tmp_path)
    client = make_client(tmp_path)
    data = client.post(PURGE_URL, json={"dry_run": False}).json()["data"]
    entries = [row for row in journal_rows(project) if row["kind"] == "run_artifacts_purged"]
    assert len(entries) == 1
    assert entries[0]["run_name"] == "example_run"
    assert entries[0]["purged_count"] == 3
    assert entries[0]["total_bytes"] == data["total_bytes"]
    assert entries[0]["keep"] == ["metrics.jsonl", "tensorboard"]


def test_second_purge_is_a_no_op_and_records_nothing(tmp_path):
    """空振りの purge は台帳にも journal にも行を足さない (再確認のたびに叩かれる)。"""
    project = make_purge_fixture(tmp_path)
    client = make_client(tmp_path)
    client.post(PURGE_URL, json={"dry_run": False})
    again = client.post(PURGE_URL, json={"dry_run": False})
    assert again.status_code == 200
    data = again.json()["data"]
    assert data["deleted"] == []
    assert data["total_bytes"] == 0
    assert data["artifact_id"] is None
    assert len([r for r in ledger_rows(project) if r["kind"] == "purge"]) == 1
    assert len([r for r in journal_rows(project) if r["kind"] == "run_artifacts_purged"]) == 1


def test_dry_run_writes_no_journal_entry(tmp_path):
    project = make_purge_fixture(tmp_path)
    client = make_client(tmp_path)
    client.post(PURGE_URL, json={})
    assert [r for r in journal_rows(project) if r["kind"] == "run_artifacts_purged"] == []


# ------------------------------------------------------------------- refusal
def test_running_run_is_refused(tmp_path, monkeypatch):
    make_purge_fixture(tmp_path)
    run_dir = tmp_path / "run_dir"
    fake, set_control = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))
    set_control({"Running": True, "Status": "running"})
    progress = tmp_path / "example_a" / "managed_runs" / "example_run.progress.json"
    record = json.loads(progress.read_text(encoding="utf-8"))
    record["finalized"] = False  # still live: the container decides
    record.pop("terminal_status", None)
    progress.write_text(json.dumps(record), encoding="utf-8")

    client = make_client(tmp_path)
    response = client.post(PURGE_URL, json={"dry_run": False})
    assert response.status_code == 409
    err = response.json()["errors"][0]
    assert err["code"] == "run.purge_active_refused"
    assert err["details"]["derived_status"] == "running"
    assert (run_dir / "checkpoints").is_dir()  # a refusal deletes nothing


def test_run_with_pending_qc_is_refused(tmp_path):
    """retention が守る checkpoint を purge が消してはならない: 判定は
    reconcile.pending_qc_steps を共用しているので、両者は定義上ずれない。"""
    from kikai_lab.reconcile import pending_qc_steps
    from kikai_lab.server.runs import load_managed_run_optional

    project = make_purge_fixture(tmp_path, qc_done=[200])  # step 300 の QC が未消化
    managed_path = project / "managed_runs" / "example_run.yaml"
    managed_path.write_text(
        managed_path.read_text(encoding="utf-8") + "qc_op:\n  kind: kikai_operation\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run_dir"
    managed = load_managed_run_optional(project, "example_run")
    progress = json.loads(
        (project / "managed_runs" / "example_run.progress.json").read_text(encoding="utf-8")
    )
    assert pending_qc_steps(managed, run_dir, progress) == [300]

    client = make_client(tmp_path)
    response = client.post(PURGE_URL, json={"dry_run": False})
    assert response.status_code == 409
    err = response.json()["errors"][0]
    assert err["code"] == "run.purge_qc_pending_refused"
    assert err["details"]["pending_qc_steps"] == [300]
    assert (run_dir / "checkpoints").is_dir()


def test_unregistered_run_is_404(tmp_path):
    make_project(tmp_path, "example_a")  # run record exists, no run_dir
    client = make_client(tmp_path)
    response = client.post(PURGE_URL, json={})
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "run.run_dir_missing"


# ------------------------------------------------------------------- sandbox
def test_outward_symlink_is_skipped_not_followed(tmp_path):
    make_purge_fixture(tmp_path)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    run_dir = tmp_path / "run_dir"
    os.symlink(outside, run_dir / "linked_dir")
    os.symlink(outside / "precious.txt", run_dir / "linked_file")

    client = make_client(tmp_path)
    data = client.post(PURGE_URL, json={"dry_run": False}).json()["data"]
    skipped = {s["path"]: s["reason"] for s in data["skipped"]}
    assert set(skipped) == {"linked_dir", "linked_file"}
    assert set(skipped.values()) == {"run.artifact_path_forbidden"}
    assert {e["path"] for e in data["deleted"]} == {"checkpoints", "qc", "stray.log"}
    # the links themselves survive, and so does everything they point at
    assert (run_dir / "linked_dir").is_symlink()
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"


def test_inward_symlink_is_unlinked_without_touching_its_target(tmp_path):
    """run_dir の中を指す symlink は消してよい: 消えるのは link であって実体ではない。"""
    make_purge_fixture(tmp_path)
    run_dir = tmp_path / "run_dir"
    os.symlink(run_dir / "metrics.jsonl", run_dir / "latest_metrics")
    client = make_client(tmp_path)
    data = client.post(PURGE_URL, json={"dry_run": False}).json()["data"]
    assert "latest_metrics" in {e["path"] for e in data["deleted"]}
    assert not (run_dir / "latest_metrics").exists()
    assert (run_dir / "metrics.jsonl").is_file()


# ----------------------------------------------------------------------- CLI
def test_cli_defaults_to_dry_run(monkeypatch, capsys):
    sent: dict = {}

    def fake_http(method, url, body=None, **kwargs):
        sent.update({"method": method, "url": url, "body": body})
        return {
            "ok": True,
            "data": {
                "dry_run": True,
                "keep": ["metrics.jsonl", "tensorboard"],
                "would_delete": [{"path": "checkpoints", "bytes": 4096, "is_dir": True}],
                "total_bytes": 4096,
                "skipped": [],
            },
        }

    monkeypatch.setattr(rc, "_http", fake_http)
    code = rc.command_remote(["--base-url", "http://x", "purge", "proj", "r"])
    out = capsys.readouterr().out
    assert code == 0
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/projects/proj/runs/r/artifacts/purge")
    assert sent["body"] == {"dry_run": True}
    assert "d           4096 checkpoints" in out
    assert "would_delete=1 total_bytes=4096" in out
    assert "--yes" in out  # the next step is stated, not guessed


def test_cli_yes_executes_and_keep_is_forwarded(monkeypatch, capsys):
    sent: dict = {}

    def fake_http(method, url, body=None, **kwargs):
        sent.update({"body": body})
        return {
            "ok": True,
            "data": {
                "dry_run": False,
                "keep": ["metrics.jsonl"],
                "deleted": [{"path": "qc", "bytes": 50, "is_dir": True}],
                "total_bytes": 50,
                "skipped": [{"path": "linked", "reason": "run.artifact_path_forbidden"}],
                "failed": [],
                "artifact_id": "r_purge_20260101T000000Z",
            },
        }

    monkeypatch.setattr(rc, "_http", fake_http)
    code = rc.command_remote(
        ["--base-url", "http://x", "purge", "proj", "r", "--yes", "--keep", "metrics.jsonl"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert sent["body"] == {"dry_run": False, "keep": ["metrics.jsonl"]}
    assert "deleted=1 total_bytes=50 kept=metrics.jsonl" in out
    assert "dry-run" not in out
    assert "linked (run.artifact_path_forbidden)" in out  # survivors are never silent


def test_cli_reports_a_refusal(monkeypatch, capsys):
    env = {
        "ok": False,
        "errors": [
            {
                "code": "run.purge_active_refused",
                "message": "run is still owned by the daemon (derived_status=running)",
            }
        ],
    }
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    code = rc.command_remote(["--base-url", "http://x", "purge", "proj", "r", "--yes"])
    out = capsys.readouterr().out
    assert code == 1
    assert "ERR run.purge_active_refused" in out
