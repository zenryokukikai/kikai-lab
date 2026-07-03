from __future__ import annotations

import json
import sys
from typing import Any

SCHEMA_VERSION = 1


def error(
    code: str,
    message: str,
    *,
    blocking: bool = True,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "blocking": blocking,
        "details": details or {},
    }


def next_action(
    action_id: str,
    kind: str,
    reason: str,
    *,
    blocking: bool = True,
    command: str | None = None,
) -> dict[str, Any]:
    action = {
        "id": action_id,
        "kind": kind,
        "blocking": blocking,
        "reason": reason,
    }
    if command is not None:
        action["command"] = command
    return action


def envelope(
    *,
    ok: bool,
    data: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "schema_version": SCHEMA_VERSION,
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors or [],
        "next_actions": next_actions or [],
    }


def emit(payload: dict[str, Any], exit_code: int) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return exit_code
