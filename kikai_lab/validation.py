from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from kikai_lab.envelope import error
from kikai_lab.operation import (
    OperationError,
    load_script_bundle,
    load_source_snapshot,
    resolve_text_ref,
    validate_script_bundle_files,
    validate_source_snapshot_files,
)
from kikai_lab.store import CurrentState


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"expected mapping in {path}")
    return data


def validate_script_bundles(project_root: Path) -> list[dict[str, Any]]:
    bundle_parent = project_root / "script_bundles"
    if not bundle_parent.exists():
        return []
    errors: list[dict[str, Any]] = []
    for bundle_dir in sorted(path for path in bundle_parent.iterdir() if path.is_dir()):
        bundle_id = bundle_dir.name
        try:
            bundle, bundle_root = load_script_bundle(project_root, bundle_id)
            validate_script_bundle_files(bundle, bundle_root, bundle_id)
        except OperationError as exc:
            errors.append(error(exc.code, exc.message, details=exc.details))
    return errors


def validate_source_snapshots(project_root: Path) -> list[dict[str, Any]]:
    snapshot_parent = project_root / "source_snapshots"
    if not snapshot_parent.exists():
        return []
    errors: list[dict[str, Any]] = []
    for snapshot_dir in sorted(path for path in snapshot_parent.iterdir() if path.is_dir()):
        source_snapshot_id = snapshot_dir.name
        try:
            snapshot, snapshot_root = load_source_snapshot(project_root, source_snapshot_id)
            validate_source_snapshot_files(snapshot, snapshot_root, source_snapshot_id)
        except OperationError as exc:
            errors.append(error(exc.code, exc.message, details=exc.details))
    return errors


CODE_MOUNT_TARGET_MARKERS = (
    "/workspace/example_project",
    "/workspace/example_engine",
    "/workspace/example-engine",
    "CONTAINER_EXAMPLE_PROJECT_ROOT",
    "CONTAINER_EXAMPLE_ENGINE_ROOT",
)

LIVE_REPO_SOURCE_MARKERS = (
    "WORKTREE",
    "HOST_EXAMPLE_PROJECT_ROOT",
    "HOST_EXAMPLE_ENGINE_ROOT",
    "EXAMPLE_PROJECT_WORKTREE",
    "EXAMPLE_ENGINE_WORKTREE",
)

KIKAI_SOURCE_SNAPSHOT_KIND = "kikai_managed_source_snapshot"

CANONICAL_DATA_SOURCE_ROLES = frozenset(
    {
        "train_manifest",
        "eval_manifest",
        "face_cache",
        "source_audio",
        "source_video",
        "tts_corpus",
        "initial_checkpoint",
        "resume_checkpoint",
        "teacher_checkpoint",
        "reference_media",
        "preview_audio",
        "metrics_log",
        "status_input",
    }
)

DATA_SOURCE_TYPES = frozenset(
    {
        "dataset_manifest",
        "dataset_directory",
        "cache_directory",
        "media_file",
        "media_directory",
        "checkpoint_file",
        "model_artifact",
        "metrics_log",
        "external_dataset",
        "opaque_input",
    }
)

DATA_SOURCE_STATUSES = frozenset({"active", "deprecated", "blocked"})
DATA_SOURCE_STORAGE_KINDS = frozenset(
    {"host_path", "container_path", "object_uri", "kikai_runtime_path", "artifact_ref"}
)
DATA_SOURCE_IMMUTABILITY_MODES = frozenset({"immutable", "append_only", "mutable_live"})
DATA_SOURCE_INTEGRITY_STRATEGIES = frozenset(
    {
        "file_sha256",
        "directory_manifest_sha256",
        "object_etag",
        "artifact_digest",
        "not_available",
    }
)
DATA_SOURCE_RESERVED_ROLE_EXTENSION_KEYS = frozenset({"role_namespace", "custom_roles"})
DATA_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_data_source_id(data_source_id: Any) -> list[dict[str, Any]]:
    if not isinstance(data_source_id, str) or not data_source_id:
        return [
            data_source_error(
                "data_source.id_invalid",
                "data_source_id must be a non-empty registry id",
                data_source_id=str(data_source_id),
            )
        ]
    if not DATA_SOURCE_ID_RE.fullmatch(data_source_id):
        return [
            data_source_error(
                "data_source.id_invalid",
                "data_source_id must match the registry id pattern and contain no path separators",
                data_source_id=data_source_id,
            )
        ]
    return []


def require_valid_data_source_id(data_source_id: Any) -> str:
    errors = validate_data_source_id(data_source_id)
    if errors:
        first = errors[0]
        raise OperationError(first["code"], first["message"], first.get("details", {}))
    return data_source_id


