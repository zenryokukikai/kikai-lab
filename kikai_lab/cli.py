from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from kikai_lab import reconcile
from kikai_lab.decision import (
    DECISION_STATUSES,
    DecisionError,
    create_decision,
    load_decisions,
)
from kikai_lab.envelope import emit, envelope, error, next_action
from kikai_lab.operation import (
    OperationError,
    _operation_format,
    add_guard_receipt,
    create_directory_data_source,
    create_file_data_source,
    create_script_bundle,
    create_source_snapshot,
    dump_operation_text,
    execute_operation_noop_only,
    load_operation,
    validate_guard_receipt,
)
from kikai_lab.remote_launch import build_script_bundle_launch_ops
from kikai_lab.report import build_project_report, render_report_html
from kikai_lab.server_config import set_server_value
from kikai_lab.store import CurrentState, compute_current_state, load_current
from kikai_lab.template import (
    TemplateError,
    list_templates,
    load_template,
    parse_set_overrides,
    render_template,
)
from kikai_lab.tensorboard import current_tensorboard_plan, write_tensorboard_operation
from kikai_lab.validation import (
    load_data_source,
    load_yaml,
    validate_data_source_record,
    validate_registry_links,
)


def build_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"kikai {command}", add_help=True)
    if command in {"validate", "current", "next"}:
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--json", action="store_true")
    elif command == "show":
        parser.add_argument("object_type", choices=["experiment", "run", "container"])
        parser.add_argument("object_id")
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--json", action="store_true")
    elif command == "script-bundle":
        subparsers = parser.add_subparsers(dest="action", required=True)
        create = subparsers.add_parser("create")
        create.add_argument("bundle_id")
        create.add_argument("--project-root", required=True)
        create.add_argument("--source-root", required=True)
        create.add_argument("--entrypoint", required=True)
        create.add_argument("--file", dest="files", action="append", default=[])
        create.add_argument("--include-dir", dest="include_dirs", action="append", default=[])
        create.add_argument("--argv", dest="entrypoint_argv", action="append", required=True)
        create.add_argument("--json", action="store_true")
    elif command == "decision":
        subparsers = parser.add_subparsers(dest="action", required=True)
        create = subparsers.add_parser("create")
        create.add_argument("decision_id")
        create.add_argument("--project-root", required=True)
        create.add_argument("--title", required=True)
        create.add_argument("--summary", default="")
        create.add_argument("--status", default="open", choices=list(DECISION_STATUSES))
        create.add_argument("--decided-at", default=None)
        create.add_argument("--link", dest="links", action="append", default=[],
                            help="kind:id link (repeatable), e.g. --link experiment:exp-001")
        create.add_argument("--json", action="store_true")
        listp = subparsers.add_parser("list")
        listp.add_argument("--project-root", required=True)
        listp.add_argument("--json", action="store_true")
    elif command == "report":
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--json", action="store_true",
                            help="Include the full report JSON in the envelope data.")
        parser.add_argument("--out", default=None,
                            help="Write the report JSON to this path.")
        parser.add_argument("--html", default=None,
                            help="Write a self-contained HTML dashboard to this path.")
    elif command == "remote-launch":
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--operation-id", required=True)
        parser.add_argument("--bundle-id", required=True)
        parser.add_argument("--container-id", required=True)
        parser.add_argument("--entrypoint", required=True)
        parser.add_argument("--ssh-host", required=True)
        parser.add_argument("--remote-project-root", required=True)
        parser.add_argument("--args-json", default=None,
                            help='JSON list of entrypoint args, e.g. \'["--max-steps","100"]\'.')
        parser.add_argument("--arg", dest="op_args", action="append", default=[],
                            help="Single entrypoint arg (repeatable; use --arg=--flag for dash-leading values).")
        parser.add_argument("--env", dest="envs", action="append", default=[],
                            help="KEY=VALUE container env (repeatable).")
        parser.add_argument("--no-detach", action="store_true")
        parser.add_argument("--container-yaml", default=None,
                            help="Relative container yaml path (default containers/<container-id>.yaml).")
        parser.add_argument("--extra-payload", dest="extra_payload", action="append", default=[],
                            help="Extra relative payload file (repeatable; default current.json).")
        parser.add_argument("--json", action="store_true")
    elif command == "template":
        subparsers = parser.add_subparsers(dest="action", required=True)
        render = subparsers.add_parser("render")
        render.add_argument("template_path")
        render.add_argument("--set", dest="sets", action="append", default=[],
                            help="key=value parameter override (repeatable).")
        render.add_argument("--out", default=None,
                            help="Write the rendered operation here (format by extension; default stdout/JSON).")
        render.add_argument("--json", action="store_true")
        listp = subparsers.add_parser("list")
        listp.add_argument("--project-root", required=True)
        listp.add_argument("--json", action="store_true")
    elif command == "source-snapshot":
        subparsers = parser.add_subparsers(dest="action", required=True)
        create = subparsers.add_parser("create")
        create.add_argument("source_snapshot_id")
        create.add_argument("--project-root", required=True)
        create.add_argument("--source-root", required=True)
        create.add_argument("--file", dest="files", action="append", default=[])
        create.add_argument("--include-dir", dest="include_dirs", action="append", default=[])
        create.add_argument("--json", action="store_true")
    elif command == "data-source":
        subparsers = parser.add_subparsers(dest="action", required=True)
        for action in ("show", "validate"):
            action_parser = subparsers.add_parser(action)
            action_parser.add_argument("data_source_id")
            action_parser.add_argument("--project-root", required=True)
            action_parser.add_argument("--json", action="store_true")
        create_file = subparsers.add_parser("create-file")
        create_file.add_argument("data_source_id")
        create_file.add_argument("--project-root", required=True)
        create_file.add_argument("--source-type", required=True)
        create_file.add_argument("--path", required=True)
        create_file.add_argument("--host-ref", required=True)
        create_file.add_argument("--role", dest="roles", action="append", required=True)
        create_file.add_argument("--summary", required=True)
        create_file.add_argument("--container-mount-path")
        create_file.add_argument(
            "--upstream-data-source-id",
            dest="upstream_data_source_ids",
            action="append",
            default=[],
        )
        create_file.add_argument(
            "--upstream-source-snapshot-id",
            dest="upstream_source_snapshot_ids",
            action="append",
            default=[],
        )
        create_file.add_argument("--overwrite", action="store_true")
        create_file.add_argument("--json", action="store_true")
        create_directory = subparsers.add_parser("create-directory")
        create_directory.add_argument("data_source_id")
        create_directory.add_argument("--project-root", required=True)
        create_directory.add_argument("--source-type", required=True)
        create_directory.add_argument("--path", required=True)
        create_directory.add_argument("--host-ref", required=True)
        create_directory.add_argument("--role", dest="roles", action="append", required=True)
        create_directory.add_argument("--summary", required=True)
        create_directory.add_argument("--container-mount-path")
        create_directory.add_argument(
            "--upstream-data-source-id",
            dest="upstream_data_source_ids",
            action="append",
            default=[],
        )
        create_directory.add_argument(
            "--upstream-source-snapshot-id",
            dest="upstream_source_snapshot_ids",
            action="append",
            default=[],
        )
        create_directory.add_argument("--overwrite", action="store_true")
        create_directory.add_argument("--json", action="store_true")
    elif command == "server":
        subparsers = parser.add_subparsers(dest="object_type", required=True)
        for object_type in ("setting", "secret"):
            object_parser = subparsers.add_parser(object_type)
            object_subparsers = object_parser.add_subparsers(dest="action", required=True)
            set_parser = object_subparsers.add_parser("set")
            set_parser.add_argument("name")
            set_parser.add_argument("--value", required=True)
            set_parser.add_argument("--json", action="store_true")
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
    elif command == "tensorboard":
        subparsers = parser.add_subparsers(dest="action", required=True)
        ensure = subparsers.add_parser("ensure-current")
        ensure.add_argument("--project-root", required=True)
        ensure.add_argument("--run-name", default=None)
        ensure.add_argument("--port", type=int, default=None)
        ensure.add_argument("--write-operation", default=None)
        ensure.add_argument("--json", action="store_true")
    elif command == "reconcile":
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--run-id", default=None)
        parser.add_argument("--once", action="store_true")
    elif command == "serve":
        parser.add_argument("--project-root", required=True)
        parser.add_argument("--run-id", default=None)
        parser.add_argument(
            "--interval", type=int, default=reconcile.DEFAULT_POLL_INTERVAL_SEC
        )
        parser.add_argument("--once", action="store_true")
    return parser


