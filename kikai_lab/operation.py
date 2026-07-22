from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from kikai_lab.server_config import resolve_registered_value


class OperationError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _operation_format(path: Path) -> str:
    """Serialization format of an operation file, by extension. YAML/TOML author the
    SAME operation object as JSON (containers/experiments are already YAML — ops no
    longer have to be hand-escaped JSON)."""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".toml":
        return "toml"
    return "json"


def _loads_operation(text: str, fmt: str) -> Any:
    if fmt == "yaml":
        import yaml

        return yaml.safe_load(text)
    if fmt == "toml":
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:  # pragma: no cover - older interpreters
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(text)
    return json.loads(text)


def dump_operation_text(operation: dict[str, Any], fmt: str) -> str:
    """Serialize an operation dict back to its source format (used to write the guard
    receipt back into the same file the operator authored)."""
    if fmt == "yaml":
        import yaml

        return yaml.safe_dump(operation, allow_unicode=True, sort_keys=True)
    if fmt == "toml":
        try:
            import tomli_w
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dep
            raise OperationError(
                "operation.toml_writeback_unavailable",
                "writing a guard receipt into a .toml operation requires the 'tomli_w' "
                "package; use .yaml or .json for now, or `pip install tomli_w`",
            ) from exc
        return tomli_w.dumps(operation)
    return json.dumps(operation, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_operation(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise OperationError(
            "operation.file_missing",
            f"operation file does not exist: {path}",
            {"path": str(path)},
        )
    fmt = _operation_format(path)
    try:
        data = _loads_operation(path.read_text(encoding="utf-8"), fmt)
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any parser error uniformly
        raise OperationError(
            "operation.invalid",
            f"operation file could not be parsed as {fmt}: {exc}",
            {"path": str(path), "format": fmt},
        ) from exc
    if not isinstance(data, dict):
        raise OperationError("operation.invalid", "operation must be a mapping/object")
    request = data.get("request")
    if not isinstance(request, dict):
        raise OperationError(
            "operation.request_missing",
            "operation must contain a request object",
        )
    return data


def canonical_request_bytes(operation: dict[str, Any]) -> bytes:
    return json.dumps(
        operation["request"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_sha256(operation: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_request_bytes(operation)).hexdigest()


LAUNCH_LIKE_DATA_SOURCE_ADAPTERS = frozenset(
    {
        "docker_exec",
        "script_bundle_exec",
        "script_bundle_run",
        "operation_sequence",
        "remote_kikai_exec",
        "data_source_create",
    }
)


def create_file_data_source(
    *,
    project_root: Path,
    data_source_id: str,
    source_type: str,
    path_ref: str,
    host_ref: str,
    role_compatibility: list[str],
    summary: str,
    container_mount_path: str | None = None,
    upstream_data_source_ids: list[str] | None = None,
    upstream_source_snapshot_ids: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    from kikai_lab.validation import (
        data_source_record_path,
        resolve_data_source_storage_file_path,
        validate_data_source_record,
    )

    if not data_source_id:
        raise OperationError("data_source.id_missing", "data_source_id is required")
    if not role_compatibility:
        raise OperationError(
            "data_source.role_missing",
            "at least one canonical role is required for data source registration",
            {"data_source_id": data_source_id},
        )
    data_source_dir = project_root / "data_sources"
    record_path = data_source_record_path(project_root, data_source_id)
    if record_path.exists() and not overwrite:
        raise OperationError(
            "data_source.exists",
            "data source record already exists and will not be overwritten",
            {"data_source_id": data_source_id, "path": str(record_path)},
        )
    storage = {
        "storage_kind": "host_path",
        "host_ref": host_ref,
        "path": path_ref,
    }
    if container_mount_path is not None:
        storage["container_mount_path"] = container_mount_path
    try:
        resolved_path = resolve_data_source_storage_file_path(
            storage=storage, project_root=project_root, data_source_id=data_source_id
        )
        file_bytes = resolved_path.read_bytes()
    except OperationError:
        raise
    except Exception as exc:
        raise OperationError(
            "data_source.file_unreadable",
            "data source file path could not be read for Kikai-managed hash calculation",
            {"data_source_id": data_source_id, "path": path_ref},
        ) from exc
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    record = {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": data_source_id,
        "status": "active",
        "summary": summary,
        "source_type": source_type,
        "immutability": {"mode": "immutable", "verified_at": now},
        "storage": storage,
        "integrity": {
            "strategy": "file_sha256",
            "sha256": hashlib.sha256(file_bytes).hexdigest(),
            "calculated_by": "kikai_lab.data-source.create-file",
            "calculated_at": now,
            "verification": "preflight_required",
        },
        "contract": {"role_compatibility": role_compatibility},
        "provenance": {
            "created_by": "kikai data-source create-file",
            "upstream_data_source_ids": upstream_data_source_ids or [],
            "upstream_source_snapshot_ids": upstream_source_snapshot_ids or [],
        },
        "notes": [],
    }
    errors = validate_data_source_record(project_root, data_source_id, record)
    if errors:
        first = errors[0]
        raise OperationError(first["code"], first["message"], first.get("details", {}))
    data_source_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return {"data_source": record, "path": str(record_path), "resolved_path": str(resolved_path)}


def create_directory_data_source(
    *,
    project_root: Path,
    data_source_id: str,
    source_type: str,
    path_ref: str,
    host_ref: str,
    role_compatibility: list[str],
    summary: str,
    container_mount_path: str | None = None,
    upstream_data_source_ids: list[str] | None = None,
    upstream_source_snapshot_ids: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    from kikai_lab.validation import (
        compute_directory_manifest_sha256,
        data_source_record_path,
        resolve_data_source_storage_directory_path,
        validate_data_source_record,
    )

    if not data_source_id:
        raise OperationError("data_source.id_missing", "data_source_id is required")
    if not role_compatibility:
        raise OperationError(
            "data_source.role_missing",
            "at least one canonical role is required for data source registration",
            {"data_source_id": data_source_id},
        )
    data_source_dir = project_root / "data_sources"
    record_path = data_source_record_path(project_root, data_source_id)
    if record_path.exists() and not overwrite:
        raise OperationError(
            "data_source.exists",
            "data source record already exists and will not be overwritten",
            {"data_source_id": data_source_id, "path": str(record_path)},
        )
    storage = {
        "storage_kind": "host_path",
        "host_ref": host_ref,
        "path": path_ref,
    }
    if container_mount_path is not None:
        storage["container_mount_path"] = container_mount_path
    resolved_path = resolve_data_source_storage_directory_path(
        storage=storage, project_root=project_root, data_source_id=data_source_id
    )
    manifest = compute_directory_manifest_sha256(resolved_path, data_source_id=data_source_id)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    record = {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": data_source_id,
        "status": "active",
        "summary": summary,
        "source_type": source_type,
        "immutability": {"mode": "immutable", "verified_at": now},
        "storage": storage,
        "integrity": {
            "strategy": "directory_manifest_sha256",
            "sha256": manifest["sha256"],
            "calculated_by": "kikai_lab.data-source.create-directory-manifest",
            "calculated_at": now,
            "verification": "preflight_required",
            "file_count": manifest["file_count"],
            "directory_count": manifest["directory_count"],
            "manifest_schema_version": manifest["manifest"]["schema_version"],
        },
        "contract": {"role_compatibility": role_compatibility},
        "provenance": {
            "created_by": "kikai data-source create-directory",
            "upstream_data_source_ids": upstream_data_source_ids or [],
            "upstream_source_snapshot_ids": upstream_source_snapshot_ids or [],
        },
        "notes": [],
    }
    errors = validate_data_source_record(project_root, data_source_id, record)
    if errors:
        first = errors[0]
        raise OperationError(first["code"], first["message"], first.get("details", {}))
    data_source_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return {
        "data_source": record,
        "path": str(record_path),
        "resolved_path": str(resolved_path),
        "file_count": manifest["file_count"],
    }


def data_source_ref_preflight_for_request(
    *,
    request: dict[str, Any],
    project_root: Path,
    operation_name: str,
    owner_path: Path,
) -> list[dict[str, Any]]:
    from kikai_lab.validation import (
        compute_directory_manifest_sha256,
        load_data_source,
        resolve_data_source_storage_directory_path,
        resolve_data_source_storage_file_path,
        validate_data_source_refs,
    )

    refs = request.get("data_source_refs")
    if refs is None:
        return []
    launch_like = request.get("adapter") in LAUNCH_LIKE_DATA_SOURCE_ADAPTERS
    ref_errors = validate_data_source_refs(
        project_root=project_root,
        refs=refs,
        owner_kind="operation",
        owner_id=operation_name,
        owner_path=owner_path,
        launch_like=launch_like,
    )
    if ref_errors:
        first = ref_errors[0]
        raise OperationError(first["code"], first["message"], first.get("details", {}))
    preflight: list[dict[str, Any]] = []
    if not isinstance(refs, list):
        return preflight
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        data_source_id = ref.get("data_source_id")
        role = ref.get("role")
        if not isinstance(data_source_id, str) or not data_source_id:
            continue
        if not isinstance(role, str) or not role:
            continue
        record = load_data_source(project_root, data_source_id)
        immutability = record.get("immutability") if isinstance(record, dict) else {}
        mode = immutability.get("mode") if isinstance(immutability, dict) else None
        integrity = record.get("integrity") if isinstance(record, dict) else {}
        strategy = integrity.get("strategy") if isinstance(integrity, dict) else None
        source_type = record.get("source_type") if isinstance(record, dict) else None
        item = {
            "data_source_id": data_source_id,
            "role": role,
            "immutability_mode": mode,
        }
        if launch_like and mode == "append_only":
            if role == "metrics_log" and source_type == "metrics_log":
                item["integrity_status"] = "append_only_not_rehashed"
                preflight.append(item)
                continue
            raise OperationError(
                "data_source.integrity_unverified",
                "append_only data sources require an explicit adapter allowance",
                {"data_source_id": data_source_id, "role": role, "source_type": source_type},
            )
        if launch_like and strategy == "directory_manifest_sha256":
            storage = record.get("storage") if isinstance(record, dict) else {}
            path_value = storage.get("path") if isinstance(storage, dict) else None
            if not isinstance(path_value, str) or not path_value:
                raise OperationError(
                    "data_source.integrity_unverified",
                    "directory_manifest_sha256 data source has no verifiable path",
                    {"data_source_id": data_source_id, "role": role},
                )
            try:
                resolved_path = resolve_data_source_storage_directory_path(
                    storage=storage, project_root=project_root, data_source_id=data_source_id
                )
                manifest = compute_directory_manifest_sha256(
                    resolved_path, data_source_id=data_source_id
                )
            except OperationError:
                raise
            except Exception as exc:
                raise OperationError(
                    "data_source.integrity_unverified",
                    "directory_manifest_sha256 data source could not be re-verified during dry-run preflight",
                    {"data_source_id": data_source_id, "role": role, "path": path_value},
                ) from exc
            expected_sha256 = integrity.get("sha256") if isinstance(integrity, dict) else None
            if manifest["sha256"] != expected_sha256:
                raise OperationError(
                    "data_source.integrity_unverified",
                    "directory_manifest_sha256 data source hash mismatch during dry-run preflight",
                    {
                        "data_source_id": data_source_id,
                        "role": role,
                        "path": str(resolved_path),
                        "expected_sha256": expected_sha256,
                        "actual_sha256": manifest["sha256"],
                    },
                )
            item["integrity_status"] = "directory_manifest_sha256_verified"
            item["path"] = str(resolved_path)
            item["file_count"] = manifest["file_count"]
            preflight.append(item)
            continue
        if launch_like and strategy == "file_sha256":
            storage = record.get("storage") if isinstance(record, dict) else {}
            path_value = storage.get("path") if isinstance(storage, dict) else None
            if not isinstance(path_value, str) or not path_value:
                raise OperationError(
                    "data_source.integrity_unverified",
                    "file_sha256 data source has no verifiable path",
                    {"data_source_id": data_source_id, "role": role},
                )
            try:
                resolved_path = resolve_data_source_storage_file_path(
                    storage=storage, project_root=project_root, data_source_id=data_source_id
                )
                actual_sha256 = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
            except OperationError:
                raise
            except Exception as exc:
                raise OperationError(
                    "data_source.integrity_unverified",
                    "file_sha256 data source could not be re-verified during dry-run preflight",
                    {"data_source_id": data_source_id, "role": role, "path": path_value},
                ) from exc
            expected_sha256 = integrity.get("sha256") if isinstance(integrity, dict) else None
            if actual_sha256 != expected_sha256:
                raise OperationError(
                    "data_source.integrity_unverified",
                    "file_sha256 data source hash mismatch during dry-run preflight",
                    {
                        "data_source_id": data_source_id,
                        "role": role,
                        "path": str(resolved_path),
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                )
            item["integrity_status"] = "file_sha256_verified"
            item["path"] = str(resolved_path)
            preflight.append(item)
            continue
        if launch_like and mode == "immutable":
            raise OperationError(
                "data_source.integrity_unverified",
                "immutable launch-like data sources require verifiable integrity metadata",
                {"data_source_id": data_source_id, "role": role, "strategy": strategy},
            )
        preflight.append(item)
    return preflight


def operation_data_source_ref_preflight(request: dict[str, Any]) -> list[dict[str, Any]]:
    preflight: list[dict[str, Any]] = []
    operation_name = str(request.get("operation") or request.get("target_id") or "operation")
    if request.get("data_source_refs") is not None:
        project_root_text = require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "operation request.project_root is required for data_source_refs",
        )
        project_root = Path(project_root_text)
        preflight.extend(
            data_source_ref_preflight_for_request(
                request=request,
                project_root=project_root,
                operation_name=operation_name,
                owner_path=project_root / "<operation>",
            )
        )
    if request.get("adapter") != "operation_sequence":
        return preflight
    sequence_project_root = request.get("project_root")
    for index, step in enumerate(request.get("steps") or []):
        if not isinstance(step, dict):
            continue
        child_request = step.get("request")
        if not isinstance(child_request, dict) or child_request.get("data_source_refs") is None:
            continue
        step_id = str(step.get("step_id") or index)
        child_project_root_text = child_request.get("project_root") or sequence_project_root
        child_project_root = Path(
            require_string(
                child_project_root_text,
                "operation.project_root_missing",
                "operation_sequence step request.project_root is required for data_source_refs",
            )
        )
        child_request_with_root = dict(child_request)
        child_request_with_root.setdefault("project_root", str(child_project_root))
        child_operation_name = str(
            child_request_with_root.get("operation")
            or child_request_with_root.get("target_id")
            or f"{operation_name}.{step_id}"
        )
        preflight.extend(
            data_source_ref_preflight_for_request(
                request=child_request_with_root,
                project_root=child_project_root,
                operation_name=child_operation_name,
                owner_path=child_project_root / "<operation_sequence>" / step_id,
            )
        )
    return preflight


def add_guard_receipt(operation_path: Path) -> dict[str, Any]:
    operation = load_operation(operation_path)
    data_source_preflight = operation_data_source_ref_preflight(operation["request"])
    receipt = {
        "schema_version": 1,
        "kind": "kikai_guard_receipt",
        "status": "passed",
        "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "request_sha256": request_sha256(operation),
    }
    if data_source_preflight:
        receipt["data_source_preflight"] = data_source_preflight
    operation["guard_receipt"] = receipt
    fmt = _operation_format(operation_path)
    tmp_path = operation_path.with_suffix(operation_path.suffix + ".tmp")
    tmp_path.write_text(dump_operation_text(operation, fmt))
    tmp_path.replace(operation_path)
    return operation


def validate_guard_receipt(operation: dict[str, Any]) -> None:
    receipt = operation.get("guard_receipt")
    if not isinstance(receipt, dict):
        raise OperationError(
            "operation.guard_receipt_missing",
            "operation JSON lacks guard_receipt; run target dry-run first",
        )
    if receipt.get("status") != "passed":
        raise OperationError(
            "operation.guard_receipt_invalid",
            "operation guard_receipt status is not passed",
            {"status": receipt.get("status")},
        )
    expected = request_sha256(operation)
    actual = receipt.get("request_sha256")
    if actual != expected:
        raise OperationError(
            "operation.guard_receipt_mismatch",
            "operation request changed after guard receipt was issued",
            {"expected": expected, "actual": actual},
        )
    current_preflight = operation_data_source_ref_preflight(operation["request"])
    recorded_preflight = receipt.get("data_source_preflight", [])
    if current_preflight or recorded_preflight:
        if recorded_preflight != current_preflight:
            raise OperationError(
                "operation.guard_receipt_mismatch",
                "operation data source preflight changed after guard receipt was issued",
                {"expected": recorded_preflight, "actual": current_preflight},
            )


def require_string_list(value: Any, *, code: str, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise OperationError(code, f"{field} must be a non-empty list of strings")
    return list(value)


def execute_remote_file_fetch_operation(request: dict[str, Any]) -> dict[str, Any]:
    ssh_host = require_safe_ssh_host(
        require_string(
            request.get("ssh_host"),
            "operation.remote_ssh_host_missing",
            "remote_file_fetch request.ssh_host is required",
        )
    )
    remote_paths = require_string_list(
        request.get("remote_paths"),
        code="operation.remote_file_fetch_paths_missing",
        field="remote_file_fetch request.remote_paths",
    )
    local_dest_root = Path(
        resolve_text_ref(
            require_string(
                request.get("local_dest_root"),
                "operation.remote_file_fetch_local_dest_root_missing",
                "remote_file_fetch request.local_dest_root is required",
            )
        )
    )
    local_dest_root.mkdir(parents=True, exist_ok=True)
    scp_bin = os.environ.get("KIKAI_SCP_BIN", "scp")
    fetched: list[dict[str, Any]] = []
    for remote_path in remote_paths:
        resolved_remote = resolve_text_ref(remote_path)
        dest = local_dest_root / Path(resolved_remote).name
        completed = subprocess.run(
            [scp_bin, "-p", f"{ssh_host}:{resolved_remote}", str(dest)],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise OperationError(
                "operation.remote_file_fetch_failed",
                "remote_file_fetch scp transfer returned non-zero",
                {
                    "remote_path": resolved_remote,
                    "stderr": completed.stderr.strip()[:500],
                },
            )
        if not dest.is_file():
            raise OperationError(
                "operation.remote_file_fetch_missing_local",
                "remote_file_fetch did not produce the expected local file",
                {"remote_path": resolved_remote, "local_path": str(dest)},
            )
        fetched.append(
            {
                "remote_path": resolved_remote,
                "local_path": str(dest),
                "size_bytes": dest.stat().st_size,
            }
        )
    return {
        "execution_status": "remote_file_fetch_completed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "fetched": fetched,
        "target_id": request.get("target_id"),
    }


# These validate values that are interpolated into a remote shell string, so they must
# anchor with \Z (true end-of-string), NOT $ -- $ also matches just before a trailing
# newline, so e.g. "run1\n" would pass and inject a newline into the remote command.
_SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SAFE_IMAGE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,127}\Z")
_SAFE_REMOTE_BUILD_DIR = re.compile(r"^/[A-Za-z0-9_./-]+\Z")
_SAFE_DOCKER_PATH = re.compile(r"^/[A-Za-z0-9_.\-/]+\Z")
_SAFE_DOCKER_VOLUME = re.compile(r"^/[A-Za-z0-9_.\-/]+:/[A-Za-z0-9_.\-/]+(:(ro|rw))?\Z")
_SAFE_DOCKER_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_DOCKER_NETWORK = re.compile(r"^[A-Za-z0-9_.\-]+\Z")
# An ssh/scp host argument that begins with '-' is parsed by ssh as an OPTION
# (e.g. -oProxyCommand=... => local command execution), so the charset alone is
# not enough — a leading '-' must be rejected explicitly.
_SAFE_SSH_HOST = re.compile(r"^[A-Za-z0-9_.@:-]+\Z")
# GPU spec accepted by `docker run --gpus`: all / none / a device count / a
# device=<ids> selector (optionally wrapped in a BALANCED pair of double quotes).
# Anything else is interpolated raw into the remote shell; the quotes must be balanced
# so a lone `"` (e.g. device=0") cannot open a quoted region in the command string.
_SAFE_DOCKER_GPUS = re.compile(r'^(all|none|\d+|device=[0-9,]+|"device=[0-9,]+")\Z')


def require_safe_ssh_host(value: Any) -> str:
    """Resolve and validate an ssh/scp host so it cannot be parsed as an ssh
    OPTION. A value beginning with '-' (e.g. '-oProxyCommand=...') would be
    treated by ssh/scp as a flag and can yield local command execution, so we
    reject a leading '-' explicitly in addition to enforcing a strict charset."""
    resolved = resolve_text_ref(value)
    if not resolved or resolved.startswith("-") or not _SAFE_SSH_HOST.match(resolved):
        raise OperationError(
            "operation.remote_ssh_host_invalid",
            "ssh_host is not a safe ssh/scp host (must match a strict charset and not begin with '-')",
            {"ssh_host": resolved},
        )
    return resolved


def reject_dotdot_segments(value: str, *, code: str, field: str) -> str:
    """Reject any '..' path segment so a regex-allowed dir like '/tmp/../etc'
    cannot escape upward. Returns the value unchanged when safe."""
    if ".." in Path(value).parts:
        raise OperationError(
            code,
            f"{field} must not contain '..' path segments",
            {"path": value},
        )
    return value


def execute_remote_docker_teardown_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Stop & remove orphaned training containers on the remote docker host over a
    guarded SSH channel (the same ssh used by remote_kikai_exec — NOT raw shell ssh,
    and NOT dependent on the remote kikai checkout). Lists `docker ps -a`, selects
    containers by explicit `container_names` and/or `name_pattern` (regex, length-bounded
    and matched with re.fullmatch so it must match the WHOLE name — not a substring), then
    `docker rm -f` each (unless `list_only`). ssh_host is regex-validated and each selected
    name is re-checked against _SAFE_CONTAINER_NAME before removal. This is the kikai-native
    way to free a GPU held by a dead/orphaned run when TaskStop only killed the local ssh."""
    ssh_host = require_safe_ssh_host(
        require_string(
            request.get("ssh_host"),
            "operation.remote_ssh_host_missing",
            "remote_docker_teardown request.ssh_host is required",
        )
    )
    explicit = request.get("container_names") or []
    if not isinstance(explicit, list):
        raise OperationError("operation.remote_docker_teardown_invalid", "container_names must be a list", {})
    explicit = [resolve_text_ref(str(n)) for n in explicit]
    name_pattern = request.get("name_pattern")
    list_only = bool(request.get("list_only"))
    ssh_bin = os.environ.get("KIKAI_SSH_BIN", "ssh")

    listing = subprocess.run(
        [ssh_bin, ssh_host, "docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}|{{.RunningFor}}'"],
        check=False, text=True, capture_output=True,
    )
    if listing.returncode != 0:
        raise OperationError(
            "operation.remote_docker_teardown_list_failed",
            "remote docker ps returned non-zero",
            {"stderr": listing.stderr.strip()[:500]},
        )
    rows = [ln for ln in listing.stdout.splitlines() if ln.strip()]
    all_names = [ln.split("|", 1)[0] for ln in rows]
    pat = None
    if name_pattern:
        if not isinstance(name_pattern, str) or len(name_pattern) > 200:
            raise OperationError(
                "operation.remote_docker_teardown_invalid_pattern",
                "remote_docker_teardown name_pattern must be a string of at most 200 chars",
                {"name_pattern_length": len(name_pattern) if isinstance(name_pattern, str) else None},
            )
        try:
            pat = re.compile(name_pattern)
        except re.error as exc:
            raise OperationError(
                "operation.remote_docker_teardown_invalid_pattern",
                "remote_docker_teardown name_pattern is not a valid regular expression",
                {"name_pattern": name_pattern, "error": str(exc)},
            ) from exc
    # fullmatch (anchored), NOT search: the pattern must match the WHOLE container
    # name so that e.g. "." cannot substring-select (and remove) every container.
    selected = sorted({n for n in all_names if (n in explicit) or (pat is not None and pat.fullmatch(n))})

    results: list[dict[str, Any]] = []
    if not list_only:
        for name in selected:
            if not _SAFE_CONTAINER_NAME.match(name):
                results.append({"name": name, "skipped": "unsafe_name"})
                continue
            rm = subprocess.run([ssh_bin, ssh_host, f"docker rm -f {name}"], check=False, text=True, capture_output=True)
            results.append({"name": name, "returncode": rm.returncode, "removed": rm.returncode == 0,
                            "stderr": rm.stderr.strip()[:200] if rm.returncode != 0 else ""})
    return {
        "execution_status": "remote_docker_teardown_completed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "all_containers": rows,
        "selected": selected,
        "list_only": list_only,
        "results": results,
        "target_id": request.get("target_id"),
    }


def execute_remote_docker_logs_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Fetch `docker logs --tail N <name>` for a detached run on the remote docker host
    over the same guarded SSH channel used by remote_docker_teardown (NOT raw shell, NOT
    dependent on a remote kikai checkout). Used to status/debug a DETACHED training run
    started via script_bundle_run with detach=true. Captures both stdout and stderr since
    training frameworks often log to stderr, and returns the combined tail."""
    ssh_host = require_safe_ssh_host(
        require_string(
            request.get("ssh_host"),
            "operation.remote_ssh_host_missing",
            "remote_docker_logs request.ssh_host is required",
        )
    )
    container_name = resolve_text_ref(
        require_string(
            request.get("container_name"),
            "operation.remote_docker_logs_name_missing",
            "remote_docker_logs request.container_name is required",
        )
    )
    if not _SAFE_CONTAINER_NAME.match(container_name):
        raise OperationError(
            "operation.remote_docker_logs_invalid_name",
            "remote_docker_logs container_name is not a safe container name",
            {"container_name": container_name},
        )
    tail = request.get("tail", 200)
    try:
        tail = int(tail)
    except (TypeError, ValueError) as exc:
        raise OperationError(
            "operation.remote_docker_logs_invalid_tail",
            "remote_docker_logs tail must be an integer",
            {"tail": request.get("tail")},
        ) from exc
    if tail < 0:
        raise OperationError(
            "operation.remote_docker_logs_invalid_tail",
            "remote_docker_logs tail must be non-negative",
            {"tail": tail},
        )
    ssh_bin = os.environ.get("KIKAI_SSH_BIN", "ssh")
    run = subprocess.run(
        [ssh_bin, ssh_host, f"docker logs --tail {tail} {container_name}"],
        check=False,
        text=True,
        capture_output=True,
    )
    stdout = run.stdout or ""
    stderr = run.stderr or ""
    # docker logs writes the container's stdout to our stdout and its stderr to our
    # stderr; concatenate so the caller sees both streams (training often logs to stderr).
    combined = stdout
    if stderr:
        combined = f"{combined}{stderr}" if combined else stderr
    combined = combined[-20000:]
    return {
        "execution_status": "remote_docker_logs_completed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "container_name": container_name,
        "tail": tail,
        "logs": combined,
        "returncode": run.returncode,
        "target_id": request.get("target_id"),
    }


def execute_remote_file_push_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Push local files/dirs to the remote host over scp (inverse of
    remote_file_fetch). Used to SYNC the kikai-lab package to the remote checkout so
    `remote_kikai_exec` runs the latest adapters. Dirs are pushed with scp -r."""
    ssh_host = require_safe_ssh_host(
        require_string(request.get("ssh_host"), "operation.remote_ssh_host_missing",
                       "remote_file_push request.ssh_host is required")
    )
    local_paths = require_string_list(request.get("local_paths"),
                                      code="operation.remote_file_push_paths_missing",
                                      field="remote_file_push request.local_paths")
    remote_dest_dir = resolve_text_ref(
        require_string(request.get("remote_dest_dir"), "operation.remote_file_push_dest_missing",
                       "remote_file_push request.remote_dest_dir is required")
    )
    if not _SAFE_REMOTE_BUILD_DIR.match(remote_dest_dir):
        raise OperationError("operation.remote_file_push_invalid_dest",
                             "remote_file_push remote_dest_dir is not a safe path",
                             {"remote_dest_dir": remote_dest_dir})
    reject_dotdot_segments(remote_dest_dir,
                           code="operation.remote_file_push_invalid_dest",
                           field="remote_file_push remote_dest_dir")
    scp_bin = os.environ.get("KIKAI_SCP_BIN", "scp")
    ssh_bin = os.environ.get("KIKAI_SSH_BIN", "ssh")
    subprocess.run([ssh_bin, ssh_host, f"mkdir -p {shlex.quote(remote_dest_dir)}"], check=False, text=True, capture_output=True)
    pushed: list[dict[str, Any]] = []
    for lp in local_paths:
        p = Path(resolve_text_ref(lp))
        if not p.exists():
            raise OperationError("operation.remote_file_push_local_missing",
                                 "remote_file_push local path does not exist", {"local_path": str(p)})
        cmd = [scp_bin, "-p"] + (["-r"] if p.is_dir() else []) + [str(p), f"{ssh_host}:{remote_dest_dir}/"]
        c = subprocess.run(cmd, check=False, text=True, capture_output=True)
        if c.returncode != 0:
            raise OperationError("operation.remote_file_push_failed",
                                 "remote_file_push scp returned non-zero",
                                 {"local_path": str(p), "stderr": c.stderr.strip()[:500]})
        pushed.append({"local_path": str(p), "is_dir": p.is_dir()})
    return {
        "execution_status": "remote_file_push_completed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "remote_dest_dir": remote_dest_dir,
        "pushed": pushed,
        "target_id": request.get("target_id"),
    }


def execute_remote_docker_build_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Build a docker image ON the remote host over the same guarded SSH channel used
    by remote_kikai_exec (NOT raw shell, NOT dependent on the remote kikai checkout).
    The full Dockerfile text is supplied inline (`dockerfile_content`) and piped to the
    remote over ssh (no scp of a temp file). Bakes a derived training image so per-run
    `pip install` is unnecessary. ssh_host, image_tag and remote_build_dir are
    regex-validated, build_arg keys are regex-validated and each k=v token is shlex-quoted,
    to keep the shell-injection surface minimal."""
    ssh_host = require_safe_ssh_host(
        require_string(request.get("ssh_host"), "operation.remote_ssh_host_missing",
                       "remote_docker_build request.ssh_host is required")
    )
    image_tag = resolve_text_ref(
        require_string(request.get("image_tag"), "operation.remote_docker_build_tag_missing",
                       "remote_docker_build request.image_tag is required")
    )
    if not _SAFE_IMAGE_TAG.match(image_tag):
        raise OperationError("operation.remote_docker_build_invalid_tag",
                             "remote_docker_build image_tag is not a safe docker tag",
                             {"image_tag": image_tag})
    dockerfile_content = require_string(
        request.get("dockerfile_content"), "operation.remote_docker_build_dockerfile_missing",
        "remote_docker_build request.dockerfile_content is required"
    )
    remote_build_dir = resolve_text_ref(
        str(request.get("remote_build_dir") or "/tmp/kikai_docker_build")
    )
    if not _SAFE_REMOTE_BUILD_DIR.match(remote_build_dir):
        raise OperationError("operation.remote_docker_build_invalid_dir",
                             "remote_docker_build remote_build_dir is not a safe path",
                             {"remote_build_dir": remote_build_dir})
    reject_dotdot_segments(remote_build_dir,
                           code="operation.remote_docker_build_invalid_dir",
                           field="remote_docker_build remote_build_dir")

    build_args = request.get("build_args") or {}
    if not isinstance(build_args, dict):
        raise OperationError("operation.remote_docker_build_invalid_build_args",
                             "remote_docker_build build_args must be a dict of str->str",
                             {})
    build_arg_parts: list[str] = []
    for k, v in build_args.items():
        ks = str(k)
        if not _SAFE_DOCKER_ENV_KEY.match(ks):
            raise OperationError("operation.remote_docker_build_invalid_build_arg_key",
                                 "remote_docker_build build_arg key is not a safe name",
                                 {"key": ks})
        vs = resolve_text_ref(str(v))
        build_arg_parts.append(f"--build-arg {shlex.quote(f'{ks}={vs}')}")
    build_args_str = " ".join(build_arg_parts)
    no_cache_flag = "--no-cache" if bool(request.get("no_cache")) else ""

    ssh_bin = os.environ.get("KIKAI_SSH_BIN", "ssh")
    subprocess.run([ssh_bin, ssh_host, f"mkdir -p {remote_build_dir}"],
                   check=False, text=True, capture_output=True)
    write = subprocess.run(
        [ssh_bin, ssh_host, f"cat > {remote_build_dir}/Dockerfile"],
        input=dockerfile_content, text=True, capture_output=True, check=False,
    )
    if write.returncode != 0:
        raise OperationError("operation.remote_docker_build_dockerfile_write_failed",
                             "remote_docker_build failed to write Dockerfile to remote",
                             {"remote_build_dir": remote_build_dir,
                              "stderr": (write.stderr or "")[-2000:]})

    build_cmd = (
        f"docker build {no_cache_flag} {build_args_str} -t {image_tag} "
        f"-f {remote_build_dir}/Dockerfile {remote_build_dir}"
    )
    build = subprocess.run(
        [ssh_bin, ssh_host, build_cmd], text=True, capture_output=True, check=False
    )
    stdout = build.stdout or ""
    stderr = build.stderr or ""
    if build.returncode != 0:
        raise OperationError("operation.remote_docker_build_failed",
                             "remote_docker_build docker build returned non-zero",
                             {"image_tag": image_tag,
                              "stderr": stderr[-2000:],
                              "stdout_tail": stdout[-2000:]})
    return {
        "execution_status": "remote_docker_build_completed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "image_tag": image_tag,
        "returncode": 0,
        "build_log_tail": (stdout + stderr)[-3000:],
        "target_id": request.get("target_id"),
    }


def execute_remote_docker_run_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Run a one-off `docker run --rm` of a given image+command on a remote host over
    the same guarded SSH channel used by remote_kikai_exec (NOT raw shell, NOT dependent
    on a remote kikai checkout). Lets us run benchmarks in NGC/other containers on a
    different machine without a kikai checkout there. The command is a LIST of argv
    strings (NOT a shell string) and each element is shlex-quoted when assembled, so the
    injection surface is minimal; ssh_host, image, gpus, volumes, workdir, name, network
    and env keys are all regex-validated (and env values are shlex-quoted)."""
    ssh_host = require_safe_ssh_host(
        require_string(request.get("ssh_host"), "operation.remote_ssh_host_missing",
                       "remote_docker_run request.ssh_host is required")
    )
    image = resolve_text_ref(
        require_string(request.get("image"), "operation.remote_docker_run_image_missing",
                       "remote_docker_run request.image is required")
    )
    if not _SAFE_IMAGE_TAG.match(image):
        raise OperationError("operation.remote_docker_run_invalid_image",
                             "remote_docker_run image is not a safe docker tag",
                             {"image": image})

    command = request.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise OperationError("operation.remote_docker_run_command_missing",
                             "remote_docker_run request.command must be a non-empty list of argv strings",
                             {})

    gpus = request.get("gpus", "all")
    if gpus is not None:
        gpus = str(gpus)
        if not _SAFE_DOCKER_GPUS.match(gpus):
            raise OperationError("operation.remote_docker_run_invalid_gpus",
                                 "remote_docker_run gpus must be all/none/<int>/device=<ids>",
                                 {"gpus": gpus})
    network = request.get("network")
    if network is not None:
        network = str(network)
        if not _SAFE_DOCKER_NETWORK.match(network):
            raise OperationError("operation.remote_docker_run_invalid_network",
                                 "remote_docker_run network is not safe", {"network": network})

    name = request.get("name")
    if name is not None:
        name = str(name)
        if not _SAFE_CONTAINER_NAME.match(name):
            raise OperationError("operation.remote_docker_run_invalid_name",
                                 "remote_docker_run name is not a safe container name", {"name": name})

    workdir = request.get("workdir")
    if workdir is not None:
        workdir = resolve_text_ref(str(workdir))
        if not _SAFE_DOCKER_PATH.match(workdir):
            raise OperationError("operation.remote_docker_run_invalid_workdir",
                                 "remote_docker_run workdir is not a safe path", {"workdir": workdir})
        reject_dotdot_segments(workdir,
                               code="operation.remote_docker_run_invalid_workdir",
                               field="remote_docker_run workdir")

    env = request.get("env") or {}
    if not isinstance(env, dict):
        raise OperationError("operation.remote_docker_run_invalid_env",
                             "remote_docker_run env must be a dict of str->str", {})
    env_parts: list[str] = []
    for k, v in env.items():
        ks = str(k)
        if not _SAFE_DOCKER_ENV_KEY.match(ks):
            raise OperationError("operation.remote_docker_run_invalid_env_key",
                                 "remote_docker_run env key is not a safe environment name", {"key": ks})
        vs = resolve_text_ref(str(v))
        env_parts.append(f"-e {ks}={shlex.quote(vs)}")

    volumes = request.get("volumes") or []
    if not isinstance(volumes, list):
        raise OperationError("operation.remote_docker_run_invalid_volumes",
                             "remote_docker_run volumes must be a list of host:container[:mode] strings", {})
    volume_parts: list[str] = []
    for vol in volumes:
        vs = resolve_text_ref(str(vol))
        if not _SAFE_DOCKER_VOLUME.match(vs):
            raise OperationError("operation.remote_docker_run_invalid_volume",
                                 "remote_docker_run volume is not a safe host:container[:mode] string",
                                 {"volume": vs})
        volume_parts.append(f"-v {vs}")

    timeout_sec = request.get("timeout_sec")
    timeout_sec = int(timeout_sec) if timeout_sec is not None else 1800

    parts: list[str] = ["docker", "run", "--rm"]
    if gpus:
        parts.append(f"--gpus {gpus}")
    if network:
        parts.append(f"--network {network}")
    if name:
        parts.append(f"--name {name}")
    if workdir:
        parts.append(f"-w {workdir}")
    parts.extend(env_parts)
    parts.extend(volume_parts)
    parts.append(image)
    parts.extend(shlex.quote(c) for c in command)
    remote_cmd = " ".join(parts)

    ssh_bin = os.environ.get("KIKAI_SSH_BIN", "ssh")
    try:
        run = subprocess.run(
            [ssh_bin, ssh_host, remote_cmd], text=True, capture_output=True, timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as exc:
        raise OperationError("operation.remote_docker_run_timeout",
                             "remote_docker_run timed out",
                             {"image": image, "timeout_sec": timeout_sec}) from exc

    stdout = run.stdout or ""
    stderr = run.stderr or ""
    if run.returncode != 0:
        raise OperationError("operation.remote_docker_run_failed",
                             "remote_docker_run docker run returned non-zero",
                             {"image": image, "returncode": run.returncode,
                              "stderr": stderr[-3000:], "stdout_tail": stdout[-3000:]})
    return {
        "execution_status": "remote_docker_run_completed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "image": image,
        "returncode": 0,
        "stdout": stdout[-6000:],
        "stderr_tail": stderr[-2000:],
        "target_id": request.get("target_id"),
    }


def execute_docker_container_restart_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Force-remove a (possibly stuck/zombie) named container so the next ephemeral
    run starts clean, or to recover a GPU/name held by a dead run. For
    status='service' containers, optionally re-run detached when mode='restart'."""
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "docker_container_restart request.project_root is required",
        )
    )
    container_id = require_string(
        request.get("container_id"),
        "operation.docker_container_restart_invalid",
        "docker_container_restart request.container_id is required",
    )
    # container_id becomes a path component (containers/<id>.yaml); reject anything with
    # '/', '..', or a non-alphanumeric leading char so it cannot traverse out of the dir.
    if not _SAFE_CONTAINER_NAME.fullmatch(container_id):
        raise OperationError(
            "operation.docker_container_restart_invalid",
            "docker_container_restart container_id is not a safe registry id",
            {"container_id": container_id},
        )
    container = load_container_record(project_root, container_id)
    container_name = docker_name_from_container(container, container_id)
    mode = request.get("mode") or "teardown"
    found_before, _, _ = docker_inspect_by_name(request, container_name)
    docker_rm_force(request, container_name)
    found_after, _, _ = docker_inspect_by_name(request, container_name)
    result = {
        "execution_status": "docker_container_restart_completed",
        "operation": request.get("operation"),
        "container_id": container_id,
        "docker_name": container_name,
        "mode": mode,
        "was_present": bool(found_before),
        "removed": bool(found_before) and not bool(found_after),
        "target_id": request.get("target_id"),
    }
    if mode == "restart":
        command = docker_detached_run_command(project_root, container)
        completed = subprocess.run(
            command, check=False, text=True, capture_output=True, env=docker_subprocess_env(request)
        )
        result["restart_returncode"] = completed.returncode
        if completed.returncode != 0:
            result["execution_status"] = "docker_container_restart_failed"
            result["restart_stderr"] = completed.stderr[-2000:]
            raise OperationError(
                "operation.docker_container_restart_failed",
                "container restart docker run returned non-zero",
                result,
            )
    return result


def execute_run_dir_chown_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Repair the root-write trap: a root-running training container leaves run_dir
    files the daemon user cannot manage (retention EPERM). One-shot ``docker run``
    with the SAME registered training image (guaranteed present on the host),
    chowning the run_dir to the requested uid/gid. Idempotent."""
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "run_dir_chown request.project_root is required",
        )
    )
    container_id = require_string(
        request.get("container_id"),
        "operation.run_dir_chown_invalid",
        "run_dir_chown request.container_id is required",
    )
    if not _SAFE_CONTAINER_NAME.fullmatch(container_id):
        raise OperationError(
            "operation.run_dir_chown_invalid",
            "run_dir_chown container_id is not a safe registry id",
            {"container_id": container_id},
        )
    run_dir = Path(
        resolve_text_ref(
            require_string(
                request.get("run_dir"),
                "operation.run_dir_chown_invalid",
                "run_dir_chown request.run_dir is required",
            )
        )
    )
    # blast-radius containment for an automatic recursive chown: the mount spec must
    # be a safe absolute docker volume (same charset rule as remote_docker_run) and
    # at least two levels deep — "/" or "/data" as run_dir is a typo, not a run
    run_dir_text = str(run_dir)
    if (
        not run_dir.is_absolute()
        or len(run_dir.parts) < 3
        # docker bind-mounts the RESOLVED target: a symlink at a safe depth pointing
        # at / would otherwise chown the host root
        or len(run_dir.resolve().parts) < 3
        or not _SAFE_DOCKER_VOLUME.fullmatch(f"{run_dir_text}:/kikai_chown_target")
    ):
        raise OperationError(
            "operation.run_dir_chown_invalid",
            "run_dir_chown run_dir must be a safe absolute path at least two "
            "levels deep",
            {"run_dir": run_dir_text},
        )
    if not run_dir.is_dir():
        raise OperationError(
            "operation.run_dir_chown_invalid",
            "run_dir_chown run_dir does not exist or is not a directory",
            {"run_dir": run_dir_text},
        )
    uid, gid = request.get("uid"), request.get("gid")
    if not (
        isinstance(uid, int)
        and isinstance(gid, int)
        and not isinstance(uid, bool)
        and not isinstance(gid, bool)
        and uid >= 0
        and gid >= 0
    ):
        raise OperationError(
            "operation.run_dir_chown_invalid",
            "run_dir_chown uid/gid must be non-negative integers",
            {"uid": uid, "gid": gid},
        )
    container = load_container_record(project_root, container_id)
    image = require_string(
        (container.get("docker") or {}).get("image"),
        "operation.run_dir_chown_invalid",
        "run_dir_chown container record declares no docker.image",
    )
    command = [
        os.environ.get("KIKAI_DOCKER_BIN", "docker"),
        "run",
        "--rm",
        "--user",
        "0:0",  # chown needs root INSIDE the container regardless of image USER
        "--entrypoint",
        "chown",
        "-v",
        f"{run_dir_text}:/kikai_chown_target",
        image,
        "-R",
        f"{uid}:{gid}",
        "/kikai_chown_target",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=900,  # a wedged docker must not block the reconciler forever
            env=docker_subprocess_env(request),
        )
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OperationError(
            "operation.run_dir_chown_failed",
            "run_dir chown docker run timed out",
            {"timeout_sec": 900, "container_id": container_id},
        ) from exc
    if completed.returncode != 0:
        raise OperationError(
            "operation.run_dir_chown_failed",
            "run_dir chown docker run returned non-zero",
            {
                "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
                "container_id": container_id,
            },
        )
    return {
        "execution_status": "run_dir_chown_completed",
        "operation": request.get("operation"),
        "container_id": container_id,
        "uid": uid,
        "gid": gid,
    }


def execute_data_source_create_operation(request: dict[str, Any]) -> dict[str, Any]:
    data_source_kind = require_string(
        request.get("data_source_kind"),
        "operation.data_source_create_invalid",
        "data_source_create request.data_source_kind is required",
    )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "data_source_create request.project_root is required",
        )
    )
    kwargs = {
        "project_root": project_root,
        "data_source_id": require_string(
            request.get("data_source_id"),
            "operation.data_source_create_invalid",
            "data_source_create request.data_source_id is required",
        ),
        "source_type": require_string(
            request.get("source_type"),
            "operation.data_source_create_invalid",
            "data_source_create request.source_type is required",
        ),
        "path_ref": require_string(
            request.get("path"),
            "operation.data_source_create_invalid",
            "data_source_create request.path is required",
        ),
        "host_ref": require_string(
            request.get("host_ref"),
            "operation.data_source_create_invalid",
            "data_source_create request.host_ref is required",
        ),
        "role_compatibility": require_string_list(
            request.get("roles"),
            code="operation.data_source_create_invalid",
            field="data_source_create request.roles",
        ),
        "summary": require_string(
            request.get("summary"),
            "operation.data_source_create_invalid",
            "data_source_create request.summary is required",
        ),
        "container_mount_path": request.get("container_mount_path"),
        "upstream_data_source_ids": request.get("upstream_data_source_ids") or [],
        "upstream_source_snapshot_ids": request.get("upstream_source_snapshot_ids") or [],
        "overwrite": bool(request.get("overwrite")),
    }
    if data_source_kind == "file":
        created = create_file_data_source(**kwargs)
    elif data_source_kind == "directory":
        created = create_directory_data_source(**kwargs)
    else:
        raise OperationError(
            "operation.data_source_create_invalid",
            "data_source_create request.data_source_kind must be file or directory",
            {"data_source_kind": data_source_kind},
        )
    data_source = created["data_source"]
    return {
        "execution_status": "data_source_created",
        "operation": request.get("operation"),
        "data_source_id": data_source["data_source_id"],
        "data_source_kind": data_source_kind,
        "path": created["path"],
        "resolved_path": created["resolved_path"],
        "integrity_strategy": data_source.get("integrity", {}).get("strategy"),
        "integrity_sha256": data_source.get("integrity", {}).get("sha256"),
        "file_count": created.get("file_count"),
    }


FORBIDDEN_DOCKER_EXEC_REQUEST_KEYS = {"command", "command_string", "shell", "heredoc", "script"}
SHELL_WRAPPER_BASENAMES = {"bash", "sh", "zsh"}


def execute_operation(operation: dict[str, Any]) -> dict[str, Any]:
    request = operation["request"]
    adapter = request.get("adapter")
    if adapter == "noop":
        return {
            "execution_status": "validated_noop",
            "operation": request.get("operation"),
            "target_id": request.get("target_id"),
        }
    if adapter == "docker_exec":
        return execute_docker_exec_operation(request)
    if adapter == "script_bundle_exec":
        return execute_script_bundle_operation(request)
    if adapter == "script_bundle_run":
        return execute_script_bundle_run_operation(request)
    if adapter == "artifact_delivery":
        return execute_artifact_delivery_operation(request)
    if adapter == "webhook_notification":
        return execute_webhook_notification_operation(request)
    if adapter == "training_progress_backfill":
        return execute_training_progress_backfill_operation(request)
    if adapter == "operation_sequence":
        return execute_operation_sequence(request)
    if adapter == "checkpoint_guard":
        return execute_checkpoint_guard_operation(request)
    if adapter == "checkpoint_retention":
        return execute_checkpoint_retention_operation(request)
    if adapter == "trt_cache_guard":
        return execute_trt_cache_guard_operation(request)
    if adapter == "artifact_summary_guard":
        return execute_artifact_summary_guard_operation(request)
    if adapter == "remote_kikai_exec":
        return execute_remote_kikai_exec_operation(request)
    if adapter == "data_source_create":
        return execute_data_source_create_operation(request)
    if adapter == "tensorboard_service":
        return execute_tensorboard_service_operation(request)
    if adapter == "remote_file_fetch":
        return execute_remote_file_fetch_operation(request)
    if adapter == "docker_container_restart":
        return execute_docker_container_restart_operation(request)
    if adapter == "run_dir_chown":
        return execute_run_dir_chown_operation(request)
    if adapter == "remote_docker_teardown":
        return execute_remote_docker_teardown_operation(request)
    if adapter == "remote_docker_logs":
        return execute_remote_docker_logs_operation(request)
    if adapter == "remote_file_push":
        return execute_remote_file_push_operation(request)
    if adapter == "remote_docker_build":
        return execute_remote_docker_build_operation(request)
    if adapter == "remote_docker_run":
        return execute_remote_docker_run_operation(request)
    raise OperationError(
        "operation.adapter_not_implemented",
        (
            "only noop, docker_exec, script_bundle_exec, script_bundle_run, artifact_delivery, "
            "webhook_notification, training_progress_backfill, operation_sequence, "
            "checkpoint_guard, checkpoint_retention, trt_cache_guard, "
            "artifact_summary_guard, remote_kikai_exec, data_source_create, tensorboard_service, "
            "remote_file_fetch, docker_container_restart, remote_docker_teardown, "
            "remote_docker_logs, remote_file_push, remote_docker_build, and remote_docker_run "
            "adapter execution are implemented"
        ),
        {"adapter": adapter},
    )


def require_string(value: Any, code: str, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise OperationError(code, message)
    return value


def validate_structured_argv(request: dict[str, Any]) -> list[str]:
    forbidden = sorted(key for key in FORBIDDEN_DOCKER_EXEC_REQUEST_KEYS if key in request)
    if forbidden:
        raise OperationError(
            "operation.command_shape_forbidden",
            "docker_exec operations must use structured argv, not command strings or scripts",
            {"forbidden_keys": forbidden},
        )
    argv = request.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise OperationError(
            "operation.argv_invalid",
            "docker_exec request.argv must be a non-empty list of strings",
        )
    executable = Path(argv[0]).name
    if executable in SHELL_WRAPPER_BASENAMES and any(
        item == "-c" or (item.startswith("-") and "c" in item) for item in argv[1:]
    ):
        raise OperationError(
            "operation.shell_wrapper_forbidden",
            "docker_exec operations must not use shell wrappers such as bash -c",
            {"argv": argv},
        )
    return argv


def load_container_record(project_root: Path, container_id: str) -> dict[str, Any]:
    container_path = project_root / "containers" / f"{container_id}.yaml"
    if not container_path.exists():
        raise OperationError(
            "operation.container_missing",
            "docker_exec container definition is missing",
            {"container_id": container_id, "path": str(container_path)},
        )
    with container_path.open("r", encoding="utf-8") as f:
        container = yaml.safe_load(f) or {}
    if not isinstance(container, dict):
        raise OperationError(
            "operation.container_invalid",
            "docker_exec container definition must be a YAML mapping",
            {"container_id": container_id, "path": str(container_path)},
        )
    if container.get("container_id") != container_id:
        raise OperationError(
            "operation.container_id_mismatch",
            "docker_exec container definition id does not match requested container_id",
            {
                "expected_container_id": container_id,
                "actual_container_id": container.get("container_id"),
                "path": str(container_path),
            },
        )
    return container


def docker_name_from_container(container: dict[str, Any], container_id: str) -> str:
    docker = container.get("docker")
    docker_name = docker.get("name") if isinstance(docker, dict) else None
    if not isinstance(docker_name, str) or not docker_name:
        raise OperationError(
            "operation.container_docker_name_missing",
            "docker_exec container definition must define docker.name",
            {"container_id": container_id},
        )
    return docker_name


def docker_image_from_container(container: dict[str, Any], container_id: str) -> str:
    docker = container.get("docker")
    image = docker.get("image") if isinstance(docker, dict) else None
    if not isinstance(image, str) or not image:
        raise OperationError(
            "operation.container_docker_image_missing",
            "docker run container definition must define docker.image",
            {"container_id": container_id},
        )
    return resolve_text_ref(image)


def resolve_env_ref(value: str) -> str:
    if not value.startswith("env:"):
        return value
    env_name = value.removeprefix("env:")
    resolved = os.environ.get(env_name)
    if resolved is None or resolved == "":
        resolved = resolve_registered_value(env_name)
    if resolved is None or resolved == "":
        raise OperationError(
            "operation.env_ref_missing",
            "required environment reference is not set",
            {"env_ref": value, "env_name": env_name},
        )
    return resolved


def resolve_op_timeout_sec(request: dict[str, Any]) -> int | None:
    """Wall-clock budget for a FOREGROUND op's subprocess (docker run / docker exec).

    request.timeout_sec: positive int = cap; 0 = EXPLICITLY unbounded (the opt-out
    for known-long foreground ops); invalid values fail loudly like every other
    request field. Absent -> KIKAI_OP_TIMEOUT_SEC env (same semantics, lenient
    parse) -> 1800s default. The default exists because one hung op used to freeze
    the reconcile daemon forever (2026-07-08)."""
    raw = request.get("timeout_sec")
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise OperationError(
                "operation.timeout_sec_invalid",
                "timeout_sec must be a non-negative integer (0 = unbounded)",
                {"timeout_sec": raw},
            )
        return None if raw == 0 else raw
    env_raw = os.environ.get("KIKAI_OP_TIMEOUT_SEC")
    if env_raw:
        try:
            value = int(env_raw)
        except ValueError:
            return 1800
        return None if value <= 0 else value
    return 1800


def _output_tail(value: Any, limit: int = 500) -> str:
    """Last `limit` chars of a subprocess capture that may be str OR bytes.

    subprocess.TimeoutExpired carries UNDECODED bytes even under text=True
    (CPython populates it from the raw buffers on POSIX) — an isinstance(str)
    guard would silently discard exactly the diagnostics a timeout needs."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[-limit:]
    if isinstance(value, str):
        return value[-limit:]
    return ""


ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_text_ref(value: str) -> str:
    resolved = resolve_env_ref(value)

    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = os.environ.get(env_name)
        if env_value is None or env_value == "":
            env_value = resolve_registered_value(env_name)
        if env_value is None or env_value == "":
            raise OperationError(
                "operation.env_ref_missing",
                "required environment reference is not set",
                {"env_ref": match.group(0), "env_name": env_name},
            )
        return env_value

    return ENV_PLACEHOLDER_RE.sub(replace, resolved)


def resolve_argv_refs(argv: list[str]) -> list[str]:
    return [resolve_text_ref(item) for item in argv]


def docker_exec_prefix(request: dict[str, Any]) -> list[str]:
    command = [os.environ.get("KIKAI_DOCKER_BIN", "docker"), "exec"]
    workdir = request.get("workdir")
    if workdir is not None:
        command.extend(
            [
                "--workdir",
                resolve_text_ref(
                    require_string(
                        workdir,
                        "operation.workdir_invalid",
                        "workdir must be a string",
                    )
                ),
            ]
        )
    env = request.get("env") or {}
    env_is_valid = isinstance(env, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    )
    if not env_is_valid:
        raise OperationError(
            "operation.env_invalid",
            "docker_exec request.env must be an object of string values",
        )
    for key, value in env.items():
        command.extend(["-e", f"{key}={resolve_text_ref(value)}"])
    return command


def docker_subprocess_env(request: dict[str, Any]) -> dict[str, str] | None:
    docker_host = request.get("docker_host")
    if docker_host is None:
        return None
    env = os.environ.copy()
    env["DOCKER_HOST"] = resolve_text_ref(
        require_string(
            docker_host,
            "operation.docker_host_invalid",
            "docker_host must be a string",
        )
    )
    return env


def docker_env_args(request: dict[str, Any]) -> list[str]:
    env = request.get("env") or {}
    env_is_valid = isinstance(env, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    )
    if not env_is_valid:
        raise OperationError(
            "operation.env_invalid",
            "docker request.env must be an object of string values",
        )
    command: list[str] = []
    for key, value in env.items():
        command.extend(["-e", f"{key}={resolve_text_ref(value)}"])
    return command


def kikai_identity_env(request: dict[str, Any], container_id: str) -> list[str]:
    """Auto-inject run-identity env vars so the orchestrator is the source of
    truth for run identity (Discord labels etc.).

    Emits ``-e KEY=VALUE`` args for KIKAI_RUN_ID, KIKAI_CONTAINER_ID (both =
    container_id) and KIKAI_OPERATION (= request["operation"] when a non-empty
    string). Any key already present in request["env"] is left untouched so an
    explicit caller-set value always wins.
    """
    existing = request.get("env")
    existing_keys = set(existing) if isinstance(existing, dict) else set()
    identity: list[tuple[str, str]] = [
        ("KIKAI_RUN_ID", container_id),
        ("KIKAI_CONTAINER_ID", container_id),
    ]
    operation = request.get("operation")
    if isinstance(operation, str) and operation:
        identity.append(("KIKAI_OPERATION", operation))
    command: list[str] = []
    for key, value in identity:
        if key in existing_keys:
            continue
        if not _SAFE_CONTAINER_NAME.match(key):
            continue
        command.extend(["-e", f"{key}={value}"])
    return command


def docker_run_mount_args(project_root: Path, container: dict[str, Any]) -> list[str]:
    command = ["-v", f"{project_root.resolve()}:/workspace/kikai_project:ro"]
    mounts = container.get("mounts") or []
    if not isinstance(mounts, list):
        raise OperationError(
            "operation.container_mounts_invalid",
            "container mounts must be a list",
        )
    for index, mount in enumerate(mounts):
        if not isinstance(mount, dict):
            raise OperationError(
                "operation.container_mount_invalid",
                "container mount entries must be objects",
                {"index": index},
            )
        if mount.get("source_kind") == "kikai_managed_source_snapshot":
            snapshot_id = require_string(
                mount.get("source_snapshot_id"),
                "operation.source_snapshot_id_missing",
                "Kikai-managed source snapshot mounts require source_snapshot_id",
            )
            snapshot, snapshot_root = load_source_snapshot(project_root, snapshot_id)
            validate_source_snapshot_files(snapshot, snapshot_root, snapshot_id)
            source = str(snapshot_root / "root")
        else:
            source = resolve_text_ref(
                require_string(
                    mount.get("source"),
                    "operation.container_mount_invalid",
                    "container mount source is required",
                )
            )
        target = resolve_text_ref(
            require_string(
                mount.get("target"),
                "operation.container_mount_invalid",
                "container mount target is required",
            )
        )
        mode = mount.get("mode")
        if mode is None or mode == "":
            spec = f"{source}:{target}"
        else:
            mode_text = require_string(
                mode,
                "operation.container_mount_invalid",
                "container mount mode must be a string",
            )
            spec = f"{source}:{target}:{mode_text}"
        command.extend(["-v", spec])
    return command


def _composed_docker_name(
    container: dict[str, Any], container_id: str, request: dict[str, Any]
) -> str | None:
    """Compose the exact --name docker run will use, respecting container ephemeral
    flag and request.container_name_suffix. Used by BOTH docker_run_command (to set
    --name) and execute_docker_run_operation's preflight (to inspect the SAME name
    the run will pass). Any drift between the two would either false-flag legitimate
    concurrent invocations as name collisions, or miss real leftovers."""
    docker_meta = container.get("docker")
    declared_name = docker_meta.get("name") if isinstance(docker_meta, dict) else None
    if not (isinstance(declared_name, str) and declared_name):
        return None
    ephemeral = bool(isinstance(docker_meta, dict) and docker_meta.get("ephemeral"))
    suffix = request.get("container_name_suffix") if isinstance(request, dict) else None
    if isinstance(suffix, str) and suffix and not ephemeral:
        raise OperationError(
            "operation.container_name_suffix_not_ephemeral",
            "container_name_suffix requires container.docker.ephemeral=true",
            {"container_id": container_id, "suffix": suffix},
        )
    resolved_name = resolve_text_ref(declared_name)
    if ephemeral and isinstance(suffix, str) and suffix:
        # Per-invocation uniqueness: mangle non-safe chars, length-bound the suffix
        # so the composed name always stays under docker's 63-char --name limit
        # and matches _SAFE_CONTAINER_NAME.
        safe_suffix = re.sub(r"[^A-Za-z0-9_.-]", "_", suffix)[:50]
        max_base = 63 - len(safe_suffix) - 2   # 2 for "__"
        base = resolved_name[: max(1, max_base)]
        resolved_name = f"{base}__{safe_suffix}"
    return resolved_name


def ephemeral_child_name_regex(
    container: dict[str, Any], container_id: str, tag: str
) -> re.Pattern[str] | None:
    """Anchored regex matching the RUNTIME names of this container's step-suffixed
    ephemeral children (suffix ``step{NNNNNN}__{tag}``), mirroring the truncation
    and mangling ``_composed_docker_name`` applies. Reconstructing names from the
    full declared docker.name is wrong the moment the 63-char ``--name`` bound
    truncates the base or the suffix mangle rewrites/cuts the tag."""
    docker_meta = container.get("docker")
    declared = docker_meta.get("name") if isinstance(docker_meta, dict) else None
    if not (isinstance(declared, str) and declared):
        return None
    resolved = resolve_text_ref(declared)
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    # step{:06d} is MINIMUM 6 digits — runs past 999,999 steps grow the suffix,
    # which shifts both the [:50] suffix cut and the base truncation. Build one
    # alternative per plausible digit count so every width matches exactly.
    alternatives = []
    for digits in range(6, 10):
        prefix_len = 4 + digits + 2  # "step" + digits + "__"
        safe_suffix_len = min(prefix_len + len(safe_tag), 50)
        kept_tag = safe_tag[: max(0, safe_suffix_len - prefix_len)]
        base = resolved[: max(1, 63 - safe_suffix_len - 2)]
        alternatives.append(
            rf"{re.escape(base)}__step\d{{{digits}}}__{re.escape(kept_tag)}"
        )
    unique = list(dict.fromkeys(alternatives))
    return re.compile(r"^(?:" + "|".join(unique) + r")$")


def docker_attribution_labels(request: dict[str, Any], container_id: str) -> list[str]:
    """``--label`` args attributing a container to its kikai registry record and
    invocation suffix. Labels survive any change to the __suffix NAME convention,
    so /docker/ps and finalize can attribute by label instead of re-parsing names."""
    labels = ["--label", f"kikai.container_id={container_id}"]
    suffix = request.get("container_name_suffix") if isinstance(request, dict) else None
    if isinstance(suffix, str) and suffix:
        labels.extend(["--label", f"kikai.suffix={suffix}"])
    return labels


def docker_run_command(
    *,
    request: dict[str, Any],
    container: dict[str, Any],
    container_id: str,
    project_root: Path,
    argv: list[str],
) -> list[str]:
    command = [os.environ.get("KIKAI_DOCKER_BIN", "docker"), "run", "--rm"]
    # Give the foreground container a deterministic --name when the container
    # definition declares docker.name, so a run can be torn down by name on stop
    # (one-run-one-named-container). Without this, `docker run --rm` is anonymous
    # and teardown-by-name silently matches nothing, orphaning the GPU.
    resolved_name = _composed_docker_name(container, container_id, request)
    if resolved_name and _SAFE_CONTAINER_NAME.match(resolved_name):
        command.extend(["--name", resolved_name])
    command.extend(docker_attribution_labels(request, container_id))
    gpus = container.get("gpus")
    if isinstance(gpus, str) and gpus:
        command.extend(["--gpus", resolve_text_ref(gpus)])
    network_mode = container.get("network_mode")
    if isinstance(network_mode, str) and network_mode:
        command.extend(["--network", resolve_text_ref(network_mode)])
    ipc_mode = container.get("ipc_mode")
    if isinstance(ipc_mode, str) and ipc_mode:
        command.extend(["--ipc", resolve_text_ref(ipc_mode)])
    shm_size = container.get("shm_size")
    if isinstance(shm_size, str) and shm_size:
        command.extend(["--shm-size", resolve_text_ref(shm_size)])
    workdir = request.get("workdir") or container.get("workdir") or "/workspace/kikai_project"
    command.extend(
        [
            "--workdir",
            resolve_text_ref(
                require_string(
                    workdir,
                    "operation.workdir_invalid",
                    "workdir must be a string",
                )
            ),
        ]
    )
    command.extend(docker_env_args(request))
    command.extend(kikai_identity_env(request, container_id))
    command.extend(docker_run_mount_args(project_root, container))
    command.append(docker_image_from_container(container, container_id))
    command.extend(argv)
    return command


def docker_detached_run_command(
    *,
    request: dict[str, Any],
    container: dict[str, Any],
    container_id: str,
    project_root: Path,
    container_name: str,
    argv: list[str],
) -> list[str]:
    command = [
        os.environ.get("KIKAI_DOCKER_BIN", "docker"),
        "run",
        "-d",
        "--name",
        container_name,
    ]
    command.extend(docker_attribution_labels(request, container_id))
    gpus = container.get("gpus")
    if isinstance(gpus, str) and gpus:
        command.extend(["--gpus", resolve_text_ref(gpus)])
    network_mode = container.get("network_mode")
    if isinstance(network_mode, str) and network_mode:
        command.extend(["--network", resolve_text_ref(network_mode)])
    ipc_mode = container.get("ipc_mode")
    if isinstance(ipc_mode, str) and ipc_mode:
        command.extend(["--ipc", resolve_text_ref(ipc_mode)])
    shm_size = container.get("shm_size")
    if isinstance(shm_size, str) and shm_size:
        command.extend(["--shm-size", resolve_text_ref(shm_size)])
    workdir = request.get("workdir") or container.get("workdir") or "/workspace/kikai_project"
    command.extend(
        [
            "--workdir",
            resolve_text_ref(
                require_string(
                    workdir,
                    "operation.workdir_invalid",
                    "workdir must be a string",
                )
            ),
        ]
    )
    command.extend(docker_env_args(request))
    command.extend(kikai_identity_env(request, container_id))
    command.extend(docker_run_mount_args(project_root, container))
    command.append(docker_image_from_container(container, container_id))
    command.extend(argv)
    return command


def docker_inspect_by_name(
    request: dict[str, Any],
    container_name: str,
    *,
    type_filter: str | None = None,
) -> tuple[bool, list[dict[str, Any]], str]:
    command = [os.environ.get("KIKAI_DOCKER_BIN", "docker"), "inspect"]
    if type_filter:
        # e.g. "container": a same-named IMAGE must read as not-found, not as a holder
        command += ["--type", type_filter]
    command.append(container_name)
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
        )
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    if completed.returncode != 0:
        return False, [], completed.stderr
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OperationError(
            "operation.docker_inspect_invalid_json",
            "docker inspect did not return JSON",
            {"container_name": container_name, "stdout": completed.stdout},
        ) from exc
    if not isinstance(data, list):
        raise OperationError(
            "operation.docker_inspect_invalid_shape",
            "docker inspect returned a non-list JSON value",
            {"container_name": container_name},
        )
    return True, data, completed.stderr


def docker_logs_by_name(
    request: dict[str, Any],
    container_name: str,
    *,
    tail: int = 200,
) -> tuple[bool, str, str]:
    """``docker logs --tail N`` for one container. Returns (found, stdout, stderr).

    Mirrors ``docker_inspect_by_name``: a non-zero exit (unknown container) is reported
    as found=False rather than an exception, so callers can answer 404 cleanly.
    """
    command = [
        os.environ.get("KIKAI_DOCKER_BIN", "docker"),
        "logs",
        "--tail",
        str(max(1, int(tail))),
        container_name,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
        )
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    if completed.returncode != 0:
        return False, "", completed.stderr
    return True, completed.stdout, completed.stderr


def docker_rm_force(request: dict[str, Any], container_name: str) -> None:
    command = [os.environ.get("KIKAI_DOCKER_BIN", "docker"), "rm", "-f", container_name]
    try:
        # Bounded + best-effort: rm is hygiene, and it is invoked from failure
        # handlers (e.g. the docker-run timeout path) whose most likely cause is a
        # wedged docker daemon — the one scenario where an unbounded `docker rm -f`
        # would re-freeze the very caller that is trying to escape a hang.
        subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc


def docker_ps_all(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Local ``docker ps -a`` as parsed rows (name/state/status/image/running_for).

    The pipe-delimited --format mirrors execute_remote_docker_teardown_operation's
    remote listing so both surfaces describe containers identically.
    """
    fmt = "{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}|{{.RunningFor}}|{{.Labels}}"
    command = [os.environ.get("KIKAI_DOCKER_BIN", "docker"), "ps", "-a", "--format", fmt]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise OperationError(
            "operation.docker_ps_timeout",
            "docker ps did not return within 60s (docker daemon wedged?)",
            {},
        ) from exc
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    if completed.returncode != 0:
        raise OperationError(
            "operation.docker_ps_failed",
            "docker ps returned non-zero",
            {"stderr": completed.stderr.strip()[:500]},
        )
    rows: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 5)
        if len(parts) < 5:
            continue
        labels: dict[str, str] = {}
        if len(parts) > 5 and parts[5]:
            for item in parts[5].split(","):
                key, sep, value = item.partition("=")
                if sep and key.startswith("kikai."):
                    labels[key] = value
        rows.append(
            {
                "name": parts[0],
                "state": parts[1],
                "status": parts[2],
                "image": parts[3],
                "running_for": parts[4],
                "labels": labels,
            }
        )
    return rows


def tensorboard_args(port: int, logdir: str) -> list[str]:
    return [
        "sh",
        "-lc",
        "python -m pip show tb-nightly >/dev/null 2>&1 && "
        "python -m pip uninstall -y tensorboard "
        ">/tmp/kikai_tensorboard_pip_uninstall.log 2>&1 || true; "
        'exec python -m tensorboard.main "$@"',
        "kikai-tensorboard",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--logdir",
        logdir,
    ]


def argv_value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        prefix = f"{flag}="
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def tensorboard_status_from_inspect(
    *, inspect_found: bool, inspect_data: list[dict[str, Any]], port: int, logdir: str
) -> dict[str, Any]:
    container = inspect_data[0] if inspect_found and inspect_data else {}
    state = container.get("State") if isinstance(container, dict) else {}
    args = container.get("Args") if isinstance(container, dict) else []
    if not isinstance(state, dict):
        state = {}
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        args = []
    actual_port = argv_value(args, "--port")
    actual_logdir = argv_value(args, "--logdir")
    return {
        "exists": bool(inspect_found),
        "running": bool(state.get("Running")) if inspect_found else False,
        "expected_port": port,
        "actual_port": actual_port,
        "port_matches": actual_port == str(port),
        "expected_logdir": logdir,
        "actual_logdir": actual_logdir,
        "logdir_matches": actual_logdir == logdir,
        "argv": args,
    }


def execute_tensorboard_service_operation(request: dict[str, Any]) -> dict[str, Any]:
    action = require_string(
        request.get("action"),
        "operation.tensorboard_action_missing",
        "tensorboard_service request.action is required",
    )
    if action not in {"status", "ensure-running"}:
        raise OperationError(
            "operation.tensorboard_action_unsupported",
            "tensorboard_service supports only status and ensure-running",
            {"action": action},
        )
    container_id = require_string(
        request.get("container_id"),
        "operation.container_id_missing",
        "tensorboard_service request.container_id is required",
    )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "tensorboard_service request.project_root is required",
        )
    )
    container = load_container_record(project_root, container_id)
    container_name = docker_name_from_container(container, container_id)
    logdir = resolve_text_ref(
        require_string(
            request.get("logdir"),
            "operation.tensorboard_logdir_missing",
            "tensorboard_service request.logdir is required",
        )
    )
    port_value = request.get("port")
    if not isinstance(port_value, int) or port_value <= 0:
        raise OperationError(
            "operation.tensorboard_port_invalid",
            "tensorboard_service request.port must be a positive integer",
            {"port": port_value},
        )
    inspect_found, inspect_data, inspect_stderr = docker_inspect_by_name(request, container_name)
    status = tensorboard_status_from_inspect(
        inspect_found=inspect_found, inspect_data=inspect_data, port=port_value, logdir=logdir
    )
    result = {
        "execution_status": "tensorboard_service_status",
        "operation": request.get("operation"),
        "target_id": request.get("target_id"),
        "container_id": container_id,
        "container_name": container_name,
        "inspect_stderr": inspect_stderr,
        **status,
    }
    if action == "status":
        return result
    if status["running"] and status["port_matches"] and status["logdir_matches"]:
        result["execution_status"] = "tensorboard_service_running"
        result["changed"] = False
        return result
    docker_rm_force(request, container_name)
    command = docker_detached_run_command(
        request=request,
        container=container,
        container_id=container_id,
        project_root=project_root,
        container_name=container_name,
        argv=tensorboard_args(port_value, logdir),
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
        )
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    if completed.returncode != 0:
        raise OperationError(
            "operation.tensorboard_start_failed",
            "TensorBoard docker run command returned non-zero",
            {
                **result,
                "start_returncode": completed.returncode,
                "start_stdout": completed.stdout,
                "start_stderr": completed.stderr,
            },
        )
    result["execution_status"] = "tensorboard_service_running"
    result["changed"] = True
    result["start_returncode"] = completed.returncode
    result["start_stdout"] = completed.stdout
    result["start_stderr"] = completed.stderr
    return result


def execute_docker_run_operation(request: dict[str, Any]) -> dict[str, Any]:
    argv = resolve_argv_refs(validate_structured_argv(request))
    container_id = require_string(
        request.get("container_id"),
        "operation.container_id_missing",
        "docker run request.container_id is required",
    )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "docker run request.project_root is required",
        )
    )
    container = load_container_record(project_root, container_id)
    # Foreground runs are --rm one-shots: a leftover container holding this name is by
    # definition dead weight (e.g. an earlier detached ad-hoc op) and silently starves
    # every later run of the profile. Clear NON-running leftovers; a RUNNING holder is
    # a real conflict and gets a precise, actionable error instead of docker's stderr.
    # Compose the EXACT name docker_run_command will pass, including the ephemeral
    # suffix when applicable — checking the base name would flag a legitimately-
    # different-suffix concurrent invocation as a collision. The composed name is
    # kept OUTSIDE the inspect try so the timeout handler can still free it when
    # the preflight inspect itself failed. Unsafe declared names never reach
    # inspect/rm (docker rm accepts ids/prefixes).
    try:
        composed_name = _composed_docker_name(container, container_id, request)
    except OperationError:
        composed_name = None
    if composed_name is not None and not _SAFE_CONTAINER_NAME.match(composed_name):
        composed_name = None
    preflight_name = composed_name
    found, data = False, []
    if preflight_name:
        try:
            found, data, _ = docker_inspect_by_name(
                request, preflight_name, type_filter="container"
            )
        except OperationError:
            found, data = False, []  # best-effort hygiene, never a gate
    if found:
        state = data[0].get("State") if data and isinstance(data[0], dict) else None
        state = state if isinstance(state, dict) else {}
        status = state.get("Status")
        if status in ("exited", "dead"):
            # A one-shot --rm run cannot legitimately coexist with a persistent
            # same-name sibling; a terminally-stopped holder is dead weight.
            docker_rm_force(request, preflight_name)
        else:
            # running/paused/restarting/created (or unknown): the holder may be alive
            # or mid-start — never destroy it; fail precisely instead.
            raise OperationError(
                "operation.docker_run_name_in_use",
                f"a container already holds this name (status: {status}); stop it first",
                {"container_name": preflight_name, "state": status},
            )
    command = docker_run_command(
        request=request,
        container=container,
        container_id=container_id,
        project_root=project_root,
        argv=argv,
    )
    # A foreground QC/probe op with NO timeout can hang forever (stuck GPU kernel,
    # wedged container) and freeze the ENTIRE reconcile daemon — observed 2026-07-08
    # (serve ticks frozen while one op never returned). See resolve_op_timeout_sec
    # for the budget semantics (0 = explicit opt-out for known-long ops).
    timeout_sec = resolve_op_timeout_sec(request)
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        details = {
            "operation": request.get("operation"),
            "container_id": container_id,
            "timeout_sec": timeout_sec,
            "stdout_tail": _output_tail(exc.stdout),
            "stderr_tail": _output_tail(exc.stderr),
        }
        if preflight_name:
            # free the held name (bounded + best-effort inside docker_rm_force)
            # so a timed-out op never wedges its successors behind the name.
            try:
                docker_rm_force(request, preflight_name)
            except OperationError:
                pass
        else:
            # killing the CLI does not stop the workload; without a name there is
            # no teardown handle — surface that instead of implying it is gone.
            details["warning"] = (
                "container has no declared docker.name; the docker client was "
                "killed but the container may still be running"
            )
        raise OperationError(
            "operation.docker_run_timeout",
            f"docker run exceeded {timeout_sec}s and was killed",
            details,
        ) from exc
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    execution_status = "docker_run_completed" if completed.returncode == 0 else "docker_run_failed"
    result = {
        "execution_status": execution_status,
        "operation": request.get("operation"),
        "target_id": request.get("target_id"),
        "container_id": container_id,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise OperationError(
            "operation.docker_run_failed",
            "docker run command returned non-zero",
            result,
        )
    return result


def execute_docker_run_detached_operation(request: dict[str, Any]) -> dict[str, Any]:
    """Start a training container DETACHED (`docker run -d --name <docker.name>`) so the
    container's lifecycle is owned by the remote docker daemon, NOT the local/ssh caller.
    Returns immediately with the started container id; logs/stop are retrieved separately
    via remote_docker_logs / remote_docker_teardown. Artifacts persist through the host rw
    mount, so detaching is safe. Foreground behavior (`docker run --rm`) is unchanged."""
    argv = resolve_argv_refs(validate_structured_argv(request))
    container_id = require_string(
        request.get("container_id"),
        "operation.container_id_missing",
        "docker run request.container_id is required",
    )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "docker run request.project_root is required",
        )
    )
    container = load_container_record(project_root, container_id)
    # Compose the SAME name docker run will pass — honoring the container's ephemeral
    # flag and request.container_name_suffix, exactly like the foreground path. The
    # detached path previously read docker.name raw, so suffixed concurrent ops
    # collided and every rerun tripped over its own exited predecessor (2026-07-08).
    container_name = _composed_docker_name(container, container_id, request)
    if container_name is None:
        raise OperationError(
            "operation.script_bundle_run_detach_requires_name",
            "detached script_bundle_run requires the container to define docker.name",
            {"container_id": container_id},
        )
    if not _SAFE_CONTAINER_NAME.match(container_name):
        raise OperationError(
            "operation.script_bundle_run_detach_requires_name",
            "detached script_bundle_run docker.name is not a safe container name",
            {"container_id": container_id, "container_name": container_name},
        )
    # Idempotency/safety, mirroring the foreground preflight: a terminally-stopped
    # (exited/dead) holder of this exact name is dead weight — remove it so detached
    # reruns are self-healing. A RUNNING/creating holder is a real conflict: refuse,
    # never clobber.
    inspect_found, inspect_data, _inspect_stderr = docker_inspect_by_name(
        request, container_name
    )
    if inspect_found:
        state = inspect_data[0].get("State") if inspect_data and isinstance(inspect_data[0], dict) else None
        state = state if isinstance(state, dict) else {}
        status = state.get("Status")
        if status in ("exited", "dead"):
            docker_rm_force(request, container_name)
        else:
            raise OperationError(
                "operation.script_bundle_run_name_in_use",
                f"a container already holds this name (status: {status}); teardown first "
                "(remote_docker_teardown) before starting a detached run",
                {"container_id": container_id, "container_name": container_name},
            )
    command = docker_detached_run_command(
        request=request,
        container=container,
        container_id=container_id,
        project_root=project_root,
        container_name=container_name,
        argv=argv,
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
        )
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    result = {
        "execution_status": "docker_run_detached_started",
        "operation": request.get("operation"),
        "target_id": request.get("target_id"),
        "container_id": container_id,
        "container_name": container_name,
        "image": docker_image_from_container(container, container_id),
        "started_container_id": completed.stdout.strip(),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise OperationError(
            "operation.docker_run_detached_failed",
            "docker run -d command returned non-zero",
            result,
        )
    return result


def execute_docker_exec_operation(request: dict[str, Any]) -> dict[str, Any]:
    argv = resolve_argv_refs(validate_structured_argv(request))
    container_id = require_string(
        request.get("container_id"),
        "operation.container_id_missing",
        "docker_exec request.container_id is required",
    )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "docker_exec request.project_root is required",
        )
    )
    container = load_container_record(project_root, container_id)
    container_name = docker_name_from_container(container, container_id)
    command = [*docker_exec_prefix(request), container_name, *argv]
    # Same hang protection as docker_run: a hung `docker exec` (stuck GPU kernel in
    # the persistent container) froze the reconcile daemon just as effectively —
    # QC templates legitimately use script_bundle_exec, so protecting only the run
    # adapter would leave the 2026-07-08 failure mode reachable one adapter over.
    timeout_sec = resolve_op_timeout_sec(request)
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=docker_subprocess_env(request),
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise OperationError(
            "operation.docker_exec_timeout",
            f"docker exec exceeded {timeout_sec}s and the client was killed "
            "(the exec'd process inside the container may still be running)",
            {
                "operation": request.get("operation"),
                "container_id": container_id,
                "container_name": container_name,
                "timeout_sec": timeout_sec,
                "stdout_tail": _output_tail(exc.stdout),
                "stderr_tail": _output_tail(exc.stderr),
            },
        ) from exc
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.docker_not_found",
            "docker executable was not found",
            {"docker_bin": command[0]},
        ) from exc
    execution_status = (
        "docker_exec_completed" if completed.returncode == 0 else "docker_exec_failed"
    )
    result = {
        "execution_status": execution_status,
        "operation": request.get("operation"),
        "target_id": request.get("target_id"),
        "container_id": container_id,
        "container_name": container_name,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise OperationError(
            "operation.docker_exec_failed",
            "docker_exec command returned non-zero",
            result,
        )
    return result


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_snapshot_relative_path(snapshot_root: Path, value: Any, *, code: str) -> Path:
    path_text = require_string(value, code, "source snapshot file path must be a non-empty string")
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise OperationError(
            "operation.source_snapshot_invalid",
            "source snapshot file paths must be relative paths inside the snapshot",
            {"path": path_text},
        )
    return snapshot_root / relative


def load_source_snapshot(
    project_root: Path, source_snapshot_id: str
) -> tuple[dict[str, Any], Path]:
    snapshot_root = project_root / "source_snapshots" / source_snapshot_id
    snapshot_path = snapshot_root / "snapshot.json"
    if not snapshot_path.exists():
        raise OperationError(
            "container.source_snapshot_missing",
            "source snapshot manifest is missing",
            {"source_snapshot_id": source_snapshot_id, "path": str(snapshot_path)},
        )
    with snapshot_path.open("r", encoding="utf-8") as f:
        snapshot = json.load(f)
    if not isinstance(snapshot, dict):
        raise OperationError(
            "operation.source_snapshot_invalid",
            "source snapshot manifest must be a JSON object",
            {"source_snapshot_id": source_snapshot_id, "path": str(snapshot_path)},
        )
    if snapshot.get("kind") != "kikai_source_snapshot":
        raise OperationError(
            "operation.source_snapshot_invalid",
            "source snapshot kind must be kikai_source_snapshot",
            {"source_snapshot_id": source_snapshot_id, "path": str(snapshot_path)},
        )
    if snapshot.get("source_snapshot_id") != source_snapshot_id:
        raise OperationError(
            "operation.source_snapshot_id_mismatch",
            "source snapshot id does not match requested source_snapshot_id",
            {
                "expected_source_snapshot_id": source_snapshot_id,
                "actual_source_snapshot_id": snapshot.get("source_snapshot_id"),
            },
        )
    if snapshot.get("immutable") is not True:
        raise OperationError(
            "operation.source_snapshot_not_immutable",
            "source snapshot execution requires immutable: true",
            {"source_snapshot_id": source_snapshot_id},
        )
    generated_by = snapshot.get("generated_by")
    if not isinstance(generated_by, dict):
        raise OperationError(
            "operation.source_snapshot_generator_missing",
            "source snapshot manifest must be generated by `kikai source-snapshot create`",
            {"source_snapshot_id": source_snapshot_id, "path": str(snapshot_path)},
        )
    if (
        generated_by.get("tool") != "kikai source-snapshot create"
        or generated_by.get("schema_version") != 1
    ):
        raise OperationError(
            "operation.source_snapshot_generator_invalid",
            "source snapshot manifest generated_by metadata is not a supported Kikai generator",
            {"source_snapshot_id": source_snapshot_id, "generated_by": generated_by},
        )
    return snapshot, snapshot_root


def validate_source_snapshot_files(
    snapshot: dict[str, Any], snapshot_root: Path, source_snapshot_id: str
) -> None:
    files = snapshot.get("files")
    if not isinstance(files, list):
        raise OperationError(
            "operation.source_snapshot_invalid",
            "source snapshot files must be a list",
            {"source_snapshot_id": source_snapshot_id},
        )
    for item in files:
        if not isinstance(item, dict):
            raise OperationError(
                "operation.source_snapshot_invalid",
                "source snapshot file entries must be objects",
                {"source_snapshot_id": source_snapshot_id},
            )
        path = safe_snapshot_relative_path(
            snapshot_root,
            item.get("path"),
            code="operation.source_snapshot_invalid",
        )
        expected = require_string(
            item.get("sha256"),
            "operation.source_snapshot_invalid",
            "source snapshot file entry must include sha256",
        )
        if not path.exists():
            raise OperationError(
                "operation.source_snapshot_file_missing",
                "source snapshot file is missing",
                {"source_snapshot_id": source_snapshot_id, "path": str(path)},
            )
        actual = hash_file(path)
        if actual != expected:
            raise OperationError(
                "operation.source_snapshot_hash_mismatch",
                "source snapshot file hash does not match manifest",
                {
                    "source_snapshot_id": source_snapshot_id,
                    "path": str(path),
                    "expected": expected,
                    "actual": actual,
                },
            )


def safe_bundle_relative_path(bundle_root: Path, value: Any, *, code: str) -> Path:
    path_text = require_string(value, code, "script bundle file path must be a non-empty string")
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise OperationError(
            "operation.script_bundle_invalid",
            "script bundle file paths must be relative paths inside the bundle",
            {"path": path_text},
        )
    return bundle_root / relative


def load_script_bundle(project_root: Path, bundle_id: str) -> tuple[dict[str, Any], Path]:
    bundle_root = project_root / "script_bundles" / bundle_id
    bundle_path = bundle_root / "bundle.json"
    if not bundle_path.exists():
        raise OperationError(
            "operation.script_bundle_missing",
            "script bundle manifest is missing",
            {"bundle_id": bundle_id, "path": str(bundle_path)},
        )
    with bundle_path.open("r", encoding="utf-8") as f:
        bundle = json.load(f)
    if not isinstance(bundle, dict):
        raise OperationError(
            "operation.script_bundle_invalid",
            "script bundle manifest must be a JSON object",
            {"bundle_id": bundle_id, "path": str(bundle_path)},
        )
    if bundle.get("kind") != "kikai_script_bundle":
        raise OperationError(
            "operation.script_bundle_invalid",
            "script bundle kind must be kikai_script_bundle",
            {"bundle_id": bundle_id, "path": str(bundle_path)},
        )
    if bundle.get("bundle_id") != bundle_id:
        raise OperationError(
            "operation.script_bundle_id_mismatch",
            "script bundle id does not match requested bundle_id",
            {"expected_bundle_id": bundle_id, "actual_bundle_id": bundle.get("bundle_id")},
        )
    if bundle.get("immutable") is not True:
        raise OperationError(
            "operation.script_bundle_not_immutable",
            "script bundle execution requires immutable: true",
            {"bundle_id": bundle_id},
        )
    generated_by = bundle.get("generated_by")
    if not isinstance(generated_by, dict):
        raise OperationError(
            "operation.script_bundle_generator_missing",
            (
                "script bundle manifest must be generated by `kikai script-bundle create`; "
                "user-supplied sha256 manifests are not accepted"
            ),
            {"bundle_id": bundle_id, "path": str(bundle_path)},
        )
    if (
        generated_by.get("tool") != "kikai script-bundle create"
        or generated_by.get("schema_version") != 1
    ):
        raise OperationError(
            "operation.script_bundle_generator_invalid",
            "script bundle manifest generated_by metadata is not a supported Kikai generator",
            {"bundle_id": bundle_id, "generated_by": generated_by},
        )
    return bundle, bundle_root


def validate_script_bundle_files(bundle: dict[str, Any], bundle_root: Path, bundle_id: str) -> None:
    files = bundle.get("files")
    if not isinstance(files, list):
        raise OperationError(
            "operation.script_bundle_invalid",
            "script bundle files must be a list",
            {"bundle_id": bundle_id},
        )
    for item in files:
        if not isinstance(item, dict):
            raise OperationError(
                "operation.script_bundle_invalid",
                "script bundle file entries must be objects",
                {"bundle_id": bundle_id},
            )
        path = safe_bundle_relative_path(
            bundle_root,
            item.get("path"),
            code="operation.script_bundle_invalid",
        )
        expected = require_string(
            item.get("sha256"),
            "operation.script_bundle_invalid",
            "script bundle file entry must include sha256",
        )
        if not path.exists():
            raise OperationError(
                "operation.script_bundle_file_missing",
                "script bundle file is missing",
                {"bundle_id": bundle_id, "path": str(path)},
            )
        actual = hash_file(path)
        if actual != expected:
            raise OperationError(
                "operation.script_bundle_hash_mismatch",
                "script bundle file hash does not match manifest",
                {"bundle_id": bundle_id, "path": str(path), "expected": expected, "actual": actual},
            )


def script_bundle_entrypoint_argv(
    bundle: dict[str, Any], bundle_id: str, entrypoint_name: str
) -> list[str]:
    entrypoints = bundle.get("entrypoints")
    if not isinstance(entrypoints, dict) or entrypoint_name not in entrypoints:
        raise OperationError(
            "operation.script_bundle_entrypoint_missing",
            "script bundle entrypoint is missing",
            {"bundle_id": bundle_id, "entrypoint": entrypoint_name},
        )
    entrypoint = entrypoints[entrypoint_name]
    if not isinstance(entrypoint, dict):
        raise OperationError(
            "operation.script_bundle_entrypoint_invalid",
            "script bundle entrypoint must be an object",
            {"bundle_id": bundle_id, "entrypoint": entrypoint_name},
        )
    argv = entrypoint.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise OperationError(
            "operation.script_bundle_entrypoint_invalid",
            "script bundle entrypoint argv must be a non-empty list of strings",
            {"bundle_id": bundle_id, "entrypoint": entrypoint_name},
        )
    return argv


def execute_script_bundle_operation(request: dict[str, Any]) -> dict[str, Any]:
    if "argv" in request:
        raise OperationError(
            "operation.script_bundle_raw_argv_forbidden",
            "script_bundle_exec operations must use bundle entrypoints, not raw request argv",
        )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "script_bundle_exec request.project_root is required",
        )
    )
    bundle_id = require_string(
        request.get("bundle_id"),
        "operation.script_bundle_missing",
        "script_bundle_exec request.bundle_id is required",
    )
    entrypoint_name = require_string(
        request.get("entrypoint"),
        "operation.script_bundle_entrypoint_missing",
        "script_bundle_exec request.entrypoint is required",
    )
    args = request.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise OperationError(
            "operation.script_bundle_args_invalid",
            "script_bundle_exec request.args must be a list of strings",
            {"bundle_id": bundle_id},
        )
    bundle, bundle_root = load_script_bundle(project_root, bundle_id)
    validate_script_bundle_files(bundle, bundle_root, bundle_id)
    expanded_argv = resolve_argv_refs(
        [*script_bundle_entrypoint_argv(bundle, bundle_id, entrypoint_name), *args]
    )
    docker_request = dict(request)
    docker_request["adapter"] = "docker_exec"
    docker_request["argv"] = expanded_argv
    result = execute_docker_exec_operation(docker_request)
    result["execution_status"] = "script_bundle_exec_completed"
    result["bundle_id"] = bundle_id
    result["entrypoint"] = entrypoint_name
    result["expanded_argv"] = expanded_argv
    return result


def execute_script_bundle_run_operation(request: dict[str, Any]) -> dict[str, Any]:
    if "argv" in request:
        raise OperationError(
            "operation.script_bundle_raw_argv_forbidden",
            "script_bundle_run operations must use bundle entrypoints, not raw request argv",
        )
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "script_bundle_run request.project_root is required",
        )
    )
    bundle_id = require_string(
        request.get("bundle_id"),
        "operation.script_bundle_missing",
        "script_bundle_run request.bundle_id is required",
    )
    entrypoint_name = require_string(
        request.get("entrypoint"),
        "operation.script_bundle_entrypoint_missing",
        "script_bundle_run request.entrypoint is required",
    )
    args = request.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise OperationError(
            "operation.script_bundle_args_invalid",
            "script_bundle_run request.args must be a list of strings",
            {"bundle_id": bundle_id},
        )
    detach = request.get("detach", False)
    if not isinstance(detach, bool):
        raise OperationError(
            "operation.script_bundle_run_detach_invalid",
            "script_bundle_run request.detach must be a boolean",
            {"bundle_id": bundle_id},
        )
    bundle, bundle_root = load_script_bundle(project_root, bundle_id)
    validate_script_bundle_files(bundle, bundle_root, bundle_id)
    expanded_argv = resolve_argv_refs(
        [*script_bundle_entrypoint_argv(bundle, bundle_id, entrypoint_name), *args]
    )
    docker_request = dict(request)
    docker_request["argv"] = expanded_argv
    if detach:
        # Detached: container lifecycle is owned by remote docker; return immediately
        # with the started container id. Stop via remote_docker_teardown, inspect via
        # docker ps, debug via remote_docker_logs.
        docker_request["adapter"] = "docker_run_detached"
        result = execute_docker_run_detached_operation(docker_request)
        result["execution_status"] = "script_bundle_run_detached_started"
    else:
        docker_request["adapter"] = "docker_run"
        result = execute_docker_run_operation(docker_request)
        result["execution_status"] = "script_bundle_run_completed"
    result["bundle_id"] = bundle_id
    result["entrypoint"] = entrypoint_name
    result["expanded_argv"] = expanded_argv
    return result


def source_snapshot_exclusions() -> tuple[set[str], set[str], set[str]]:
    return (
        {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"},
        {".DS_Store"},
        {".pyc", ".pyo"},
    )


def collect_snapshot_source_files(
    *,
    source_root: Path,
    file_paths: list[str],
    include_dirs: list[str] | None,
    code_prefix: str,
) -> list[Path]:
    include_dirs = include_dirs or []
    if not file_paths and not include_dirs:
        raise OperationError(
            f"{code_prefix}.create_file_missing",
            "at least one --file or --include-dir is required",
        )
    excluded_dir_names, excluded_file_names, excluded_file_suffixes = source_snapshot_exclusions()

    def normalize_source_relative(path_text: str, *, code: str, path_kind: str) -> Path:
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts or not path_text or path_text == ".":
            raise OperationError(
                code,
                f"source {path_kind} must be a relative path inside source root",
                {"path": path_text},
            )
        return relative

    def should_skip_source_file(relative: Path) -> bool:
        return (
            any(part in excluded_dir_names for part in relative.parts)
            or relative.name in excluded_file_names
            or relative.suffix in excluded_file_suffixes
        )

    normalized_files: list[Path] = []
    seen: set[str] = set()

    def add_source_file(relative: Path, *, original_path: str) -> None:
        if should_skip_source_file(relative):
            return
        normalized = relative.as_posix()
        if normalized in seen:
            raise OperationError(
                f"{code_prefix}.create_file_duplicate",
                "source file is listed more than once",
                {"path": normalized},
            )
        source_path = source_root / relative
        if not source_path.exists() or not source_path.is_file():
            raise OperationError(
                f"{code_prefix}.create_file_missing",
                "source file does not exist",
                {"path": original_path, "source_path": str(source_path)},
            )
        seen.add(normalized)
        normalized_files.append(relative)

    for file_text in file_paths:
        relative = normalize_source_relative(
            file_text,
            code=f"{code_prefix}.create_file_path_invalid",
            path_kind="files",
        )
        add_source_file(relative, original_path=file_text)

    for dir_text in include_dirs:
        relative_dir = normalize_source_relative(
            dir_text,
            code=f"{code_prefix}.create_include_dir_path_invalid",
            path_kind="directories",
        )
        source_dir = source_root / relative_dir
        if not source_dir.exists() or not source_dir.is_dir():
            raise OperationError(
                f"{code_prefix}.create_include_dir_missing",
                "source include directory does not exist",
                {"path": dir_text, "source_path": str(source_dir)},
            )
        for source_path in sorted(source_dir.rglob("*")):
            if source_path.is_file():
                add_source_file(
                    source_path.relative_to(source_root),
                    original_path=str(source_path),
                )

    if not normalized_files:
        raise OperationError(
            f"{code_prefix}.create_file_missing",
            "no source files matched --file or --include-dir",
        )
    return normalized_files


def create_source_snapshot(
    *,
    project_root: Path,
    source_root: Path,
    source_snapshot_id: str,
    file_paths: list[str],
    include_dirs: list[str] | None = None,
) -> dict[str, Any]:
    if not project_root.exists():
        project_root.mkdir(parents=True)
    if not source_root.exists():
        raise OperationError(
            "source_snapshot.create_source_root_missing",
            "source root does not exist",
            {"source_root": str(source_root)},
        )
    if not source_snapshot_id or Path(source_snapshot_id).name != source_snapshot_id:
        raise OperationError(
            "source_snapshot.create_snapshot_id_invalid",
            "source_snapshot_id must be a single path segment",
            {"source_snapshot_id": source_snapshot_id},
        )
    normalized_files = collect_snapshot_source_files(
        source_root=source_root,
        file_paths=file_paths,
        include_dirs=include_dirs,
        code_prefix="source_snapshot",
    )
    snapshot_root = project_root / "source_snapshots" / source_snapshot_id
    if snapshot_root.exists():
        raise OperationError(
            "source_snapshot.create_snapshot_exists",
            "source snapshot already exists and will not be overwritten",
            {"source_snapshot_id": source_snapshot_id, "snapshot_dir": str(snapshot_root)},
        )

    files_manifest: list[dict[str, str]] = []
    try:
        for relative in sorted(normalized_files, key=lambda item: item.as_posix()):
            source_path = source_root / relative
            target_path = snapshot_root / "root" / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            files_manifest.append(
                {"path": f"root/{relative.as_posix()}", "sha256": hash_file(target_path)}
            )
        manifest = {
            "schema_version": 1,
            "kind": "kikai_source_snapshot",
            "source_snapshot_id": source_snapshot_id,
            "immutable": True,
            "generated_by": {
                "tool": "kikai source-snapshot create",
                "schema_version": 1,
            },
            "files": files_manifest,
        }
        manifest_path = snapshot_root / "snapshot.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        loaded, loaded_root = load_source_snapshot(project_root, source_snapshot_id)
        validate_source_snapshot_files(loaded, loaded_root, source_snapshot_id)
    except Exception:
        if snapshot_root.exists():
            shutil.rmtree(snapshot_root)
        raise

    return {
        "source_snapshot_id": source_snapshot_id,
        "snapshot_dir": str(snapshot_root),
        "snapshot_manifest": str(snapshot_root / "snapshot.json"),
        "source_root": str(source_root),
        "file_count": len(files_manifest),
    }


BUNDLE_EXCLUDED_DIR_NAMES = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
BUNDLE_EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})
BUNDLE_EXCLUDED_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def should_skip_bundle_file(relative: Path) -> bool:
    """Files create_script_bundle silently drops; upload idempotency must agree."""
    return (
        any(part in BUNDLE_EXCLUDED_DIR_NAMES for part in relative.parts)
        or relative.name in BUNDLE_EXCLUDED_FILE_NAMES
        or relative.suffix in BUNDLE_EXCLUDED_FILE_SUFFIXES
    )


def create_script_bundle(
    *,
    project_root: Path,
    source_root: Path,
    bundle_id: str,
    entrypoint: str | None = None,
    file_paths: list[str],
    entrypoint_argv: list[str] | None = None,
    include_dirs: list[str] | None = None,
    entrypoints: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Create an immutable script bundle.

    Entrypoints come either as the single ``entrypoint``/``entrypoint_argv`` pair (the
    CLI form, unchanged) or as an ``entrypoints`` map ``{name: argv}`` (the server's
    bundle-upload form; bundle.json and the run-side ``script_bundle_entrypoint_argv``
    already support multiple entrypoints — this only generalizes the create side).
    """
    if not project_root.exists():
        project_root.mkdir(parents=True)
    if not source_root.exists():
        raise OperationError(
            "script_bundle.create_source_root_missing",
            "source root does not exist",
            {"source_root": str(source_root)},
        )
    if not bundle_id or Path(bundle_id).name != bundle_id:
        raise OperationError(
            "script_bundle.create_bundle_id_invalid",
            "bundle_id must be a single path segment",
            {"bundle_id": bundle_id},
        )
    if entrypoints is None:
        if not entrypoint:
            raise OperationError(
                "script_bundle.create_entrypoint_invalid",
                "entrypoint must be non-empty",
            )
        entrypoints = {entrypoint: list(entrypoint_argv or [])}
    if not entrypoints:
        raise OperationError(
            "script_bundle.create_entrypoint_invalid",
            "at least one entrypoint is required",
        )
    for name, argv in entrypoints.items():
        if not isinstance(name, str) or not name:
            raise OperationError(
                "script_bundle.create_entrypoint_invalid",
                "entrypoint names must be non-empty strings",
            )
        if not argv or not all(isinstance(item, str) for item in argv):
            raise OperationError(
                "script_bundle.create_argv_invalid",
                "entrypoint argv must be a non-empty list of strings",
                {"entrypoint": name},
            )
        validate_structured_argv({"argv": argv})
    include_dirs = include_dirs or []
    if not file_paths and not include_dirs:
        raise OperationError(
            "script_bundle.create_file_missing",
            "at least one --file or --include-dir is required",
        )

    def normalize_source_relative(path_text: str, *, code: str, path_kind: str) -> Path:
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts or not path_text or path_text == ".":
            raise OperationError(
                code,
                f"bundle source {path_kind} must be a relative path inside source root",
                {"path": path_text},
            )
        return relative

    normalized_files: list[Path] = []
    seen: set[str] = set()

    def add_source_file(relative: Path, *, original_path: str) -> None:
        if should_skip_bundle_file(relative):
            return
        normalized = relative.as_posix()
        if normalized in seen:
            raise OperationError(
                "script_bundle.create_file_duplicate",
                "bundle source file is listed more than once",
                {"path": normalized},
            )
        source_path = source_root / relative
        if not source_path.exists() or not source_path.is_file():
            raise OperationError(
                "script_bundle.create_file_missing",
                "bundle source file does not exist",
                {"path": original_path, "source_path": str(source_path)},
            )
        seen.add(normalized)
        normalized_files.append(relative)

    for file_text in file_paths:
        relative = normalize_source_relative(
            file_text,
            code="script_bundle.create_file_path_invalid",
            path_kind="files",
        )
        add_source_file(relative, original_path=file_text)

    for dir_text in include_dirs:
        relative_dir = normalize_source_relative(
            dir_text,
            code="script_bundle.create_include_dir_path_invalid",
            path_kind="directories",
        )
        source_dir = source_root / relative_dir
        if not source_dir.exists() or not source_dir.is_dir():
            raise OperationError(
                "script_bundle.create_include_dir_missing",
                "bundle source include directory does not exist",
                {"path": dir_text, "source_path": str(source_dir)},
            )
        for source_path in sorted(source_dir.rglob("*")):
            if source_path.is_file():
                add_source_file(
                    source_path.relative_to(source_root),
                    original_path=str(source_path),
                )

    if not normalized_files:
        raise OperationError(
            "script_bundle.create_file_missing",
            "no bundle source files matched --file or --include-dir",
        )

    bundle_root = project_root / "script_bundles" / bundle_id
    if bundle_root.exists():
        raise OperationError(
            "script_bundle.create_bundle_exists",
            "script bundle already exists and will not be overwritten",
            {"bundle_id": bundle_id, "bundle_dir": str(bundle_root)},
        )

    rewrite_map = {
        relative.as_posix(): f"script_bundles/{bundle_id}/root/{relative.as_posix()}"
        for relative in normalized_files
    }
    source_root_resolved = source_root.resolve()
    all_argv_items = [item for argv in entrypoints.values() for item in argv]
    for item in all_argv_items:
        item_path = Path(item)
        if item_path.is_absolute():
            try:
                is_source_path = item_path.resolve().is_relative_to(source_root_resolved)
            except OSError:
                is_source_path = False
            if is_source_path:
                raise OperationError(
                    "script_bundle.create_unmanaged_source_argv_forbidden",
                    (
                        "entrypoint argv must not reference source-root files directly; "
                        "include them in the bundle and use source-relative argv so "
                        "Kikai rewrites them"
                    ),
                    {"argv_item": item, "source_root": str(source_root)},
                )
            continue
        if item in rewrite_map:
            continue
        if ".." in item_path.parts:
            continue
        candidate = source_root / item_path
        if candidate.exists():
            raise OperationError(
                "script_bundle.create_unmanaged_source_argv_forbidden",
                (
                    "entrypoint argv references a source-root path that is not part "
                    "of the immutable bundle"
                ),
                {"argv_item": item, "source_path": str(candidate)},
            )
    rewritten_entrypoints = {
        name: {"argv": [rewrite_map.get(item, item) for item in argv]}
        for name, argv in entrypoints.items()
    }

    files_manifest: list[dict[str, str]] = []
    try:
        for relative in sorted(normalized_files, key=lambda item: item.as_posix()):
            source_path = source_root / relative
            target_path = bundle_root / "root" / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            files_manifest.append(
                {
                    "path": f"root/{relative.as_posix()}",
                    "sha256": hash_file(target_path),
                }
            )
        manifest = {
            "schema_version": 1,
            "kind": "kikai_script_bundle",
            "bundle_id": bundle_id,
            "immutable": True,
            "generated_by": {
                "tool": "kikai script-bundle create",
                "schema_version": 1,
            },
            "entrypoints": rewritten_entrypoints,
            "files": files_manifest,
        }
        manifest_path = bundle_root / "bundle.json"
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        manifest_path.write_text(manifest_text)
        loaded, loaded_root = load_script_bundle(project_root, bundle_id)
        validate_script_bundle_files(loaded, loaded_root, bundle_id)
    except Exception:
        if bundle_root.exists():
            shutil.rmtree(bundle_root)
        raise

    result = {
        "bundle_id": bundle_id,
        "bundle_dir": str(bundle_root),
        "bundle_manifest": str(bundle_root / "bundle.json"),
        "entrypoints": sorted(rewritten_entrypoints),
        "file_count": len(files_manifest),
    }
    if entrypoint is not None and entrypoint in rewritten_entrypoints:
        # Back-compat: the CLI single-entrypoint form keeps its original result fields.
        result["entrypoint"] = entrypoint
        result["entrypoint_argv"] = rewritten_entrypoints[entrypoint]["argv"]
    return result


def load_delivery_target(project_root: Path, target_id: str) -> dict[str, Any]:
    targets_dir = project_root / "delivery_targets"
    candidates = [targets_dir / f"{target_id}.json", targets_dir / f"{target_id}.yaml"]
    target_path = next((path for path in candidates if path.exists()), None)
    if target_path is None:
        raise OperationError(
            "operation.delivery_target_missing",
            "artifact_delivery target record is missing",
            {"target_id": target_id, "searched": [str(path) for path in candidates]},
        )
    with target_path.open("r", encoding="utf-8") as f:
        if target_path.suffix == ".json":
            target = json.load(f)
        else:
            target = yaml.safe_load(f) or {}
    if not isinstance(target, dict):
        raise OperationError(
            "operation.delivery_target_invalid",
            "artifact_delivery target record must be an object",
            {"target_id": target_id, "path": str(target_path)},
        )
    if target.get("target_id") != target_id:
        raise OperationError(
            "operation.delivery_target_id_mismatch",
            "artifact_delivery target id does not match requested delivery_target_id",
            {"expected_target_id": target_id, "actual_target_id": target.get("target_id")},
        )
    return target


def build_multipart_body(*, message: str, file_path: Path) -> tuple[bytes, str]:
    boundary_seed = f"{file_path.name}:{file_path.stat().st_size}:{message}"
    boundary = "kikai-" + hashlib.sha256(boundary_seed.encode("utf-8")).hexdigest()[:32]
    payload_json = json.dumps({"content": message}, ensure_ascii=False).encode("utf-8")
    file_bytes = file_path.read_bytes()
    filename = file_path.name
    parts = [
        b"--" + boundary.encode("ascii"),
        b'Content-Disposition: form-data; name="payload_json"',
        b"Content-Type: application/json; charset=utf-8",
        b"",
        payload_json,
        b"--" + boundary.encode("ascii"),
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"'.encode(),
        b"Content-Type: application/octet-stream",
        b"",
        file_bytes,
        b"--" + boundary.encode("ascii") + b"--",
        b"",
    ]
    return b"\r\n".join(parts), boundary


def post_discord_webhook(
    *,
    webhook_url: str,
    message: str,
    file_path: Path,
    max_retries: int = 3,
    retry_delay_sec: int = 10,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    body, boundary = build_multipart_body(message=message, file_path=file_path)
    request = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "User-Agent": "kikai-lab/0.1 artifact-delivery",
        },
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                response_body = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            if 500 <= exc.code < 600 and attempt < max_retries:
                attempt += 1
                retry_sleep(retry_delay_sec)
                continue
            raise OperationError(
                "operation.delivery_http_failed",
                "artifact_delivery webhook returned non-success HTTP status",
                {
                    "http_status": exc.code,
                    "response_body": response_body,
                    "attempts": attempt + 1,
                },
            ) from exc
        except urllib.error.URLError as exc:
            raise OperationError(
                "operation.delivery_http_error",
                "artifact_delivery webhook request failed",
                {"reason": str(exc.reason), "attempts": attempt + 1},
            ) from exc
        if status >= 400:
            if 500 <= status < 600 and attempt < max_retries:
                attempt += 1
                retry_sleep(retry_delay_sec)
                continue
            raise OperationError(
                "operation.delivery_http_failed",
                "artifact_delivery webhook returned non-success HTTP status",
                {
                    "http_status": status,
                    "response_body": response_body,
                    "attempts": attempt + 1,
                },
            )
        return {"http_status": status, "response_body": response_body, "attempts": attempt + 1}


def post_discord_json_webhook(*, webhook_url: str, message: str) -> dict[str, Any]:
    body = json.dumps({"content": message}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "User-Agent": "kikai-lab/0.1 webhook-notification",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            response_body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise OperationError(
            "operation.notification_http_failed",
            "webhook_notification returned non-success HTTP status",
            {"http_status": exc.code, "response_body": response_body},
        ) from exc
    except urllib.error.URLError as exc:
        raise OperationError(
            "operation.notification_http_error",
            "webhook_notification request failed",
            {"reason": str(exc.reason)},
        ) from exc
    if status >= 400:
        raise OperationError(
            "operation.notification_http_failed",
            "webhook_notification returned non-success HTTP status",
            {"http_status": status, "response_body": response_body},
        )
    return {"http_status": status, "response_body": response_body}


def discord_webhook_url_from_target(target: dict[str, Any], target_id: str) -> str:
    if target.get("kind") != "discord_webhook":
        raise OperationError(
            "operation.delivery_target_kind_unsupported",
            "webhook operations currently support only discord_webhook targets",
            {"target_id": target_id, "kind": target.get("kind")},
        )
    webhook_url = resolve_text_ref(
        require_string(
            target.get("webhook_url"),
            "operation.delivery_webhook_url_missing",
            "discord_webhook delivery target must define webhook_url",
        )
    )
    if not (webhook_url.startswith("http://") or webhook_url.startswith("https://")):
        raise OperationError(
            "operation.delivery_webhook_url_invalid",
            "discord_webhook URL must be http or https",
            {"target_id": target_id},
        )
    return webhook_url


def execute_webhook_notification_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "webhook_notification request.project_root is required",
        )
    )
    notification_id = require_string(
        request.get("notification_id"),
        "operation.notification_id_missing",
        "webhook_notification request.notification_id is required",
    )
    delivery_target_id = require_string(
        request.get("delivery_target_id"),
        "operation.delivery_target_id_missing",
        "webhook_notification request.delivery_target_id is required",
    )
    message = require_string(
        request.get("message"),
        "operation.notification_message_missing",
        "webhook_notification request.message is required",
    )
    target = load_delivery_target(project_root, delivery_target_id)
    webhook_url = discord_webhook_url_from_target(target, delivery_target_id)
    notifications_dir = project_root / "notifications"
    record_path = notifications_dir / f"{notification_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.notification_record_exists",
            "webhook_notification record already exists and will not be overwritten",
            {"notification_id": notification_id, "path": str(record_path)},
        )
    response = post_discord_json_webhook(webhook_url=webhook_url, message=message)
    record = {
        "schema_version": 1,
        "notification_id": notification_id,
        "target_id": delivery_target_id,
        "status": "delivered",
        "delivered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "message": message,
        "severity": request.get("severity", "info"),
        "run_name": request.get("run_name"),
        "http_status": response["http_status"],
        "response_body": response["response_body"],
    }
    notifications_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "execution_status": "webhook_notification_completed",
        "operation": request.get("operation"),
        "notification_id": notification_id,
        "target_id": delivery_target_id,
        "http_status": response["http_status"],
        "notification_record": str(record_path),
    }


CHECKPOINT_STEP_RE = re.compile(r"(?:^|[_-])step[_-]?(\d+)")


def optional_int(value: Any, *, code: str, message: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise OperationError(code, message, {"value": value})
    if isinstance(value, int):
        return value
    raise OperationError(code, message, {"value": value})


def read_tail_jsonl(path: Path, *, max_rows: int) -> list[dict[str, Any]]:
    if max_rows <= 0:
        raise OperationError(
            "operation.metrics_tail_rows_invalid",
            "training_progress_backfill metrics_tail_rows must be positive",
            {"metrics_tail_rows": max_rows},
        )
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise OperationError(
                    "operation.metrics_jsonl_invalid",
                    "training_progress_backfill metrics JSONL contains invalid JSON",
                    {"path": str(path), "line": line_number},
                ) from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows[-max_rows:]


def row_step(row: dict[str, Any]) -> int | None:
    step = row.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        return step
    return None


def checkpoint_step_from_name(path: Path) -> int | None:
    match = CHECKPOINT_STEP_RE.search(path.name)
    if match is None:
        return None
    return int(match.group(1))


def latest_checkpoint_record(checkpoint_dir: Path) -> dict[str, Any] | None:
    if not checkpoint_dir.exists():
        return None
    candidates: list[tuple[int, Path]] = []
    for path in checkpoint_dir.glob("*"):
        if not path.is_file():
            continue
        step = checkpoint_step_from_name(path)
        if step is not None:
            candidates.append((step, path))
    if not candidates:
        return None
    step, path = max(candidates, key=lambda item: (item[0], item[1].stat().st_mtime, item[1].name))
    return {"step": step, "path": str(path), "name": path.name}


def tensorboard_event_count(tensorboard_dir: Path) -> int:
    if not tensorboard_dir.exists():
        return 0
    return sum(1 for path in tensorboard_dir.rglob("events.out.tfevents*") if path.is_file())


def compact_metric_fields(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(row):
        if key in {"event", "path", "step", "timestamp", "time"}:
            continue
        value = row[key]
        if isinstance(value, int | float) and not isinstance(value, bool):
            parts.append(f"{key}={value}")
        elif isinstance(value, str) and len(value) <= 80:
            parts.append(f"{key}={value}")
    return ", ".join(parts[:8])


def build_training_progress_backfill_message(
    *,
    prefix: str,
    run_name: str,
    model_arch: str | None,
    latest_step: int | None,
    max_steps: int | None,
    latest_checkpoint: dict[str, Any] | None,
    metric_tail: list[dict[str, Any]],
    tb_event_count: int,
) -> str:
    step_text = "step unknown"
    if latest_step is not None and max_steps is not None:
        step_text = f"step {latest_step}/{max_steps}"
    elif latest_step is not None:
        step_text = f"step {latest_step}"
    checkpoint_text = "checkpoint none"
    if latest_checkpoint is not None:
        checkpoint_text = (
            f"checkpoint {latest_checkpoint['name']} (step {latest_checkpoint['step']})"
        )
    latest_metric = next(
        (
            row
            for row in reversed(metric_tail)
            if row_step(row) == latest_step and compact_metric_fields(row)
        ),
        None,
    )
    if latest_metric is None:
        latest_metric = next(
            (row for row in reversed(metric_tail) if compact_metric_fields(row)), None
        )
    metric_text = (
        compact_metric_fields(latest_metric) if latest_metric is not None else "metrics none"
    )
    arch_text = f" arch={model_arch}" if model_arch else ""
    return (
        f"{prefix}: {run_name}{arch_text} backfill — {step_text}; "
        f"{checkpoint_text}; TensorBoard events={tb_event_count}; latest metrics: {metric_text}"
    )


def execute_training_progress_backfill_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        resolve_text_ref(
            require_string(
                request.get("project_root"),
                "operation.project_root_missing",
                "training_progress_backfill request.project_root is required",
            )
        )
    )
    run_name = require_string(
        request.get("run_name"),
        "operation.run_name_missing",
        "training_progress_backfill request.run_name is required",
    )
    run_dir = Path(
        resolve_text_ref(
            require_string(
                request.get("run_dir"),
                "operation.run_dir_missing",
                "training_progress_backfill request.run_dir is required",
            )
        )
    )
    if not run_dir.exists():
        raise OperationError(
            "operation.run_dir_missing",
            "training_progress_backfill run_dir does not exist",
            {"run_dir": str(run_dir)},
        )
    notification_id = require_string(
        request.get("notification_id"),
        "operation.notification_id_missing",
        "training_progress_backfill request.notification_id is required",
    )
    delivery_target_id = require_string(
        request.get("delivery_target_id"),
        "operation.delivery_target_id_missing",
        "training_progress_backfill request.delivery_target_id is required",
    )
    metrics_tail_rows = optional_int(
        request.get("metrics_tail_rows", 20),
        code="operation.metrics_tail_rows_invalid",
        message="training_progress_backfill metrics_tail_rows must be an integer",
    )
    assert metrics_tail_rows is not None
    max_steps = optional_int(
        request.get("max_steps"),
        code="operation.max_steps_invalid",
        message="training_progress_backfill max_steps must be an integer",
    )
    metrics_path = Path(
        resolve_text_ref(request.get("metrics_path", str(run_dir / "metrics.jsonl")))
    )
    checkpoint_dir = Path(
        resolve_text_ref(request.get("checkpoint_dir", str(run_dir / "checkpoints")))
    )
    tensorboard_dir = Path(
        resolve_text_ref(request.get("tensorboard_dir", str(run_dir / "tensorboard")))
    )
    metric_tail = read_tail_jsonl(metrics_path, max_rows=metrics_tail_rows)
    latest_step = max(
        (step for row in metric_tail if (step := row_step(row)) is not None), default=None
    )
    latest_checkpoint = latest_checkpoint_record(checkpoint_dir)
    tb_event_count = tensorboard_event_count(tensorboard_dir)
    prefix = request.get("message_prefix", "Kikai training progress backfill")
    if not isinstance(prefix, str) or not prefix:
        raise OperationError(
            "operation.message_prefix_invalid",
            "training_progress_backfill message_prefix must be a non-empty string",
        )
    model_arch = request.get("model_arch")
    if model_arch is not None and not isinstance(model_arch, str):
        raise OperationError(
            "operation.model_arch_invalid",
            "training_progress_backfill model_arch must be a string",
        )
    message = build_training_progress_backfill_message(
        prefix=prefix,
        run_name=run_name,
        model_arch=model_arch,
        latest_step=latest_step,
        max_steps=max_steps,
        latest_checkpoint=latest_checkpoint,
        metric_tail=metric_tail,
        tb_event_count=tb_event_count,
    )
    target = load_delivery_target(project_root, delivery_target_id)
    webhook_url = discord_webhook_url_from_target(target, delivery_target_id)
    notifications_dir = project_root / "notifications"
    record_path = notifications_dir / f"{notification_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.notification_record_exists",
            (
                "training_progress_backfill notification record already exists "
                "and will not be overwritten"
            ),
            {"notification_id": notification_id, "path": str(record_path)},
        )
    response = post_discord_json_webhook(webhook_url=webhook_url, message=message)
    record = {
        "schema_version": 1,
        "notification_id": notification_id,
        "target_id": delivery_target_id,
        "status": "delivered",
        "delivered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "message": message,
        "severity": request.get("severity", "info"),
        "run_name": run_name,
        "model_arch": model_arch,
        "run_dir": str(run_dir),
        "metrics_path": str(metrics_path),
        "latest_step": latest_step,
        "max_steps": max_steps,
        "latest_checkpoint": latest_checkpoint,
        "tensorboard_event_count": tb_event_count,
        "metric_tail": metric_tail,
        "http_status": response["http_status"],
        "response_body": response["response_body"],
    }
    notifications_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "execution_status": "training_progress_backfill_completed",
        "operation": request.get("operation"),
        "notification_id": notification_id,
        "target_id": delivery_target_id,
        "http_status": response["http_status"],
        "notification_record": str(record_path),
        "latest_step": latest_step,
        "latest_checkpoint_step": latest_checkpoint["step"]
        if latest_checkpoint is not None
        else None,
        "tensorboard_event_count": tb_event_count,
    }


def operation_sequence_record(
    *,
    pipeline_run_id: str,
    status: str,
    operation: str | None,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pipeline_run_id": pipeline_run_id,
        "operation": operation,
        "status": status,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "steps": steps,
    }


def write_operation_sequence_record(project_root: Path, record: dict[str, Any]) -> Path:
    pipeline_run_id = require_string(
        record.get("pipeline_run_id"),
        "operation.sequence_pipeline_run_id_missing",
        "operation_sequence record requires pipeline_run_id",
    )
    record_dir = project_root / "pipeline_runs"
    record_path = record_dir / f"{pipeline_run_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.sequence_record_exists",
            "operation_sequence pipeline_run record already exists and will not be overwritten",
            {"pipeline_run_id": pipeline_run_id, "path": str(record_path)},
        )
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return record_path


