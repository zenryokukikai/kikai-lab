from __future__ import annotations

from pathlib import Path

import pytest

from kikai_lab.operation import OperationError
from kikai_lab.tensorboard import current_tensorboard_plan


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_current_tensorboard_plan_requires_run_related_or_policy_container(tmp_path):
    write_text(
        tmp_path / "current.json",
        '{"current_run_name": "demo", "current_experiment_id": "exp"}\n',
    )
    write_text(
        tmp_path / "runs" / "demo.yaml",
        "experiment_id: exp\noutputs:\n  tensorboard: /runs/demo/tensorboard\n",
    )
    write_text(
        tmp_path / "experiments" / "exp.yaml",
        "tensorboard:\n  required: true\n  port: 6006\n",
    )
    write_text(
        tmp_path / "containers" / "unrelated_tensorboard.yaml",
        (
            "kind: docker_container\n"
            "role: tensorboard\n"
            "container_id: tb-unrelated\n"
            "related_runs:\n"
            "  - other-run\n"
        ),
    )

    with pytest.raises(OperationError) as exc_info:
        current_tensorboard_plan(tmp_path)

    assert exc_info.value.code == "tensorboard.container_missing"