def build_top_level_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kikai", add_help=True)
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "validate",
            "current",
            "show",
            "next",
            "script-bundle",
            "decision",
            "report",
            "remote-launch",
            "source-snapshot",
            "data-source",
            "server",
            "tensorboard",
            "reconcile",
            "remote",
            "serve",
            "target",
            "exec",
        ],
    )
    return parser


def build_target_action_parser(action: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"kikai target {action}", add_help=True)
    parser.add_argument("operation_json")
    return parser


def build_exec_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kikai exec", add_help=True)
    parser.add_argument("operation_json")
    return parser


def project_root_missing(project_root: Path) -> int:
    payload = envelope(
        ok=False,
        errors=[
            error(
                "registry.project_root_missing",
                f"project root does not exist: {project_root}",
                details={"project_root": str(project_root)},
            )
        ],
        next_actions=[
            next_action(
                "create_registry_root",
                "create_directory",
                "create or choose a registry root before running this command",
            )
        ],
    )
    return emit(payload, 2)


def current_warning(state: CurrentState) -> list[dict]:
    if state.staleness != "warn":
        return []
    return [
        error(
            "current.staleness_warn",
            "current pointer verification is older than warn threshold",
            blocking=False,
            details={"age_hours": state.age_hours},
        )
    ]


def verify_current_action() -> dict:
    return next_action(
        "verify_current",
        "registry_update",
        "run kikai verify-current after checking current run/checkpoint/model_arch",
        command="kikai verify-current --project-root <registry-root> --json",
    )


