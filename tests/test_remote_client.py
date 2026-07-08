"""Terse-output formatting of the `kikai remote` client (token-economy contract)."""
from __future__ import annotations

import json

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
