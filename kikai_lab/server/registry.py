"""Projects-root resolution and registry file helpers for the HTTP server.

A projects root is a directory whose immediate children are kikai project registries;
a child directory is a project iff it contains ``project.yaml``. ``project_id`` is the
directory name and must be a single safe path segment — this is the only place ids from
the network are turned into filesystem paths, so validation lives here.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kikai_lab.operation import OperationError

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# FastAPI runs sync handlers concurrently in a threadpool, so read-check-write sequences
# (idempotency probe -> write, archived probe -> write) need one process-wide lock to keep
# "archived fail-closes writes" true under concurrent requests. Writes are all fast local
# file operations; a single coarse lock is sufficient and simple. (Single-worker uvicorn
# is a documented deployment constraint, so a process lock covers the whole server.)
WRITE_LOCK = threading.Lock()

# Registry directories scaffolded for a new project (mirrors examples/example_project).
PROJECT_DIRS = (
    "experiments",
    "runs",
    "containers",
    "data_sources",
    "decisions",
    "artifacts",
    "script_bundles",
    "source_snapshots",
    "managed_runs",
    "ops",
    "delivery_targets",
)


@dataclass(frozen=True)
class ServerConfig:
    projects_root: Path
    host_id: str = "local"
    content_roots: tuple[Path, ...] = ()
    path_map: dict[str, str] = field(default_factory=dict)
    # When configured, run_dir-based reads (metrics/checkpoints/events) and managed
    # submissions are contained to these roots — run_dir is client-influenced once the
    # submit endpoint exists, and /metrics would otherwise read any JSONL-parseable
    # file's rows. Empty = no containment (single-operator localhost deployments).
    run_dir_roots: tuple[Path, ...] = ()
    with_reconciler: bool = False
    reconcile_interval: int = 60
    # Opt-in bearer auth for deployments beyond a trusted network: when set, every
    # request except /healthz must carry "Authorization: Bearer <token>". This is a
    # shared-secret gate, not user management — front with a reverse proxy for more.
    auth_token: str | None = None


def require_safe_id(value: Any, *, kind: str) -> str:
    """Validate a network-supplied id before it ever touches a path."""
    text = value if isinstance(value, str) else ""
    if not SAFE_ID.fullmatch(text) or Path(text).name != text:
        raise OperationError(
            f"{kind}.id_invalid",
            f"{kind} id must match {SAFE_ID.pattern} and be a single path segment",
            {"id": text},
        )
    return text


def project_path(config: ServerConfig, project_id: str) -> Path:
    return config.projects_root / require_safe_id(project_id, kind="project")


def is_project_dir(path: Path) -> bool:
    return (path / "project.yaml").is_file()


def load_project_yaml(path: Path) -> dict[str, Any]:
    try:
        with (path / "project.yaml").open("r", encoding="utf-8") as f:
            record = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise OperationError(
            "project.record_invalid",
            "project.yaml is not parseable YAML",
            {"project_id": path.name, "yaml_error": type(exc).__name__},
        ) from exc
    if not isinstance(record, dict):
        raise OperationError(
            "project.record_invalid",
            "project.yaml must be a mapping",
            {"project_id": path.name},
        )
    return record


def require_project(config: ServerConfig, project_id: str) -> Path:
    path = project_path(config, project_id)
    if not is_project_dir(path):
        raise OperationError(
            "project.not_found",
            f"no project '{project_id}' under the projects root",
            {"project_id": project_id},
        )
    return path


def project_status(record: dict[str, Any]) -> str:
    status = record.get("status")
    return status if isinstance(status, str) and status else "active"


def require_active_project(config: ServerConfig, project_id: str) -> Path:
    path = require_project(config, project_id)
    if project_status(load_project_yaml(path)) == "archived":
        raise OperationError(
            "project.archived",
            f"project '{project_id}' is archived; unarchive it before writing",
            {"project_id": project_id},
        )
    return path


def count_children(path: Path, subdir: str, suffix: str) -> int:
    directory = path / subdir
    if not directory.is_dir():
        return 0
    return sum(1 for child in directory.glob(f"*{suffix}") if child.is_file())


def project_summary(path: Path) -> dict[str, Any]:
    record = load_project_yaml(path)
    return {
        "project_id": path.name,
        "status": project_status(record),
        "summary": record.get("summary"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "experiment_count": count_children(path, "experiments", ".yaml"),
        "run_count": count_children(path, "runs", ".yaml"),
        "managed_run_count": count_children(path, "managed_runs", ".yaml"),
    }


def list_projects(config: ServerConfig, *, include_archived: bool = False) -> list[dict[str, Any]]:
    root = config.projects_root
    if not root.is_dir():
        raise OperationError(
            "server.projects_root_missing",
            f"projects root does not exist: {root}",
            {"projects_root": str(root)},
        )
    summaries: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not is_project_dir(child):
            continue
        try:
            summary = project_summary(child)
        except OperationError as exc:
            # One hand-broken project.yaml must not take down the discovery endpoint:
            # surface the project as invalid instead of failing the whole listing.
            summary = {"project_id": child.name, "status": "invalid", "error_code": exc.code}
        if not include_archived and summary["status"] == "archived":
            continue
        summaries.append(summary)
    return summaries


def select_fields(record: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """Trim a record to the requested fields (token economy for agents)."""
    if not fields:
        return record
    return {key: record.get(key) for key in fields if key in record}


def utc_now_text() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Fields the server manages itself; they never participate in idempotency comparison.
SERVER_MANAGED_KEYS = frozenset({"created_at", "updated_at", "archived_at", "created_by"})


def jsonable(value: Any) -> Any:
    """Normalize a YAML-loaded structure for canonical JSON hashing.

    ``yaml.safe_load`` parses unquoted ISO timestamps into ``datetime``/``date`` objects
    and permits non-string mapping keys; both crash ``json.dumps``. Hand-edited registry
    records are first-class here, so canonicalization must absorb them.
    """
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):  # datetime.datetime / datetime.date
        # Normalize to the Z-suffixed form the rest of the registry writes, so an
        # unquoted YAML timestamp compares equal to its JSON string twin.
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)


def canonical_hash(payload: dict[str, Any]) -> str:
    import hashlib

    text = json.dumps(jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_server_managed(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in SERVER_MANAGED_KEYS}


def diff_keys(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Top-level keys whose values differ — the 409 payload agents read to see what
    diverged, without echoing either full document back."""
    old, new = jsonable(strip_server_managed(old)), jsonable(strip_server_managed(new))
    keys = set(old) | set(new)
    return sorted(k for k in keys if old.get(k) != new.get(k))