def data_source_record_path(project_root: Path, data_source_id: str) -> Path:
    valid_id = require_valid_data_source_id(data_source_id)
    data_source_dir = project_root / "data_sources"
    path = data_source_dir / f"{valid_id}.yaml"
    try:
        resolved_path = path.resolve(strict=False)
        resolved_dir = data_source_dir.resolve(strict=False)
    except Exception as exc:
        raise OperationError(
            "data_source.id_invalid",
            "data_source_id could not be resolved to a registry path",
            {"data_source_id": data_source_id, "path": str(path)},
        ) from exc
    if not resolved_path.is_relative_to(resolved_dir):
        raise OperationError(
            "data_source.path_invalid",
            "data source record path resolves outside data_sources",
            {
                "data_source_id": data_source_id,
                "path": str(path),
                "resolved_path": str(resolved_path),
            },
        )
    return path


def is_code_mount_target(target: Any) -> bool:
    if not isinstance(target, str):
        return False
    return any(marker in target for marker in CODE_MOUNT_TARGET_MARKERS)


def is_live_repo_source(source: Any) -> bool:
    if not isinstance(source, str):
        return False
    source_upper = source.upper()
    return any(marker in source_upper for marker in LIVE_REPO_SOURCE_MARKERS)


def validate_container_mount_reproducibility(
    *, container_id: str, container: dict[str, Any], path: Path
) -> list[dict[str, Any]]:
    mounts = container.get("mounts") or []
    if not isinstance(mounts, list):
        return []
    errors: list[dict[str, Any]] = []
    for index, mount in enumerate(mounts):
        if not isinstance(mount, dict):
            continue
        target = mount.get("target")
        if not is_code_mount_target(target):
            continue
        source = mount.get("source")
        source_kind = mount.get("source_kind")
        if is_live_repo_source(source):
            errors.append(
                error(
                    "container.live_repo_mount_forbidden",
                    "code mounts must not point at mutable live repo/worktree sources; "
                    "use a Kikai-managed immutable source snapshot instead",
                    details={
                        "container_id": container_id,
                        "mount_index": index,
                        "source": source,
                        "target": target,
                        "path": str(path),
                    },
                )
            )
            continue
        if source_kind != KIKAI_SOURCE_SNAPSHOT_KIND:
            errors.append(
                error(
                    "container.source_snapshot_required",
                    "code mounts must declare source_kind=kikai_managed_source_snapshot",
                    details={
                        "container_id": container_id,
                        "mount_index": index,
                        "source": source,
                        "target": target,
                        "source_kind": source_kind,
                        "path": str(path),
                    },
                )
            )
            continue
        source_snapshot_id = mount.get("source_snapshot_id")
        if not isinstance(source_snapshot_id, str) or not source_snapshot_id:
            errors.append(
                error(
                    "container.source_snapshot_id_missing",
                    "Kikai-managed source snapshot mounts must declare source_snapshot_id",
                    details={
                        "container_id": container_id,
                        "mount_index": index,
                        "source": source,
                        "target": target,
                        "path": str(path),
                    },
                )
            )
            continue
        try:
            project_root = path.parents[1]
            snapshot, snapshot_root = load_source_snapshot(project_root, source_snapshot_id)
            validate_source_snapshot_files(snapshot, snapshot_root, source_snapshot_id)
        except OperationError as exc:
            errors.append(error(exc.code, exc.message, details=exc.details))
    return errors


def load_data_source(project_root: Path, data_source_id: str) -> dict[str, Any]:
    path = data_source_record_path(project_root, data_source_id)
    if not path.exists():
        raise OperationError(
            "data_source.missing",
            "data source record is missing",
            {"data_source_id": data_source_id, "path": str(path)},
        )
    try:
        record = load_yaml(path)
    except Exception as exc:
        raise OperationError(
            "data_source.invalid",
            "data source record must be a YAML mapping",
            {"data_source_id": data_source_id, "path": str(path)},
        ) from exc
    return record


def data_source_error(
    code: str,
    message: str,
    *,
    data_source_id: str,
    path: Path | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"data_source_id": data_source_id}
    if path is not None:
        payload["path"] = str(path)
    if details:
        payload.update(details)
    return error(code, message, details=payload)


def validate_data_source_storage(
    storage: Any, *, data_source_id: str, path: Path | None
) -> list[dict[str, Any]]:
    if not isinstance(storage, dict):
        return [
            data_source_error(
                "data_source.storage_invalid",
                "data source storage must be an object",
                data_source_id=data_source_id,
                path=path,
                details={"storage": storage},
            )
        ]
    storage_kind = storage.get("storage_kind")
    if storage_kind not in DATA_SOURCE_STORAGE_KINDS:
        return [
            data_source_error(
                "data_source.storage_invalid",
                "data source storage_kind is unsupported",
                data_source_id=data_source_id,
                path=path,
                details={"storage_kind": storage_kind},
            )
        ]
    required_by_kind = {
        "host_path": ("host_ref", "path"),
        "container_path": ("path",),
        "object_uri": ("uri",),
        "kikai_runtime_path": ("path",),
        "artifact_ref": ("artifact_id",),
    }
    missing = [
        key
        for key in required_by_kind[storage_kind]
        if not isinstance(storage.get(key), str) or not storage.get(key)
    ]
    if missing:
        return [
            data_source_error(
                "data_source.storage_invalid",
                "data source storage is missing required fields for storage_kind",
                data_source_id=data_source_id,
                path=path,
                details={"storage_kind": storage_kind, "missing_fields": missing},
            )
        ]
    return []