def validated_sequence_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise OperationError(
            "operation.sequence_steps_invalid",
            "operation_sequence request.steps must be a non-empty list",
        )
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, step in enumerate(value):
        if not isinstance(step, dict):
            raise OperationError(
                "operation.sequence_step_invalid",
                "operation_sequence step must be an object",
                {"index": index},
            )
        step_id = require_string(
            step.get("step_id"),
            "operation.sequence_step_id_missing",
            "operation_sequence step.step_id is required",
        )
        if step_id in seen:
            raise OperationError(
                "operation.sequence_step_id_duplicate",
                "operation_sequence step_id values must be unique",
                {"step_id": step_id},
            )
        child_request = step.get("request")
        if not isinstance(child_request, dict):
            raise OperationError(
                "operation.sequence_step_request_missing",
                "operation_sequence step.request must be an object",
                {"step_id": step_id},
            )
        if child_request.get("adapter") == "operation_sequence":
            raise OperationError(
                "operation.sequence_nested_forbidden",
                "operation_sequence steps must not recursively use operation_sequence",
                {"step_id": step_id},
            )
        seen.add(step_id)
        steps.append({"step_id": step_id, "request": child_request})
    return steps


def execute_operation_sequence(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "operation_sequence request.project_root is required",
        )
    )
    pipeline_run_id = require_string(
        request.get("pipeline_run_id"),
        "operation.sequence_pipeline_run_id_missing",
        "operation_sequence request.pipeline_run_id is required",
    )
    record_path = project_root / "pipeline_runs" / f"{pipeline_run_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.sequence_record_exists",
            "operation_sequence pipeline_run record already exists and will not be overwritten",
            {"pipeline_run_id": pipeline_run_id, "path": str(record_path)},
        )
    steps = validated_sequence_steps(request.get("steps"))
    step_records: list[dict[str, Any]] = []
    for step in steps:
        step_id = step["step_id"]
        child_request = dict(step["request"])
        child_request.setdefault("project_root", str(project_root))
        try:
            result = execute_operation({"request": child_request})
        except OperationError as exc:
            step_records.append(
                {
                    "step_id": step_id,
                    "status": "failed",
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            )
            failed_record = operation_sequence_record(
                pipeline_run_id=pipeline_run_id,
                status="failed",
                operation=request.get("operation"),
                steps=step_records,
            )
            failed_record_path = write_operation_sequence_record(project_root, failed_record)
            raise OperationError(
                "operation.sequence_step_failed",
                "operation_sequence stopped after a failed step",
                {
                    "pipeline_run_id": pipeline_run_id,
                    "pipeline_record": str(failed_record_path),
                    "failed_step_id": step_id,
                    "step_error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
            ) from exc
        step_records.append(
            {
                "step_id": step_id,
                "status": "completed",
                "execution_status": result.get("execution_status"),
                "result": result,
            }
        )
    completed_record = operation_sequence_record(
        pipeline_run_id=pipeline_run_id,
        status="completed",
        operation=request.get("operation"),
        steps=step_records,
    )
    completed_record_path = write_operation_sequence_record(project_root, completed_record)
    return {
        "execution_status": "operation_sequence_completed",
        "operation": request.get("operation"),
        "pipeline_run_id": pipeline_run_id,
        "pipeline_record": str(completed_record_path),
        "steps": step_records,
    }


def load_current_record(project_root: Path) -> dict[str, Any]:
    current_path = project_root / "current.json"
    if not current_path.exists():
        raise OperationError(
            "operation.current_missing",
            "checkpoint_guard requires current.json",
            {"path": str(current_path)},
        )
    with current_path.open("r", encoding="utf-8") as f:
        current = json.load(f)
    if not isinstance(current, dict):
        raise OperationError(
            "operation.current_invalid",
            "current.json must contain an object",
            {"path": str(current_path)},
        )
    return current


def current_list(current: dict[str, Any], key: str) -> list[str]:
    value = current.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OperationError(
            "operation.current_invalid",
            f"current.{key} must be a list of strings when present",
            {"key": key},
        )
    return value


def assert_current_string_match(
    *,
    field: str,
    requested: str,
    current: dict[str, Any],
    current_key: str,
) -> None:
    expected = require_string(
        current.get(current_key),
        "operation.current_invalid",
        f"current.{current_key} must be a non-empty string",
    )
    if requested != expected:
        raise OperationError(
            "operation.checkpoint_guard_mismatch",
            "checkpoint_guard request does not match current pointer",
            {"field": field, "expected": expected, "actual": requested},
        )


def execute_checkpoint_guard_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "checkpoint_guard request.project_root is required",
        )
    )
    guard_id = require_string(
        request.get("guard_id"),
        "operation.checkpoint_guard_id_missing",
        "checkpoint_guard request.guard_id is required",
    )
    run_name = require_string(
        request.get("run_name"),
        "operation.checkpoint_guard_run_name_missing",
        "checkpoint_guard request.run_name is required",
    )
    checkpoint = require_string(
        request.get("checkpoint"),
        "operation.checkpoint_guard_checkpoint_missing",
        "checkpoint_guard request.checkpoint is required",
    )
    model_arch = require_string(
        request.get("model_arch"),
        "operation.checkpoint_guard_model_arch_missing",
        "checkpoint_guard request.model_arch is required",
    )
    artifact_class = require_string(
        request.get("artifact_class"),
        "operation.checkpoint_guard_artifact_class_missing",
        "checkpoint_guard request.artifact_class is required",
    )
    record_dir = project_root / "guard_records"
    record_path = record_dir / f"{guard_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.checkpoint_guard_record_exists",
            "checkpoint_guard record already exists and will not be overwritten",
            {"guard_id": guard_id, "path": str(record_path)},
        )
    current = load_current_record(project_root)
    if run_name in current_list(current, "do_not_use_as_current"):
        raise OperationError(
            "operation.checkpoint_guard_forbidden_run",
            "checkpoint_guard request uses a forbidden run name",
            {"run_name": run_name},
        )
    if artifact_class in current_list(current, "artifact_class_forbidden_next"):
        raise OperationError(
            "operation.checkpoint_guard_forbidden_artifact_class",
            "checkpoint_guard request uses a forbidden artifact class",
            {"artifact_class": artifact_class},
        )
    allowed_artifact_classes = current_list(current, "artifact_class_allowed_next")
    if allowed_artifact_classes and artifact_class not in allowed_artifact_classes:
        raise OperationError(
            "operation.checkpoint_guard_artifact_class_not_allowed",
            "checkpoint_guard request artifact class is not in current allowed list",
            {"artifact_class": artifact_class, "allowed": allowed_artifact_classes},
        )
    assert_current_string_match(
        field="run_name",
        requested=run_name,
        current=current,
        current_key="current_run_name",
    )
    assert_current_string_match(
        field="checkpoint",
        requested=checkpoint,
        current=current,
        current_key="current_checkpoint",
    )
    assert_current_string_match(
        field="model_arch",
        requested=model_arch,
        current=current,
        current_key="current_model_arch",
    )
    record = {
        "schema_version": 1,
        "guard_id": guard_id,
        "status": "passed",
        "passed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "run_name": run_name,
        "checkpoint": checkpoint,
        "model_arch": model_arch,
        "artifact_class": artifact_class,
        "current_experiment_id": current.get("current_experiment_id"),
        "operation": request.get("operation"),
    }
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "execution_status": "checkpoint_guard_passed",
        "operation": request.get("operation"),
        "guard_id": guard_id,
        "guard_record": str(record_path),
        "run_name": run_name,
        "checkpoint": checkpoint,
        "model_arch": model_arch,
        "artifact_class": artifact_class,
    }


