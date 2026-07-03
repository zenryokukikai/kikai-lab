"""Script-bundle upload: a raw tar body becomes an immutable script bundle.

The tar must carry a ``kikai_bundle.json`` manifest at its root declaring the
entrypoints map (source-relative argv; the create machinery rewrites them into
``script_bundles/<id>/root/...`` exactly like the CLI form). Extraction is
fail-closed: absolute paths, ``..`` components, links and non-file members are
rejected before anything touches disk. Bundles are immutable — a re-upload with
identical content is ``already_exists``; different content is a 409.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from kikai_lab.envelope import next_action
from kikai_lab.operation import (
    OperationError,
    create_script_bundle,
    should_skip_bundle_file,
)
from kikai_lab.server.app import envelope_response
from kikai_lab.server.registry import (
    WRITE_LOCK,
    ServerConfig,
    require_active_project,
    require_project,
    require_safe_id,
)

BUNDLE_MANIFEST_NAME = "kikai_bundle.json"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def safe_extract_tar(body: bytes, destination: Path) -> None:
    """Extract a (possibly gzipped) tar, rejecting every escape vector explicitly."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:*")
    except tarfile.TarError as exc:
        raise OperationError(
            "script_bundle.upload_invalid",
            "request body is not a readable tar archive",
            {"tar_error": type(exc).__name__},
        ) from exc
    with archive:
        for member in archive.getmembers():
            name = member.name
            parts = Path(name).parts
            # Name checks apply to EVERY member (directories included): on Pythons
            # without the tar data filter, the plain-extractall fallback relies on this.
            if Path(name).is_absolute() or ".." in parts or name.startswith("/"):
                raise OperationError(
                    "script_bundle.upload_member_invalid",
                    "tar member names must be plain relative paths",
                    {"member": name},
                )
            if member.isdir():
                continue
            if not member.isfile():
                raise OperationError(
                    "script_bundle.upload_member_invalid",
                    "tar members must be plain relative files (no links/devices/escapes)",
                    {"member": name},
                )
        try:
            archive.extractall(destination, filter="data")
        except TypeError:  # Python without the tar filter API
            archive.extractall(destination)  # members were validated above
        except tarfile.TarError as exc:
            raise OperationError(
                "script_bundle.upload_member_invalid",
                "tar extraction was rejected by the data filter",
                {"tar_error": type(exc).__name__},
            ) from exc


