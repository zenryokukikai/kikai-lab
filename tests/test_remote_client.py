"""Terse-output formatting of the `kikai remote` client (token-economy contract)."""
from __future__ import annotations

import io
import json
import tarfile

import pytest

from kikai_lab import remote_client as rc


def test_daemon_one_liner(monkeypatch, capsys):
    env = {"ok": True, "data": {"seconds_since_update": 12.4, "state": {
        "phase": "tick", "current_run_id": "run_x",
        "last_pass": {"managed_runs": 3, "errors": []}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "daemon", "proj"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "phase=tick" in out[0] and "current=run_x" in out[0] and "errors=0" in out[0]


def test_op_extracts_events_and_errors(monkeypatch, capsys, tmp_path):
    env = {"ok": False,
           "data": {"result": {"execution_status": "docker_run_failed",
                               "stdout": '{"event": "tts_noise_pad", "seconds": 0.5}\nnoise\n'}},
           "errors": [{"code": "operation.docker_run_failed",
                       "details": {"stderr": "Traceback...\nUnboundLocalError: wspeech_np"}}]}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    f = tmp_path / "op.json"
    f.write_text(json.dumps({"adapter": "script_bundle_run"}))
    code = rc.command_remote(["--base-url", "http://x", "op", "proj", "--file", str(f)])
    out = capsys.readouterr().out
    assert code == 1
    assert "execution=docker_run_failed" in out
    assert "tts_noise_pad" in out                     # script events surfaced
    assert "UnboundLocalError: wspeech_np" in out     # stderr tail surfaced


def test_run_digest_shows_giveups(monkeypatch, capsys):
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 21000, "loss": 5.1234},
        "progress": {"qc_done_steps": [1000, 2000],
                     "probes_done_steps": {"p1": [1000]},
                     "op_gave_up": ["probe:p1:3000"],
                     "last_error": "probe p1@3000: x"}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    out = capsys.readouterr().out
    assert "step=21000" in out and "loss=5.123" in out
    assert "qc_done=2" in out and "p1:1" in out and "probe:p1:3000" in out


def test_run_digest_shows_fails_and_delivery_failures(monkeypatch, capsys):
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 5000, "loss": 1.0},
        "progress": {"qc_done_steps": [1000],
                     "op_fail_counts": {"qc:2000": 2},
                     "delivery": {
                         "qc:1000": {"status": 200},
                         "probe:preview:1000": {"status": None,
                                                "skipped_reason": "no_webhook"}}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    out = capsys.readouterr().out
    assert "fails={qc:2000:2}" in out
    assert "delivery_failures:" in out and "no_webhook" in out
    assert "qc:1000" not in out  # a delivered post is not a failure


# ------------------------------------------------------------------- artifacts

def test_artifacts_listing_terse_output(monkeypatch, capsys):
    env = {"ok": True, "data": {
        "entries": [
            {"path": "checkpoints", "is_dir": True, "size": None, "mtime": 1.0},
            {"path": "metrics.jsonl", "is_dir": False, "size": 2048, "mtime": 2.0}],
        "total": 2, "truncated": False}}
    calls = _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "artifacts", "proj", "r", "--path", "qc", "--depth", "2"]
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert calls["url"] == "http://x/v1/projects/proj/runs/r/artifacts?path=qc&depth=2"
    assert out[0].startswith("d") and out[0].endswith("checkpoints")
    assert "2048" in out[1] and out[1].endswith("metrics.jsonl")
    assert out[2] == "total=2"


def test_artifacts_file_prints_content(monkeypatch, capsys):
    env = {"ok": True, "data": {
        "binary": False, "truncated": False, "size": 12, "content": '{"step": 1}\n'}}
    calls = _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "artifacts", "proj", "r",
         "--file", "qc/summary.json"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out == '{"step": 1}\n'
    assert calls["url"] == (
        "http://x/v1/projects/proj/runs/r/artifacts/file"
        "?path=qc%2Fsummary.json&max_bytes=65536&tail=false"
    )


def test_artifacts_file_binary_and_truncated_notes(monkeypatch, capsys):
    env = {"ok": True, "data": {"binary": True, "size": 999, "content": None}}
    _capture_http(monkeypatch, env)
    rc.command_remote(
        ["--base-url", "http://x", "artifacts", "proj", "r", "--file", "a.mp4"]
    )
    assert "binary size=999" in capsys.readouterr().out

    env = {"ok": True, "data": {
        "binary": False, "truncated": True, "tail": True, "size": 10_000_000,
        "content": "tail text"}}
    _capture_http(monkeypatch, env)
    rc.command_remote(
        ["--base-url", "http://x", "artifacts", "proj", "r",
         "--file", "metrics.jsonl", "--tail", "--max-bytes", "4096"]
    )
    out = capsys.readouterr().out
    assert "# truncated: last 4096 of 10000000 bytes" in out and "tail text" in out


def test_artifacts_sandbox_error_surfaces(monkeypatch, capsys):
    env = {"ok": False, "data": {},
           "errors": [{"code": "run.artifact_path_forbidden",
                       "message": "path escapes the run_dir"}]}
    _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "artifacts", "proj", "r", "--file", "../x"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "ERR run.artifact_path_forbidden" in out


# ------------------------------------------------------------------ bundle-put

def _bundle_dir(tmp_path, with_manifest=True):
    d = tmp_path / "bundle_src"
    (d / "scripts").mkdir(parents=True)
    if with_manifest:
        (d / "kikai_bundle.json").write_text(
            json.dumps({"entrypoints": {"train": {"argv": ["python", "scripts/train.py"]}}})
        )
    (d / "scripts" / "train.py").write_text("print('train')\n")
    (d / "scripts" / "util.py").write_text("X = 1\n")
    # macOS junk that a hand-rolled `tar` smuggles in — must NOT reach the wire
    (d / "scripts" / "._train.py").write_bytes(b"\x00\x05\x16\x07AppleDouble")
    (d / ".DS_Store").write_bytes(b"junk")
    (d / "__MACOSX").mkdir()
    (d / "__MACOSX" / "._scripts").write_bytes(b"junk")
    return d


def _capture_http(monkeypatch, env):
    calls: dict = {}

    def fake(method, url, body=None, timeout=600, **kw):
        calls.update(method=method, url=url, body=body, timeout=timeout, **kw)
        return env

    monkeypatch.setattr(rc, "_http", fake)
    return calls


def test_bundle_put_tars_dir_and_excludes_appledouble(monkeypatch, capsys, tmp_path):
    d = _bundle_dir(tmp_path)
    env = {"ok": True, "data": {
        "bundle_id": "b1", "created": True, "file_count": 2,
        "entrypoints": {"train": {"argv": ["script_bundles/b1/root/scripts/train.py"]}}}}
    calls = _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "bundle-put", "proj", "b1", "--dir", str(d)]
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert out == ["ok=True created=True files=2 entrypoints=train"]
    assert calls["method"] == "PUT"
    assert calls["url"] == "http://x/v1/projects/proj/bundles/b1"
    assert calls["content_type"] == "application/x-tar"
    with tarfile.open(fileobj=io.BytesIO(calls["raw"]), mode="r:*") as tar:
        names = sorted(tar.getnames())
    assert names == ["kikai_bundle.json", "scripts/train.py", "scripts/util.py"]


