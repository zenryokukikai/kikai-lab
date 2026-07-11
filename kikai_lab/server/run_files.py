"""Run-dir inspection plane: list files and fetch small text artifacts, ssh-free.

Agents used to ssh into the training host to answer "which checkpoints exist",
"what did qc/step012000 render", or "tail metrics.jsonl" — unusable wherever ssh
is not available. These endpoints expose the SAME daemon-local run_dir the server
already reads for /metrics and /detail checkpoints, with the same trust model:
the run_dir comes from server-side records (managed_run / submission), is
contained by ``run_dir_roots`` when configured, and every client-supplied path is
sandboxed to it — traversal and symlink escapes are refused, never followed.

Content fetches are for SMALL TEXT/JSON files (progress records, metrics tails,
QC summaries). Binary files return metadata only; media bytes stay on the
artifact ledger's ``/content`` route, which is fail-closed behind content roots.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from kikai_lab.operation import OperationError
from kikai_lab.server.app import envelope_response
from kikai_lab.server.registry import ServerConfig, require_project, require_safe_id
from kikai_lab.server.runs import (
    load_managed_run_optional,
    require_run_record,
    resolved_run_dir,
)

MAX_LIST_ENTRIES = 5000
MAX_FILE_BYTES = 1_048_576  # hard cap; default stays far smaller


def require_run_dir(config: ServerConfig, project_root: Path, run_name: str) -> Path:
    require_run_record(project_root, run_name)
    managed = load_managed_run_optional(project_root, run_name)
    run_dir = resolved_run_dir(project_root, run_name, managed, config)
    if run_dir is None:
        raise OperationError(
            "run.run_dir_missing",
            "run declares no resolvable run_dir (managed_run or submission required)",
            {"run_name": run_name},
        )
    if not run_dir.is_dir():
        raise OperationError(
            "run.run_dir_missing",
            "run_dir does not exist on this host (yet)",
            {"run_name": run_name},
        )
    return run_dir


def resolve_in_run_dir(run_dir: Path, rel_path: str) -> Path:
    """Turn a client-supplied relative path into a real path INSIDE run_dir.

    Fail-closed twice: lexically (absolute paths and ``..`` segments are refused
    before touching the filesystem) and physically (the resolved real path must
    stay under the resolved run_dir, so a symlink planted inside the run_dir can
    never leak files outside it)."""
    rel = rel_path.strip()
    parts = Path(rel).parts if rel else ()
    if Path(rel).is_absolute() or any(part in ("..",) for part in parts):
        raise OperationError(
            "run.artifact_path_forbidden",
            "path must be relative to the run_dir and must not traverse upward",
            {"path": rel_path},
        )
    try:
        base = run_dir.resolve(strict=True)
        real = (run_dir / rel).resolve(strict=True)
    except OSError as exc:
        raise OperationError(
            "run.artifact_path_not_found",
            "no such file or directory under the run_dir",
            {"path": rel_path},
        ) from exc
    if os.path.commonpath([str(real), str(base)]) != str(base):
        raise OperationError(
            "run.artifact_path_forbidden",
            "path escapes the run_dir (symlink or traversal)",
            {"path": rel_path},
        )
    return real


def entry_summary(base: Path, item: Path) -> dict[str, Any] | None:
    try:
        stat = item.lstat()
    except OSError:
        return None
    is_dir = item.is_dir()
    return {
        "path": item.relative_to(base).as_posix(),
        "is_dir": is_dir,
        "size": None if is_dir else stat.st_size,
        "mtime": stat.st_mtime,
    }


def list_entries(base: Path, start: Path, depth: int) -> list[dict[str, Any]]:
    """Breadth-first listing under ``start``, ``depth`` levels deep, name-sorted
    per directory, bounded by MAX_LIST_ENTRIES (checkpoint dirs stay small; QC
    dirs can sprawl — a bound beats an unbounded walk on the request thread)."""
    entries: list[dict[str, Any]] = []
    frontier = [start]
    for _ in range(depth):
        next_frontier: list[Path] = []
        for directory in frontier:
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for child in children:
                if len(entries) >= MAX_LIST_ENTRIES:
                    return entries
                summary = entry_summary(base, child)
                if summary is None:
                    continue
                entries.append(summary)
                # never descend through a symlinked dir: resolve_in_run_dir guards
                # single fetches, but a walk must not follow links out of the tree
                if summary["is_dir"] and not child.is_symlink():
                    next_frontier.append(child)
        frontier = next_frontier
    return entries


def build_run_files_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["run-files"])

    @router.get("/projects/{project_id}/runs/{run_name}/artifacts")
    def run_artifacts_index(
        project_id: str,
        run_name: str,
        path: str = Query("", description="relative path inside the run_dir"),
        depth: int = Query(1, ge=1, le=3),
        limit: int = Query(500, ge=1, le=MAX_LIST_ENTRIES),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        project_root = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        run_dir = require_run_dir(config, project_root, run_name)
        start = resolve_in_run_dir(run_dir, path)
        if not start.is_dir():
            raise OperationError(
                "run.artifact_path_invalid",
                "path is a file; use .../artifacts/file to fetch its content",
                {"path": path},
            )
        entries = list_entries(run_dir, start, depth)
        window = entries[offset : offset + limit]
        return envelope_response(
            ok=True,
            data={
                "run_name": run_name,
                "path": path,
                "depth": depth,
                "entries": window,
                "total": len(entries),
                "offset": offset,
                "limit": limit,
                "truncated": len(entries) >= MAX_LIST_ENTRIES,
            },
        )

    @router.get("/projects/{project_id}/runs/{run_name}/artifacts/file")
    def run_artifact_file(
        project_id: str,
        run_name: str,
        path: str = Query(..., description="relative path inside the run_dir"),
        max_bytes: int = Query(65536, ge=1, le=MAX_FILE_BYTES),
        tail: bool = Query(
            False, description="read the LAST max_bytes instead of the first"
        ),
    ) -> JSONResponse:
        project_root = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        run_dir = require_run_dir(config, project_root, run_name)
        real = resolve_in_run_dir(run_dir, path)
        if not real.is_file():
            raise OperationError(
                "run.artifact_path_invalid",
                "path is a directory; use .../artifacts to list it",
                {"path": path},
            )
        stat = real.stat()
        truncated = stat.st_size > max_bytes
        try:
            with real.open("rb") as f:
                if tail and truncated:
                    f.seek(stat.st_size - max_bytes)
                chunk = f.read(max_bytes)
        except OSError as exc:
            raise OperationError(
                "run.artifact_path_not_found",
                "file became unreadable while serving it",
                {"path": path},
            ) from exc
        meta = {
            "run_name": run_name,
            "path": path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "truncated": truncated,
            "tail": bool(tail and truncated),
        }
        if b"\x00" in chunk:
            # binary: metadata only — media bytes belong to the artifact ledger's
            # fail-closed /content route, not to a JSON text endpoint.
            return envelope_response(
                ok=True, data={**meta, "binary": True, "content": None}
            )
        return envelope_response(
            ok=True,
            data={
                **meta,
                "binary": False,
                # a cut multi-byte char at a chunk boundary must not 500 the fetch
                "content": chunk.decode("utf-8", errors="replace"),
            },
        )

    return router
