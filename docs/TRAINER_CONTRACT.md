# Trainer contract

kikai does not run your training loop — it launches your container and then
**reads two things your trainer writes** into the run directory. Meet this
contract and every kikai feature (live metrics, gates, QC, retention,
early-stop detection, the control plane) works with your existing trainer,
whatever framework it uses.

Nothing here is kikai-specific machinery you must import. It is a file format.

## 1. `metrics.jsonl` — one JSON object per line

Write append-only to `<run_dir>/metrics.jsonl`. Two event types matter.

### Per-step metrics

```json
{"event": "train_metrics", "step": 1200, "loss": 4.53, "lr": 0.0002, "sharpness": 0.83}
```

- `event` MUST be `"train_metrics"`; `step` MUST be an integer.
- Every other numeric field becomes a queryable metric series (`GET
  .../metrics?keys=loss`). Emit whatever you want to gate on.
- Write on an interval (e.g. every 100 steps), flushing each line.

### Terminal events

Write exactly one when the run ends, so kikai knows it finished (vs crashed):

```json
{"event": "done", "step": 60000}
{"event": "early_stop", "step": 41000}
{"event": "stopped_by_control", "step": 22000}
```

`done` = ran to completion, `early_stop` = your own early-stopping fired,
`stopped_by_control` = you honored a control-plane graceful stop (see §3). Any
of the three finalizes the run; the absence of all three on an exited container
is treated as a crash (`failed`).

## 2. Checkpoint filenames — carry the step (and optionally the loss)

Write checkpoints into `<run_dir>/checkpoints/`. The filename is the contract;
kikai never opens the weights.

```
checkpoint_step_012000_loss4p5312.pt     # periodic — retention keep_latest / keep_milestones
best_step_009000_loss3p8100.pt           # your best-so-far — retention keep_best
best_checkpoint.pt                        # optional stable pointer to the newest best
```

- Step token: `step` optionally joined by `_`/`-`, then digits
  (`_step_012000`). This is what retention windows, probes, and QC key on.
- Loss tag (optional): `_loss<value>` with `.`→`p` and `-`→`m`
  (`4.5312` → `loss4p5312`). Lets retention rank by loss without reading
  metrics; omitted → resolved from `metrics.jsonl` or treated as unknown.
- Two independent families: `checkpoint_step_*` (periodic) and `best_step_*`
  (curated). Retention keeps rolling windows of each plus any milestones; the
  trainer only WRITES — it must not prune (that fights best-protection).

## 3. Control plane (optional) — live policy changes without a restart

To let operators change a running run's termination policy (raise the step cap,
retune early-stop, request a graceful stop) via `POST /runs/{run}/control`,
poll `<run_dir>/control.json` on your metrics interval:

- Stat the file; only re-read when the mtime changed (near-zero steady cost).
- Apply whitelisted keys to your loop state: `max_steps`,
  `early_stop_patience`, `early_stop_min_delta`, `stop: "graceful"`.
- On `stop: "graceful"`: finish the current step, write a checkpoint, emit the
  `stopped_by_control` terminal event, exit 0.
- Log what you applied so operators can confirm it landed
  (`GET /runs/{run}/control` reads this back):

  ```json
  {"event": "control_applied", "step": 22300, "applied": {"max_steps": 120000}, "ignored": []}
  ```

- A malformed/partial control file must never crash training — report once and
  ignore.

Trainers that skip §3 still get everything in §1–§2; `GET .../control` simply
reports `applied: null`.

## Minimal reference

`examples/toy_trainer/train.py` is a small dependency-free trainer that
implements all three sections. Run it directly to see the files it produces, or
use it as the entrypoint of a bundle to exercise kikai end-to-end. See
[examples/toy_trainer/README.md](../examples/toy_trainer/README.md).
