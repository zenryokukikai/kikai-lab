from __future__ import annotations

import argparse
import json
from pathlib import Path

from kikai_lab.cli_commands.common import (
    add_json_flag,
    add_project_root,
    operation_error,
    single_operation_json_error,
    unknown_command,
)
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
    create_script_bundle,
    dump_operation_text,
    execute_operation_noop_only,
    load_operation,
    validate_guard_receipt,
)
from kikai_lab.report import build_project_report, render_report_html
from kikai_lab.template import (
    TemplateError,
    list_templates,
    load_template,
    parse_set_overrides,
    render_template,
)


def build_target_action_parser(action: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"kikai target {action}", add_help=True)
    parser.add_argument("operation_json")
    return parser


def build_exec_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kikai exec", add_help=True)
    parser.add_argument("operation_json")
    return parser


def configure_script_bundle_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("bundle_id")
    add_project_root(create)
    create.add_argument("--source-root", required=True)
    create.add_argument("--entrypoint", required=True)
    create.add_argument("--file", dest="files", action="append", default=[])
    create.add_argument("--include-dir", dest="include_dirs", action="append", default=[])
    create.add_argument("--argv", dest="entrypoint_argv", action="append", required=True)
    add_json_flag(create)


def configure_decision_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("decision_id")
    add_project_root(create)
    create.add_argument("--title", required=True)
    create.add_argument("--summary", default="")
    create.add_argument("--status", default="open", choices=list(DECISION_STATUSES))
    create.add_argument("--decided-at", default=None)
    create.add_argument("--link", dest="links", action="append", default=[],
                        help="kind:id link (repeatable), e.g. --link experiment:exp-001")
    add_json_flag(create)
    listp = subparsers.add_parser("list")
    add_project_root(listp)
    add_json_flag(listp)


def configure_report_parser(parser: argparse.ArgumentParser) -> None:
    add_project_root(parser)
    parser.add_argument("--json", action="store_true",
                        help="Include the full report JSON in the envelope data.")
    parser.add_argument("--out", default=None,
                        help="Write the report JSON to this path.")
    parser.add_argument("--html", default=None,
                        help="Write a self-contained HTML dashboard to this path.")


def configure_template_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("template_path")
    render.add_argument("--set", dest="sets", action="append", default=[],
                        help="key=value parameter override (repeatable).")
    render.add_argument("--out", default=None,
                        help="Write the rendered operation here (format by extension; "
                             "default stdout/JSON).")
    add_json_flag(render)
    listp = subparsers.add_parser("list")
    add_project_root(listp)
    add_json_flag(listp)


def run_operation_file(operation_path: Path) -> int:
    operation = load_operation(operation_path)
    validate_guard_receipt(operation)
    result = execute_operation_noop_only(operation)
    result["operation_file"] = str(operation_path)
    return emit(envelope(ok=True, data=result), 0)


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
        return run_operation_file(operation_path)
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
        return run_operation_file(operation_path)
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
        templates = list_templates(Path(args.project_root))
        return emit(envelope(ok=True, data={"templates": templates}), 0)
    if args.action == "render":
        try:
            template = load_template(Path(args.template_path))
            overrides = parse_set_overrides(args.sets)
            operation = render_template(template, overrides)
        except TemplateError as exc:
            return emit(envelope(ok=False, errors=[error(
                exc.code, exc.message, details=exc.details)]), 2)
        data: dict[str, object] = {"operation": operation}
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                dump_operation_text(operation, _operation_format(out_path)),
                encoding="utf-8")
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
