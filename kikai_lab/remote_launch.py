"""Build remote_kikai_exec launch operations without hand-patching JSON.

Absorbs the per-launch boilerplate an operator otherwise repeats every iteration:
  * walking the script-bundle tree to list the text payload files,
  * constructing the inner script_bundle_run operation,
  * wrapping it as a remote_kikai_exec op (local_operation_template branch) with the
    remote_payload_project_root / remote_operation_path / pipeline_run_id fields.

The builders are PURE (no I/O, no env reads) so they are trivially testable; the caller
writes the returned dicts to disk and runs `kikai target run <remote_op>`.

See execute_remote_kikai_exec_operation in operation.py for the consuming contract.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Text suffixes shipped in the payload (the remote materializes each with write_text;
# binary files cannot ride this channel — push those with remote_file_push).
PAYLOAD_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".json", ".yaml", ".yml", ".sh", ".txt", ".md"}
)


def collect_bundle_payload_paths(
    project_root: str | Path,
    bundle_id: str,
    *,
    extra: Iterable[str] = (),
    suffixes: Iterable[str] = PAYLOAD_TEXT_SUFFIXES,
) -> list[str]:
    """Relative payload paths for a bundle launch: ``extra`` (e.g. current.json, the
    container yaml, the inner op json) followed by every text file under the bundle
    tree, de-duplicated and order-preserving. Raises if the bundle dir is absent.
    """
    root = Path(project_root)
    bundle_dir = root / "script_bundles" / bundle_id
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle dir not found: {bundle_dir}")
    sset = {s if s.startswith(".") else "." + s for s in suffixes}
    bundle_files = sorted(
        str(p.relative_to(root))
        for p in bundle_dir.rglob("*")
        # Skip symlinks: they must not be listed into the payload (a symlinked
        # entry could otherwise point outside the bundle/project root).
        if p.is_file() and not p.is_symlink() and p.suffix in sset and "__pycache__" not in p.parts
    )
    out: list[str] = []
    seen: set[str] = set()
    for rel in [*extra, *bundle_files]:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def build_remote_kikai_exec_op(
    *,
    operation_id: str,
    ssh_host: str,
    remote_project_root: str,
    local_project_root: str | Path,
    local_operation_template: str | Path,
    local_project_payload_paths: list[str],
    env: dict[str, str] | None = None,
    remote_payload_project_root: str | None = None,
    remote_operation_path: str | None = None,
    pipeline_run_id: str | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    """A complete remote_kikai_exec operation (local_operation_template branch) that
    ships the payload files and runs the local template remotely. Defaults:
    remote_payload_project_root=/tmp/kikai_<operation_id>_project, remote_operation_path
    under that root's ops/, pipeline_run_id=operation_id. Mirrors the fields validated
    by execute_remote_kikai_exec_operation.
    """
    payload_root = remote_payload_project_root or f"/tmp/kikai_{operation_id}_project"
    op_path = remote_operation_path or f"{payload_root}/ops/{operation_id}.json"
    request: dict[str, Any] = {
        "adapter": "remote_kikai_exec",
        "operation": operation_id,
        "ssh_host": ssh_host,
        "remote_project_root": remote_project_root,
        "local_operation_template": str(local_operation_template),
        "local_project_root": str(local_project_root),
        "local_project_payload_paths": list(local_project_payload_paths),
        "remote_payload_project_root": payload_root,
        "remote_operation_path": op_path,
        "pipeline_run_id": pipeline_run_id or operation_id,
    }
    if env:
        request["env"] = dict(env)
    return {"kind": "kikai_operation", "schema_version": schema_version, "request": request}


def build_script_bundle_launch_ops(
    *,
    operation_id: str,
    project_root: str | Path,
    bundle_id: str,
    container_id: str,
    entrypoint: str,
    args: list[str],
    ssh_host: str,
    remote_project_root: str,
    env: dict[str, str] | None = None,
    detach: bool = True,
    container_yaml_rel: str | None = None,
    extra_payload: Iterable[str] = ("current.json",),
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """One call for the whole launch: returns ``(inner_op, remote_op, inner_rel,
    remote_rel)``. The caller writes ``inner_op`` to ``<project_root>/<inner_rel>`` and
    ``remote_op`` to ``<project_root>/<remote_rel>``, then runs
    ``kikai target run <project_root>/<remote_rel>``. The payload bundles current.json,
    the container yaml, the inner op json, and the whole bundle tree automatically.
    """
    root = Path(project_root)
    inner_rel = f"ops/{operation_id}.json"
    remote_rel = f"ops/remote_{operation_id}.json"
    container_rel = container_yaml_rel or f"containers/{container_id}.yaml"

    inner_request: dict[str, Any] = {
        "adapter": "script_bundle_run",
        "operation": operation_id,
        "project_root": str(root),
        "bundle_id": bundle_id,
        "entrypoint": entrypoint,
        "container_id": container_id,
        "detach": detach,
        "args": list(args),
    }
    if env:
        inner_request["env"] = dict(env)
    inner_op = {"kind": "kikai_operation", "schema_version": 1, "request": inner_request}

    payload_paths = collect_bundle_payload_paths(
        root, bundle_id, extra=[*extra_payload, container_rel, inner_rel]
    )
    remote_op = build_remote_kikai_exec_op(
        operation_id=f"remote_{operation_id}",
        ssh_host=ssh_host,
        remote_project_root=remote_project_root,
        local_project_root=root,
        local_operation_template=str(root / inner_rel),
        local_project_payload_paths=payload_paths,
        env=env,
    )
    return inner_op, remote_op, inner_rel, remote_rel