def records_equal(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return canonical_hash(strip_server_managed(old)) == canonical_hash(strip_server_managed(new))


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def schemas_dir() -> Path:
    """Locate schemas both from a checkout (repo-root schemas/) and from a wheel
    (force-included as kikai_lab/_schemas by the build config)."""
    packaged = Path(__file__).resolve().parents[1] / "_schemas"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    if name not in _SCHEMA_CACHE:
        schema_path = schemas_dir() / f"{name}.schema.json"
        with schema_path.open("r", encoding="utf-8") as f:
            _SCHEMA_CACHE[name] = json.load(f)
    return _SCHEMA_CACHE[name]


def validate_record_schema(record: dict[str, Any], schema_name: str, *, kind: str) -> None:
    from jsonschema import Draft202012Validator

    schema = load_schema(schema_name)
    findings = [
        {"path": "/".join(str(p) for p in err.absolute_path), "message": err.message}
        for err in Draft202012Validator(schema).iter_errors(record)
    ]
    if findings:
        raise OperationError(
            f"{kind}.record_invalid",
            f"{kind} record failed schema validation ({schema_name}.schema.json)",
            {"validation_errors": findings[:20]},
        )


def load_yaml_record(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            record = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise OperationError(
            f"{kind}.record_invalid",
            f"{kind} record is not parseable YAML",
            {"id": path.stem, "yaml_error": type(exc).__name__},
        ) from exc
    if not isinstance(record, dict):
        raise OperationError(
            f"{kind}.record_invalid",
            f"{kind} record must be a mapping",
            {"id": path.stem},
        )
    return record


def upsert_yaml_record(
    path: Path, record: dict[str, Any], *, kind: str, mutable: bool = True
) -> dict[str, Any]:
    """Idempotent PUT semantics for one YAML registry record.

    Same canonical content -> ``already_exists`` (success, no write). Divergent content:
    mutable kinds are overwritten (``updated``); immutable kinds raise ``{kind}.exists``
    (-> 409) with the diverging top-level keys so the agent sees what changed without
    either document being echoed back.
    """
    if path.exists():
        existing = load_yaml_record(path, kind=kind)
        if records_equal(existing, record):
            return {"already_exists": True}
        if not mutable:
            raise OperationError(
                f"{kind}.exists",
                f"{kind} already exists with different content",
                {"id": path.stem, "diff_keys": diff_keys(existing, record)},
            )
        atomic_write_yaml(path, record)
        return {"updated": True}
    atomic_write_yaml(path, record)
    return {"created": True}


def normalized_record_body(
    body: Any, *, kind: str, id_field: str, record_id: str, enforced: dict[str, Any]
) -> dict[str, Any]:
    """Merge a client body with server-enforced fields, rejecting contradictions.

    A body that names a different id (or kind) than the URL is a client bug worth a
    loud 422 rather than a silent override — and that includes an explicit ``null``,
    which would otherwise clobber the enforced value and create a record invisible to
    kind-filtered listings. ``null`` values and server-managed timestamps are stripped
    so create/update/compare all see one canonical shape (idempotency convergence).
    """
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise OperationError(
            f"{kind}.record_invalid",
            f"{kind} body must be a JSON object",
            {"id": record_id},
        )
    for key, value in ((id_field, record_id), *enforced.items()):
        if key in body and body[key] != value:
            raise OperationError(
                f"{kind}.record_invalid",
                f"body field '{key}' contradicts the request path/server value",
                {"id": record_id, "field": key, "declared": body[key], "expected": value},
            )
    cleaned = {
        k: v for k, v in body.items() if v is not None and k not in SERVER_MANAGED_KEYS
    }
    return {"schema_version": 1, **enforced, **cleaned, id_field: record_id}


def append_journal(project_root: Path, kind: str, data: dict[str, Any]) -> None:
    """Append one event to the project's chronological journal (journal.jsonl).

    Best-effort: the journal is an operator/agent convenience (GET /journal), never a
    reason to fail the mutation that triggered it."""
    try:
        entry = {"at": utc_now_text(), "kind": kind, **data}
        path = Path(project_root) / "journal.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 - best-effort by contract, incl. TypeError
        pass


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(path, text + "\n")


def atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=True))
