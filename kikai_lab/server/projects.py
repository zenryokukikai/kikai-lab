"""Projects router — the read plane over a projects root (list/detail/report/validate).

Write endpoints (PUT/archive) land in the CRUD milestone; keeping this router read-only
first lets the server ship against live registries with zero mutation risk.

Handlers are deliberately plain ``def``: FastAPI runs them in its threadpool, so
registry walks and integrity hashing never block the event loop (and ``/healthz``
stays responsive as a liveness probe).
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from kikai_lab.envelope import error, next_action
from kikai_lab.operation import OperationError
from kikai_lab.report import build_project_report
from kikai_lab.server.app import envelope_response
from kikai_lab.server.registry import (
    PROJECT_DIRS,
    WRITE_LOCK,
    ServerConfig,
    atomic_write_json,
    atomic_write_yaml,
    is_project_dir,
    list_projects,
    load_project_yaml,
    normalized_record_body,
    project_path,
    project_status,
    project_summary,
    records_equal,
    require_project,
    select_fields,
    utc_now_text,
    validate_record_schema,
)
from kikai_lab.server.runs import display_status
from kikai_lab.store import compute_current_state, load_current
from kikai_lab.validation import (
    validate_data_sources,
    validate_script_bundles,
    validate_source_snapshots,
)


def require_readable_current(path: Path) -> dict[str, Any]:
    """Load current.json, mapping the expected failure modes onto OperationError.

    Shared by /report and /validate so a routine registry state (project created but
    never verified) is a 404/422 envelope, not a 500 with a host path in it.
    """
    try:
        return load_current(path)
    except FileNotFoundError as exc:
        raise OperationError(
            "project.current_missing",
            "project has no readable current.json",
            {"project_id": path.name},
        ) from exc
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise OperationError(
            "project.current_invalid",
            "current.json is not parseable JSON",
            {"project_id": path.name},
        ) from exc


def read_brief_current(path: Path) -> dict[str, Any]:
    try:
        return load_current(path)
    except (FileNotFoundError, ValueError):
        return {}


def build_projects_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["projects"])

    @router.get("/projects")
    def projects_index(
        include_archived: bool = Query(False),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        fields: str | None = Query(None, description="comma-separated field allowlist"),
    ) -> JSONResponse:
        field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
        summaries = list_projects(config, include_archived=include_archived)
        window = summaries[offset : offset + limit]
        return envelope_response(
            ok=True,
            data={
                "projects": [select_fields(s, field_list) for s in window],
                "total": len(summaries),
                "offset": offset,
                "limit": limit,
            },
        )

    @router.get("/projects/{project_id}")
    def project_detail(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        record = load_project_yaml(path)
        warnings: list[dict[str, Any]] = []
        declared_id = record.get("project_id")
        if declared_id not in (None, path.name):
            warnings.append(
                error(
                    "project.id_mismatch",
                    "project.yaml declares a different project_id; the directory name wins",
                    blocking=False,
                    details={"declared": declared_id, "directory": path.name},
                )
            )
        return envelope_response(
            ok=True,
            data={
                "project": {**record, "project_id": path.name, "status": project_status(record)},
                "counts": {
                    key: value
                    for key, value in project_summary(path).items()
                    if key.endswith("_count")
                },
            },
            warnings=warnings,
        )

    @router.put("/projects/{project_id}")
    def project_put(
        project_id: str, body: Annotated[dict[str, Any] | None, Body()] = None
    ) -> JSONResponse:
        path = project_path(config, project_id)
        record = normalized_record_body(
            body, kind="project", id_field="project_id", record_id=path.name, enforced={}
        )
        if record.get("status") not in (None, "active"):
            raise OperationError(
                "project.record_invalid",
                "status cannot be set via PUT; use POST .../archive or .../unarchive",
                {"project_id": path.name, "status": record.get("status")},
            )
        record.setdefault("status", "active")
        validate_record_schema(record, "project", kind="project")

        with WRITE_LOCK:
            if is_project_dir(path):
                existing = load_project_yaml(path)
                if project_status(existing) == "archived":
                    raise OperationError(
                        "project.archived",
                        "project is archived; unarchive it before updating",
                        {"project_id": path.name},
                    )
                if records_equal(existing, record):
                    return envelope_response(
                        ok=True, data={"project_id": path.name, "already_exists": True}
                    )
                merged = {
                    **record,
                    "created_at": existing.get("created_at"),
                    "created_by": existing.get("created_by"),
                    "updated_at": utc_now_text(),
                }
                cleaned = {k: v for k, v in merged.items() if v is not None}
                atomic_write_yaml(path / "project.yaml", cleaned)
                return envelope_response(
                    ok=True, data={"project_id": path.name, "updated": True}
                )

            for name in PROJECT_DIRS:
                (path / name).mkdir(parents=True, exist_ok=True)
            now = utc_now_text()
            stamped = {
                **record,
                "created_at": now,
                "updated_at": now,
                "created_by": "kikai-server",
            }
            atomic_write_yaml(path / "project.yaml", stamped)
            if not (path / "current.json").exists():
                atomic_write_json(
                    path / "current.json",
                    {
                        "schema_version": 1,
                        "project_id": path.name,
                        "last_verified_at": now,
                        "verified_by": "kikai-server-scaffold",
                    },
                )
        return envelope_response(
            ok=True,
            data={"project_id": path.name, "created": True},
            next_actions=[
                next_action(
                    "register_experiment",
                    "http_request",
                    "register an experiment before submitting runs",
                    blocking=False,
                    command=f"PUT /v1/projects/{path.name}/experiments/{{experiment_id}}",
                )
            ],
            status_code=201,
        )

    @router.post("/projects/{project_id}/archive")
    def project_archive(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        with WRITE_LOCK:
            record = load_project_yaml(path)
            if project_status(record) == "archived":
                return envelope_response(
                    ok=True,
                    data={"project_id": path.name, "status": "archived", "already_exists": True},
                )
            now = utc_now_text()
            record.update(status="archived", archived_at=now, updated_at=now)
            atomic_write_yaml(path / "project.yaml", record)
        return envelope_response(ok=True, data={"project_id": path.name, "status": "archived"})

    @router.post("/projects/{project_id}/unarchive")
    def project_unarchive(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        with WRITE_LOCK:
            record = load_project_yaml(path)
            if project_status(record) != "archived":
                return envelope_response(
                    ok=True,
                    data={"project_id": path.name, "status": "active", "already_exists": True},
                )
            record.pop("archived_at", None)
            record.update(status="active", updated_at=utc_now_text())
            atomic_write_yaml(path / "project.yaml", record)
        return envelope_response(ok=True, data={"project_id": path.name, "status": "active"})

    @router.get("/projects/{project_id}/journal")
    def project_journal(
        project_id: str,
        since: str | None = Query(None, description="ISO timestamp lower bound"),
        limit: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        """The project's chronological event log (submits, stops, conclusions, gate
        failures, finalizations) — a handover in one call."""
        path = require_project(config, project_id)
        journal_path = path / "journal.jsonl"
        entries: list[dict[str, Any]] = []
        if journal_path.is_file():
            import json as _json

            for line in journal_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = _json.loads(stripped)
                except ValueError:
                    continue
                # at >= since is INCLUDED (at-least-once): utc_now_text is
                # second-resolution, so an exclusive bound would permanently drop
                # events sharing the caller's last-seen second. Dedupe client-side.
                if since and str(entry.get("at", "")) < since:
                    continue
                entries.append(entry)
        return envelope_response(
            ok=True,
            data={"events": entries[-limit:], "total": len(entries)},
        )

    @router.get("/projects/{project_id}/daemon")
    def project_daemon(project_id: str) -> JSONResponse:
        """The reconcile daemon's heartbeat (managed_runs/_serve_state.json): what it
        is doing RIGHT NOW (phase, current run, pass start time, writer_pid) plus the
        last completed pass's per-run error summary. Answers 'is the daemon alive,
        wedged, or grinding a long QC backlog?' without host access — the 2026-07-08
        freeze was undiagnosable through the API precisely because this did not exist.
        Written ONLY by long-running reconcilers (external `kikai serve` or the
        embedded BackgroundReconciler — the latter ALSO reports via /healthz;
        one-shot `kikai reconcile` never stamps it)."""
        import time as _time

        from kikai_lab.reconcile import load_serve_state

        path = require_project(config, project_id)
        state = load_serve_state(path)
        data: dict[str, Any] = {"state": state}
        updated = state.get("updated_at")
        if isinstance(updated, (int, float)):
            data["seconds_since_update"] = max(0.0, _time.time() - float(updated))
        data["hint"] = (
            "no heartbeat yet (daemon never ran with heartbeat support, or not running)"
            if not state else
            "phase=tick + large seconds_since_update = the daemon is INSIDE that run's "
            "tick (long QC backlog or a hung op); phase=idle = between passes"
        )
        return envelope_response(ok=True, data=data)

    @router.get("/projects/{project_id}/brief")
    def project_brief(project_id: str) -> JSONResponse:
        """The decision-relevant digest in ONE call: runs with verdicts and gate
        states, unfinished business, and the recent journal — what an agent needs to
        resume work without N round-trips."""
        from kikai_lab.reconcile import load_progress
        from kikai_lab.server.resources import list_yaml_records

        path = require_project(config, project_id)
        runs: list[dict[str, Any]] = []
        attention: list[dict[str, Any]] = []
        for record in list_yaml_records(path / "runs", kind="run"):
            run_name = record.get("run_name")
            if not run_name:
                if record.get("_invalid"):
                    # a corrupt run yaml is exactly what attention exists to surface
                    attention.append(
                        {"run_name": record.get("_id"), "reason": "record_invalid"}
                    )
                continue
            progress = load_progress(path, run_name)
            managed = (path / "managed_runs" / f"{run_name}.yaml").is_file()
            entry = {
                "run_name": run_name,
                "status": display_status(record, progress),
                "verdict": record.get("verdict"),
                "probe": (record.get("probe") or {}).get("question")
                if record.get("probe")
                else None,
                "lifecycle_state": progress.get("lifecycle_state") if managed else None,
                "check_verdicts": progress.get("check_verdicts") or None,
                "qc_max_step": max(progress.get("qc_done_steps") or [0]) or None,
                "last_error": progress.get("last_error"),
            }
            runs.append(entry)
            if progress.get("finalized") and not record.get("conclusions"):
                attention.append(
                    {"run_name": run_name, "reason": "finalized_without_conclusion"}
                )
            if record.get("status") == "submit_failed":
                attention.append({"run_name": run_name, "reason": "submit_failed"})
            for check_id, verdict in (progress.get("check_verdicts") or {}).items():
                if verdict == "fail":
                    attention.append(
                        {"run_name": run_name, "reason": f"metric_check_fail:{check_id}"}
                    )
        journal_path = path / "journal.jsonl"
        recent: list[dict[str, Any]] = []
        if journal_path.is_file():
            import json as _json

            lines = journal_path.read_text(encoding="utf-8").splitlines()
            for line in lines[-10:]:
                try:
                    recent.append(_json.loads(line))
                except ValueError:
                    continue
        current = read_brief_current(path)
        return envelope_response(
            ok=True,
            data={
                "project": {
                    "project_id": path.name,
                    "current_run_name": current.get("current_run_name"),
                    "current_experiment_id": current.get("current_experiment_id"),
                },
                "runs": runs,
                "attention": attention,
                "recent_events": recent,
            },
        )

    @router.get("/projects/{project_id}/report")
    def project_report(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        require_readable_current(path)
        return envelope_response(ok=True, data={"report": build_project_report(path)})

    @router.get("/projects/{project_id}/validate")
    def project_validate(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        current = require_readable_current(path)
        try:
            current["last_verified_at"]
        except KeyError as exc:
            raise OperationError(
                "project.current_invalid",
                "current.json has no last_verified_at",
                {"project_id": path.name},
            ) from exc
        try:
            compute_current_state(current)
        except (KeyError, ValueError) as exc:  # bad last_verified_at timestamp and kin
            raise OperationError(
                "project.current_invalid",
                f"current.json is not validatable: {exc}",
                {"project_id": path.name},
            ) from exc
        # Late import: keeps the heavy cli/operation import graph off create_app; the
        # CLI side imports the server package lazily too, so neither pays for the other.
        from kikai_lab.cli import validate_project

        try:
            state, warnings, errors, actions = validate_project(path)
        except yaml.YAMLError as exc:  # a hand-corrupted registry record
            raise OperationError(
                "project.registry_record_invalid",
                "a registry record is not parseable YAML",
                {"project_id": path.name, "yaml_error": type(exc).__name__},
            ) from exc
        except ValueError as exc:
            # current.json already proved computable above, so a remaining ValueError
            # comes from a registry record (e.g. YAML that parses to a non-mapping).
            raise OperationError(
                "project.registry_record_invalid",
                "a registry record is not usable",
                {"project_id": path.name, "reason": str(exc)[:200]},
            ) from exc
        errors = [
            *errors,
            *validate_data_sources(path),
            *validate_script_bundles(path),
            *validate_source_snapshots(path),
        ]
        data = {"staleness": state.staleness, "age_hours": round(state.age_hours, 1)}
        return envelope_response(
            ok=not errors,
            data=data if not errors else {},
            warnings=warnings,
            errors=errors,
            next_actions=actions,
            status_code=200 if not errors else 422,
        )

    return router