def validate_data_source_immutability(
    immutability: Any, *, data_source_id: str, path: Path | None, launch_like: bool
) -> list[dict[str, Any]]:
    if not isinstance(immutability, dict):
        return [
            data_source_error(
                "data_source.immutability_invalid",
                "data source immutability must be an object",
                data_source_id=data_source_id,
                path=path,
                details={"immutability": immutability},
            )
        ]
    mode = immutability.get("mode")
    if mode not in DATA_SOURCE_IMMUTABILITY_MODES:
        return [
            data_source_error(
                "data_source.immutability_invalid",
                "data source immutability.mode is unsupported",
                data_source_id=data_source_id,
                path=path,
                details={"mode": mode},
            )
        ]
    if launch_like and mode == "mutable_live":
        return [
            data_source_error(
                "data_source.mutable_live_forbidden",
                "mutable_live data sources are forbidden for launch-like operations",
                data_source_id=data_source_id,
                path=path,
                details={"mode": mode},
            )
        ]
    return []


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_data_source_storage_directory_path(
    *, storage: Any, project_root: Path | None, data_source_id: str
) -> Path:
    if not isinstance(storage, dict):
        raise OperationError(
            "data_source.integrity_unverified",
            "directory_manifest_sha256 integrity cannot be verified without storage metadata",
            {"data_source_id": data_source_id, "strategy": "directory_manifest_sha256"},
        )
    if storage.get("storage_kind") != "host_path":
        raise OperationError(
            "data_source.integrity_unverified",
            "directory_manifest_sha256 integrity requires host_path storage for verification",
            {
                "data_source_id": data_source_id,
                "strategy": "directory_manifest_sha256",
                "storage_kind": storage.get("storage_kind"),
            },
        )
    path_ref = storage.get("path")
    if not isinstance(path_ref, str) or not path_ref:
        raise OperationError(
            "data_source.integrity_unverified",
            "directory_manifest_sha256 integrity cannot be verified without a storage path",
            {
                "data_source_id": data_source_id,
                "strategy": "directory_manifest_sha256",
                "storage_path": path_ref,
            },
        )
    try:
        resolved_ref = resolve_text_ref(path_ref)
    except OperationError as exc:
        raise OperationError(
            "data_source.integrity_unverified",
            "directory_manifest_sha256 integrity storage path could not be resolved",
            {"data_source_id": data_source_id, "strategy": "directory_manifest_sha256", **exc.details},
        ) from exc
    candidate = Path(resolved_ref)
    if candidate.is_absolute():
        actual_path = candidate
    else:
        if project_root is None:
            raise OperationError(
                "data_source.integrity_unverified",
                "relative directory_manifest_sha256 storage paths require a project_root",
                {
                    "data_source_id": data_source_id,
                    "strategy": "directory_manifest_sha256",
                    "storage_path": path_ref,
                    "resolved_path": resolved_ref,
                },
            )
        actual_path = project_root / candidate
        project_root_resolved = project_root.resolve(strict=False)
        actual_path_resolved = actual_path.resolve(strict=False)
        if not actual_path_resolved.is_relative_to(project_root_resolved):
            raise OperationError(
                "data_source.integrity_unverified",
                "relative directory_manifest_sha256 storage path resolves outside project_root",
                {
                    "data_source_id": data_source_id,
                    "strategy": "directory_manifest_sha256",
                    "storage_path": path_ref,
                    "resolved_path": str(actual_path_resolved),
                    "project_root": str(project_root_resolved),
                },
            )
        actual_path = actual_path_resolved
    if not actual_path.is_dir():
        raise OperationError(
            "data_source.integrity_unverified",
            "directory_manifest_sha256 storage directory is missing",
            {
                "data_source_id": data_source_id,
                "strategy": "directory_manifest_sha256",
                "storage_path": path_ref,
                "resolved_path": str(actual_path),
            },
        )
    return actual_path


