#!/usr/bin/env python3
"""A small, dependency-free trainer that satisfies the kikai trainer contract.

It trains nothing real — it descends a toy loss — but it writes metrics.jsonl,
step/loss-tagged checkpoints, terminal events, and honors the control plane
exactly as docs/TRAINER_CONTRACT.md describes. Use it to exercise kikai
end-to-end or as a template for wiring your own loop.

    python examples/toy_trainer/train.py --run-dir /tmp/toy --max-steps 5000
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path


def loss_tag(loss: float) -> str:
    return "loss" + f"{loss:.4f}".replace("-", "m").replace(".", "p")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--max-steps", type=int, default=5000)
    ap.add_argument("--metrics-interval", type=int, default=100)
    ap.add_argument("--checkpoint-interval", type=int, default=1000)
    ap.add_argument("--early-stop-patience", type=int, default=20)
    ap.add_argument("--early-stop-min-delta", type=float, default=1e-4)
    ap.add_argument("--step-delay", type=float, default=0.0, help="seconds/step, for demos")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    metrics = (run_dir / "metrics.jsonl").open("a", encoding="utf-8")
    control_path = run_dir / "control.json"

    def emit(row: dict) -> None:
        metrics.write(json.dumps(row, sort_keys=True) + "\n")
        metrics.flush()

    # mutable termination policy (args are only the initial values)
    max_steps = int(args.max_steps)
    es_patience = int(args.early_stop_patience)
    es_min_delta = float(args.early_stop_min_delta)
    control_mtime: float | None = None

    best = math.inf
    no_improve = 0
    step = 0
    stop_requested = False

    while step < max_steps and not stop_requested:
        step += 1
        loss = 10.0 * math.exp(-step / 1500.0) + 0.5  # toy descent to a floor
        if args.step_delay:
            time.sleep(args.step_delay)

        if step == 1 or step % args.metrics_interval == 0:
            # --- control plane: mtime-gated poll, whitelist apply ---
            try:
                mtime = control_path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if mtime is not None and mtime != control_mtime:
                control_mtime = mtime
                try:
                    ctl = json.loads(control_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    ctl = None
                if isinstance(ctl, dict):
                    applied = {}
                    ms = ctl.get("max_steps")
                    if isinstance(ms, int) and not isinstance(ms, bool) and ms > 0:
                        max_steps = ms
                        applied["max_steps"] = ms
                    pat = ctl.get("early_stop_patience")
                    if isinstance(pat, int) and not isinstance(pat, bool) and pat > 0:
                        es_patience = pat
                        applied["early_stop_patience"] = pat
                    md = ctl.get("early_stop_min_delta")
                    if isinstance(md, (int, float)) and not isinstance(md, bool):
                        es_min_delta = float(md)
                        applied["early_stop_min_delta"] = es_min_delta
                    if ctl.get("stop") == "graceful":
                        stop_requested = True
                        applied["stop"] = "graceful"
                    emit({
                        "event": "control_applied", "step": step,
                        "applied": applied,
                        "ignored": sorted(set(ctl) - set(applied)),
                    })

            emit({"event": "train_metrics", "step": step, "loss": round(loss, 6)})

            if loss < best - es_min_delta:
                best, no_improve = loss, 0
            else:
                no_improve += 1

        if step % args.checkpoint_interval == 0:
            name = f"checkpoint_step_{step:06d}_{loss_tag(loss)}.pt"
            (run_dir / "checkpoints" / name).write_text("weights")

        if no_improve >= es_patience:
            emit({"event": "early_stop", "step": step})
            return 0

    final_name = f"checkpoint_step_{step:06d}_{loss_tag(loss)}.pt"
    (run_dir / "checkpoints" / final_name).write_text("weights")
    emit({"event": "stopped_by_control" if stop_requested else "done", "step": step})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
