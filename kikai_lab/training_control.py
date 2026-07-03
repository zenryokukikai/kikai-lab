from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_STOP_ENV = "KIKAI_TRAINING_CONTROL_FILE"


def control_file_from_env(env_var: str = DEFAULT_STOP_ENV) -> Path | None:
    value = os.environ.get(env_var)
    if not value:
        return None
    return Path(value)


def request_stop(
    path: str | Path,
    *,
    reason: str,
    source: str = "kikai_lab",
    step: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(path)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "kikai_training_stop_request",
        "requested_at_unix": time.time(),
        "source": source,
        "reason": reason,
    }
    if step is not None:
        payload["step"] = int(step)
    if extra:
        payload["extra"] = extra
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)
    return payload


def read_stop_request(path: str | Path | None = None) -> dict[str, Any] | None:
    target = Path(path) if path is not None else control_file_from_env()
    if target is None or not target.exists():
        return None
    try:
        payload = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "kikai_training_stop_request":
        return None
    return payload


def should_stop(path: str | Path | None = None) -> bool:
    return read_stop_request(path) is not None


def clear_stop_request(path: str | Path | None = None) -> bool:
    target = Path(path) if path is not None else control_file_from_env()
    if target is None or not target.exists():
        return False
    target.unlink()
    return True