def test_bundle_put_tar_is_accepted_by_server_extractor(tmp_path):
    """The CLI-built tar must pass the server's fail-closed extraction + manifest
    read (the exact path a curl upload goes through)."""
    from kikai_lab.server.bundles import read_upload_manifest, safe_extract_tar

    d = _bundle_dir(tmp_path)
    body, n_files = rc._build_bundle_tar(d)
    assert n_files == 3  # manifest + 2 scripts; junk excluded
    dest = tmp_path / "extracted"
    dest.mkdir()
    safe_extract_tar(body, dest)
    entrypoints = read_upload_manifest(dest)
    assert entrypoints == {"train": ["python", "scripts/train.py"]}
    assert (dest / "scripts" / "train.py").is_file()
    assert not list(dest.rglob("._*")) and not (dest / ".DS_Store").exists()


def test_bundle_put_requires_manifest(monkeypatch, tmp_path):
    d = _bundle_dir(tmp_path, with_manifest=False)
    monkeypatch.setattr(
        rc, "_http", lambda *a, **k: pytest.fail("must not reach the server")
    )
    with pytest.raises(SystemExit, match="kikai_bundle.json"):
        rc.command_remote(
            ["--base-url", "http://x", "bundle-put", "proj", "b1", "--dir", str(d)]
        )


def test_bundle_put_missing_dir_is_input_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rc, "_http", lambda *a, **k: pytest.fail("must not reach the server")
    )
    with pytest.raises(SystemExit, match="not a directory"):
        rc.command_remote(
            ["--base-url", "http://x", "bundle-put", "proj", "b1",
             "--dir", str(tmp_path / "nope")]
        )


