from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from kikai_lab import reconcile
from kikai_lab.cli_commands.common import (
    add_json_flag,
    add_project_root,
    operation_error,
    project_root_missing,
    unknown_command,
)
from kikai_lab.envelope import emit, envelope, error, next_action
from kikai_lab.operation import OperationError
from kikai_lab.remote_launch import build_script_bundle_launch_ops
from kikai_lab.server_config import set_server_value
from kikai_lab.tensorboard import current_tensorboard_plan, write_tensorboard_operation


def configure_remote_launch_parser(parser: argparse.ArgumentParser) -> None:
    add_project_root(parser)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--remote-project-root", required=True)
    parser.add_argument("--args-json", default=None,
                        help='JSON list of entrypoint args, e.g. \'["--max-steps","100"]\'.')
    parser.add_argument("--arg", dest="op_args", action="append", default=[],
                        help="Single entrypoint arg (repeatable; use --arg=--flag "
                             "for dash-leading values).")
    parser.add_argument("--env", dest="envs", action="append", default=[],
                        help="KEY=VALUE container env (repeatable).")
    parser.add_argument("--no-detach", action="store_true")
    parser.add_argument("--container-yaml", default=None,
                        help="Relative container yaml path "
                             "(default containers/<container-id>.yaml).")
    parser.add_argument("--extra-payload", dest="extra_payload", action="append", default=[],
                        help="Extra relative payload file (repeatable; default current.json).")
    add_json_flag(parser)


def configure_server_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="object_type", required=True)
    for object_type in ("setting", "secret"):
        object_parser = subparsers.add_parser(object_type)
        object_subparsers = object_parser.add_subparsers(dest="action", required=True)
        set_parser = object_subparsers.add_parser("set")
        set_parser.add_argument("name")
        set_parser.add_argument("--value", required=True)
        add_json_flag(set_parser)
    start = subparsers.add_parser(
        "start", help="Run the kikai HTTP server over a projects root."
    )
    start.add_argument("--projects-root", required=True)
    start.add_argument("--host", default="127.0.0.1",
                       help="Bind address (default 127.0.0.1; 0.0.0.0 must be explicit).")
    start.add_argument("--port", type=int, default=8300)
    start.add_argument("--auth-token", default=None,
                       help="Require 'Authorization: Bearer <token>' on every "
                            "request except /healthz (default: KIKAI_AUTH_TOKEN "
                            "env, else no auth — see SECURITY.md).")
    start.add_argument("--host-id", default="local",
                       help="This host's id for future multi-host routing (default: local).")
    start.add_argument("--content-root", dest="content_roots", action="append", default=[],
                       help="Directory artifact /content may serve files from "
                            "(repeatable; none configured = content serving disabled).")
    start.add_argument("--path-map", dest="path_maps", action="append", default=[],
                       help="CONTAINER_PREFIX=HOST_PREFIX rewrite for artifact "
                            "container_path locations (repeatable; env:/${} refs "
                            "allowed in HOST_PREFIX, resolved at startup).")
    start.add_argument("--run-dir-root", dest="run_dir_roots", action="append", default=[],
                       help="Contain run_dir-based reads (metrics/checkpoints) to "
                            "these roots (repeatable; recommended when exposed "
                            "beyond localhost).")
    start.add_argument("--with-reconciler", action="store_true",
                       help="Run the reconciler loop over every active project in "
                            "this server process (one reconciler per registry; do "
                            "not combine with an external 'kikai serve').")
    start.add_argument("--reconcile-interval", type=int, default=60)


def configure_tensorboard_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    ensure = subparsers.add_parser("ensure-current")
    add_project_root(ensure)
    ensure.add_argument("--run-name", default=None)
    ensure.add_argument("--port", type=int, default=None)
    ensure.add_argument("--write-operation", default=None)
    add_json_flag(ensure)


def configure_reconcile_parser(parser: argparse.ArgumentParser) -> None:
    add_project_root(parser)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--once", action="store_true")


def configure_serve_parser(parser: argparse.ArgumentParser) -> None:
    add_project_root(parser)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--interval", type=int, default=reconcile.DEFAULT_POLL_INTERVAL_SEC
    )
    parser.add_argument("--once", action="store_true")


