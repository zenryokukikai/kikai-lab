"""The shipped toy trainer must satisfy the contract kikai reads against —
if this breaks, the onboarding example lies about how to integrate."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kikai_lab.operation import checkpoint_loss_from_name, checkpoint_step_from_name
from kikai_lab.reconcile import checkpoint_steps, read_terminal_event
from kikai_lab.server.metrics import read_last_train_metrics

TOY = Path(__file__).resolve().parent.parent / "examples" / "toy_trainer" / "train.py"


def run_toy(run_dir: Path, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, str(TOY), "--run-dir", str(run_dir), *args],
        check=False, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_toy_trainer_emits_contract_files(tmp_path: Path) -> None:
    run_toy(tmp_path, "--max-steps", "3000", "--checkpoint-interval", "1000")

    # terminal event kikai recognizes
    assert read_terminal_event(tmp_path / "metrics.jsonl") == "done"
    # last train row parses
    last = read_last_train_metrics(tmp_path / "metrics.jsonl")
    assert last["step"] == 3000 and isinstance(last["loss"], float)
    # checkpoints carry step + loss the retention adapter can read
    steps = checkpoint_steps(tmp_path)
    assert [s for s, _ in steps] == [1000, 2000, 3000]
    name = steps[-1][1]
    assert checkpoint_step_from_name(name) == 3000
    assert checkpoint_loss_from_name(name) is not None


def test_toy_trainer_honors_graceful_stop(tmp_path: Path) -> None:
    (tmp_path).mkdir(exist_ok=True)
    # pre-place a graceful stop; the trainer applies it on its first metrics tick
    (tmp_path / "control.json").write_text('{"stop": "graceful"}', encoding="utf-8")
    run_toy(tmp_path, "--max-steps", "100000", "--metrics-interval", "1")

    assert read_terminal_event(tmp_path / "metrics.jsonl") == "stopped_by_control"
    events = [
        json.loads(line)
        for line in (tmp_path / "metrics.jsonl").read_text().splitlines()
    ]
    applied = [e for e in events if e.get("event") == "control_applied"]
    assert applied and applied[-1]["applied"]["stop"] == "graceful"
    # it stopped early, not at 100000
    assert events[-1]["step"] < 1000
