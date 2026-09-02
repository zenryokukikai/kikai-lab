from __future__ import annotations

import argparse
from pathlib import Path

from kikai_lab.cli_commands.common import (
    add_json_flag,
    add_project_root,
    operation_error,
    project_root_missing,
    unknown_command,
)
from kikai_lab.envelope import emit, envelope, error
from kikai_lab.operation import (
    OperationError,
    create_directory_data_source,
    create_file_data_source,
    create_source_snapshot,
)
from kikai_lab.publish import publish_operations
from kikai_lab.validation import load_data_source, validate_data_source_record


def configure_source_snapshot_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("source_snapshot_id")
    add_project_root(create)
    create.add_argument("--source-root", required=True)
    create.add_argument("--file", dest="files", action="append", default=[])
    create.add_argument("--include-dir", dest="include_dirs", action="append", default=[])
    add_json_flag(create)


def configure_data_source_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("show", "validate"):
        action_parser = subparsers.add_parser(action)
        action_parser.add_argument("data_source_id")
        add_project_root(action_parser)
        add_json_flag(action_parser)
    for action in ("create-file", "create-directory"):
        create = subparsers.add_parser(action)
        create.add_argument("data_source_id")
        add_project_root(create)
        create.add_argument("--source-type", required=True)
        create.add_argument("--path", required=True)
        create.add_argument("--host-ref", required=True)
        create.add_argument("--role", dest="roles", action="append", required=True)
        create.add_argument("--summary", required=True)
        create.add_argument("--container-mount-path")
        create.add_argument(
            "--upstream-data-source-id",
            dest="upstream_data_source_ids",
            action="append",
            default=[],
        )
        create.add_argument(
            "--upstream-source-snapshot-id",
            dest="upstream_source_snapshot_ids",
            action="append",
            default=[],
        )
        create.add_argument("--overwrite", action="store_true")
        add_json_flag(create)


def configure_publish_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    add_project_root(parser)
    parser.add_argument(
        "--ops",
        nargs="+",
        required=True,
        metavar="OP_JSON",
        help="operation files; the given order IS the execution order",
    )
    parser.add_argument(
        "--out", default=None, help="output directory (default: ./publish/<name>)"
    )


def command_source_snapshot(args: argparse.Namespace) -> int:
    if args.action != "create":
        return unknown_command(f"source-snapshot {args.action}")
    try:
        result = create_source_snapshot(
            project_root=Path(args.project_root),
            source_root=Path(args.source_root),
            source_snapshot_id=args.source_snapshot_id,
            file_paths=args.files,
            include_dirs=args.include_dirs,
        )
        return emit(envelope(ok=True, data=result), 0)
    except OperationError as exc:
        return operation_error(exc)


def command_data_source(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    if args.action in {"create-file", "create-directory"}:
        create = (
            create_file_data_source
            if args.action == "create-file"
            else create_directory_data_source
        )
        try:
            result = create(
                project_root=project_root,
                data_source_id=args.data_source_id,
                source_type=args.source_type,
                path_ref=args.path,
                host_ref=args.host_ref,
                role_compatibility=args.roles,
                summary=args.summary,
                container_mount_path=args.container_mount_path,
                upstream_data_source_ids=args.upstream_data_source_ids,
                upstream_source_snapshot_ids=args.upstream_source_snapshot_ids,
                overwrite=args.overwrite,
            )
        except OperationError as exc:
            return operation_error(exc)
        return emit(envelope(ok=True, data=result), 0)
    try:
        data_source = load_data_source(project_root, args.data_source_id)
    except OperationError as exc:
        return operation_error(exc)
    if args.action == "show":
        return emit(envelope(ok=True, data={"data_source": data_source}), 0)
    if args.action == "validate":
        errors = validate_data_source_record(project_root, args.data_source_id, data_source)
        return emit(
            envelope(
                ok=not errors,
                data={"data_source_id": args.data_source_id} if not errors else {},
                errors=errors,
            ),
            0 if not errors else 1,
        )
    return unknown_command(f"data-source {args.action}")


def command_publish(args: argparse.Namespace) -> int:
    """Write the given op sequence out as a kikai-independent package (issue #63)."""
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    out_dir = Path(args.out) if args.out else Path("publish") / args.name
    try:
        result, plan = publish_operations(
            name=args.name,
            project_root=project_root,
            operation_paths=[Path(item) for item in args.ops],
            out_dir=out_dir,
        )
    except OperationError as exc:
        return operation_error(exc)
    # 除外した op は envelope にも必ず出す。README だけに書いて黙って落とすのが一番まずい。
    warnings = [
        error(
            "publish.operation_excluded",
            f"op {op.index}/{len(plan.ops)} '{op.operation}' was not published: "
            f"{op.skipped_reason}",
            blocking=False,
            details={"index": op.index, "operation": op.operation, "adapter": op.adapter},
        )
        for op in plan.skipped
    ]
    return emit(envelope(ok=True, data=result, warnings=warnings), 0)