def read_upload_manifest(source_root: Path) -> dict[str, list[str]]:
    manifest_path = source_root / BUNDLE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise OperationError(
            "script_bundle.upload_manifest_invalid",
            f"the tar must contain {BUNDLE_MANIFEST_NAME} at its root "
            '(e.g. {"entrypoints": {"train": {"argv": ["python", "train.py"]}}})',
            {},
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OperationError(
            "script_bundle.upload_manifest_invalid",
            f"{BUNDLE_MANIFEST_NAME} is not parseable JSON",
            {},
        ) from exc
    entrypoints_raw = manifest.get("entrypoints")
    if not isinstance(entrypoints_raw, dict) or not entrypoints_raw:
        raise OperationError(
            "script_bundle.upload_manifest_invalid",
            f"{BUNDLE_MANIFEST_NAME} must declare a non-empty entrypoints object",
            {},
        )
    entrypoints: dict[str, list[str]] = {}
    for name, spec in entrypoints_raw.items():
        argv = spec.get("argv") if isinstance(spec, dict) else None
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or not argv:
            raise OperationError(
                "script_bundle.upload_manifest_invalid",
                f"entrypoint '{name}' must declare a non-empty string argv list",
                {"entrypoint": name},
            )
        entrypoints[name] = argv
    manifest_path.unlink()  # the manifest describes the bundle; it is not part of it
    return entrypoints


def bundle_content_signature(bundle_manifest: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (item.get("path", ""), item.get("sha256", ""))
        for item in bundle_manifest.get("files", [])
        if isinstance(item, dict)
    }


def upload_file_list(source_root: Path) -> list[Path]:
    """Relative files that will actually land in the bundle — the same exclusions
    create_script_bundle applies, so idempotency signatures agree with reality
    (a .pyc in the tar must not break re-upload retry safety)."""
    return [
        p.relative_to(source_root)
        for p in sorted(source_root.rglob("*"))
        if p.is_file() and not should_skip_bundle_file(p.relative_to(source_root))
    ]


def uploaded_content_signature(source_root: Path) -> set[tuple[str, str]]:
    signature: set[tuple[str, str]] = set()
    for relative in upload_file_list(source_root):
        digest = hashlib.sha256((source_root / relative).read_bytes()).hexdigest()
        signature.add((f"root/{relative.as_posix()}", digest))
    return signature


def rewritten_upload_entrypoints(
    source_root: Path, bundle_id: str, entrypoints: dict[str, list[str]]
) -> dict[str, Any]:
    """The stored-manifest form of the uploaded entrypoints (same rewrite
    create_script_bundle applies), for idempotency comparison."""
    return {
        name: {
            "argv": [
                f"script_bundles/{bundle_id}/root/{item}"
                if (source_root / item).is_file() and not Path(item).is_absolute()
                else item
                for item in argv
            ]
        }
        for name, argv in entrypoints.items()
    }


def load_bundle_manifest(project_root: Path, bundle_id: str) -> dict[str, Any]:
    manifest_path = project_root / "script_bundles" / bundle_id / "bundle.json"
    if not manifest_path.is_file():
        raise OperationError(
            "script_bundle.not_found",
            f"no script bundle '{bundle_id}' in project '{project_root.name}'",
            {"project_id": project_root.name, "bundle_id": bundle_id},
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def build_bundles_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["bundles"])

    @router.get("/projects/{project_id}/bundles")
    def bundles_index(project_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        directory = path / "script_bundles"
        bundles = []
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if not (child / "bundle.json").is_file():
                    continue
                try:
                    manifest = load_bundle_manifest(path, child.name)
                except (OperationError, json.JSONDecodeError):
                    bundles.append({"bundle_id": child.name, "_invalid": True})
                    continue
                bundles.append(
                    {
                        "bundle_id": child.name,
                        "entrypoints": sorted((manifest.get("entrypoints") or {}).keys()),
                        "file_count": len(manifest.get("files", [])),
                    }
                )
        return envelope_response(ok=True, data={"bundles": bundles, "total": len(bundles)})

    @router.get("/projects/{project_id}/bundles/{bundle_id}")
    def bundle_detail(project_id: str, bundle_id: str) -> JSONResponse:
        path = require_project(config, project_id)
        bundle_id = require_safe_id(bundle_id, kind="script_bundle")
        return envelope_response(
            ok=True, data={"bundle": load_bundle_manifest(path, bundle_id)}
        )

    @router.put("/projects/{project_id}/bundles/{bundle_id}")
    async def bundle_put(project_id: str, bundle_id: str, request: Request) -> JSONResponse:
        path = require_active_project(config, project_id)
        bundle_id = require_safe_id(bundle_id, kind="script_bundle")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
            raise OperationError(
                "script_bundle.upload_invalid",
                "bundle upload exceeds the size limit",
                {"limit_bytes": MAX_UPLOAD_BYTES, "content_length": int(declared)},
            )
        body = await request.body()
        if len(body) > MAX_UPLOAD_BYTES:
            raise OperationError(
                "script_bundle.upload_invalid",
                "bundle upload exceeds the size limit",
                {"limit_bytes": MAX_UPLOAD_BYTES, "got_bytes": len(body)},
            )
        if not body:
            raise OperationError(
                "script_bundle.upload_invalid",
                "bundle upload body is empty; send a tar archive "
                "(curl --data-binary @bundle.tar -H 'content-type: application/x-tar')",
                {},
            )
        return await run_in_threadpool(_bundle_put_sync, path, bundle_id, body)

    def _bundle_put_sync(path: Path, bundle_id: str, body: bytes) -> JSONResponse:
        with tempfile.TemporaryDirectory(prefix="kikai_bundle_upload_") as tmp:
            source_root = Path(tmp) / "src"
            source_root.mkdir()
            safe_extract_tar(body, source_root)
            entrypoints = read_upload_manifest(source_root)
            file_paths = [p.as_posix() for p in upload_file_list(source_root)]
            # Hash OUTSIDE the lock (a large upload must not stall every write
            # endpoint); existence is re-checked inside.
            uploaded_signature = uploaded_content_signature(source_root)
            uploaded_entrypoints = rewritten_upload_entrypoints(
                source_root, bundle_id, entrypoints
            )
            with WRITE_LOCK:
                require_active_project(config, path.name)
                bundle_dir = path / "script_bundles" / bundle_id
                if bundle_dir.exists():
                    try:
                        existing = load_bundle_manifest(path, bundle_id)
                    except (OperationError, json.JSONDecodeError) as exc:
                        raise OperationError(
                            "script_bundle.create_bundle_exists",
                            "a corrupt bundle directory occupies this id; choose a "
                            "new bundle_id",
                            {"bundle_id": bundle_id},
                        ) from exc
                    same_files = bundle_content_signature(existing) == uploaded_signature
                    # Entrypoints are part of the bundle identity: identical files with
                    # different argv must NOT silently keep the old entrypoints.
                    same_entrypoints = (
                        existing.get("entrypoints") or {}
                    ) == uploaded_entrypoints
                    if same_files and same_entrypoints:
                        return envelope_response(
                            ok=True, data={"bundle_id": bundle_id, "already_exists": True}
                        )
                    diff = []
                    if not same_files:
                        diff.append("files")
                    if not same_entrypoints:
                        diff.append("entrypoints")
                    raise OperationError(
                        "script_bundle.create_bundle_exists",
                        "bundle exists with different content; bundles are immutable — "
                        "upload under a new bundle_id",
                        {"bundle_id": bundle_id, "diff": diff},
                    )
                try:
                    result = create_script_bundle(
                        project_root=path,
                        source_root=source_root,
                        bundle_id=bundle_id,
                        file_paths=file_paths,
                        entrypoints=entrypoints,
                    )
                except OperationError as exc:
                    if exc.code == "script_bundle.create_file_missing":
                        raise OperationError(
                            "script_bundle.upload_invalid",
                            "the tar contains no bundle payload files",
                            {"bundle_id": bundle_id},
                        ) from exc
                    raise
        return envelope_response(
            ok=True,
            data={
                "bundle_id": bundle_id,
                "created": True,
                "entrypoints": result["entrypoints"],
                "file_count": result["file_count"],
            },
            next_actions=[
                next_action(
                    "submit_run",
                    "http_request",
                    "submit a run using this bundle",
                    blocking=False,
                    command=f"POST /v1/projects/{path.name}/runs/{{run_name}}/submit",
                )
            ],
            status_code=201,
        )

    return router