def command_remote_launch(args: argparse.Namespace) -> int:
    """Build + write the inner script_bundle_run op and its remote_kikai_exec wrapper
    (payload auto-collected from the bundle tree). Prints both paths and the ready
    `kikai target run` command. Absorbs the per-launch JSON boilerplate."""
    try:
        env: dict[str, str] = {}
        for kv in args.envs:
            if "=" not in kv:
                return emit(envelope(ok=False, errors=[error(
                    "remote_launch.env_invalid",
                    f"--env must be KEY=VALUE, got: {kv}")]), 2)
            key, value = kv.split("=", 1)
            env[key] = value
        if args.args_json is not None:
            op_args = json.loads(args.args_json)
            if not isinstance(op_args, list) or not all(isinstance(a, str) for a in op_args):
                return emit(envelope(ok=False, errors=[error(
                    "remote_launch.args_json_invalid",
                    "--args-json must be a JSON list of strings")]), 2)
        else:
            op_args = list(args.op_args)
        inner_op, remote_op, inner_rel, remote_rel = build_script_bundle_launch_ops(
            operation_id=args.operation_id,
            project_root=Path(args.project_root),
            bundle_id=args.bundle_id,
            container_id=args.container_id,
            entrypoint=args.entrypoint,
            args=op_args,
            ssh_host=args.ssh_host,
            remote_project_root=args.remote_project_root,
            env=env or None,
            detach=not args.no_detach,
            container_yaml_rel=args.container_yaml,
            extra_payload=tuple(args.extra_payload) if args.extra_payload else ("current.json",),
        )
        root = Path(args.project_root)
        inner_path = root / inner_rel
        remote_path = root / remote_rel
        inner_path.parent.mkdir(parents=True, exist_ok=True)
        inner_path.write_text(json.dumps(inner_op, ensure_ascii=False, indent=2) + "\n")
        remote_path.write_text(json.dumps(remote_op, ensure_ascii=False, indent=2) + "\n")
        return emit(envelope(ok=True, data={
            "inner_operation": str(inner_path),
            "remote_operation": str(remote_path),
            "payload_file_count": len(remote_op["request"]["local_project_payload_paths"]),
        }, next_actions=[next_action(
            "run", "run",
            "Ship the payload and run the launch on the training host.",
            command=f"kikai target run {remote_path}")]), 0)
    except FileNotFoundError as exc:
        return emit(envelope(ok=False, errors=[error(
            "remote_launch.bundle_missing", str(exc))]), 2)
    except json.JSONDecodeError as exc:
        return emit(envelope(ok=False, errors=[error(
            "remote_launch.args_json_invalid", f"--args-json is not valid JSON: {exc}")]), 2)


def command_server(args: argparse.Namespace) -> int:
    if args.object_type == "start":
        # Lazy imports keep FastAPI/uvicorn off the hot path of every other command.
        import uvicorn

        from kikai_lab.server.app import create_app
        from kikai_lab.server.registry import ServerConfig

        projects_root = Path(args.projects_root)
        if not projects_root.is_dir():
            payload = envelope(
                ok=False,
                errors=[
                    error(
                        "server.projects_root_missing",
                        f"projects root does not exist: {projects_root}",
                        details={"projects_root": str(projects_root)},
                    )
                ],
                next_actions=[
                    next_action(
                        "create_projects_root",
                        "create_directory",
                        "create or choose a projects root before starting the server",
                    )
                ],
            )
            return emit(payload, 2)
        path_map: dict[str, str] = {}
        for entry in args.path_maps:
            prefix, separator, target = entry.partition("=")
            if not separator or not prefix or not target:
                return emit(
                    envelope(
                        ok=False,
                        errors=[
                            error(
                                "server.path_map_invalid",
                                "--path-map must be CONTAINER_PREFIX=HOST_PREFIX",
                                details={"entry": entry},
                            )
                        ],
                    ),
                    2,
                )
            from kikai_lab.operation import resolve_text_ref

            path_map[prefix] = resolve_text_ref(target)
        config = ServerConfig(
            projects_root=projects_root,
            host_id=args.host_id,
            content_roots=tuple(Path(p) for p in args.content_roots),
            path_map=path_map,
            run_dir_roots=tuple(Path(p) for p in args.run_dir_roots),
            with_reconciler=args.with_reconciler,
            reconcile_interval=args.reconcile_interval,
            auth_token=args.auth_token or os.environ.get("KIKAI_AUTH_TOKEN") or None,
        )
        uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1)
        return 0
    if args.action != "set":
        return unknown_command(f"server {args.object_type} {args.action}")
    kind = "secrets" if args.object_type == "secret" else "settings"
    set_server_value(kind, args.name, args.value)
    payload = envelope(
        ok=True,
        data={
            "name": args.name,
            "stored": True,
            "secret": args.object_type == "secret",
        },
    )
    return emit(payload, 0)


def command_tensorboard(args: argparse.Namespace) -> int:
    if args.action != "ensure-current":
        return unknown_command(f"tensorboard {args.action}")
    try:
        project_root = Path(args.project_root)
        if not project_root.exists():
            return project_root_missing(project_root)
        data = current_tensorboard_plan(
            project_root, port_override=args.port, run_name_override=args.run_name
        )
        if args.write_operation:
            operation = data.get("operation")
            if not isinstance(operation, dict):
                raise OperationError(
                    "tensorboard.not_required",
                    "TensorBoard is not required for the current run",
                    {"project_root": str(project_root)},
                )
            op_path = Path(args.write_operation)
            write_tensorboard_operation(op_path, operation)
            data["operation_file"] = str(op_path)
        return emit(envelope(ok=True, data=data), 0)
    except OperationError as exc:
        return operation_error(exc)


def command_reconcile(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    try:
        result = reconcile.reconcile_once(project_root, args.run_id)
    except OperationError as exc:
        return operation_error(exc)
    return emit(envelope(ok=True, data=result), 0)


def command_serve(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    try:
        if args.once:
            result = reconcile.serve(
                project_root, interval=args.interval, once=True, run_id=args.run_id
            )
            return emit(envelope(ok=True, data=result), 0)
        # Long-running: reconcile every --interval seconds until interrupted.
        reconcile.serve(project_root, interval=args.interval, once=False, run_id=args.run_id)
        return 0
    except KeyboardInterrupt:
        return 0
    except OperationError as exc:
        return operation_error(exc)