CHECKPOINT_LOSS_TAG_RE = re.compile(r"_loss([0-9pm]+)(?=\.[^.]+$)")


def optional_bool(value: Any, *, code: str, message: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise OperationError(code, message, {"value": value})


def checkpoint_loss_from_name(path: Path) -> float | None:
    """Decode the loss embedded in a checkpoint filename tag.

    The trainer encodes the loss as e.g. ``20p6986`` (``.`` -> ``p``, ``-`` -> ``m``), so
    ``checkpoint_step_021500_loss20p6986.pt`` carries loss ``20.6986``. Returns ``None`` when
    the filename has no ``_loss`` tag or the tag cannot be parsed as a number.
    """
    match = CHECKPOINT_LOSS_TAG_RE.search(path.name)
    if match is None:
        return None
    numeric = match.group(1).replace("p", ".").replace("m", "-")
    try:
        return float(numeric)
    except ValueError:
        return None


def checkpoint_loss_from_metrics(
    step: int,
    *,
    eval_rows: list[tuple[int, float]],
    train_rows: list[tuple[int, float]],
) -> float | None:
    """Look up a checkpoint's loss from metrics.jsonl by nearest step.

    Prefers the ``early_stop_eval.mean_train_loss`` row at the exact step, then the nearest
    ``train_metrics.loss`` by absolute step distance, then the nearest eval row.
    """
    for eval_step, loss in eval_rows:
        if eval_step == step:
            return loss

    def nearest(rows: list[tuple[int, float]]) -> float | None:
        if not rows:
            return None
        # nearest by step distance; on a tie prefer the LATER step (fresher loss).
        best_step, best_loss = min(rows, key=lambda item: (abs(item[0] - step), -item[0]))
        return best_loss

    train_nearest = nearest(train_rows)
    if train_nearest is not None:
        return train_nearest
    return nearest(eval_rows)


def load_checkpoint_retention_config(
    request: dict[str, Any], project_root: Path
) -> dict[str, Any]:
    """Resolve retention config from explicit request overrides or the experiment yaml.

    Explicit ``keep_latest``/``keep_best``/``keep_milestones``/``metric_key``/
    ``metric_mode`` request fields win.
    Otherwise, when ``experiment_id`` is given, the ``checkpoint_retention`` section of
    ``<project_root>/experiments/<experiment_id>.yaml`` supplies the values. Missing keep counts
    are a clear error.
    """
    keep_latest = optional_int(
        request.get("keep_latest"),
        code="operation.checkpoint_retention_keep_latest_invalid",
        message="checkpoint_retention request.keep_latest must be an integer",
    )
    keep_best = optional_int(
        request.get("keep_best"),
        code="operation.checkpoint_retention_keep_best_invalid",
        message="checkpoint_retention request.keep_best must be an integer",
    )
    metric_key = request.get("metric_key")
    metric_mode = request.get("metric_mode")
    keep_milestones = request.get("keep_milestones")
    source = "request"

    experiment_id = request.get("experiment_id")
    experiment_section: dict[str, Any] = {}
    if experiment_id is not None:
        experiment_id = require_string(
            experiment_id,
            "operation.checkpoint_retention_experiment_id_invalid",
            "checkpoint_retention request.experiment_id must be a non-empty string",
        )
        experiment_path = project_root / "experiments" / f"{experiment_id}.yaml"
        if not experiment_path.exists():
            raise OperationError(
                "operation.checkpoint_retention_experiment_missing",
                "checkpoint_retention experiment definition is missing",
                {"experiment_id": experiment_id, "path": str(experiment_path)},
            )
        with experiment_path.open("r", encoding="utf-8") as f:
            experiment = yaml.safe_load(f) or {}
        if not isinstance(experiment, dict):
            raise OperationError(
                "operation.checkpoint_retention_experiment_invalid",
                "checkpoint_retention experiment definition must be a YAML mapping",
                {"experiment_id": experiment_id, "path": str(experiment_path)},
            )
        section = experiment.get("checkpoint_retention")
        if section is not None and not isinstance(section, dict):
            raise OperationError(
                "operation.checkpoint_retention_experiment_invalid",
                "checkpoint_retention section in experiment must be a mapping",
                {"experiment_id": experiment_id, "path": str(experiment_path)},
            )
        experiment_section = section or {}

    if keep_latest is None and "keep_latest" in experiment_section:
        keep_latest = optional_int(
            experiment_section.get("keep_latest"),
            code="operation.checkpoint_retention_keep_latest_invalid",
            message="experiment checkpoint_retention.keep_latest must be an integer",
        )
        source = "experiment"
    if keep_best is None and "keep_best" in experiment_section:
        keep_best = optional_int(
            experiment_section.get("keep_best"),
            code="operation.checkpoint_retention_keep_best_invalid",
            message="experiment checkpoint_retention.keep_best must be an integer",
        )
        source = "experiment"
    if metric_key is None:
        metric_key = experiment_section.get("metric_key")
        if metric_key is not None:
            source = "experiment"
    if metric_mode is None:
        metric_mode = experiment_section.get("metric_mode")
        if metric_mode is not None:
            source = "experiment"
    if keep_milestones is None and "keep_milestones" in experiment_section:
        keep_milestones = experiment_section.get("keep_milestones")
        source = "experiment"

    if keep_latest is None or keep_best is None:
        raise OperationError(
            "operation.checkpoint_retention_config_missing",
            (
                "checkpoint_retention requires keep_latest and keep_best via request fields or the "
                "experiment checkpoint_retention section"
            ),
            {
                "keep_latest": keep_latest,
                "keep_best": keep_best,
                "experiment_id": experiment_id,
            },
        )
    if keep_latest < 0 or keep_best < 0:
        raise OperationError(
            "operation.checkpoint_retention_config_invalid",
            "checkpoint_retention keep_latest and keep_best must be non-negative",
            {"keep_latest": keep_latest, "keep_best": keep_best},
        )

    milestone_rules: list[dict[str, int | None]] = []
    if keep_milestones is not None:
        # PROBE anchors: keep trajectory checkpoints so warm-starts can enter at
        # any interesting point, not just the end. Fail-closed parsing — a typo'd
        # rule must not silently delete the anchors it was meant to protect.
        if not isinstance(keep_milestones, list):
            raise OperationError(
                "operation.checkpoint_retention_config_invalid",
                "checkpoint_retention keep_milestones must be a list of rules",
                {"keep_milestones": keep_milestones},
            )
        for rule in keep_milestones:
            if not isinstance(rule, dict):
                raise OperationError(
                    "operation.checkpoint_retention_config_invalid",
                    "keep_milestones rules must be objects "
                    "({every_steps, from_step?, until_step?})",
                    {"rule": rule},
                )
            every = rule.get("every_steps")
            from_step = rule.get("from_step", 0)
            until_step = rule.get("until_step")
            ok = (
                isinstance(every, int) and not isinstance(every, bool) and every > 0
                and isinstance(from_step, int) and not isinstance(from_step, bool)
                and from_step >= 0
                and (
                    until_step is None
                    or (
                        isinstance(until_step, int)
                        and not isinstance(until_step, bool)
                        and until_step >= from_step
                    )
                )
            )
            if not ok:
                raise OperationError(
                    "operation.checkpoint_retention_config_invalid",
                    "keep_milestones rule needs every_steps>0, from_step>=0, "
                    "until_step>=from_step (or omitted)",
                    {"rule": rule},
                )
            milestone_rules.append(
                {"every_steps": every, "from_step": from_step, "until_step": until_step}
            )

    metric_key = metric_key if isinstance(metric_key, str) and metric_key else "mean_train_loss"
    metric_mode = metric_mode if metric_mode is not None else "min"
    if metric_mode not in {"min", "max"}:
        raise OperationError(
            "operation.checkpoint_retention_metric_mode_invalid",
            "checkpoint_retention metric_mode must be 'min' or 'max'",
            {"metric_mode": metric_mode},
        )

    return {
        "keep_latest": keep_latest,
        "keep_best": keep_best,
        "keep_milestones": milestone_rules,
        "metric_key": metric_key,
        "metric_mode": metric_mode,
        "source": source,
    }


def read_checkpoint_metrics_rows(
    metrics_path: Path,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (eval_rows, train_rows) of (step, loss) parsed from a metrics.jsonl file.

    ``early_stop_eval`` rows contribute ``mean_train_loss`` and ``train_metrics`` rows contribute
    ``loss``. Malformed lines and rows without a usable step/loss are skipped.
    """
    eval_rows: list[tuple[int, float]] = []
    train_rows: list[tuple[int, float]] = []
    if not metrics_path.exists():
        return eval_rows, train_rows
    with metrics_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            step = row_step(row)
            if step is None:
                continue
            event = row.get("event")
            if event == "early_stop_eval":
                value = row.get("mean_train_loss")
                if isinstance(value, int | float) and not isinstance(value, bool):
                    eval_rows.append((step, float(value)))
            elif event == "train_metrics":
                value = row.get("loss")
                if isinstance(value, int | float) and not isinstance(value, bool):
                    train_rows.append((step, float(value)))
    return eval_rows, train_rows


def resolve_metrics_path(run_dir: Path) -> Path:
    """Locate a run's metrics.jsonl.

    The cooperating trainer writes ``run_dir/metrics.jsonl`` (a sibling of the checkpoints/
    directory, NOT inside it). Older layouts placed it under ``checkpoints/``; prefer the
    run_dir file and fall back to the nested one so both conventions work. Shared by the
    reconciler daemon and checkpoint_retention so the two never drift.
    """
    run_dir = Path(run_dir)
    primary = run_dir / "metrics.jsonl"
    if primary.exists():
        return primary
    nested = run_dir / "checkpoints" / "metrics.jsonl"
    if nested.exists():
        return nested
    return primary


def execute_checkpoint_retention_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        resolve_text_ref(
            require_string(
                request.get("project_root"),
                "operation.project_root_missing",
                "checkpoint_retention request.project_root is required",
            )
        )
    )
    run_dir = Path(
        resolve_text_ref(
            require_string(
                request.get("run_dir"),
                "operation.checkpoint_retention_run_dir_missing",
                "checkpoint_retention request.run_dir is required",
            )
        )
    )
    dry_run = optional_bool(
        request.get("dry_run"),
        code="operation.checkpoint_retention_dry_run_invalid",
        message="checkpoint_retention request.dry_run must be a boolean",
    )
    # protect_steps: checkpoints the caller knows still have pending QC/probe work.
    # Fail-closed parsing — a malformed list must not silently protect nothing.
    protect_steps_raw = request.get("protect_steps")
    protect_steps: set[int] = set()
    if protect_steps_raw is not None:
        if not isinstance(protect_steps_raw, list) or any(
            not isinstance(s, int) or isinstance(s, bool) for s in protect_steps_raw
        ):
            raise OperationError(
                "operation.checkpoint_retention_protect_steps_invalid",
                "checkpoint_retention request.protect_steps must be a list of ints",
                {"protect_steps": protect_steps_raw},
            )
        protect_steps = {int(s) for s in protect_steps_raw}
    config = load_checkpoint_retention_config(request, project_root)

    checkpoint_dir = run_dir / "checkpoints"
    if not checkpoint_dir.exists():
        raise OperationError(
            "operation.checkpoint_retention_checkpoint_dir_missing",
            "checkpoint_retention run checkpoints directory is missing",
            {"run_dir": str(run_dir), "path": str(checkpoint_dir)},
        )

    warnings: list[dict[str, Any]] = []
    # Two INDEPENDENT families, each with its own rolling window (never cross-protect):
    #   - 'latest' = checkpoint_step_*.pt : keep the newest keep_latest by STEP.
    #   - 'best'   = best_step_*.pt        : keep the newest keep_best by STEP. The trainer
    #     only writes a best_step_* when a NEW best early-stop metric is reached, so the
    #     newest N best_step_* ARE the N best-ever models; there is no need to (and we must
    #     not) pull periodic checkpoint_step_* into the best set -- that would let a noisy
    #     low-loss periodic checkpoint evict the curated best archive.
    family_paths: list[tuple[str, Path]] = []
    for path in sorted(checkpoint_dir.glob("checkpoint_step_*.pt")):
        if path.is_file():
            family_paths.append(("latest", path))
    for path in sorted(checkpoint_dir.glob("best_step_*.pt")):
        if path.is_file():
            family_paths.append(("best", path))

    # metrics.jsonl (run_dir preferred, checkpoints/ fallback) — only used to resolve a
    # checkpoint's loss when the filename lacks a _loss tag.
    eval_rows, train_rows = read_checkpoint_metrics_rows(resolve_metrics_path(run_dir))

    records: list[dict[str, Any]] = []
    for family, path in family_paths:
        step = checkpoint_step_from_name(path)
        loss = checkpoint_loss_from_name(path)
        loss_source = "filename"
        if loss is None:
            loss_source = "metrics"
            loss = (
                checkpoint_loss_from_metrics(
                    step, eval_rows=eval_rows, train_rows=train_rows
                )
                if step is not None
                else None
            )
            if loss is None:
                loss_source = "unknown"
        if CHECKPOINT_LOSS_TAG_RE.search(path.name) is None:
            warnings.append(
                {
                    "code": "operation.checkpoint_retention_filename_convention",
                    "message": (
                        "checkpoint filename does not match the _loss convention; "
                        "loss resolved from metrics.jsonl or treated as unknown"
                    ),
                    "details": {"name": path.name},
                }
            )
        records.append(
            {
                "path": path,
                "name": path.name,
                "step": step,
                "loss": loss,
                "loss_source": loss_source,
                "family": family,
            }
        )

    protected: set[str] = set()

    # latest window: newest keep_latest by step, from the 'latest' family only.
    latest_sorted = sorted(
        (r for r in records if r["family"] == "latest"),
        key=lambda r: (r["step"] if r["step"] is not None else -1, r["name"]),
        reverse=True,
    )
    kept_latest: list[str] = []
    for record in latest_sorted:
        if len(kept_latest) >= config["keep_latest"]:
            break
        kept_latest.append(record["name"])
        protected.add(record["name"])

    # best window: newest keep_best by step, from the 'best' family only (each best_step_*
    # is a strictly-better-than-the-last snapshot, so newest-by-step == best-by-metric).
    best_sorted = sorted(
        (r for r in records if r["family"] == "best"),
        key=lambda r: (r["step"] if r["step"] is not None else -1, r["name"]),
        reverse=True,
    )
    kept_best: list[str] = []
    for record in best_sorted:
        if len(kept_best) >= config["keep_best"]:
            break
        kept_best.append(record["name"])
        protected.add(record["name"])

    # milestone anchors: periodic checkpoints matching any keep_milestones rule are
    # protected regardless of the rolling windows — they are the warm-start entry
    # points offline probes need along the trajectory, not just at the end.
    def is_milestone(step: int | None) -> bool:
        if step is None:
            return False
        for rule in config.get("keep_milestones") or []:
            from_step = int(rule.get("from_step") or 0)
            until_step = rule.get("until_step")
            if step < from_step:
                continue
            if until_step is not None and step > int(until_step):
                continue
            if (step - from_step) % int(rule["every_steps"]) == 0:
                return True
        return False

    kept_milestones: list[str] = []
    for record in records:
        if record["family"] == "latest" and is_milestone(record["step"]):
            if record["name"] not in protected:
                kept_milestones.append(record["name"])
            protected.add(record["name"])

    # pending-QC protection: never delete a checkpoint whose diagnostics have not
    # run yet — QC/probe ops are serialized and can lag many checkpoints behind a
    # fast trainer. The caller (reconcile) computes the pending set; give-up-capped
    # steps are excluded there, so a permanently-broken op cannot pin disk forever.
    kept_pending_qc: list[str] = []
    for record in records:
        if record["family"] == "latest" and record["step"] in protect_steps:
            if record["name"] not in protected:
                kept_pending_qc.append(record["name"])
            protected.add(record["name"])

    deleted: list[str] = []
    for record in records:
        if record["name"] in protected:
            continue
        deleted.append(record["name"])
        if not dry_run:
            # missing_ok: a concurrent daemon / manual cleanup may have removed it between
            # glob and unlink; that is not an error for us.
            record["path"].unlink(missing_ok=True)

    best_pointer_path = checkpoint_dir / "best_checkpoint.pt"
    best_checkpoint_pointer = "best_checkpoint.pt" if best_pointer_path.exists() else None

    return {
        "execution_status": (
            "checkpoint_retention_previewed" if dry_run else "checkpoint_retention_applied"
        ),
        "operation": request.get("operation"),
        "run_dir": str(run_dir),
        "dry_run": dry_run,
        "kept_latest": kept_latest,
        "kept_best": kept_best,
        "kept_milestones": kept_milestones,
        "kept_pending_qc": kept_pending_qc,
        "deleted": deleted,
        "best_checkpoint_pointer": best_checkpoint_pointer,
        "config": config,
        "warnings": warnings,
    }


def execute_trt_cache_guard_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "trt_cache_guard request.project_root is required",
        )
    )
    guard_id = require_string(
        request.get("guard_id"),
        "operation.trt_cache_guard_id_missing",
        "trt_cache_guard request.guard_id is required",
    )
    model_arch = require_string(
        request.get("model_arch"),
        "operation.trt_cache_model_arch_missing",
        "trt_cache_guard request.model_arch is required",
    )
    trt_cache_dir = require_string(
        request.get("trt_cache_dir"),
        "operation.trt_cache_dir_missing",
        "trt_cache_guard request.trt_cache_dir is required",
    )
    compile_mode = require_string(
        request.get("compile_mode"),
        "operation.trt_cache_compile_mode_missing",
        "trt_cache_guard request.compile_mode is required",
    )
    require_compile_cache = request.get("require_compile_cache")
    if require_compile_cache is not True:
        raise OperationError(
            "operation.trt_cache_required",
            "trt_cache_guard requires request.require_compile_cache to be true",
            {"actual": require_compile_cache},
        )
    forbidden_modes = {"disabled", "none", "no_cache", "off"}
    if compile_mode in forbidden_modes:
        raise OperationError(
            "operation.trt_cache_compile_mode_forbidden",
            "trt_cache_guard compile_mode disables or bypasses TRT cache",
            {"compile_mode": compile_mode},
        )
    current = load_current_record(project_root)
    current_model_arch = require_string(
        current.get("current_model_arch"),
        "operation.current_invalid",
        "current.current_model_arch must be a non-empty string",
    )
    if model_arch != current_model_arch:
        raise OperationError(
            "operation.trt_cache_model_arch_mismatch",
            "trt_cache_guard model_arch does not match current model arch",
            {"expected": current_model_arch, "actual": model_arch},
        )
    record_dir = project_root / "guard_records"
    record_path = record_dir / f"{guard_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.trt_cache_guard_record_exists",
            "trt_cache_guard record already exists and will not be overwritten",
            {"guard_id": guard_id, "path": str(record_path)},
        )
    record = {
        "schema_version": 1,
        "guard_id": guard_id,
        "status": "passed",
        "passed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "model_arch": model_arch,
        "trt_cache_dir": trt_cache_dir,
        "compile_mode": compile_mode,
        "require_compile_cache": require_compile_cache,
        "operation": request.get("operation"),
    }
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "execution_status": "trt_cache_guard_passed",
        "operation": request.get("operation"),
        "guard_id": guard_id,
        "guard_record": str(record_path),
        "model_arch": model_arch,
        "trt_cache_dir": trt_cache_dir,
        "compile_mode": compile_mode,
        "require_compile_cache": require_compile_cache,
    }


def load_summary_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise OperationError(
            "operation.artifact_summary_missing",
            "artifact_summary_guard summary_path does not exist or is not a file",
            {"summary_path": str(path)},
        )
    with path.open("r", encoding="utf-8") as f:
        summary = json.load(f)
    if not isinstance(summary, dict):
        raise OperationError(
            "operation.artifact_summary_invalid",
            "artifact_summary_guard summary JSON must be an object",
            {"summary_path": str(path)},
        )
    return summary


def validated_path_mappings(value: Any) -> list[dict[str, str]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise OperationError(
            "operation.path_mappings_invalid",
            "path_mappings must be a list of {from,to} objects",
        )
    mappings: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise OperationError(
                "operation.path_mappings_invalid",
                "path_mappings entries must be objects",
                {"index": index},
            )
        source = resolve_text_ref(
            require_string(
                item.get("from"),
                "operation.path_mappings_invalid",
                "path_mappings entries require from",
            )
        )
        target = resolve_text_ref(
            require_string(
                item.get("to"),
                "operation.path_mappings_invalid",
                "path_mappings entries require to",
            )
        )
        if not source.startswith("/") or not target.startswith("/"):
            raise OperationError(
                "operation.path_mappings_invalid",
                "path_mappings from/to must be absolute paths",
                {"index": index, "from": source, "to": target},
            )
        mappings.append({"from": source.rstrip("/"), "to": target.rstrip("/")})
    return mappings


def map_summary_path(path_text: Any, mappings: list[dict[str, str]]) -> str:
    raw = require_string(
        path_text,
        "operation.artifact_summary_path_invalid",
        "summary path fields must be strings",
    )
    for mapping in mappings:
        source = mapping["from"]
        if raw == source or raw.startswith(source + "/"):
            return mapping["to"] + raw[len(source) :]
    return raw


def require_summary_file(
    path_text: Any,
    *,
    code: str,
    message: str,
    path_mappings: list[dict[str, str]] | None = None,
) -> Path:
    path = Path(map_summary_path(path_text, path_mappings or []))
    if not path.exists() or not path.is_file():
        raise OperationError(code, message, {"path": str(path)})
    return path


def assert_summary_optimize(summary: dict[str, Any], required: Any) -> None:
    if required is None:
        return
    required_text = require_string(
        required,
        "operation.artifact_summary_require_optimize_invalid",
        "artifact_summary_guard require_optimize must be a string when supplied",
    )
    actual = summary.get("optimize")
    if actual != required_text:
        raise OperationError(
            "operation.artifact_summary_optimize_mismatch",
            "artifact_summary_guard summary optimize does not match requirement",
            {"expected": required_text, "actual": actual},
        )


def assert_summary_preview_contract(summary: dict[str, Any], required: Any) -> dict[str, Any]:
    if required is None:
        return {}
    required_text = require_string(
        required,
        "operation.artifact_summary_require_preview_contract_invalid",
        "artifact_summary_guard require_preview_contract must be a string when supplied",
    )
    actual = summary.get("preview_contract")
    if actual != required_text:
        raise OperationError(
            "operation.artifact_summary_preview_contract_mismatch",
            "artifact_summary_guard summary preview_contract does not match requirement",
            {"expected": required_text, "actual": actual},
        )
    return {"preview_contract": actual}


def _artifact_video_stream(artifact_summary: dict[str, Any], *, summary_key: str) -> dict[str, Any]:
    ffprobe = artifact_summary.get("ffprobe")
    if not isinstance(ffprobe, dict):
        raise OperationError(
            "operation.artifact_summary_video_probe_missing",
            "artifact_summary_guard artifact ffprobe summary is missing",
            {"summary_key": summary_key},
        )
    streams = ffprobe.get("streams")
    if not isinstance(streams, list):
        raise OperationError(
            "operation.artifact_summary_video_probe_missing",
            "artifact_summary_guard artifact ffprobe streams are missing",
            {"summary_key": summary_key},
        )
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            return stream
    raise OperationError(
        "operation.artifact_summary_video_stream_missing",
        "artifact_summary_guard artifact video stream is missing",
        {"summary_key": summary_key},
    )


def assert_artifact_video_requirements(
    artifact_summary: dict[str, Any], requirement: dict[str, Any], *, summary_key: str
) -> dict[str, Any]:
    if not any(
        key in requirement for key in ("min_width", "min_height", "require_width", "require_height")
    ):
        return {}
    stream = _artifact_video_stream(artifact_summary, summary_key=summary_key)
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    min_width = requirement.get("min_width")
    min_height = requirement.get("min_height")
    require_width = requirement.get("require_width")
    require_height = requirement.get("require_height")
    if min_width is not None and width < int(min_width):
        raise OperationError(
            "operation.artifact_summary_video_resolution_mismatch",
            "artifact_summary_guard artifact video width is below requirement",
            {
                "summary_key": summary_key,
                "expected_min_width": int(min_width),
                "actual_width": width,
            },
        )
    if min_height is not None and height < int(min_height):
        raise OperationError(
            "operation.artifact_summary_video_resolution_mismatch",
            "artifact_summary_guard artifact video height is below requirement",
            {
                "summary_key": summary_key,
                "expected_min_height": int(min_height),
                "actual_height": height,
            },
        )
    if require_width is not None and width != int(require_width):
        raise OperationError(
            "operation.artifact_summary_video_resolution_mismatch",
            "artifact_summary_guard artifact video width does not match requirement",
            {
                "summary_key": summary_key,
                "expected_width": int(require_width),
                "actual_width": width,
            },
        )
    if require_height is not None and height != int(require_height):
        raise OperationError(
            "operation.artifact_summary_video_resolution_mismatch",
            "artifact_summary_guard artifact video height does not match requirement",
            {
                "summary_key": summary_key,
                "expected_height": int(require_height),
                "actual_height": height,
            },
        )
    return {"video": {"width": width, "height": height}}


def assert_summary_trt_cache(
    summary: dict[str, Any], request: dict[str, Any], path_mappings: list[dict[str, str]]
) -> dict[str, Any]:
    if request.get("require_trt_cache_dir") is not True:
        return {}
    trt_cache_dir = require_string(
        summary.get("trt_cache_dir"),
        "operation.artifact_summary_trt_cache_dir_missing",
        "artifact_summary_guard requires summary.trt_cache_dir",
    )
    checked: dict[str, Any] = {"trt_cache_dir": map_summary_path(trt_cache_dir, path_mappings)}
    if request.get("require_trt_cache_files") is True:
        optimize_meta = summary.get("optimize_meta")
        if not isinstance(optimize_meta, dict):
            raise OperationError(
                "operation.artifact_summary_trt_cache_files_missing",
                "artifact_summary_guard requires summary.optimize_meta for TRT cache checks",
            )
        cache_paths: dict[str, str] = {}
        for key, value in optimize_meta.items():
            if "trt" not in str(key) or "cache" not in str(key):
                continue
            if isinstance(value, str) and value:
                mapped_value = map_summary_path(value, path_mappings)
                path = Path(mapped_value)
                if not path.exists():
                    raise OperationError(
                        "operation.artifact_summary_trt_cache_file_missing",
                        "artifact_summary_guard TRT cache file/dir from optimize_meta is missing",
                        {"field": key, "path": mapped_value, "summary_path": value},
                    )
                cache_paths[str(key)] = mapped_value
        if not cache_paths:
            raise OperationError(
                "operation.artifact_summary_trt_cache_files_missing",
                "artifact_summary_guard found no TRT cache paths in summary.optimize_meta",
            )
        checked["trt_cache_paths"] = cache_paths
    return checked


def validated_artifact_requirements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise OperationError(
            "operation.artifact_summary_artifacts_invalid",
            "artifact_summary_guard request.artifacts must be a non-empty list",
        )
    artifacts: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise OperationError(
                "operation.artifact_summary_artifact_invalid",
                "artifact_summary_guard artifact requirement must be an object",
                {"index": index},
            )
        artifact_id = require_string(
            item.get("artifact_id"),
            "operation.artifact_summary_artifact_id_missing",
            "artifact_summary_guard artifact.artifact_id is required",
        )
        summary_key = require_string(
            item.get("summary_key"),
            "operation.artifact_summary_key_missing",
            "artifact_summary_guard artifact.summary_key is required",
        )
        artifacts.append({**item, "artifact_id": artifact_id, "summary_key": summary_key})
    return artifacts


def validate_summary_artifact(
    summary: dict[str, Any], requirement: dict[str, Any], path_mappings: list[dict[str, str]]
) -> dict[str, Any]:
    summary_key = requirement["summary_key"]
    artifact_summary = summary.get(summary_key)
    if not isinstance(artifact_summary, dict):
        raise OperationError(
            "operation.artifact_summary_artifact_missing",
            "artifact_summary_guard expected artifact summary object is missing",
            {"summary_key": summary_key},
        )
    artifact_path = require_summary_file(
        artifact_summary.get("path"),
        code="operation.artifact_summary_file_missing",
        message="artifact_summary_guard artifact path is missing or does not exist",
        path_mappings=path_mappings,
    )
    expected_frames = artifact_summary.get("expected_frames")
    written_frames = artifact_summary.get("written_frame_count")
    if expected_frames is not None and written_frames is not None:
        if int(expected_frames) != int(written_frames):
            raise OperationError(
                "operation.artifact_summary_frame_count_mismatch",
                "artifact_summary_guard expected_frames does not match written_frame_count",
                {
                    "summary_key": summary_key,
                    "expected_frames": expected_frames,
                    "written_frame_count": written_frames,
                },
            )
    video_record = assert_artifact_video_requirements(
        artifact_summary, requirement, summary_key=summary_key
    )
    audio_record: dict[str, Any] | None = None
    if requirement.get("require_audio") is True:
        if artifact_summary.get("silent") is True:
            raise OperationError(
                "operation.artifact_summary_audio_required",
                "artifact_summary_guard artifact is marked silent but audio is required",
                {"summary_key": summary_key},
            )
        audio = artifact_summary.get("audio")
        if not isinstance(audio, dict) or float(audio.get("duration_sec") or 0.0) <= 0.0:
            raise OperationError(
                "operation.artifact_summary_audio_required",
                "artifact_summary_guard artifact audio summary is missing "
                "or has non-positive duration",
                {"summary_key": summary_key, "audio": audio},
            )
        audio_path = require_summary_file(
            audio.get("path"),
            code="operation.artifact_summary_audio_required",
            message="artifact_summary_guard artifact audio path is missing or does not exist",
            path_mappings=path_mappings,
        )
        audio_volume = artifact_summary.get("audio_volume")
        min_volume = float(requirement.get("min_audio_max_volume_db", -30.0))
        actual_volume = None
        if isinstance(audio_volume, dict) and audio_volume.get("max_volume_db") is not None:
            actual_volume = float(audio_volume["max_volume_db"])
        if actual_volume is None or actual_volume < min_volume:
            raise OperationError(
                "operation.artifact_summary_audio_too_quiet",
                "artifact_summary_guard artifact audio volume is missing or below threshold",
                {
                    "summary_key": summary_key,
                    "min_audio_max_volume_db": min_volume,
                    "actual": actual_volume,
                },
            )
        audio_duration_sec = float(audio["duration_sec"])
        if requirement.get("require_full_source_audio") is True:
            source_duration = audio.get("source_duration_sec")
            if source_duration is None:
                raise OperationError(
                    "operation.artifact_summary_audio_source_duration_missing",
                    "artifact_summary_guard requires audio.source_duration_sec "
                    "to verify full-source audio coverage",
                    {"summary_key": summary_key, "audio": audio},
                )
            start_sec = float(audio.get("start_sec") or 0.0)
            required_duration = max(0.0, float(source_duration) - start_sec)
            tolerance_sec = float(requirement.get("audio_duration_tolerance_sec", 0.05))
            if audio_duration_sec + tolerance_sec < required_duration:
                raise OperationError(
                    "operation.artifact_summary_audio_source_incomplete",
                    "artifact_summary_guard artifact audio duration does not cover "
                    "the requested source audio through its end",
                    {
                        "summary_key": summary_key,
                        "duration_sec": audio_duration_sec,
                        "source_duration_sec": float(source_duration),
                        "start_sec": start_sec,
                        "required_duration_sec": required_duration,
                        "audio_duration_tolerance_sec": tolerance_sec,
                    },
                )
        audio_record = {
            "path": str(audio_path),
            "duration_sec": audio_duration_sec,
            "max_volume_db": actual_volume,
        }
    return {
        "artifact_id": requirement["artifact_id"],
        "summary_key": summary_key,
        "path": str(artifact_path),
        "expected_frames": expected_frames,
        "written_frame_count": written_frames,
        "audio": audio_record,
        **video_record,
    }


def execute_artifact_summary_guard_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "artifact_summary_guard request.project_root is required",
        )
    )
    guard_id = require_string(
        request.get("guard_id"),
        "operation.artifact_summary_guard_id_missing",
        "artifact_summary_guard request.guard_id is required",
    )
    summary_path = Path(
        resolve_text_ref(
            require_string(
                request.get("summary_path"),
                "operation.artifact_summary_path_missing",
                "artifact_summary_guard request.summary_path is required",
            )
        )
    )
    record_dir = project_root / "guard_records"
    record_path = record_dir / f"{guard_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.artifact_summary_guard_record_exists",
            "artifact_summary_guard record already exists and will not be overwritten",
            {"guard_id": guard_id, "path": str(record_path)},
        )
    summary = load_summary_json(summary_path)
    path_mappings = validated_path_mappings(request.get("path_mappings"))
    assert_summary_optimize(summary, request.get("require_optimize"))
    preview_contract = assert_summary_preview_contract(
        summary, request.get("require_preview_contract")
    )
    trt_cache = assert_summary_trt_cache(summary, request, path_mappings)
    artifact_records = [
        validate_summary_artifact(summary, requirement, path_mappings)
        for requirement in validated_artifact_requirements(request.get("artifacts"))
    ]
    record = {
        "schema_version": 1,
        "guard_id": guard_id,
        "status": "passed",
        "passed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "operation": request.get("operation"),
        "summary_path": str(summary_path),
        "optimize": summary.get("optimize"),
        "trt_cache": trt_cache,
        **preview_contract,
        "artifacts": artifact_records,
        "path_mappings": path_mappings,
    }
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "execution_status": "artifact_summary_guard_passed",
        "operation": request.get("operation"),
        "guard_id": guard_id,
        "guard_record": str(record_path),
        "summary_path": str(summary_path),
        "artifacts": artifact_records,
    }


def execute_artifact_delivery_operation(request: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(
        require_string(
            request.get("project_root"),
            "operation.project_root_missing",
            "artifact_delivery request.project_root is required",
        )
    )
    delivery_id = require_string(
        request.get("delivery_id"),
        "operation.delivery_id_missing",
        "artifact_delivery request.delivery_id is required",
    )
    delivery_target_id = require_string(
        request.get("delivery_target_id"),
        "operation.delivery_target_id_missing",
        "artifact_delivery request.delivery_target_id is required",
    )
    artifact_id = require_string(
        request.get("artifact_id"),
        "operation.artifact_id_missing",
        "artifact_delivery request.artifact_id is required",
    )
    file_path = Path(
        resolve_text_ref(
            require_string(
                request.get("file_path"),
                "operation.delivery_file_path_missing",
                "artifact_delivery request.file_path is required",
            )
        )
    )
    if not file_path.exists() or not file_path.is_file():
        raise OperationError(
            "operation.delivery_file_missing",
            "artifact_delivery file_path does not exist or is not a file",
            {"file_path": str(file_path)},
        )
    target = load_delivery_target(project_root, delivery_target_id)
    if target.get("kind") != "discord_webhook":
        raise OperationError(
            "operation.delivery_target_kind_unsupported",
            "artifact_delivery currently supports only discord_webhook targets",
            {"target_id": delivery_target_id, "kind": target.get("kind")},
        )
    webhook_url = resolve_text_ref(
        require_string(
            target.get("webhook_url"),
            "operation.delivery_webhook_url_missing",
            "discord_webhook delivery target must define webhook_url",
        )
    )
    if not (webhook_url.startswith("http://") or webhook_url.startswith("https://")):
        raise OperationError(
            "operation.delivery_webhook_url_invalid",
            "discord_webhook URL must be http or https",
            {"target_id": delivery_target_id},
        )
    message = request.get("message")
    if message is None:
        message = f"artifact ready: {artifact_id}"
    message = require_string(
        message,
        "operation.delivery_message_invalid",
        "artifact_delivery request.message must be a non-empty string when supplied",
    )
    deliveries_dir = project_root / "artifact_deliveries"
    record_path = deliveries_dir / f"{delivery_id}.json"
    if record_path.exists():
        raise OperationError(
            "operation.delivery_record_exists",
            "artifact_delivery record already exists and will not be overwritten",
            {"delivery_id": delivery_id, "path": str(record_path)},
        )

    response = post_discord_webhook(
        webhook_url=webhook_url,
        message=message,
        file_path=file_path,
    )
    record = {
        "schema_version": 1,
        "delivery_id": delivery_id,
        "artifact_id": artifact_id,
        "target_id": delivery_target_id,
        "status": "delivered",
        "delivered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "file_path": str(file_path),
        "message": message,
        "http_status": response["http_status"],
        "response_body": response["response_body"],
    }
    deliveries_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "execution_status": "artifact_delivery_completed",
        "operation": request.get("operation"),
        "delivery_id": delivery_id,
        "artifact_id": artifact_id,
        "target_id": delivery_target_id,
        "http_status": response["http_status"],
        "file_path": str(file_path),
        "delivery_record": str(record_path),
    }


REMOTE_KIKAI_SCRIPT = """
import json
import os
import subprocess
from pathlib import Path


def replace_strings(value, replacements):
    if isinstance(value, str):
        for item in replacements:
            value = value.replace(item['from'], item['to'])
        return value
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_strings(item, replacements) for key, item in value.items()}
    return value


payload = PAYLOAD
remote_project_root = Path(payload['remote_project_root'])
remote_payload_project_root = payload.get('remote_payload_project_root')
if remote_payload_project_root:
    payload_root = Path(remote_payload_project_root)
    for item in payload.get('project_payload_files', []):
        relative = Path(item['path'])
        target = payload_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item['text'], encoding='utf-8')