def test_bundle_put_server_conflict_surfaces_error(monkeypatch, capsys, tmp_path):
    d = _bundle_dir(tmp_path)
    env = {"ok": False, "data": {},
           "errors": [{"code": "script_bundle.create_bundle_exists",
                       "message": "bundle exists with different content"}]}
    _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "bundle-put", "proj", "b1", "--dir", str(d)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "ok=False" in out
    assert "ERR script_bundle.create_bundle_exists" in out


# --------------------------------------------------------------- container-put

def test_container_put_yaml_outcome_line(monkeypatch, capsys, tmp_path):
    f = tmp_path / "container.yaml"
    f.write_text(
        "docker:\n  name: example-training\n  image: example:latest\n"
        "mounts:\n- source: env:HOST_RUNS_ROOT\n  target: env:CONTAINER_RUNS_ROOT\n"
        "  mode: rw\n"
    )
    env = {"ok": True, "data": {"container_id": "c1", "updated": True}}
    calls = _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "container-put", "proj", "c1", "--file", str(f)]
    )
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "ok=True outcome=updated"
    assert calls["method"] == "PUT"
    assert calls["url"] == "http://x/v1/projects/proj/containers/c1"
    assert calls["body"]["docker"]["image"] == "example:latest"


def test_container_put_rejects_non_object_file(monkeypatch, tmp_path):
    f = tmp_path / "container.json"
    f.write_text('["not", "an", "object"]')
    monkeypatch.setattr(
        rc, "_http", lambda *a, **k: pytest.fail("must not reach the server")
    )
    with pytest.raises(SystemExit, match="object"):
        rc.command_remote(
            ["--base-url", "http://x", "container-put", "proj", "c1", "--file", str(f)]
        )


def test_container_put_server_error(monkeypatch, capsys, tmp_path):
    f = tmp_path / "container.json"
    f.write_text(json.dumps({"docker": {"name": "x", "image": "y"}}))
    env = {"ok": False, "data": {},
           "errors": [{"code": "container.mount_forbidden",
                       "message": "no live-worktree mounts"}]}
    _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "container-put", "proj", "c1", "--file", str(f)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "ERR container.mount_forbidden" in out


# ------------------------------------------------------------------- qc-config

def test_qc_config_updated_and_warnings(monkeypatch, capsys, tmp_path):
    f = tmp_path / "qc.json"
    f.write_text(json.dumps({"probes": [{"id": "preview"}]}))
    env = {"ok": True,
           "data": {"run_name": "r", "updated": ["probes"], "removed": []},
           "warnings": [{"code": "run.qc_config_probe_backfill"}]}
    calls = _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "qc-config", "proj", "r", "--file", str(f)]
    )
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "ok=True updated=probes warnings=run.qc_config_probe_backfill"
    assert calls["method"] == "POST"
    assert calls["url"] == "http://x/v1/projects/proj/runs/r/qc-config"
    assert calls["body"] == {"probes": [{"id": "preview"}]}


def test_qc_config_removal_and_no_warnings(monkeypatch, capsys, tmp_path):
    f = tmp_path / "qc.json"
    f.write_text(json.dumps({"qc_op": None}))
    env = {"ok": True, "data": {"run_name": "r", "updated": [], "removed": ["qc_op"]}}
    _capture_http(monkeypatch, env)
    rc.command_remote(
        ["--base-url", "http://x", "qc-config", "proj", "r", "--file", str(f)]
    )
    out = capsys.readouterr().out.strip()
    assert out == "ok=True updated=- removed=qc_op warnings=-"


def test_qc_config_server_rejects_unknown_key(monkeypatch, capsys, tmp_path):
    f = tmp_path / "qc.json"
    f.write_text(json.dumps({"bogus": 1}))
    env = {"ok": False, "data": {},
           "errors": [{"code": "run.qc_config_invalid",
                       "message": "unknown qc-config keys (whitelist: probes, qc_op)"}]}
    _capture_http(monkeypatch, env)
    code = rc.command_remote(
        ["--base-url", "http://x", "qc-config", "proj", "r", "--file", str(f)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "ERR run.qc_config_invalid" in out


def test_qc_config_bad_json_file(monkeypatch, tmp_path):
    f = tmp_path / "qc.json"
    f.write_text("{not json")
    monkeypatch.setattr(
        rc, "_http", lambda *a, **k: pytest.fail("must not reach the server")
    )
    with pytest.raises(SystemExit, match="not parseable JSON"):
        rc.command_remote(
            ["--base-url", "http://x", "qc-config", "proj", "r", "--file", str(f)]
        )