def compute_directory_manifest_sha256(root: Path, *, data_source_id: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    file_count = 0
    directory_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise OperationError(
                "data_source.directory_manifest_unverified",
                "directory_manifest_sha256 does not support symlinks",
                {"data_source_id": data_source_id, "path": str(path), "relative_path": relative_path},
            )
        if path.is_dir():
            entries.append({"path": relative_path, "type": "directory"})
            directory_count += 1
            continue
        if path.is_file():
            entries.append(
                {
                    "path": relative_path,
                    "type": "file",
                    "size": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            )
            file_count += 1
            continue
        raise OperationError(
            "data_source.directory_manifest_unverified",
            "directory_manifest_sha256 supports only regular files and directories",
            {"data_source_id": data_source_id, "path": str(path), "relative_path": relative_path},
        )
    manifest = {"schema_version": 1, "entries": entries}
    digest = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "sha256": digest,
        "file_count": file_count,
        "directory_count": directory_count,
        "manifest": manifest,
    }


def resolve_data_source_storage_file_path(
    *, storage: Any, project_root: Path | None, data_source_id: str
) -> Path:
    if not isinstance(storage, dict):
        raise OperationError(
            "data_source.integrity_unverified",
            "file_sha256 integrity cannot be verified without storage metadata",
            {"data_source_id": data_source_id, "strategy": "file_sha256"},
        )
    if storage.get("storage_kind") != "host_path":
        raise OperationError(
            "data_source.integrity_unverified",
            "file_sha256 integrity requires host_path storage for verification",
            {
                "data_source_id": data_source_id,
                "strategy": "file_sha256",
                "storage_kind": storage.get("storage_kind"),
            },
        )
    path_ref = storage.get("path")
    if not isinstance(path_ref, str) or not path_ref:
        raise OperationError(
            "data_source.integrity_unverified",
            "file_sha256 integrity cannot be verified without a storage path",
            {
                "data_source_id": data_source_id,
                "strategy": "file_sha256",
                "storage_path": path_ref,
            },
        )
    try:
        resolved_ref = resolve_text_ref(path_ref)
    except OperationError as exc:
        raise OperationError(
            "data_source.integrity_unverified",
            "file_sha256 integrity storage path could not be resolved",
            {"data_source_id": data_source_id, "strategy": "file_sha256", **exc.details},
        ) from exc
    candidate = Path(resolved_ref)
    if candidate.is_absolute():
        actual_path = candidate
    else:
        if project_root is None:
            raise OperationError(
                "data_source.integrity_unverified",
                "relative file_sha256 storage paths require a project_root",
                {
                    "data_source_id": data_source_id,
                    "strategy": "file_sha256",
                    "storage_path": path_ref,
                    "resolved_path": resolved_ref,
                },
            )
        actual_path = project_root / candidate
        project_root_resolved = project_root.resolve(strict=False)
        actual_path_resolved = actual_path.resolve(strict=False)
        if not actual_path_resolved.is_relative_to(project_root_resolved):
            raise OperationError(
                "data_source.integrity_unverified",
                "relative file_sha256 storage path resolves outside project_root",
                {
                    "data_source_id": data_source_id,
                    "strategy": "file_sha256",
                    "storage_path": path_ref,
                    "resolved_path": str(actual_path_resolved),
                    "project_root": str(project_root_resolved),
                },
            )
        actual_path = actual_path_resolved
    if not actual_path.is_file():
        raise OperationError(
            "data_source.integrity_unverified",
            "file_sha256 integrity storage file is missing",
            {
                "data_source_id": data_source_id,
                "strategy": "file_sha256",
                "storage_path": path_ref,
                "resolved_path": str(actual_path),
            },
        )
    return actual_path


def verify_file_sha256_integrity(
    integrity: dict[str, Any],
    *,
    storage: Any,
    project_root: Path | None,
    data_source_id: str,
    path: Path | None,
) -> list[dict[str, Any]]:
    sha256 = integrity.get("sha256")
    try:
        actual_path = resolve_data_source_storage_file_path(
            storage=storage, project_root=project_root, data_source_id=data_source_id
        )
    except OperationError as exc:
        return [
            data_source_error(
                exc.code,
                exc.message,
                data_source_id=data_source_id,
                path=path,
                details=exc.details,
            )
        ]
    actual_sha256 = file_sha256(actual_path)
    if actual_sha256 != sha256:
        return [
            data_source_error(
                "data_source.integrity_unverified",
                "file_sha256 integrity does not match the current storage file",
                data_source_id=data_source_id,
                path=path,
                details={
                    "strategy": "file_sha256",
                    "storage_path": storage.get("path") if isinstance(storage, dict) else None,
                    "resolved_path": str(actual_path),
                    "expected_sha256": sha256,
                    "actual_sha256": actual_sha256,
                },
            )
        ]
    return []


def verify_directory_manifest_integrity(
    integrity: dict[str, Any],
    *,
    storage: Any,
    project_root: Path | None,
    data_source_id: str,
    path: Path | None,
) -> list[dict[str, Any]]:
    sha256 = integrity.get("sha256")
    try:
        actual_path = resolve_data_source_storage_directory_path(
            storage=storage, project_root=project_root, data_source_id=data_source_id
        )
        manifest = compute_directory_manifest_sha256(actual_path, data_source_id=data_source_id)
    except OperationError as exc:
        return [
            data_source_error(
                exc.code,
                exc.message,
                data_source_id=data_source_id,
                path=path,
                details=exc.details,
            )
        ]
    actual_sha256 = manifest["sha256"]
    if actual_sha256 != sha256:
        return [
            data_source_error(
                "data_source.integrity_unverified",
                "directory_manifest_sha256 integrity does not match the current storage directory",
                data_source_id=data_source_id,
                path=path,
                details={
                    "strategy": "directory_manifest_sha256",
                    "storage_path": storage.get("path") if isinstance(storage, dict) else None,
                    "resolved_path": str(actual_path),
                    "expected_sha256": sha256,
                    "actual_sha256": actual_sha256,
                },
            )
        ]
    return []


def validate_data_source_integrity(
    integrity: Any,
    *,
    data_source_id: str,
    path: Path | None,
    storage: Any = None,
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
    if integrity is None:
        return []
    if not isinstance(integrity, dict):
        return [
            data_source_error(
                "data_source.integrity_invalid",
                "data source integrity must be an object",
                data_source_id=data_source_id,
                path=path,
                details={"integrity": integrity},
            )
        ]
    strategy = integrity.get("strategy")
    if strategy not in DATA_SOURCE_INTEGRITY_STRATEGIES:
        return [
            data_source_error(
                "data_source.integrity_invalid",
                "data source integrity.strategy is unsupported",
                data_source_id=data_source_id,
                path=path,
                details={"strategy": strategy},
            )
        ]
    if strategy == "file_sha256":
        sha256 = integrity.get("sha256")
        calculated_by = integrity.get("calculated_by")
        calculated_at = integrity.get("calculated_at")
        if calculated_by != "kikai_lab.data-source.create-file":
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "file_sha256 integrity must be calculated by Kikai Lab registration",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy, "calculated_by": calculated_by},
                )
            ]
        if not isinstance(calculated_at, str) or not calculated_at:
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "file_sha256 integrity requires calculated_at from Kikai Lab registration",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy, "calculated_at": calculated_at},
                )
            ]
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "file_sha256 integrity requires a lowercase 64-hex sha256",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy, "sha256": sha256},
                )
            ]
        verification_errors = verify_file_sha256_integrity(
            integrity,
            storage=storage,
            project_root=project_root,
            data_source_id=data_source_id,
            path=path,
        )
        if verification_errors:
            return verification_errors
    if strategy == "directory_manifest_sha256":
        sha256 = integrity.get("sha256")
        calculated_by = integrity.get("calculated_by")
        calculated_at = integrity.get("calculated_at")
        if calculated_by != "kikai_lab.data-source.create-directory-manifest":
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "directory_manifest_sha256 integrity must be calculated by Kikai Lab",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy, "calculated_by": calculated_by},
                )
            ]
        if not isinstance(calculated_at, str) or not calculated_at:
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "directory_manifest_sha256 integrity requires calculated_at from Kikai Lab",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy, "calculated_at": calculated_at},
                )
            ]
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "directory_manifest_sha256 integrity requires a lowercase 64-hex sha256",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy, "sha256": sha256},
                )
            ]
        verification_errors = verify_directory_manifest_integrity(
            integrity,
            storage=storage,
            project_root=project_root,
            data_source_id=data_source_id,
            path=path,
        )
        if verification_errors:
            return verification_errors
    if strategy == "not_available":
        reason = integrity.get("reason")
        if not isinstance(reason, str) or not reason:
            return [
                data_source_error(
                    "data_source.integrity_invalid",
                    "not_available integrity requires an explicit reason",
                    data_source_id=data_source_id,
                    path=path,
                    details={"strategy": strategy},
                )
            ]
    return []


