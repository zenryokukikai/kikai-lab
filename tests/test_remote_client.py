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
    assert "delivery=1/2 delivered" in out  # scale before samples
    assert "skipped=1" in out and "failed=0" in out  # a skip is not a failure
    assert "delivery_failures:" in out and "no_webhook" in out
    assert "qc:1000" not in out  # a delivered post is not a failure


def test_run_digest_delivery_scale_beats_the_five_row_tail(monkeypatch, capsys):
    """60 unconfirmed QC posts must not read like 5 (the 2026-07-25 incident)."""
    env = {"ok": True, "data": {
        "derived_status": "completed", "container": {"running": False},
        "latest_metrics": {"step": 60000, "loss": 1.0},
        "progress": {"qc_done_steps": list(range(1000, 61000, 1000)),
                     "delivery": {
                         f"qc:{s}": {"status": None,
                                     "outcome": "no_delivery_event",
                                     "skipped_reason": "no_delivery_event"}
                         for s in range(1000, 61000, 1000)}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    out = capsys.readouterr().out
    assert "delivery=0/60 delivered" in out
    assert "unverified=60" in out and "failed=0" in out  # unverified != failed
    assert "no_delivery_event:60" in out


def test_run_digest_prints_delivery_line_when_nothing_was_recorded(monkeypatch, capsys):
    """total==0 with 60 QC steps is the incident, not a healthy run: the line
    must be printed (it used to be suppressed on a falsy total)."""
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 60000, "loss": 1.0},
        "progress": {"qc_done_steps": list(range(1000, 61000, 1000)),
                     "probes_done_steps": {"preview": [1000, 2000]}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    out = capsys.readouterr().out
    assert "delivery=0/62 delivered" in out
    assert "unrecorded=62" in out


def test_run_digest_delivery_line_is_capped_like_its_siblings(monkeypatch, capsys):
    """The reasons blob is bounded: fixed-vocabulary keys, then a hard cap."""
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 60000, "loss": 1.0},
        "progress": {"qc_done_steps": list(range(1000, 61000, 1000)),
                     "delivery": {
                         f"qc:{s}": {"status": None, "outcome": "post_failed",
                                     "skipped_reason": f"post_failed:boom {'x' * 200}{s}"}
                         for s in range(1000, 61000, 1000)}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("delivery=")
    )
    assert "post_failed:60" in line and len(line) < 200


def test_run_digest_omits_delivery_line_without_qc_activity(monkeypatch, capsys):
    """No QC, no probes, no records — nothing to say (the only silent case)."""
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 100, "loss": 1.0}, "progress": {}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    assert "delivery=" not in capsys.readouterr().out


def _delivery_line(out: str, prefix: str = "delivery=") -> str:
    return next(ln for ln in out.splitlines() if ln.startswith(prefix))


def test_delivery_reasons_are_deterministic_and_marked_when_cut():
    """Which buckets survive the cut must not depend on dict insertion order.

    Sorted by count DESC then key: two reads of the same run print the same
    line, and a cut drops the SMALLEST buckets — and says it cut (an unmarked
    truncation reads as a complete message, the rule `clip_text` exists for)."""
    reasons = {"http_500": 1, "post_failed": 9, "skipped": 9, "http_404": 3}
    text = rc._delivery_reasons_text(reasons)
    assert text == "post_failed:9, skipped:9, http_404:3, http_500:1"
    # same content, different insertion order -> byte-identical line
    assert rc._delivery_reasons_text(dict(reversed(list(reasons.items())))) == text
    # over budget: marked, capped, and the big buckets are the survivors
    many = {f"http_{code}": 600 - code for code in range(400, 460)}
    cut = rc._delivery_reasons_text(many)
    assert len(cut) == rc.DELIVERY_REASONS_MAX and cut.endswith("…")
    assert cut.startswith("http_400:200, http_401:199")
    assert "http_459" not in cut  # smallest bucket, dropped
    assert rc._delivery_reasons_text({}) == "-"


def test_delivery_failure_tail_is_compact_and_never_cut_mid_row(monkeypatch, capsys):
    """The tail's 300-char budget must buy ROWS, not JSON punctuation.

    Regression: adding the `outcome` key to each row pushed the visible rows of
    `json.dumps(bad)[:300]` from 4 to 3 (1-2 for `post_failed` rows), and the
    cut landed mid-object — unparseable and unmarked."""
    reason = "post_failed:HTTPSConnectionPool(host='example.invalid', port=443): timeout"
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 5000, "loss": 1.0},
        "progress": {"qc_done_steps": [1000, 2000, 3000, 4000, 5000],
                     "delivery": {
                         "qc:1000": {"status": 500, "outcome": "http_error"},
                         "qc:2000": {"status": None, "outcome": "skipped",
                                     "skipped_reason": "no_webhook"},
                         "qc:3000": {"status": None, "outcome": "no_delivery_event",
                                     "skipped_reason": "no_delivery_event"},
                         "probe:preview:3000": {"status": 403, "outcome": "http_error"},
                         "qc:4000": {"status": None, "outcome": "post_failed",
                                     "skipped_reason": reason}}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    line = _delivery_line(capsys.readouterr().out, "delivery_failures:")
    tail = line.split(": ", 1)[1]
    assert len(tail) <= rc.DELIVERY_TAIL_MAX
    assert "{" not in tail and '"' not in tail  # not JSON, so nothing to cut mid-object
    # all five rows fit, each as key=outcome(status):detail
    assert tail.split(" ")[:4] == [
        "qc:1000=http_error(500)",
        "qc:2000=skipped:no_webhook",
        "qc:3000=no_delivery_event",     # free text repeats the outcome -> printed once
        "probe:preview:3000=http_error(403)",
    ]
    assert not tail.startswith("…")  # nothing dropped: all five rows fit
    assert all(f"{k}=" in tail for k in
               ("qc:1000", "qc:2000", "qc:3000", "probe:preview:3000", "qc:4000"))
    assert "qc:4000=post_failed:HTTPSConnectionPool" in tail  # detail kept, prefix dropped


def test_delivery_failure_tail_drops_whole_rows_with_a_count(monkeypatch, capsys):
    """When the budget does bind, the drop is stated and lands on a row boundary."""
    reason = "post_failed:" + "e" * 300
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 5000, "loss": 1.0},
        "progress": {"qc_done_steps": list(range(1000, 6000, 1000)),
                     "delivery": {
                         f"qc:{s}": {"status": None, "outcome": "post_failed",
                                     "skipped_reason": reason}
                         for s in range(1000, 6000, 1000)}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    tail = _delivery_line(capsys.readouterr().out, "delivery_failures:").split(": ", 1)[1]
    assert len(tail) <= rc.DELIVERY_TAIL_MAX
    assert tail.startswith("…(+1 older) ")          # marked, and says how many
    assert tail.endswith("…")                        # per-row detail cut is marked too
    assert tail.count("qc:") == 4                    # 4 of 5 rows, vs 1-2 as JSON
    assert "qc:5000=post_failed:eee" in tail         # the newest row is kept
    # every kept row is whole: it starts with a key and carries its outcome
    for row in tail.split(" ")[2:]:  # [0:2] is the "…(+1 older)" marker
        assert row.startswith("qc:") and "=post_failed:" in row


def test_delivery_tail_row_classifies_legacy_records():
    """A pre-`outcome` record still renders with an explicit outcome."""
    assert rc._delivery_tail_row(
        {"key": "qc:1000", "status": None, "skipped_reason": "no_webhook"}
    ) == "qc:1000=skipped:no_webhook"
    assert rc._delivery_tail_row({"key": "qc:2000", "status": 404}) == "qc:2000=http_error(404)"


def test_delivery_tail_row_keeps_reasons_that_start_with_the_outcome_word():
    """The outcome prefix is dropped only on a SEPARATOR, never mid-word.

    Regression: a bare `startswith` cut turned a script's own
    `skipped_by_config` into `skipped:_by_config` and `no_delivery_eventual`
    into `no_delivery_event:ual` — mangled text in the field whose whole job is
    to be read literally."""
    def row(outcome: str, reason: str) -> str:
        return rc._delivery_tail_row(
            {"key": "qc:1000", "outcome": outcome, "skipped_reason": reason}
        )

    assert row("skipped", "skipped_by_config") == "qc:1000=skipped:skipped_by_config"
    assert row("no_delivery_event", "no_delivery_eventual") == (
        "qc:1000=no_delivery_event:no_delivery_eventual"
    )
    # the cases the stripping exists for still strip
    assert row("post_failed", "post_failed:boom") == "qc:1000=post_failed:boom"
    assert row("post_failed", "post_failed boom") == "qc:1000=post_failed:boom"
    assert row("no_delivery_event", "no_delivery_event") == "qc:1000=no_delivery_event"


def test_delivery_tail_marker_is_charged_against_the_budget():
    """`…(+N older)` is content: it must be paid for, not prepended for free.

    Regression: the marker was added AFTER the accounting loop, so a tail
    documented as <=300 chars measured 313."""
    rows = [
        {"key": f"qc:{step}", "outcome": "http_error", "status": 500}
        for step in range(1000, 20000, 1000)
    ]
    text = rc._delivery_tail_text(rows)
    assert len(text) <= rc.DELIVERY_TAIL_MAX
    assert text.startswith("…(+")
    # the marker's own cost bought fewer rows, and the count still matches
    dropped = int(text.split("(+", 1)[1].split(" ", 1)[0])
    assert dropped == len(rows) - text.count("qc:")
    assert rc._delivery_tail_text([]) == ""


def test_delivery_tail_keeps_one_clipped_row_when_a_row_exceeds_the_budget():
    """A single oversized row is CLIPPED, never dropped to nothing.

    Regression: one >300-char row rendered as `…(+1 older)` — a line that
    announces failures and then says nothing about any of them."""
    rows = [
        {"key": "probe:" + "n" * 400 + ":1000", "outcome": "post_failed"},
        {"key": "probe:" + "m" * 400 + ":2000", "outcome": "post_failed"},
    ]
    text = rc._delivery_tail_text(rows)
    assert len(text) <= rc.DELIVERY_TAIL_MAX
    assert text.startswith("…(+1 older) ")
    assert text.endswith("…")                      # the clip is marked
    assert "probe:mmm" in text                     # ...and the NEWEST row is what survives
    # a lone oversized row has nothing older to report, so no marker at all
    solo = rc._delivery_tail_text(rows[:1])
    assert len(solo) <= rc.DELIVERY_TAIL_MAX
    assert solo.startswith("probe:nnn") and solo.endswith("…")


@pytest.mark.parametrize("progress", [
    {"probes_done_steps": ["preview:1000"]},        # list where a map is expected
    {"probes_done_steps": "preview"},               # string
    {"probes_done_steps": {"preview": None}},       # steps not a list
    {"probes_done_steps": {"preview": 5}},
    {"qc_done_steps": 1000},                        # scalar
    {"qc_done_steps": {"1000": True}},
    {"op_fail_counts": ["qc:1000"]},                # list where a map is expected
    {"op_fail_counts": "qc:1000"},
    {"delivery": ["qc:1000"]},
    {"delivery": "qc:1000"},
    "garbage",                                      # the whole payload is wrong
    42,
    None,
])
def test_run_digest_survives_a_corrupt_progress_json(monkeypatch, capsys, progress):
    """`kikai remote run` is the command an operator runs BECAUSE a run looks
    wrong — it must never be the thing that dies on the wrong-looking payload.

    Regression: `delivery_summary` was hardened but the three lines printed
    BEFORE it still indexed the raw fields, so the exact shapes the corrupt-
    payload test declares safe for `/status` still killed the CLI with
    `AttributeError: 'list' object has no attribute 'items'`."""
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 5000, "loss": 1.0},
        "progress": progress}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    assert rc.command_remote(["--base-url", "http://x", "run", "proj", "r"]) == 0
    out = capsys.readouterr().out
    # degraded to "no information", not to a traceback: the run's own status
    # (the reason the operator called) is still printed
    assert "status=running" in out and "step=5000" in out
    assert "qc_done=" in out and "probes={" in out and "fails={" in out


