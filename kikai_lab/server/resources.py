"""Experiments / decisions / containers / data-sources — list, detail, idempotent PUT.

These are the registry records agents register *before* submitting a run. Every PUT is
schema-validated (schemas/*.schema.json) and fail-closed, so mistakes surface at
registration time with precise error envelopes instead of at launch time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from kikai_lab.envelope import next_action
from kikai_lab.operation import OperationError
from kikai_lab.reconcile import display_status, load_progress
from kikai_lab.server.app import envelope_response, sanitize_details, sanitize_errors
from kikai_lab.server.registry import (
    WRITE_LOCK,
    ServerConfig,
    diff_keys,
    load_yaml_record,
    normalized_record_body,
    records_equal,
    require_active_project,
    require_project,
    require_safe_id,
    select_fields,
    upsert_yaml_record,
    validate_record_schema,
)
from kikai_lab.store import load_current
from kikai_lab.validation import (
    load_data_source,
    validate_container_mount_reproducibility,
    validate_data_source_record,
)


def list_yaml_records(directory: Path, *, kind: str) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            records.append(load_yaml_record(path, kind=kind))
        except OperationError as exc:
            records.append({"_id": path.stem, "_invalid": True, "_error_code": exc.code})
    return records


def read_current(path: Path) -> dict[str, Any]:
    try:
        return load_current(path)
    except (FileNotFoundError, ValueError):
        return {}


def put_result_response(
    resource: str, record_id: str, outcome: dict[str, Any], *, actions: list | None = None
) -> JSONResponse:
    return envelope_response(
        ok=True,
        data={resource: record_id, **outcome},
        next_actions=actions or [],
        status_code=201 if outcome.get("created") else 200,
    )


_DECISION_STATUS_RANK = {"open": 0, "decided": 1, "superseded": 2}


def upsert_decision(record_path: Path, record: dict[str, Any], decision_id: str) -> dict[str, Any]:
    """Decisions are the audit trail: forward status transitions only, no rewrites.

    Allowed divergence from the stored record is limited to ``status`` (moving forward
    through open -> decided -> superseded) and ``decided_at``. Anything else — title,
    summary, links — must be a NEW superseding decision, so history is never silently
    rewritten. This is the immutable-PUT machinery agents rely on for retry safety.
    """
    from kikai_lab.server.registry import atomic_write_yaml

    if not record_path.exists():
        atomic_write_yaml(record_path, record)
        return {"created": True}
    existing = load_yaml_record(record_path, kind="decision")
    if records_equal(existing, record):
        return {"already_exists": True}
    changed = diff_keys(existing, record)
    old_rank = _DECISION_STATUS_RANK.get(existing.get("status"), 0)
    new_rank = _DECISION_STATUS_RANK.get(record.get("status"), 0)
    if set(changed) <= {"status", "decided_at"} and new_rank >= old_rank:
        atomic_write_yaml(record_path, record)
        return {"updated": True}
    raise OperationError(
        "decision.exists",
        "decision exists with different content; record a NEW superseding decision "
        "instead of rewriting history (only forward status/decided_at changes are allowed)",
        {"id": decision_id, "diff_keys": changed},
    )


def build_resources_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["resources"])

    # ------------------------------------------------------------------ experiments
    @router.get("/projects/{project_id}/experiments")
    def experiments_index(
        project_id: str,
        fields: str | None = Query(None),
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        path = require_project(config, project_id)
        current_experiment = read_current(path).get("current_experiment_id")
        field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
        summaries = []
        for record in list_yaml_records(path / "experiments", kind="experiment"):
            if record.get("_invalid"):
                summaries.append(record)
                continue
            if record.get("kind") != "experiment":
                # A yaml file without kind: experiment still counts in project counts;
                # hide-nothing: surface it as an invalid stub instead of skipping.
                summaries.append(
                    {
                        "_id": record.get("experiment_id") or "unknown",
                        "_invalid": True,
                        "_error_code": "experiment.kind_missing",
                    }
                )
                continue
            summaries.append(
                select_fields(
                    {
                        "experiment_id": record.get("experiment_id"),
                        "title": record.get("title"),
                        "summary": record.get("summary"),
                        "status": record.get("status"),
                        "stage": record.get("stage"),
                        "is_current": record.get("experiment_id") == current_experiment,
                    },
                    field_list,
                )
            )
        window = summaries[offset : offset + limit]
        return envelope_response(
            ok=True,
            data={"experiments": window, "total": len(summaries), "offset": offset, "limit": limit},
        )

    @router.get("/projects/{project_id}/experiments/{experiment_id}")
    def experiment_detail(project_id: str, experiment_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        experiment_id = require_safe_id(experiment_id, kind="experiment")
        record_path = path / "experiments" / f"{experiment_id}.yaml"
        if not record_path.is_file():
            raise OperationError(
                "experiment.not_found",
                f"no experiment '{experiment_id}' in project '{path.name}'",
                {"project_id": path.name, "experiment_id": experiment_id},
            )
        record = load_yaml_record(record_path, kind="experiment")
        runs = []
        for run in list_yaml_records(path / "runs", kind="run"):
            if run.get("experiment_id") == experiment_id:
                run_name = run.get("run_name")
                progress = load_progress(path, run_name) if run_name else {}
                runs.append(
                    {
                        "run_name": run_name,
                        "status": display_status(run, progress),
                        "fresh_no_resume": run.get("fresh_no_resume"),
                    }
                )
        return envelope_response(
            ok=True, data={"experiment": record, "runs": runs, "run_count": len(runs)}
        )

    @router.put("/projects/{project_id}/experiments/{experiment_id}")
    def experiment_put(
        project_id: str, experiment_id: str, body: Annotated[dict[str, Any], Body()] = ...
    ) -> JSONResponse:
        path = require_active_project(config, project_id)
        experiment_id = require_safe_id(experiment_id, kind="experiment")
        record = normalized_record_body(
            body,
            kind="experiment",
            id_field="experiment_id",
            record_id=experiment_id,
            enforced={"kind": "experiment"},
        )
        validate_record_schema(record, "experiment", kind="experiment")
        with WRITE_LOCK:
            require_active_project(config, project_id)
            outcome = upsert_yaml_record(
                path / "experiments" / f"{experiment_id}.yaml", record, kind="experiment"
            )
        return put_result_response(
            "experiment_id",
            experiment_id,
            outcome,
            actions=[
                next_action(
                    "register_container",
                    "http_request",
                    "register a container profile, then a bundle, then submit runs",
                    blocking=False,
                    command=f"PUT /v1/projects/{path.name}/containers/{{container_id}}",
                )
            ],
        )

    # ------------------------------------------------------------------- decisions
    @router.get("/projects/{project_id}/decisions")
    def decisions_index(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        records = [
            r
            for r in list_yaml_records(path / "decisions", kind="decision")
            if r.get("_invalid") or r.get("kind") == "decision"
        ]
        return envelope_response(ok=True, data={"decisions": records, "total": len(records)})

    @router.put("/projects/{project_id}/decisions/{decision_id}")
    def decision_put(
        project_id: str, decision_id: str, body: Annotated[dict[str, Any], Body()] = ...
    ) -> JSONResponse:
        path = require_active_project(config, project_id)
        decision_id = require_safe_id(decision_id, kind="decision")
        record = normalized_record_body(
            body,
            kind="decision",
            id_field="decision_id",
            record_id=decision_id,
            enforced={"kind": "decision"},
        )
        if not isinstance(record.get("title"), str) or not record["title"]:
            raise OperationError(
                "decision.record_invalid",
                "decision requires a non-empty title",
                {"id": decision_id},
            )
        status = record.get("status", "open")
        if status not in ("open", "decided", "superseded"):
            raise OperationError(
                "decision.record_invalid",
                "decision status must be open|decided|superseded",
                {"id": decision_id, "status": status},
            )
        record["status"] = status
        validate_record_schema(record, "decision", kind="decision")
        record_path = path / "decisions" / f"{decision_id}.yaml"
        with WRITE_LOCK:
            require_active_project(config, project_id)
            outcome = upsert_decision(record_path, record, decision_id)
        return put_result_response("decision_id", decision_id, outcome)

    # ------------------------------------------------------------------ containers
    @router.get("/projects/{project_id}/containers")
    def containers_index(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        records = []
        for record in list_yaml_records(path / "containers", kind="container"):
            if record.get("_invalid"):
                records.append(record)
                continue
            if record.get("kind") != "docker_container":
                continue
            docker = record.get("docker") or {}
            records.append(
                {
                    "container_id": record.get("container_id"),
                    "name": docker.get("name"),
                    "image": docker.get("image"),
                    "role": record.get("role"),
                    "status": record.get("status"),
                    "summary": record.get("summary"),
                }
            )
        return envelope_response(ok=True, data={"containers": records, "total": len(records)})

    @router.get("/projects/{project_id}/containers/{container_id}")
    def container_detail(project_id: str, container_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        container_id = require_safe_id(container_id, kind="container")
        record_path = path / "containers" / f"{container_id}.yaml"
        if not record_path.is_file():
            raise OperationError(
                "container.not_found",
                f"no container '{container_id}' in project '{path.name}'",
                {"project_id": path.name, "container_id": container_id},
            )
        return envelope_response(
            ok=True, data={"container": load_yaml_record(record_path, kind="container")}
        )

    @router.put("/projects/{project_id}/containers/{container_id}")
    def container_put(
        project_id: str, container_id: str, body: Annotated[dict[str, Any], Body()] = ...
    ) -> JSONResponse:
        path = require_active_project(config, project_id)
        container_id = require_safe_id(container_id, kind="container")
        record = normalized_record_body(
            body,
            kind="container",
            id_field="container_id",
            record_id=container_id,
            enforced={"kind": "docker_container"},
        )
        validate_record_schema(record, "docker_container", kind="container")
        record_path = path / "containers" / f"{container_id}.yaml"
        mount_errors = validate_container_mount_reproducibility(
            container_id=container_id, container=record, path=record_path
        )
        if mount_errors:
            return envelope_response(
                ok=False, errors=sanitize_errors(mount_errors), status_code=422
            )
        with WRITE_LOCK:
            require_active_project(config, project_id)
            outcome = upsert_yaml_record(record_path, record, kind="container")
        return put_result_response("container_id", container_id, outcome)

    # ---------------------------------------------------------------- data sources
    @router.get("/projects/{project_id}/data-sources")
    def data_sources_index(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        records = []
        for record in list_yaml_records(path / "data_sources", kind="data_source"):
            if record.get("_invalid"):
                records.append(record)
                continue
            records.append(
                {
                    "data_source_id": record.get("data_source_id"),
                    "source_type": record.get("source_type"),
                    "status": record.get("status"),
                    "summary": record.get("summary"),
                    "roles": (record.get("contract") or {}).get("role_compatibility"),
                }
            )
        return envelope_response(ok=True, data={"data_sources": records, "total": len(records)})

    @router.get("/projects/{project_id}/data-sources/{data_source_id}")
    def data_source_detail(project_id: str, data_source_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        record = load_data_source(path, data_source_id)
        return envelope_response(ok=True, data={"data_source": record})

    @router.put("/projects/{project_id}/data-sources/{data_source_id}")
    def data_source_put(
        project_id: str, data_source_id: str, body: Annotated[dict[str, Any], Body()] = ...
    ) -> JSONResponse:
        """Register a data source from a SERVER-LOCAL path. Integrity (sha256 /
        directory manifest) is always computed by the server — never caller-supplied —
        which is the whole point of routing registration through kikai."""
        from kikai_lab.operation import (
            create_directory_data_source,
            create_file_data_source,
        )

        path = require_active_project(config, project_id)
        data_source_id = require_safe_id(data_source_id, kind="data_source")
        if not isinstance(body, dict):
            raise OperationError(
                "data_source.record_invalid", "body must be a JSON object", {}
            )
        source_kind = body.get("kind")
        if source_kind not in ("file", "directory"):
            raise OperationError(
                "data_source.record_invalid",
                "body.kind must be 'file' or 'directory'",
                {"kind": source_kind},
            )
        if body.get("roles") is not None and not isinstance(body["roles"], list):
            raise OperationError(
                "data_source.record_invalid",
                "roles must be a list of canonical role names",
                {"roles": body.get("roles")},
            )
        required = {"source_type", "path", "host_ref", "roles", "summary"}
        missing = sorted(k for k in required if not body.get(k))
        if missing:
            raise OperationError(
                "data_source.record_invalid",
                "missing required fields",
                {"missing": missing},
            )
        record_path = path / "data_sources" / f"{data_source_id}.yaml"
        with WRITE_LOCK:
            require_active_project(config, project_id)
            if record_path.exists():
                existing = load_data_source(path, data_source_id)
                storage = existing.get("storage") or {}
                expected_strategy = (
                    "file_sha256" if source_kind == "file" else "directory_manifest_sha256"
                )
                same_target = (
                    storage.get("path") == body["path"]
                    and storage.get("host_ref") == body["host_ref"]
                    and existing.get("source_type") == body["source_type"]
                    and (existing.get("integrity") or {}).get("strategy")
                    == expected_strategy
                    and (existing.get("contract") or {}).get("role_compatibility")
                    == body["roles"]
                )
                if same_target:
                    return envelope_response(
                        ok=True,
                        data={"data_source_id": data_source_id, "already_exists": True},
                    )
                raise OperationError(
                    "data_source.exists",
                    "data source exists with a different target; data sources are "
                    "immutable — register a new id",
                    {
                        "data_source_id": data_source_id,
                        "diff_keys": diff_keys(
                            {
                                "path": storage.get("path"),
                                "source_type": existing.get("source_type"),
                                "roles": (existing.get("contract") or {}).get(
                                    "role_compatibility"
                                ),
                            },
                            {
                                "path": body["path"],
                                "source_type": body["source_type"],
                                "roles": body["roles"],
                            },
                        ),
                    },
                )
            create = (
                create_file_data_source
                if source_kind == "file"
                else create_directory_data_source
            )
            result = create(
                project_root=path,
                data_source_id=data_source_id,
                source_type=body["source_type"],
                path_ref=body["path"],
                host_ref=body["host_ref"],
                role_compatibility=list(body["roles"]),
                summary=body["summary"],
                container_mount_path=body.get("container_mount_path"),
                upstream_data_source_ids=body.get("upstream_data_source_ids") or [],
                upstream_source_snapshot_ids=body.get("upstream_source_snapshot_ids")
                or [],
            )
        return envelope_response(
            ok=True,
            data={
                "data_source_id": data_source_id,
                "created": True,
                **sanitize_details(result),
            },
            status_code=201,
        )

    @router.post("/projects/{project_id}/data-sources/{data_source_id}/verify")
    def data_source_verify(project_id: str, data_source_id: str) -> JSONResponse:
        """Re-verify a data source fail-closed (launch_like re-hashes immutable
        files/directories) — what an agent runs right before relying on it."""
        path = require_project(config, project_id)
        data_source_id = require_safe_id(data_source_id, kind="data_source")
        record = load_data_source(path, data_source_id)
        errors = validate_data_source_record(
            path, data_source_id, record, launch_like=True
        )
        return envelope_response(
            ok=not errors,
            data={"data_source_id": data_source_id, "verified": not errors},
            errors=sanitize_errors(errors),
            status_code=200 if not errors else 422,
        )

    return router
