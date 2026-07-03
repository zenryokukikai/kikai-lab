"""First-class DECISION records, managed inside kikai-lab itself.

Previously a project's `current.must_read_external_ref_ids` had to be satisfied by an
experiment's `external_refs` pointing OUT to an external system (omoikane decision IDs).
Decisions now live in the project as `decisions/<decision_id>.yaml`, so kikai-lab owns
the decision log; validation accepts an internal decision (or a legacy external_ref) as
satisfying a must-read. No external system is required.

Record shape (decisions/<id>.yaml):
  schema_version: 1
  kind: decision
  decision_id: D-001
  title: ...
  summary: ...
  status: open | decided | superseded
  decided_at: <RFC3339>            # optional
  links: [{kind: experiment, id: ...}, {kind: run, id: ...}]   # optional
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_SAFE_DECISION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DECISION_STATUSES = ("open", "decided", "superseded")


class DecisionError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def create_decision(
    project_root: str | Path,
    decision_id: str,
    *,
    title: str,
    summary: str,
    status: str = "open",
    decided_at: str | None = None,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write decisions/<decision_id>.yaml and return a small result. Refuses to
    overwrite an existing decision."""
    if not _SAFE_DECISION_ID.match(decision_id or ""):
        raise DecisionError(
            "decision.invalid_id",
            f"decision_id must match {_SAFE_DECISION_ID.pattern}",
            {"decision_id": decision_id},
        )
    if status not in DECISION_STATUSES:
        raise DecisionError(
            "decision.invalid_status",
            f"status must be one of {', '.join(DECISION_STATUSES)}",
            {"status": status},
        )
    if not title or not str(title).strip():
        raise DecisionError("decision.missing_title", "decision title is required")
    root = Path(project_root)
    decisions_dir = root / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    path = decisions_dir / f"{decision_id}.yaml"
    if path.exists():
        raise DecisionError(
            "decision.exists", f"decision already exists: {path}", {"path": str(path)})
    record: dict[str, Any] = {
        "schema_version": 1,
        "kind": "decision",
        "decision_id": decision_id,
        "title": title,
        "summary": summary,
        "status": status,
    }
    if decided_at:
        record["decided_at"] = decided_at
    if links:
        record["links"] = links
    path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {"decision_id": decision_id, "path": str(path), "status": status}


def load_decisions(project_root: str | Path) -> list[dict[str, Any]]:
    """All `kind: decision` records under decisions/, sorted by decision_id."""
    decisions_dir = Path(project_root) / "decisions"
    out: list[dict[str, Any]] = []
    if decisions_dir.is_dir():
        for path in sorted(decisions_dir.glob("*.yaml")):
            with path.open("r", encoding="utf-8") as handle:
                rec = yaml.safe_load(handle)
            if isinstance(rec, dict) and rec.get("kind") == "decision":
                out.append(rec)
    return out


def decision_ids(project_root: str | Path) -> set[str]:
    """The set of decision_ids managed in this project."""
    return {str(d.get("decision_id")) for d in load_decisions(project_root) if d.get("decision_id")}