def validate_data_source_roles(
    record: dict[str, Any], *, data_source_id: str, path: Path | None, role: str | None
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if role is not None and role not in CANONICAL_DATA_SOURCE_ROLES:
        errors.append(
            data_source_error(
                "data_source.role_unknown",
                "data source ref role is not in the canonical v1 vocabulary",
                data_source_id=data_source_id,
                path=path,
                details={"role": role},
            )
        )
        return errors
    contract = record.get("contract")
    if contract is None:
        return errors
    if not isinstance(contract, dict):
        errors.append(
            data_source_error(
                "data_source.invalid",
                "data source contract must be an object",
                data_source_id=data_source_id,
                path=path,
                details={"contract": contract},
            )
        )
        return errors
    role_compatibility = contract.get("role_compatibility")
    if role_compatibility is None:
        return errors
    if not isinstance(role_compatibility, list) or not all(
        isinstance(item, str) for item in role_compatibility
    ):
        errors.append(
            data_source_error(
                "data_source.role_unknown",
                "data source role_compatibility must be a list of canonical roles",
                data_source_id=data_source_id,
                path=path,
                details={"role_compatibility": role_compatibility},
            )
        )
        return errors
    unknown_roles = sorted(set(role_compatibility) - CANONICAL_DATA_SOURCE_ROLES)
    if unknown_roles:
        errors.append(
            data_source_error(
                "data_source.role_unknown",
                "data source role_compatibility contains unknown roles",
                data_source_id=data_source_id,
                path=path,
                details={"roles": unknown_roles},
            )
        )
    if role is not None and role not in role_compatibility:
        errors.append(
            data_source_error(
                "data_source.role_incompatible",
                "requested data source role is not listed in role_compatibility",
                data_source_id=data_source_id,
                path=path,
                details={"role": role, "role_compatibility": role_compatibility},
            )
        )
    return errors


def validate_data_source_record(
    project_root: Path,
    data_source_id: str,
    record: dict[str, Any],
    *,
    role: str | None = None,
    launch_like: bool = False,
) -> list[dict[str, Any]]:
    id_errors = validate_data_source_id(data_source_id)
    path = None if id_errors else data_source_record_path(project_root, data_source_id)
    errors: list[dict[str, Any]] = [*id_errors]
    if not isinstance(record, dict):
        return [
            data_source_error(
                "data_source.invalid",
                "data source record must be a mapping",
                data_source_id=data_source_id,
                path=path,
                details={"record": record},
            )
        ]
    forbidden_keys = sorted(DATA_SOURCE_RESERVED_ROLE_EXTENSION_KEYS & set(record))
    if forbidden_keys:
        errors.append(
            data_source_error(
                "data_source.invalid",
                "role_namespace/custom_roles are reserved for a future design and forbidden in v1",
                data_source_id=data_source_id,
                path=path,
                details={"forbidden_keys": forbidden_keys},
            )
        )
    if record.get("kind") != "kikai_data_source":
        errors.append(
            data_source_error(
                "data_source.kind_invalid",
                "data source kind must be kikai_data_source",
                data_source_id=data_source_id,
                path=path,
                details={"kind": record.get("kind")},
            )
        )
    actual_id = record.get("data_source_id")
    if actual_id != data_source_id:
        errors.append(
            data_source_error(
                "data_source.id_mismatch",
                "data_source_id must match filename stem",
                data_source_id=data_source_id,
                path=path,
                details={"actual_data_source_id": actual_id},
            )
        )
    status = record.get("status")
    if status not in DATA_SOURCE_STATUSES or status == "blocked":
        errors.append(
            data_source_error(
                "data_source.status_invalid",
                "data source status must be active/deprecated and not blocked",
                data_source_id=data_source_id,
                path=path,
                details={"status": status},
            )
        )
    source_type = record.get("source_type")
    if source_type not in DATA_SOURCE_TYPES:
        errors.append(
            data_source_error(
                "data_source.source_type_invalid",
                "data source source_type is unsupported",
                data_source_id=data_source_id,
                path=path,
                details={"source_type": source_type},
            )
        )
    errors.extend(
        validate_data_source_storage(
            record.get("storage"), data_source_id=data_source_id, path=path
        )
    )
    errors.extend(
        validate_data_source_immutability(
            record.get("immutability"),
            data_source_id=data_source_id,
            path=path,
            launch_like=launch_like,
        )
    )
    errors.extend(
        validate_data_source_integrity(
            record.get("integrity"),
            data_source_id=data_source_id,
            path=path,
            storage=record.get("storage"),
            project_root=project_root,
        )
    )
    errors.extend(
        validate_data_source_roles(record, data_source_id=data_source_id, path=path, role=role)
    )
    provenance = record.get("provenance") or {}
    if isinstance(provenance, dict):
        upstream_ids = provenance.get("upstream_data_source_ids") or []
        if not isinstance(upstream_ids, list):
            errors.append(
                data_source_error(
                    "data_source.invalid",
                    "data source provenance.upstream_data_source_ids must be a list",
                    data_source_id=data_source_id,
                    path=path,
                    details={"upstream_data_source_ids": upstream_ids},
                )
            )
        else:
            for upstream_id in upstream_ids:
                for item in validate_data_source_id(upstream_id):
                    errors.append(
                        data_source_error(
                            item["code"],
                            item["message"],
                            data_source_id=data_source_id,
                            path=path,
                            details={
                                **item.get("details", {}),
                                "upstream_data_source_id": upstream_id,
                            },
                        )
                    )
            if actual_id in upstream_ids:
                errors.append(
                    data_source_error(
                        "data_source.lineage_cycle",
                        "data source provenance must not reference itself",
                        data_source_id=data_source_id,
                        path=path,
                        details={"upstream_data_source_ids": upstream_ids},
                    )
                )
    return errors


def validate_data_source_lineage_graph(project_root: Path) -> list[dict[str, Any]]:
    data_source_dir = project_root / "data_sources"
    if not data_source_dir.exists():
        return []
    graph: dict[str, list[str]] = {}
    for path in sorted(data_source_dir.glob("*.yaml")):
        data_source_id = path.stem
        try:
            record_path = data_source_record_path(project_root, data_source_id)
            record = load_yaml(record_path)
        except Exception:
            continue
        provenance = record.get("provenance") or {}
        upstream_ids = (
            provenance.get("upstream_data_source_ids") if isinstance(provenance, dict) else []
        )
        graph[data_source_id] = [str(item) for item in upstream_ids or [] if isinstance(item, str)]
    errors: list[dict[str, Any]] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = trail[trail.index(node) :] + [node] if node in trail else [node, node]
            errors.append(
                data_source_error(
                    "data_source.lineage_cycle",
                    "data source provenance graph contains a cycle",
                    data_source_id=node,
                    path=data_source_dir / f"{node}.yaml",
                    details={"cycle": cycle},
                )
            )
            return
        visiting.add(node)
        for upstream in graph.get(node, []):
            if upstream in graph:
                visit(upstream, [*trail, upstream])
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node, [node])
    return errors


