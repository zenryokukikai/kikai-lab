"""Artifact ledger queries + fail-closed content streaming.

``artifacts/<run_name>.jsonl`` is an append-only ledger (one JSON object per line).
``/content`` resolves an artifact's first usable location to a real file and streams it
(Range-capable, so QC videos scrub in the dashboard) — but ONLY if the resolved real
path falls under an explicitly configured ``--content-root``. No roots configured means
no content is served: fail-closed is the point, because this endpoint is the only place
the server turns registry data into filesystem reads on behalf of the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from kikai_lab.operation import OperationError, resolve_text_ref
from kikai_lab.server.app import envelope_response, sanitize_details
from kikai_lab.server.registry import ServerConfig, require_project, require_safe_id


def iter_ledger_rows(project_root: Path):
    directory = project_root / "artifacts"
    if not directory.is_dir():
        return
    for ledger in sorted(directory.glob("*.jsonl")):
        with ledger.open("r", encoding="utf-8") as f:
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


def artifact_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": row.get("artifact_id"),
        "run_name": row.get("run_name"),
        "kind": row.get("kind"),
        "artifact_class": row.get("artifact_class"),
        "location_kinds": [
            loc.get("kind") for loc in row.get("locations", []) if isinstance(loc, dict)
        ],
    }


def find_artifact(project_root: Path, artifact_id: str) -> dict[str, Any]:
    # Ledgers are append-only; the LAST row wins if an id was ever re-recorded.
    found: dict[str, Any] | None = None
    for row in iter_ledger_rows(project_root):
        if row.get("artifact_id") == artifact_id:
            found = row
    if found is None:
        raise OperationError(
            "artifact.not_found",
            f"no artifact '{artifact_id}' in project '{project_root.name}'",
            {"project_id": project_root.name, "artifact_id": artifact_id},
        )
    return found


def apply_path_map(path_text: str, path_map: dict[str, str]) -> str:
    """Rewrite container-space prefixes to host-space ones (longest prefix wins).

    Matches only on a path-component boundary: /a/b must not rewrite /a/b_evil.
    """
    for prefix in sorted(path_map, key=len, reverse=True):
        boundary = path_text == prefix or path_text.startswith(prefix.rstrip("/") + "/")
        if boundary:
            return path_map[prefix] + path_text[len(prefix) :]
    return path_text


def resolve_artifact_file(config: ServerConfig, artifact: dict[str, Any]) -> Path:
    """First location that resolves to a real file inside a configured content root."""
    if not config.content_roots:
        raise OperationError(
            "artifact.content_root_forbidden",
            "no --content-root is configured; artifact content serving is disabled",
            {"artifact_id": artifact.get("artifact_id")},
        )
    attempts: list[dict[str, Any]] = []
    for location in artifact.get("locations", []):
        if not isinstance(location, dict):
            continue
        kind = location.get("kind")
        raw = location.get("path")
        if kind not in ("host_path", "container_path") or not isinstance(raw, str):
            attempts.append({"kind": kind, "reason": "unsupported_location"})
            continue
        try:
            text = resolve_text_ref(raw)
        except OperationError:
            attempts.append({"kind": kind, "reason": "unresolvable_ref"})
            continue
        if kind == "container_path":
            text = apply_path_map(text, config.path_map)
        try:
            real = Path(text).resolve(strict=True)
        except OSError:
            attempts.append({"kind": kind, "reason": "file_missing"})
            continue
        if not real.is_file():  # a directory resolves fine but FileResponse would 500
            attempts.append({"kind": kind, "reason": "file_missing"})
            continue
        contained = any(
            os.path.commonpath([str(real), str(root.resolve())]) == str(root.resolve())
            for root in config.content_roots
            if root.is_dir()
        )
        if not contained:
            attempts.append({"kind": kind, "reason": "outside_content_roots"})
            continue
        return real
    raise OperationError(
        "artifact.content_root_forbidden",
        "no artifact location resolves to a file inside the configured content roots",
        {"artifact_id": artifact.get("artifact_id"), "attempts": attempts},
    )


def build_artifacts_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["artifacts"])

    @router.get("/projects/{project_id}/artifacts")
    def artifacts_index(
        project_id: str,
        run_name: str | None = Query(None),
        kind: str | None = Query(None),
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        path = require_project(config, project_id)
        rows = []
        for row in iter_ledger_rows(path):
            if run_name and row.get("run_name") != run_name:
                continue
            if kind and row.get("kind") != kind:
                continue
            rows.append(artifact_summary(row))
        window = rows[offset : offset + limit]
        return envelope_response(
            ok=True,
            data={"artifacts": window, "total": len(rows), "offset": offset, "limit": limit},
        )

    @router.get("/projects/{project_id}/artifacts/{artifact_id}")
    def artifact_detail(project_id: str, artifact_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        artifact_id = require_safe_id(artifact_id, kind="artifact")
        # locations[].path are absolute host paths; the port may be exposed beyond
        # localhost with no auth, so mask them like error details (basename kept —
        # agents fetch bytes via /content, never via raw paths).
        artifact = sanitize_details(find_artifact(path, artifact_id))
        return envelope_response(ok=True, data={"artifact": artifact})

    @router.get("/projects/{project_id}/artifacts/{artifact_id}/content")
    def artifact_content(project_id: str, artifact_id: str) -> FileResponse:
        path = require_project(config, project_id)
        artifact_id = require_safe_id(artifact_id, kind="artifact")
        artifact = find_artifact(path, artifact_id)
        real = resolve_artifact_file(config, artifact)
        return FileResponse(real, filename=real.name)

    return router
