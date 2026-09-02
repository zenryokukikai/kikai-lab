from __future__ import annotations

import argparse
from pathlib import Path

from kikai_lab.envelope import emit, envelope, error, next_action
from kikai_lab.operation import OperationError


def add_project_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", required=True)


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


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