def validate_data_sources(project_root: Path) -> list[dict[str, Any]]:
    data_source_dir = project_root / "data_sources"
    if not data_source_dir.exists():
        return []
    errors: list[dict[str, Any]] = []
    for path in sorted(data_source_dir.glob("*.yaml")):
        data_source_id = path.stem
        try:
            record_path = data_source_record_path(project_root, data_source_id)
            record = load_yaml(record_path)
        except OperationError as exc:
            errors.append(error(exc.code, exc.message, details=exc.details))
            continue
        except Exception:
            errors.append(
                data_source_error(
                    "data_source.invalid",
                    "data source record must be a YAML mapping",
                    data_source_id=data_source_id,
                    path=path,
                )
            )
            continue
        errors.extend(validate_data_source_record(project_root, data_source_id, record))
    errors.extend(validate_data_source_lineage_graph(project_root))
    return errors


def validate_data_source_refs(
    *,
    project_root: Path,
    refs: Any,
    owner_kind: str,
    owner_id: str,
    owner_path: Path,
    launch_like: bool = False,
    fresh_no_resume: bool = False,
) -> list[dict[str, Any]]:
    if refs is None:
        return []
    invalid_code = f"{owner_kind}.data_source_ref_invalid"
    if not isinstance(refs, list):
        return [
            error(
                invalid_code,
                "data_source_refs must be a list",
                details={owner_kind: owner_id, "path": str(owner_path), "data_source_refs": refs},
            )
        ]
    errors: list[dict[str, Any]] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            errors.append(
                error(
                    invalid_code,
                    "data_source_refs entries must be objects",
                    details={owner_kind: owner_id, "path": str(owner_path), "index": index},
                )
            )
            continue
        role = ref.get("role")
        data_source_id = ref.get("data_source_id")
        required = ref.get("required", True)
        ref_details = {
            owner_kind: owner_id,
            "path": str(owner_path),
            "index": index,
            "role": role,
            "data_source_id": data_source_id,
            "required": required,
        }
        if not isinstance(role, str) or not role:
            errors.append(
                error(
                    "data_source.role_missing",
                    "data source refs must declare a role",
                    details=ref_details,
                )
            )
            continue
        if not isinstance(required, bool):
            errors.append(
                error(
                    invalid_code,
                    "data_source_refs required must be a boolean when present",
                    details=ref_details,
                )
            )
            continue
        if role not in CANONICAL_DATA_SOURCE_ROLES:
            errors.append(
                error(
                    "data_source.role_unknown",
                    "data source ref role is not in the canonical v1 vocabulary",
                    details=ref_details,
                )
            )
            continue
        if required is True and (not isinstance(data_source_id, str) or not data_source_id):
            errors.append(
                error(
                    "data_source.required_missing",
                    "required data source refs must declare data_source_id",
                    details=ref_details,
                )
            )
            continue
        if required is False and data_source_id is None:
            continue
        if not isinstance(data_source_id, str) or not data_source_id:
            errors.append(
                error(
                    invalid_code,
                    "data source refs must use a string data_source_id or required=false with null",
                    details=ref_details,
                )
            )
            continue
        id_errors = validate_data_source_id(data_source_id)
        if id_errors:
            errors.extend(
                error(
                    item["code"],
                    item["message"],
                    details={**item.get("details", {}), **ref_details},
                )
                for item in id_errors
            )
            continue
        if (
            fresh_no_resume
            and required is True
            and role in {"initial_checkpoint", "resume_checkpoint"}
        ):
            errors.append(
                error(
                    "run.data_source_ref_invalid",
                    "fresh_no_resume runs must not require an initial/resume checkpoint "
                    "data source",
                    details=ref_details,
                )
            )
        try:
            record = load_data_source(project_root, data_source_id)
        except OperationError as exc:
            errors.append(error(exc.code, exc.message, details={**exc.details, **ref_details}))
            continue
        errors.extend(
            validate_data_source_record(
                project_root,
                data_source_id,
                record,
                role=role,
                launch_like=launch_like,
            )
        )
    return errors


