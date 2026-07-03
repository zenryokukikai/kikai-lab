"""Parse remote_docker_logs results without re-writing regex in every monitor loop.

An operator polling a detached training container otherwise hand-writes, every run:
  * a regex to pull the JSON-escaped `logs` field out of the op result,
  * `unicode_escape` decoding,
  * a JSONL scan for `train_metrics` rows + the last step,
  * substring scans for "diagnostic posted" / OOM / traceback.

These pure helpers absorb that. They take either a raw remote_docker_logs op-result
string (with a `"logs": "..."` field) or already-decoded log text.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

_LOGS_FIELD = re.compile(r'"logs"\s*:\s*"((?:[^"\\]|\\.)*)"')

DEFAULT_ERROR_PATTERNS: tuple[str, ...] = (
    "out of memory",
    "Traceback",
    "CUDA error",
    "error:",
)


def decode_remote_logs(op_result_text: str) -> str:
    """Pull the `logs` string out of a remote_docker_logs op result (a JSON-escaped
    field) and return the decoded text. Returns "" if no `logs` field is present."""
    match = _LOGS_FIELD.search(op_result_text)
    if not match or not match.group(1):
        return ""
    return match.group(1).encode().decode("unicode_escape")


def parse_jsonl_metrics(logs: str, *, contains: str = "train_metrics") -> list[dict[str, Any]]:
    """Every line of `logs` that is a JSON object and contains the marker `contains`."""
    rows: list[dict[str, Any]] = []
    for line in logs.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or contains not in stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def scan_markers(
    logs: str,
    *,
    error_patterns: Iterable[str] = DEFAULT_ERROR_PATTERNS,
    ready_patterns: Iterable[str] = (),
) -> dict[str, Any]:
    """Substring-scan for error and (optional) ready markers. Returns
    {has_error, error_lines, ready, ready_lines}."""
    error_list = list(error_patterns)
    ready_list = list(ready_patterns)
    error_lines = [ln.strip() for ln in logs.splitlines() if any(p in ln for p in error_list)]
    ready_lines = (
        [ln.strip() for ln in logs.splitlines() if any(p in ln for p in ready_list)]
        if ready_list
        else []
    )
    return {
        "has_error": bool(error_lines),
        "error_lines": error_lines,
        "ready": bool(ready_lines),
        "ready_lines": ready_lines,
    }


def summarize_training_logs(
    op_result_or_logs: str,
    *,
    metrics_key: str = "train_metrics",
    step_key: str = "step",
    ready_patterns: Iterable[str] = (),
    error_patterns: Iterable[str] = DEFAULT_ERROR_PATTERNS,
) -> dict[str, Any]:
    """One call: decode (if a `logs` field is present, else treat input as raw logs),
    extract JSONL metrics, find the last step, and scan markers. Returns
    {logs, metrics, last_metrics, last_step, has_error, error_lines, ready, ready_lines}."""
    logs = decode_remote_logs(op_result_or_logs) if '"logs"' in op_result_or_logs else op_result_or_logs
    metrics = parse_jsonl_metrics(logs, contains=metrics_key)
    last = metrics[-1] if metrics else None
    last_step = int(last[step_key]) if (last is not None and step_key in last) else None
    markers = scan_markers(logs, error_patterns=error_patterns, ready_patterns=ready_patterns)
    return {
        "logs": logs,
        "metrics": metrics,
        "last_metrics": last,
        "last_step": last_step,
        **markers,
    }
