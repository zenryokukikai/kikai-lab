from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from kikai_lab.cli_commands import COMMANDS
from kikai_lab.cli_commands.common import (
    operation_error,
    project_root_missing,
    single_operation_json_error,
    unknown_command,
)
from kikai_lab.cli_commands.data import (
    command_data_source,
    command_publish,
    command_source_snapshot,
)
from kikai_lab.cli_commands.operations import (
    build_exec_parser,
    build_target_action_parser,
    command_decision,
    command_exec,
    command_report,
    command_script_bundle,
    command_target,
    command_template,
)
from kikai_lab.cli_commands.project import (
    command_current,
    command_next,
    command_show,
    command_validate,
    current_warning,
    experiment_next_actions,
    validate_project,
    verify_current_action,
)
from kikai_lab.cli_commands.remote import (
    command_reconcile,
    command_remote_launch,
    command_serve,
    command_server,
    command_tensorboard,
)

__all__ = [
    "build_exec_parser",
    "build_parser",
    "build_target_action_parser",
    "build_top_level_parser",
    "command_current",
    "command_data_source",
    "command_decision",
    "command_exec",
    "command_next",
    "command_publish",
    "command_reconcile",
    "command_remote_launch",
    "command_report",
    "command_script_bundle",
    "command_serve",
    "command_server",
    "command_show",
    "command_source_snapshot",
    "command_target",
    "command_template",
    "command_tensorboard",
    "command_validate",
    "current_warning",
    "experiment_next_actions",
    "main",
    "operation_error",
    "project_root_missing",
    "single_operation_json_error",
    "unknown_command",
    "validate_project",
    "verify_current_action",
]


def build_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"kikai {command}", add_help=True)
    entry = COMMANDS.get(command)
    if entry is not None:
        entry.configure(parser)
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
            "publish",
            "reconcile",
            "remote",
            "serve",
            "target",
            "exec",
        ],
    )
    return parser


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
    entry = COMMANDS.get(command)
    if entry is None:
        return unknown_command(command)
    args = build_parser(command).parse_args(argv[1:])
    return entry.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
