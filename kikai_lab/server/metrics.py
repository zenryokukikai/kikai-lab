"""Streaming, columnar, downsampled reads over a trainer's ``metrics.jsonl``.

The trainer appends one JSON object per line (``event: train_metrics`` rows carry the
numeric series; ``early_stop``/``done`` rows are terminal markers). Files reach hundreds
of thousands of lines, so this module never loads the whole file into memory and returns
columnar arrays (``{"step": [...], "series": {"loss": [...]}}``) — the token-cheapest
shape for agents and exactly what the dashboard's chart wants.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from kikai_lab.reconcile import TERMINAL_TRAINING_EVENTS

# the ONE terminal-event vocabulary lives in reconcile — a run the daemon
# finalizes as terminal must read as terminal here too
TERMINAL_EVENTS = tuple(sorted(TERMINAL_TRAINING_EVENTS))
TRAIN_METRICS_EVENT = "train_metrics"
# Keys that are row bookkeeping, not plottable series.
_NON_SERIES_KEYS = frozenset({"event", "step"})


def parse_rows(metrics_path: Path):
    """Yield parsed JSON rows, skipping blank/corrupt lines (a live trainer may be
    mid-write on the last line)."""
    with Path(metrics_path).open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def read_metrics_columnar(
    metrics_path: Path,
    *,
    keys: list[str] | None = None,
    since_step: int = 0,
    max_points: int = 500,
    every: int | None = None,
) -> dict[str, Any]:
    """One streaming pass: collect requested series, then stride-downsample.

    The exact latest train_metrics row is always returned undownsampled (``last_row``)
    and always included as the final chart point, so a poller sees fresh values even
    when the stride would have skipped them. Notes: ``points`` may exceed ``max_points``
    by one (that appended freshest point); ``rows_total`` counts rows at/after
    ``since_step``, not the whole file.
    """
    steps: list[int] = []
    collected: dict[str, list[Any]] = {}
    requested = list(keys) if keys else None
    available: set[str] = set()
    terminal_event: dict[str, Any] | None = None
    last_row: dict[str, Any] | None = None
    rows_total = 0

    for row in parse_rows(metrics_path):
        event = row.get("event")
        if event in TERMINAL_EVENTS:
            terminal_event = {"event": event, "step": row.get("step")}
            continue
        if event != TRAIN_METRICS_EVENT:
            continue
        step = row.get("step")
        if not isinstance(step, int) or step < since_step:
            continue
        rows_total += 1
        last_row = row
        available.update(k for k in row if k not in _NON_SERIES_KEYS)
        if requested is None:
            # No explicit keys: collect every numeric series, including keys that first
            # appear mid-run (eval metrics) — earlier points are back-filled with None.
            discovered = [
                k
                for k in row
                if k not in _NON_SERIES_KEYS
                and isinstance(row[k], int | float)
                and not isinstance(row[k], bool)
                and k not in collected
            ]
            for key in sorted(discovered):
                collected[key] = [None] * len(steps)
            steps.append(step)
            for key in collected:
                collected[key].append(row.get(key))
            continue
        steps.append(step)
        for key in requested:
            collected.setdefault(key, []).append(row.get(key))

    stride = max(1, int(every)) if every else max(1, math.ceil(len(steps) / max(1, max_points)))
    downsampled = stride > 1
    if downsampled:
        picked = list(range(0, len(steps), stride))
        if picked and picked[-1] != len(steps) - 1:
            picked.append(len(steps) - 1)  # the freshest point always survives
        steps = [steps[i] for i in picked]
        collected = {k: [v[i] for i in picked] for k, v in collected.items()}

    return {
        "keys": requested if requested is not None else sorted(collected),
        "step": steps,
        "series": collected,
        "rows_total": rows_total,
        "points": len(steps),
        "downsampled": downsampled,
        "stride": stride,
        "available_keys": sorted(available),
        "terminal_event": terminal_event,
        "last_row": last_row,
    }


def read_last_train_metrics(
    metrics_path: Path, *, window_bytes: int = 65536
) -> dict[str, Any] | None:
    """The latest train_metrics row, reading only the file tail (cheap polling)."""
    path = Path(metrics_path)
    if not path.exists():
        return None
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - window_bytes))
        chunk = f.read().decode("utf-8", errors="replace")
    for line in reversed(chunk.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event") == TRAIN_METRICS_EVENT:
            return row
    return None
