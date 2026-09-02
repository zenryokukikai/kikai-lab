from __future__ import annotations

import argparse
from pathlib import Path

from kikai_lab.cli_commands.common import (
    add_json_flag,
    add_project_root,
    project_root_missing,
)
from kikai_lab.envelope import emit, envelope, error, next_action
from kikai_lab.store import CurrentState, compute_current_state, load_current
from kikai_lab.validation import load_yaml, validate_registry_links


def configure_project_state_parser(parser: argparse.ArgumentParser) -> None:
    add_project_root(parser)
    add_json_flag(parser)


def configure_show_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("object_type", choices=["experiment", "run", "container"])
    parser.add_argument("object_id")
    add_project_root(parser)
    add_json_flag(parser)


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