def validate_project(project_root: Path) -> tuple[CurrentState, list[dict], list[dict], list[dict]]:
    state = compute_current_state(load_current(project_root))
    warnings = current_warning(state)
    errors: list[dict] = []
    actions: list[dict] = []
    if state.staleness == "stale":
        errors.append(
            error(
                "current.stale",
                "current pointer verification is older than block threshold",
                details={"age_hours": state.age_hours},
            )
        )
        actions.append(verify_current_action())
    else:
        errors.extend(validate_registry_links(project_root, state))
    return state, warnings, errors, actions


def command_current(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    state = compute_current_state(load_current(project_root))
    payload = envelope(
        ok=True,
        data={
            "current": state.current,
            "age_hours": state.age_hours,
            "staleness": state.staleness,
        },
        warnings=current_warning(state),
    )
    return emit(payload, 0)


def command_validate(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    state, warnings, errors, actions = validate_project(project_root)
    payload = envelope(
        ok=not errors,
        data={"staleness": state.staleness, "age_hours": state.age_hours} if not errors else {},
        warnings=warnings,
        errors=errors,
        next_actions=actions,
    )
    return emit(payload, 0 if not errors else 1)


def command_show(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    if args.object_type == "experiment":
        path = project_root / "experiments" / f"{args.object_id}.yaml"
        data_key = "experiment"
        missing_code = "show.experiment_missing"
    elif args.object_type == "run":
        path = project_root / "runs" / f"{args.object_id}.yaml"
        data_key = "run"
        missing_code = "show.run_missing"
    else:
        path = project_root / "containers" / f"{args.object_id}.yaml"
        data_key = "container"
        missing_code = "show.container_missing"
    if not path.exists():
        payload = envelope(
            ok=False,
            errors=[
                error(
                    missing_code,
                    f"record is missing: {args.object_id}",
                    details={"path": str(path)},
                )
            ],
        )
        return emit(payload, 1)
    payload = envelope(ok=True, data={data_key: load_yaml(path)})
    return emit(payload, 0)


def experiment_next_actions(project_root: Path, state: CurrentState) -> list[dict]:
    experiment_id = state.current.get("current_experiment_id")
    path = project_root / "experiments" / f"{experiment_id}.yaml"
    if not path.exists():
        return []
    experiment = load_yaml(path)
    actions = []
    for action in experiment.get("next_actions", []) or []:
        if not isinstance(action, dict):
            continue
        item = dict(action)
        item.setdefault("blocking", False)
        actions.append(item)
    return actions


def command_next(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    if not project_root.exists():
        return project_root_missing(project_root)
    state, warnings, errors, actions = validate_project(project_root)
    if not errors:
        actions.extend(experiment_next_actions(project_root, state))
    payload = envelope(
        ok=not errors,
        data={"staleness": state.staleness, "age_hours": state.age_hours},
        warnings=warnings,
        errors=errors,
        next_actions=actions,
    )
    return emit(payload, 0 if not errors else 1)


def unknown_command(command: str) -> int:
    payload = envelope(
        ok=False,
        errors=[error("cli.unknown_command", f"unknown command: {command}")],
    )
    return emit(payload, 2)


def single_operation_json_error() -> int:
    payload = envelope(
        ok=False,
        errors=[
            error(
                "operation.single_json_argument_required",
                "side-effect commands accept exactly one positional operation JSON path",
            )
        ],
    )
    return emit(payload, 2)


def operation_error(exc: OperationError) -> int:
    payload = envelope(
        ok=False,
        errors=[error(exc.code, exc.message, details=exc.details)],
    )
    return emit(payload, 1)


def command_target(argv: list[str]) -> int:
    if (
        len(argv) >= 2
        and argv[1] in {"dry-run", "run"}
        and any(item in {"-h", "--help"} for item in argv[2:])
    ):
        build_target_action_parser(argv[1]).parse_args(argv[2:])
        return 0
    if len(argv) != 3:
        return single_operation_json_error()
    _, action, operation_path_text = argv
    if action not in {"dry-run", "run"} or operation_path_text.startswith("-"):
        return single_operation_json_error()
    operation_path = Path(operation_path_text)
    try:
        if action == "dry-run":
            operation = add_guard_receipt(operation_path)
            payload = envelope(
                ok=True,
                data={
                    "operation_file": str(operation_path),
                    "guard_receipt": operation["guard_receipt"],
                },
            )
            return emit(payload, 0)
        operation = load_operation(operation_path)
        validate_guard_receipt(operation)
        result = execute_operation_noop_only(operation)
        result["operation_file"] = str(operation_path)
        return emit(envelope(ok=True, data=result), 0)
    except OperationError as exc:
        return operation_error(exc)


def command_exec(argv: list[str]) -> int:
    if any(item in {"-h", "--help"} for item in argv[1:]):
        build_exec_parser().parse_args(argv[1:])
        return 0
    if len(argv) != 2:
        return single_operation_json_error()
    _, operation_path_text = argv
    if operation_path_text.startswith("-"):
        return single_operation_json_error()
    operation_path = Path(operation_path_text)
    try:
        operation = load_operation(operation_path)
        validate_guard_receipt(operation)
        result = execute_operation_noop_only(operation)
        result["operation_file"] = str(operation_path)
        return emit(envelope(ok=True, data=result), 0)
    except OperationError as exc:
        return operation_error(exc)


def command_script_bundle(args: argparse.Namespace) -> int:
    if args.action != "create":
        return unknown_command(f"script-bundle {args.action}")
    try:
        result = create_script_bundle(
            project_root=Path(args.project_root),
            source_root=Path(args.source_root),
            bundle_id=args.bundle_id,
            entrypoint=args.entrypoint,
            file_paths=args.files,
            include_dirs=args.include_dirs,
            entrypoint_argv=args.entrypoint_argv,
        )
        return emit(envelope(ok=True, data=result), 0)
    except OperationError as exc:
        return operation_error(exc)


def command_decision(args: argparse.Namespace) -> int:
    """Manage decision records inside the project (decisions/<id>.yaml)."""
    if args.action == "create":
        try:
            links: list[dict[str, str]] = []
            for spec in args.links:
                if ":" not in spec:
                    return emit(envelope(ok=False, errors=[error(
                        "decision.link_invalid", f"--link must be kind:id, got: {spec}")]), 2)
                kind, ref_id = spec.split(":", 1)
                links.append({"kind": kind, "id": ref_id})
            result = create_decision(
                Path(args.project_root), args.decision_id,
                title=args.title, summary=args.summary, status=args.status,
                decided_at=args.decided_at, links=links or None)
            return emit(envelope(ok=True, data=result), 0)
        except DecisionError as exc:
            return emit(envelope(ok=False, errors=[error(
                exc.code, exc.message, details=exc.details)]), 2)
    if args.action == "list":
        decisions = load_decisions(Path(args.project_root))
        return emit(envelope(ok=True, data={"decisions": decisions, "count": len(decisions)}), 0)
    return unknown_command(f"decision {args.action}")


def command_template(args: argparse.Namespace) -> int:
    """Render a parameterised operation template into a concrete operation, or list templates.

    `render` substitutes {{name}} placeholders from --set overrides + declared defaults, then
    writes a normal operation object (that still goes through `kikai target dry-run`/`run`).
    `list` shows templates/<name>.* with their parameters."""
    if args.action == "list":
        return emit(envelope(ok=True, data={"templates": list_templates(Path(args.project_root))}), 0)
    if args.action == "render":
        try:
            template = load_template(Path(args.template_path))
            overrides = parse_set_overrides(args.sets)
            operation = render_template(template, overrides)
        except TemplateError as exc:
            return emit(envelope(ok=False, errors=[error(exc.code, exc.message, details=exc.details)]), 2)
        data: dict[str, object] = {"operation": operation}
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(dump_operation_text(operation, _operation_format(out_path)), encoding="utf-8")
            data = {"operation_path": str(out_path)}
            next_op = next_action(
                "target_dry_run", "guard_check",
                "dry-run the rendered operation to compute its guard receipt before running",
                command=f"kikai target dry-run {out_path}")
            return emit(envelope(ok=True, data=data, next_actions=[next_op]), 0)
        return emit(envelope(ok=True, data=data), 0)
    return unknown_command(f"template {args.action}")


def command_report(args: argparse.Namespace) -> int:
    """Aggregate the project (current.json + experiments/ + containers/) into a report;
    emit JSON and/or a self-contained offline HTML dashboard."""
    try:
        report = build_project_report(Path(args.project_root))
    except FileNotFoundError as exc:
        return emit(envelope(ok=False, errors=[error(
            "report.project_missing", str(exc))]), 2)
    data: dict[str, object] = {
        "experiment_count": report["experiment_count"],
        "run_count": report["run_count"],
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        data["json_path"] = str(Path(args.out))
    if args.html:
        Path(args.html).write_text(render_report_html(report), encoding="utf-8")
        data["html_path"] = str(Path(args.html))
    if args.json or (not args.out and not args.html):
        data["report"] = report
    return emit(envelope(ok=True, data=data), 0)


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
    if args.action == "create-file":
        try:
            result = create_file_data_source(
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
    if args.action == "create-directory":
        try:
            result = create_directory_data_source(
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


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        build_top_level_parser().print_help()
        return 0
    command = argv[0]
    if command in {"-h", "--help"}:
        build_top_level_parser().parse_args(argv)
        return 0
    if command == "remote":
        from kikai_lab.remote_client import command_remote
        return command_remote(argv[1:])
    if command == "target":
        return command_target(argv)
    if command == "exec":
        return command_exec(argv)
    if command not in {
        "validate",
        "current",
        "show",
        "next",
        "script-bundle",
        "decision",
        "report",
        "remote-launch",
        "template",
        "source-snapshot",
        "data-source",
        "server",
        "tensorboard",
        "reconcile",
        "serve",
    }:
        return unknown_command(command)
    parser = build_parser(command)
    args = parser.parse_args(argv[1:])
    if command == "validate":
        return command_validate(args)
    if command == "current":
        return command_current(args)
    if command == "show":
        return command_show(args)
    if command == "next":
        return command_next(args)
    if command == "script-bundle":
        return command_script_bundle(args)
    if command == "decision":
        return command_decision(args)
    if command == "report":
        return command_report(args)
    if command == "remote-launch":
        return command_remote_launch(args)
    if command == "template":
        return command_template(args)
    if command == "source-snapshot":
        return command_source_snapshot(args)
    if command == "data-source":
        return command_data_source(args)
    if command == "server":
        return command_server(args)
    if command == "tensorboard":
        return command_tensorboard(args)
    if command == "reconcile":
        return command_reconcile(args)
    if command == "serve":
        return command_serve(args)
    return unknown_command(command)


if __name__ == "__main__":
    raise SystemExit(main())
