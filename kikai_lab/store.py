from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CurrentState:
    current: dict[str, Any]
    age_hours: float
    staleness: str


def load_current(project_root: Path) -> dict[str, Any]:
    current_path = project_root / "current.json"
    with current_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_utc_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return dt.astimezone(UTC)


def compute_current_state(current: dict[str, Any], *, now: datetime | None = None) -> CurrentState:
    now = now or datetime.now(UTC)
    last_verified_at = parse_utc_timestamp(str(current["last_verified_at"]))
    age_hours = max(0.0, (now - last_verified_at).total_seconds() / 3600)
    warn_after = float(current.get("staleness_warn_after_hours", 72))
    block_after = float(current.get("staleness_block_after_hours", 168))
    if age_hours >= block_after:
        staleness = "stale"
    elif age_hours >= warn_after:
        staleness = "warn"
    else:
        staleness = "fresh"
    return CurrentState(current=current, age_hours=age_hours, staleness=staleness)
