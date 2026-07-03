# Toy trainer

A small, dependency-free trainer that satisfies the full
[trainer contract](../../docs/TRAINER_CONTRACT.md): per-step `metrics.jsonl`,
step/loss-tagged checkpoints, terminal events, and the control plane.

## Run it standalone

```bash
python examples/toy_trainer/train.py --run-dir /tmp/toy --max-steps 5000
ls /tmp/toy/checkpoints/          # checkpoint_step_*_loss*.pt
tail -1 /tmp/toy/metrics.jsonl    # {"event": "done", ...}
```

## Watch the control plane live

In one shell, start a slow run:

```bash
python examples/toy_trainer/train.py --run-dir /tmp/toy --max-steps 100000 --step-delay 0.02
```

In another, change its policy without restarting — write the file the server
would write:

```bash
echo '{"max_steps": 2000}' > /tmp/toy/control.json     # cut it short cleanly
echo '{"stop": "graceful"}' > /tmp/toy/control.json     # or stop now, checkpointed
grep control_applied /tmp/toy/metrics.jsonl             # the trainer's acknowledgement
```

Through a running kikai server the same thing is
`POST /v1/projects/{p}/runs/{run}/control` — the server writes `control.json`
for you and `GET .../control` reads the acknowledgement back.

## Use it as a bundle entrypoint

Point a bundle's `train` entrypoint at `scripts/train.py` (the tar layout in the
[quickstart](../../README.md#quickstart)) to exercise submit → gates → QC →
retention → finalize end to end against a real registry.
