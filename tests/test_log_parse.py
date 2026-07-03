import json

from kikai_lab.log_parse import (
    decode_remote_logs,
    parse_jsonl_metrics,
    scan_markers,
    summarize_training_logs,
)


def _result(inner_logs: str) -> str:
    # mimic a remote_docker_logs op result: the logs ride a JSON-escaped field
    return json.dumps({"execution_status": "remote_docker_logs_completed", "logs": inner_logs})


def test_decode_remote_logs_roundtrips_escaped_field():
    inner = 'line one\n{"event":"train_metrics","step":1}\n"quoted"'
    assert decode_remote_logs(_result(inner)) == inner
    assert decode_remote_logs('{"execution_status":"x"}') == ""


def test_parse_jsonl_metrics_filters_to_marker_objects():
    logs = "\n".join([
        '{"event":"train_metrics","step":100,"loss":5.0}',
        "noise line",
        '{"event":"other","step":150}',
        '{"event":"train_metrics","step":200,"loss":4.0}',
        "{not json",
    ])
    rows = parse_jsonl_metrics(logs)
    assert [r["step"] for r in rows] == [100, 200]


def test_scan_markers_detects_error_and_ready():
    logs = "starting\nutt_done diagnostic posted\nall good"
    m = scan_markers(logs, ready_patterns=("utt_done",))
    assert m["ready"] is True and m["has_error"] is False
    err = scan_markers("Traceback (most recent call last):\nRuntimeError")
    assert err["has_error"] is True


def test_summarize_training_logs_end_to_end():
    inner = "\n".join([
        '{"event":"train_metrics","step":100,"loss":5.0}',
        '{"event":"train_metrics","step":200,"loss":4.0,"lips":0.02}',
        "training_ready posted",
    ])
    s = summarize_training_logs(_result(inner), ready_patterns=("training_ready",))
    assert s["last_step"] == 200
    assert s["last_metrics"]["lips"] == 0.02
    assert len(s["metrics"]) == 2
    assert s["ready"] is True
    assert s["has_error"] is False


def test_summarize_accepts_raw_logs_without_logs_field():
    raw = '{"event":"train_metrics","step":7,"loss":1.0}'
    s = summarize_training_logs(raw)
    assert s["last_step"] == 7
