from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kikai_lab.operation import OperationError
from kikai_lab.validation import load_yaml


def tensorboard_policy(*records: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    for record in records:
        observability = record.get("observability")
        if isinstance(observability, dict):
            tensorboard = observability.get("tensorboard")
            if isinstance(tensorboard, dict):
                policy.update(tensorboard)
        tensorboard = record.get("tensorboard")
        if isinstance(tensorboard, dict):
            policy.update(tensorboard)
    return policy


def first_tensorboard_container(project_root: Path, run_name: str) -> str | None:
    containers_dir = project_root / "containers"
    if not containers_dir.exists():
        return None
    for path in sorted(containers_dir.glob("*.yaml")):
        container = load_yaml(path)
        if container.get("kind") != "docker_container":
            continue
        if container.get("role") != "tensorboard":
            continue
        container_id = container.get("container_id")
        if not isinstance(container_id, str) or not container_id:
            continue
        related = container.get("related_runs") or []
        if isinstance(related, list) and run_name in {str(item) for item in related}:
            return container_id
    return None


def current_tensorboard_plan(
    project_root: Path, *, port_override: int | None = None, run_name_override: str | None = None
) -> dict[str, Any]:
    current_path = project_root / "current.json"
    if not current_path.exists():
        raise OperationError(
            "tensorboard.current_missing",
            "current.json is required to plan current TensorBoard service",
            {"project_root": str(project_root)},
        )
    current = json.loads(current_path.read_text(encoding="utf-8"))
    run_name = str(run_name_override or current.get("current_run_name") or "")
    if not run_name:
        raise OperationError("tensorboard.run_missing", "current_run_name is required")
    run = load_yaml(project_root / "runs" / f"{run_name}.yaml")
    experiment_id = str(run.get("experiment_id") or current.get("current_experiment_id") or "")
    experiment = load_yaml(project_root / "experiments" / f"{experiment_id}.yaml")
    policy = tensorboard_policy(current, experiment, run)
    required = bool(policy.get("required"))
    outputs_obj = run.get("outputs")
    outputs = outputs_obj if isinstance(outputs_obj, dict) else {}
    logdir = policy.get("logdir") or outputs.get("tensorboard")
    if required and not logdir:
        raise OperationError(
            "tensorboard.logdir_missing",
            "TensorBoard is required but no tensorboard logdir is configured",
            {"run_name": run_name},
        )
    container_id = policy.get("container_id") or first_tensorboard_container(project_root, run_name)
    if required and not container_id:
        raise OperationError(
            "tensorboard.container_missing",
            "TensorBoard is required but no tensorboard container is configured",
            {"run_name": run_name},
        )
    port = port_override or policy.get("port")
    if required and (not isinstance(port, int) or port <= 0):
        raise OperationError(
            "tensorboard.port_missing",
            "TensorBoard is required but no positive port is configured",
            {"run_name": run_name, "port": port},
        )
    operation = None
    if required:
        operation = {
            "kind": "kikai_operation",
            "request": {
                "adapter": "tensorboard_service",
                "operation": f"{run_name}_tensorboard_ensure_running",
                "project_root": str(project_root),
                "target_id": container_id,
                "container_id": container_id,
                "action": "ensure-running",
                "port": port,
                "logdir": logdir,
            },
            "schema_version": 1,
        }
    return {
        "required": required,
        "project_root": str(project_root),
        "experiment_id": experiment_id,
        "run_name": run_name,
        "container_id": container_id,
        "port": port,
        "logdir": logdir,
        "operation": operation,
    }


def write_tensorboard_operation(path: Path, operation: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(operation, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