operation_payload = payload.get('operation_payload')
if operation_payload is not None:
    operation = operation_payload
else:
    template_path = remote_project_root / payload['remote_operation_template']
    operation = json.loads(template_path.read_text())
operation = replace_strings(operation, payload.get('string_replacements', []))
request = operation.get('request') or {}
pipeline_run_id = payload['pipeline_run_id']
request['pipeline_run_id'] = pipeline_run_id
for step in request.get('steps', []):
    child = step.get('request') or {}
    step_id = step.get('step_id') or 'step'
    if 'guard_id' in child:
        child['guard_id'] = f'{pipeline_run_id}_{step_id}'
    if 'notification_id' in child:
        child['notification_id'] = f'{pipeline_run_id}_{step_id}'
    if 'delivery_id' in child:
        child['delivery_id'] = f'{pipeline_run_id}_{step_id}'
operation['request'] = request
remote_operation_path = Path(payload['remote_operation_path'])
remote_operation_path.parent.mkdir(parents=True, exist_ok=True)
operation_json = json.dumps(
    operation,
    ensure_ascii=False,
    indent=2,
    sort_keys=True,
)
remote_operation_path.write_text(operation_json + '\\n')
env = os.environ.copy()
env.update(payload.get('env', {}))
uv_bin = payload.get('uv_bin') or 'uv'
dry = subprocess.run(
    [uv_bin, 'run', 'kikai', 'target', 'dry-run', str(remote_operation_path)],
    cwd=str(remote_project_root),
    env=env,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
dry_event = {
    'event': 'remote_kikai_dry_run',
    'returncode': dry.returncode,
    'output_tail': dry.stdout[-4000:],
}
print(json.dumps(dry_event), flush=True)
if dry.returncode != 0:
    raise SystemExit(dry.returncode)
run = subprocess.Popen(
    [uv_bin, 'run', 'kikai', 'exec', str(remote_operation_path)],
    cwd=str(remote_project_root),
    env=env,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    bufsize=1,
)
run_lines = []
for line in run.stdout:
    run_lines.append(line)
    print('@@STREAM@@ ' + line, end='', flush=True)
run.wait()
run_event = {
    'event': 'remote_kikai_exec',
    'returncode': run.returncode,
    'output_tail': ''.join(run_lines)[-12000:],
}
print(json.dumps(run_event), flush=True)
raise SystemExit(run.returncode)
"""


def validated_string_dict(value: Any, *, code: str, field: str) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise OperationError(code, f"{field} must be an object of string values")
    return dict(value)


def validated_replacements(value: Any) -> list[dict[str, str]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise OperationError(
            "operation.remote_replacements_invalid",
            "remote_kikai_exec string_replacements must be a list",
        )
    replacements: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise OperationError(
                "operation.remote_replacements_invalid",
                "remote_kikai_exec string_replacements entries must be objects",
                {"index": index},
            )
        old = require_string(
            item.get("from"),
            "operation.remote_replacements_invalid",
            "remote_kikai_exec string_replacements entries require from",
        )
        new = require_string(
            item.get("to"),
            "operation.remote_replacements_invalid",
            "remote_kikai_exec string_replacements entries require to",
        )
        replacements.append({"from": old, "to": resolve_text_ref(new)})
    return replacements


def replace_strings_in_value(value: Any, replacements: list[dict[str, str]]) -> Any:
    if isinstance(value, str):
        for item in replacements:
            value = value.replace(item["from"], item["to"])
        return value
    if isinstance(value, list):
        return [replace_strings_in_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_strings_in_value(item, replacements) for key, item in value.items()}
    return value


def local_project_payload_files(local_project_root: Path, paths: Any) -> list[dict[str, str]]:
    if paths in (None, []):
        return []
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise OperationError(
            "operation.remote_project_payload_invalid",
            "remote_kikai_exec local_project_payload_paths must be a list of relative paths",
        )
    files: list[dict[str, str]] = []
    for raw_path in paths:
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts or not raw_path:
            raise OperationError(
                "operation.remote_project_payload_invalid",
                "remote_kikai_exec payload paths must stay inside local_project_root",
                {"path": raw_path},
            )
        source = local_project_root / relative
        # Containment: resolve symlinks on BOTH sides and assert the real source
        # stays within the real project root, so a symlinked payload entry cannot
        # exfiltrate a file from outside the root (e.g. /etc/passwd).
        real_root = os.path.realpath(local_project_root)
        real_source = os.path.realpath(source)
        if os.path.commonpath([real_root, real_source]) != real_root:
            raise OperationError(
                "operation.remote_payload_path_escapes_root",
                "remote_kikai_exec payload path escapes local_project_root",
                {"path": raw_path, "source": str(source), "resolved": real_source},
            )
        if not source.exists() or not source.is_file():
            raise OperationError(
                "operation.remote_project_payload_missing",
                "remote_kikai_exec payload file is missing",
                {"path": raw_path, "source": str(source)},
            )
        files.append({"path": relative.as_posix(), "text": source.read_text(encoding="utf-8")})
    return files


def load_local_operation_template(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.exists() or not path.is_file():
        raise OperationError(
            "operation.local_operation_template_missing",
            "remote_kikai_exec local_operation_template is missing",
            {"path": path_text},
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise OperationError(
            "operation.local_operation_template_invalid",
            "remote_kikai_exec local_operation_template must contain a JSON object",
            {"path": path_text},
        )
    return data


def remote_kikai_python_payload(payload: dict[str, Any]) -> str:
    payload_source = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    # Embed the payload as a Python STRING literal that is json.loads'd at runtime,
    # NOT as a bare literal: raw JSON booleans/null (true/false/null) are invalid
    # Python and would raise `NameError: name 'true' is not defined` on line 1 the
    # moment any request carries a boolean (e.g. detach: true).
    return (
        "import json as _kikai_payload_json\n"
        "PAYLOAD = _kikai_payload_json.loads(" + repr(payload_source) + ")\n"
        + REMOTE_KIKAI_SCRIPT
    )


REMOTE_STREAM_PREFIX = "@@STREAM@@"


def run_remote_ssh_streaming(command: list[str], input_text: str) -> tuple[int, str, str]:
    """Run the remote ssh command, forwarding @@STREAM@@-prefixed remote lines to
    stderr live so long-running remote ops show progress instead of blocking
    silently until completion. Non-stream lines (the structured dry-run/exec event
    JSON) are captured and returned for the operation result. Returns
    (returncode, captured_stdout, stderr)."""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def _feed() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(input_text)
                proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    out_lines: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            if line.startswith(REMOTE_STREAM_PREFIX):
                sys.stderr.write(line[len(REMOTE_STREAM_PREFIX):].lstrip())
                sys.stderr.flush()
            else:
                out_lines.append(line)
    stderr_text = proc.stderr.read() if proc.stderr is not None else ""
    proc.wait()
    feeder.join(timeout=5)
    return proc.returncode, "".join(out_lines), stderr_text


def execute_remote_kikai_exec_operation(request: dict[str, Any]) -> dict[str, Any]:
    ssh_host_value = require_string(
        request.get("ssh_host"),
        "operation.remote_ssh_host_missing",
        "remote_kikai_exec request.ssh_host is required",
    )
    ssh_host = require_safe_ssh_host(ssh_host_value)
    remote_project_root = require_string(
        request.get("remote_project_root"),
        "operation.remote_project_root_missing",
        "remote_kikai_exec request.remote_project_root is required",
    )
    remote_operation_template_value = request.get("remote_operation_template")
    local_operation_template_value = request.get("local_operation_template")
    if bool(remote_operation_template_value) == bool(local_operation_template_value):
        raise OperationError(
            "operation.remote_operation_template_missing",
            (
                "remote_kikai_exec requires exactly one of remote_operation_template "
                "or local_operation_template"
            ),
        )
    remote_operation_template = ""
    operation_payload: dict[str, Any] | None = None
    if remote_operation_template_value:
        remote_operation_template = require_string(
            remote_operation_template_value,
            "operation.remote_operation_template_missing",
            "remote_kikai_exec request.remote_operation_template is required",
        )
    else:
        operation_payload = load_local_operation_template(
            require_string(
                local_operation_template_value,
                "operation.local_operation_template_missing",
                "remote_kikai_exec request.local_operation_template is required",
            )
        )
    pipeline_run_id = resolve_text_ref(
        require_string(
            request.get("pipeline_run_id"),
            "operation.sequence_pipeline_run_id_missing",
            "remote_kikai_exec request.pipeline_run_id is required",
        )
    )
    remote_operation_path = resolve_text_ref(
        request.get("remote_operation_path") or f"/tmp/{pipeline_run_id}.json"
    )
    env = {
        key: resolve_text_ref(value)
        for key, value in validated_string_dict(
            request.get("env"),
            code="operation.remote_env_invalid",
            field="remote_kikai_exec request.env",
        ).items()
    }
    replacements = validated_replacements(request.get("string_replacements"))
    project_payload_files: list[dict[str, str]] = []
    remote_payload_project_root = request.get("remote_payload_project_root")
    if operation_payload is not None:
        local_project_root_text = require_string(
            request.get("local_project_root"),
            "operation.local_project_root_missing",
            (
                "remote_kikai_exec request.local_project_root is required "
                "with local_operation_template"
            ),
        )
        local_project_root = Path(local_project_root_text)
        if not local_project_root.exists() or not local_project_root.is_dir():
            raise OperationError(
                "operation.local_project_root_missing",
                "remote_kikai_exec local_project_root does not exist",
                {"local_project_root": local_project_root_text},
            )
        remote_payload_project_root_text = resolve_text_ref(
            require_string(
                remote_payload_project_root,
                "operation.remote_payload_project_root_missing",
                (
                    "remote_kikai_exec request.remote_payload_project_root is required "
                    "with local_operation_template"
                ),
            )
        )
        project_payload_files = local_project_payload_files(
            local_project_root,
            request.get("local_project_payload_paths"),
        )
        operation_replacements = [
            *replacements,
            {"from": str(local_project_root), "to": remote_payload_project_root_text},
        ]
        operation_payload = replace_strings_in_value(operation_payload, operation_replacements)
    else:
        remote_payload_project_root_text = ""
    payload = {
        "remote_project_root": resolve_text_ref(remote_project_root),
        "remote_operation_template": remote_operation_template,
        "operation_payload": operation_payload,
        "project_payload_files": project_payload_files,
        "remote_payload_project_root": remote_payload_project_root_text,
        "remote_operation_path": remote_operation_path,
        "pipeline_run_id": pipeline_run_id,
        "env": env,
        "string_replacements": replacements,
        "uv_bin": resolve_text_ref(str(request.get("uv_bin") or "uv")),
    }
    command = [
        os.environ.get("KIKAI_SSH_BIN", "ssh"),
        ssh_host,
        str(request.get("remote_python_bin") or "python3"),
        "-",
    ]
    try:
        returncode, stdout_text, stderr_text = run_remote_ssh_streaming(
            command,
            remote_kikai_python_payload(payload),
        )
    except FileNotFoundError as exc:
        raise OperationError(
            "operation.ssh_not_found",
            "ssh executable was not found",
            {"ssh_bin": command[0]},
        ) from exc
    result = {
        "execution_status": "remote_kikai_exec_completed"
        if returncode == 0
        else "remote_kikai_exec_failed",
        "operation": request.get("operation"),
        "ssh_host": ssh_host,
        "remote_project_root": payload["remote_project_root"],
        "remote_operation_template": remote_operation_template,
        "remote_operation_path": remote_operation_path,
        "pipeline_run_id": pipeline_run_id,
        "returncode": returncode,
        "stdout_tail": stdout_text[-12000:],
        "stderr_tail": stderr_text[-4000:],
    }
    if returncode != 0:
        raise OperationError(
            "operation.remote_kikai_exec_failed",
            "remote kikai operation returned non-zero",
            result,
        )
    return result


# Backward-compatible name used by older call sites.
def execute_operation_noop_only(operation: dict[str, Any]) -> dict[str, Any]:
    return execute_operation(operation)