def test_run_digest_survives_a_corrupt_envelope(monkeypatch, capsys):
    """Same contract one level up: a non-mapping data/container/metrics block."""
    monkeypatch.setattr(rc, "_http", lambda *a, **k: {"ok": True, "data": "nope"})
    assert rc.command_remote(["--base-url", "http://x", "run", "proj", "r"]) == 0
    assert "status=None" in capsys.readouterr().out
    env = {"ok": True, "data": {"derived_status": "running", "container": [],
                                "latest_metrics": "x", "progress": {}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    assert rc.command_remote(["--base-url", "http://x", "run", "proj", "r"]) == 0
    assert "step=- loss=-" in capsys.readouterr().out


def test_run_digest_counts_steps_like_the_delivery_denominator(monkeypatch, capsys):
    """`qc_done` + `probes` and `delivery=/N` are computed by the SAME helper,
    so the digest can never contradict itself (duplicates, bools, junk rows)."""
    env = {"ok": True, "data": {
        "derived_status": "running", "container": {"running": True},
        "latest_metrics": {"step": 3000, "loss": 1.0},
        "progress": {"qc_done_steps": [1000, 2000, 2000, True, "3000"],
                     "probes_done_steps": {"preview": [1000, 1000], "bad": "x"}}}}
    monkeypatch.setattr(rc, "_http", lambda *a, **k: env)
    rc.command_remote(["--base-url", "http://x", "run", "proj", "r"])
    out = capsys.readouterr().out
    assert "qc_done=2" in out and "preview:1" in out and "bad:0" in out
    assert "delivery=0/3 delivered" in out  # 2 qc + 1 probe, the same arithmetic


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