def validate_registry_links(project_root: Path, state: CurrentState) -> list[dict[str, Any]]:
    current = state.current
    errors: list[dict[str, Any]] = []

    experiment_id = str(current.get("current_experiment_id", ""))
    run_name = str(current.get("current_run_name", ""))

    experiment_path = project_root / "experiments" / f"{experiment_id}.yaml"
    run_path = project_root / "runs" / f"{run_name}.yaml"

    if not experiment_path.exists():
        errors.append(
            error(
                "current.experiment_missing",
                f"current experiment record is missing: {experiment_id}",
                details={"experiment_id": experiment_id, "path": str(experiment_path)},
            )
        )
        return errors
    if not run_path.exists():
        errors.append(
            error(
                "current.run_missing",
                f"current run record is missing: {run_name}",
                details={"run_name": run_name, "path": str(run_path)},
            )
        )
        return errors

    experiment = load_yaml(experiment_path)
    run = load_yaml(run_path)

    external_ref_ids = {
        str(ref.get("id"))
        for ref in experiment.get("external_refs", [])
        if isinstance(ref, dict) and ref.get("id")
    }
    # A must-read is satisfied by an experiment external_ref (legacy) OR by a decision
    # managed inside this project (decisions/<id>.yaml). kikai-lab owns the decision log;
    # no external system is required.
    from kikai_lab.decision import (
        decision_ids as _internal_decision_ids,  # deferred: avoid import cycle
    )

    satisfying_ids = external_ref_ids | _internal_decision_ids(project_root)
    must_read = [str(ref_id) for ref_id in current.get("must_read_external_ref_ids", [])]
    missing_must_read = sorted(ref_id for ref_id in must_read if ref_id not in satisfying_ids)
    if missing_must_read:
        errors.append(
            error(
                "current.must_read_not_in_external_refs",
                "current must_read_external_ref_ids contains IDs not listed in any "
                "experiment external_refs or project decision record",
                details={"missing_ids": missing_must_read},
            )
        )

    if run.get("experiment_id") != experiment_id:
        errors.append(
            error(
                "current.run_experiment_mismatch",
                "current run does not belong to current experiment",
                details={
                    "run_experiment_id": run.get("experiment_id"),
                    "current_experiment_id": experiment_id,
                },
            )
        )

    current_model_arch = current.get("current_model_arch")
    run_model_arch = run.get("model_arch")
    if run_model_arch != current_model_arch:
        errors.append(
            error(
                "current.model_arch_mismatch",
                "current model_arch does not match run record",
                details={
                    "current_model_arch": current_model_arch,
                    "run_model_arch": run_model_arch,
                },
            )
        )

    current_checkpoint = current.get("current_checkpoint")
    run_checkpoint = (
        run.get("checkpoint", {}).get("latest") if isinstance(run.get("checkpoint"), dict) else None
    )
    if run_checkpoint != current_checkpoint:
        errors.append(
            error(
                "current.checkpoint_mismatch",
                "current checkpoint does not match run record latest checkpoint",
                details={
                    "current_checkpoint": current_checkpoint,
                    "run_checkpoint": run_checkpoint,
                },
            )
        )

    errors.extend(
        validate_data_source_refs(
            project_root=project_root,
            refs=run.get("data_source_refs"),
            owner_kind="run",
            owner_id=run_name,
            owner_path=run_path,
            fresh_no_resume=run.get("fresh_no_resume") is True,
        )
    )

    for container_id in current.get("required_container_ids", []) or []:
        container_id = str(container_id)
        container_path = project_root / "containers" / f"{container_id}.yaml"
        if not container_path.exists():
            errors.append(
                error(
                    "current.container_missing",
                    "current required container definition is missing",
                    details={"container_id": container_id, "path": str(container_path)},
                )
            )
            continue
        container = load_yaml(container_path)
        if container.get("kind") != "docker_container":
            errors.append(
                error(
                    "container.kind_invalid",
                    "container record kind must be docker_container",
                    details={"container_id": container_id, "kind": container.get("kind")},
                )
            )
        if container.get("container_id") != container_id:
            errors.append(
                error(
                    "container.id_mismatch",
                    "container record id must match current required container id",
                    details={
                        "expected_container_id": container_id,
                        "actual_container_id": container.get("container_id"),
                        "path": str(container_path),
                    },
                )
            )
        docker_obj = container.get("docker")
        docker = docker_obj if isinstance(docker_obj, dict) else {}
        missing_fields = [
            field
            for field, value in {
                "docker.name": docker.get("name"),
                "docker.image": docker.get("image"),
            }.items()
            if not value
        ]
        if missing_fields:
            errors.append(
                error(
                    "container.docker_identity_missing",
                    "container record must define docker.name and docker.image",
                    details={"container_id": container_id, "missing_fields": missing_fields},
                )
            )
        errors.extend(
            validate_container_mount_reproducibility(
                container_id=container_id,
                container=container,
                path=container_path,
            )
        )

    errors.extend(validate_data_sources(project_root))
    errors.extend(validate_source_snapshots(project_root))
    errors.extend(validate_script_bundles(project_root))

    return errors
