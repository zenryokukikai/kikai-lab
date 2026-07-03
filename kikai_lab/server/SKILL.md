# kikai server — agent skill

You are talking to a **kikai server**: one HTTP endpoint that manages ML experiment
projects, launches training, and serves results. You never touch docker, ssh, or host
paths — you register pieces by id and delegate management to kikai.

Discover the server: `GET /healthz` (version, host_id, reconciler state). Full API
schema: `GET /openapi.json`. This document is served by the same process at
`GET /v1/skill.md`, so it cannot drift from the server you are calling.

## How to read every response

Every JSON body is the same envelope:

```json
{"ok": true, "schema_version": 1, "data": {...},
 "warnings": [...], "errors": [{"code": "...", "message": "...", "details": {...}}],
 "next_actions": [{"id": "...", "reason": "...", "command": "GET /v1/..."}]}
```

- Act on `ok` and `errors[].code` (stable, machine-readable), not on prose.
- `next_actions[].command` tells you the most useful next request — follow it.

## Rules that save you tokens (and mistakes)

1. **You pick every id** (project, experiment, run, bundle...). Ids match
   `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`.
2. **PUT is idempotent.** Re-sending the same content returns `already_exists: true` —
   that is SUCCESS, not an error. Retry freely.
3. **409 means your content diverged** from an immutable record. Read
   `details.diff_keys` / `details.diff`, then use a NEW id (bundles, runs, data
   sources) or a superseding record (decisions). Never try to overwrite.
4. **Ask for less**: `?fields=a,b`, `?limit=`, and on metrics `?keys=loss&max_points=200`.
   Metrics come back columnar (`{"step": [...], "series": {"loss": [...]}}`).
5. Registration is **fail-closed**: a typo in a field name (422), an unregistered
   reference (404), or a tampered input (422 on verify) dies at registration time with
   a precise code — fix and retry, nothing was launched.

## Golden path: from nothing to a managed training run

```bash
BASE=http://SERVER:8300/v1
# 1. project (idempotent; scaffolds the registry)
curl -X PUT $BASE/projects/example_proj -H 'content-type: application/json' \
  -d '{"summary": "example project"}'
# 2. experiment
curl -X PUT $BASE/projects/example_proj/experiments/example_exp \
  -H 'content-type: application/json' -d '{"title": "example hypothesis"}'
# 3. data source (server computes the sha256; path is SERVER-local)
curl -X PUT $BASE/projects/example_proj/data-sources/example_manifest \
  -H 'content-type: application/json' -d '{"kind": "file",
  "source_type": "dataset_manifest", "path": "/data/example/manifest.jsonl",
  "host_ref": "local", "roles": ["train_manifest"], "summary": "example manifest"}'
curl -X POST $BASE/projects/example_proj/data-sources/example_manifest/verify
# 4. container profile (docker.name/image/mounts; no live-worktree mounts allowed)
curl -X PUT $BASE/projects/example_proj/containers/example_training \
  -H 'content-type: application/json' -d '{"docker": {"name": "example-training",
  "image": "example-image:latest"}, "mounts": [{"source": "env:HOST_RUNS_ROOT",
  "target": "env:CONTAINER_RUNS_ROOT", "mode": "rw"}]}'
# 5. bundle: tar your scripts + a kikai_bundle.json manifest at tar root:
#    {"entrypoints": {"train": {"argv": ["python", "scripts/train.py"]}}}
curl -X PUT $BASE/projects/example_proj/bundles/example_trainer_v1 \
  -H 'content-type: application/x-tar' --data-binary @bundle.tar
# 6. dry-run the submission (validates everything, launches nothing)
curl -X POST $BASE/projects/example_proj/runs/example_run_001/submit \
  -H 'content-type: application/json' -d '{"experiment_id": "example_exp",
  "container_id": "example_training", "bundle_id": "example_trainer_v1",
  "entrypoint": "train", "args": ["--max-steps", "40000"],
  "data_source_refs": [{"role": "train_manifest", "data_source_id": "example_manifest"}],
  "run_dir": "${HOST_RUNS_ROOT}/example_run_001/run",
  "managed": {"max_step": 40000, "retention": {"keep_latest": 3, "keep_best": 2}},
  "dry_run": true}'
# 7. same body without dry_run -> launches; kikai manages QC/retention/finalize
# 8. poll (see below), fetch metrics, artifacts
```

Retention keeps two rolling windows (`keep_latest` periodic + `keep_best` curated).
If you may later warm-start probes from points ALONG the trajectory, also declare
milestone anchors — they survive outside the rolling windows:
`"retention": {"keep_latest": 3, "keep_best": 2,
"keep_milestones": [{"every_steps": 1000, "until_step": 10000}]}`.

## Monitoring a run

- `GET .../runs/{run}/status` — the tiny polling payload. `derived_status` vocabulary:

| derived_status | meaning | what you do |
|---|---|---|
| `submitted` | recorded/created, container not running yet | keep polling |
| `running` | training container is up | poll status/metrics |
| `exited_pending_finalize` | container exited; daemon will finalize | keep polling |
| `completed` | finished (`done` in metrics) + finalized | fetch artifacts |
| `early_stopped` | early-stop fired + finalized | fetch artifacts |
| `stopped` | control-plane graceful stop (checkpointed) + finalized | fetch artifacts |
| `failed` | crashed (non-zero exit / no terminal event) | read `/logs`, fix, resubmit under a NEW run_name |
| `submitting` | launch in flight, or interrupted mid-launch | resubmit the SAME body — the server adopts or relaunches |
| `submit_failed` | launch failed before the container started | fix the cause, resubmit the SAME run_name (retry is safe) |
| `unknown` | inconclusive evidence | poll again; check `/logs` |

- **Long-poll instead of polling**:
  `GET .../runs/{run}/status?wait=state_change&timeout=300` holds the request until
  `derived_status` changes (or timeout), returning the fresh status plus
  `changed` / `baseline` / `waited_sec`. Pass `from=running` to give an explicit
  baseline (a change that happened BEFORE your call still returns immediately;
  a value outside the derived-status vocabulary is 422). Sampling is 5s-granular:
  a status that flips A->B->A within one window reads as unchanged — re-derive on
  wake, don't trust `changed` as an event log. One held request replaces N polls.
- `GET .../runs/{run}/events?since_seq=N` — QC deliveries / terminal / finalized as
  monotonically-numbered events; resume from the returned `last_seq` (events may
  repeat until finalize — treat them idempotently).
- `GET .../runs/{run}/metrics?keys=loss&max_points=200` — columnar + downsampled; the
  exact latest row always rides along as `last_row`. Discover series via
  `available_keys`.
- `GET .../runs/{run}/logs?tail=200` — container log tail.
- Artifacts: `GET .../artifacts?run_name=...`, then
  `GET .../artifacts/{id}/content` for bytes (videos support Range).

## Iterating: submit-from (differential submission)

Almost every next run is "the previous run with one variable changed". Do NOT rebuild
the whole submit body — inherit it:

```bash
curl -X POST $BASE/projects/example_proj/runs/example_run_002/submit-from/example_run_001 \
  -H 'content-type: application/json' \
  -d '{"overrides": {"args_set": {"--vgg-weight": "5.0", "--max-steps": null}}}'
```

- The parent's full submission (bundle, container, args, env, run_dir, managed config
  incl. retention / qc / evaluations / metric_checks) is reconstructed from the
  registry, every occurrence of the parent run name (run_dir, qc paths, ...) is
  rebound to the child name, then your `overrides` apply.
- **If the parent's run_dir does not contain its run name** (a different naming
  scheme, e.g. run `example_run` with run_dir `.../example_renderer/run`), the
  rebind cannot relocate it and the call is REFUSED (`run.run_dir_not_relocated`)
  — writing into the parent's dir would corrupt it. Override `run_dir` and the
  trainer's run-dir arg to a fresh path.
- `overrides.args_set`: upserts flag values; `null` REMOVES a flag and all its
  values; `""` strips values to a bare flag; a JSON list sets a multi-value (nargs)
  flag. Repeated flags: only the first occurrence is touched; values starting
  with `--` can't be expressed, and single-dash tokens (`-v`) after a flag are
  consumed as its values — override `args` wholesale for those.
  `overrides.managed` merges one level deep; any other key (`args`, `env`,
  `run_dir`, ...) replaces the parent value outright.
- Rebinding is boundary-safe (`run_1` never rewrites `run_10`); if the inherited
  body references another run REGISTERED IN THIS PROJECT whose name extends the
  parent's (`example_run` vs `example_run_v2`), the call fails 422 — override that
  field explicitly. Unregistered names extending the parent's are still rewritten.
- `{"dry_run": true}` previews the merged body without launching.
- Lineage is recorded (`submission.parent_run` + your overrides) and shown in the
  run record, so "what changed vs parent" is always answerable.

## Resuming a session: brief + journal (one call each)

Start of session, or after any gap — do NOT re-list runs and poll them one by one:

- `GET $BASE/projects/example_proj/brief` — the decision digest: every run with its
  status / verdict / lifecycle / gate verdicts (`check_verdicts`) / QC progress, an
  `attention` list (finalized runs missing a conclusion, `submit_failed`, failed
  metric checks), and the 10 most recent journal events.
- `GET $BASE/projects/example_proj/journal?since=2026-07-02T00:00:00Z&limit=50` —
  the chronological event log (submits with lineage, failed submits, stops,
  conclusions, gate failures, finalizations). `since` is at-least-once
  (`at >= since` included) — dedupe on `(at, kind, run_name)` when resuming.

Work through `attention` first; it is the unfinished business.

## Probing before committing (offline probe)

A new idea (loss family, hyperparameter regime) does NOT deserve a fresh full run
until a PROBE has shown it moves the needle. A probe warm-starts from the parent's
checkpoint, runs a few thousand steps, and answers ONE question:

```bash
curl -X POST $BASE/projects/example_proj/runs/example_probe_001/probe-from/example_run_001 \
  -H 'content-type: application/json' -d '{
  "question": "does the higher learning rate improve sharpness at all?",
  "probe_steps": 5000,
  "checkpoint": "best",
  "overrides": {"args_set": {"--adv-weight": "0.05"},
    "managed": {"metric_checks": [{"check_id": "sharpness_moves", "key": "sharpness",
      "expect": "decreasing", "window_steps": [500, 4500],
      "window_steps_relative": true}]}}}'
```

- The server resolves the parent's `best` / `latest` / integer-step checkpoint,
  injects `--resume-checkpoint` (container path INTO the parent's run_dir) and
  `--max-steps` = resume_step + probe_steps, and defaults retention to 1+1.
  Non-default trainer flags: pass `resume_arg` / `max_steps_arg` / `run_dir_arg`.
- `question` is REQUIRED — a probe without a question is just a short run.
- The server's resume/max-steps injection wins over your `overrides` — the probe
  owns its budget. Budget math assumes the trainer restores its ABSOLUTE step
  counter from the checkpoint; a trainer that resets to 0 on resume will run
  resume_step + probe_steps steps.
- Declare gates with `window_steps_relative: true`: offsets from the probe's first
  metrics step, so "did it move within the budget" works wherever the parent stopped.
- The run record carries `probe` (question, budget, resume point); `brief` shows the
  question. Reproducibility rules for adopted results still demand a FRESH run of
  the winning config afterwards — the probe only decides whether to pay for one.

## Comparing runs (what actually changed?)

`GET $BASE/projects/example_proj/compare?runs=example_run_001,example_run_002` — the
comparison table you would otherwise build by hand: per-flag `args` diff, `env` /
`submission` / `managed` key diffs (each run's own name is normalized to `{run}`
first, so run_dir-style cosmetic differences don't show up), plus per-run
status / verdict / parent_run / latest step+loss. Identical values are omitted —
the response IS the "what changed" answer for your conclusion.

## Recording your conclusion (close the loop)

When you have judged a run, write the analysis WHERE THE RUN LIVES — not in chat:

```bash
curl -X POST $BASE/projects/example_proj/runs/example_run_001/conclusion \
  -H 'content-type: application/json' -d '{"verdict": "rejected",
  "summary": "texture terms flattened after step 6000; VGG still dominated the pull",
  "evidence": ["metric_check highpass_must_decline: fail", "teeth diag step 12000"],
  "next_run": "example_run_002"}'
```

`verdict`: adopted | rejected | superseded | inconclusive. Conclusions are
append-only (later entries supersede, never erase) and render on the dashboard's run
page; the latest verdict badges the runs list. A run without a conclusion is unfinished
business.

## Live control plane (change a running run's termination policy)

Restarting a healthy training just to raise its step cap wastes the warmup — and
"looks good, keep going" should be a policy change, not a resubmit:

```bash
curl -X POST $BASE/projects/example_proj/runs/example_run_001/control \
  -H 'content-type: application/json' \
  -d '{"max_steps": 120000, "early_stop_patience": 15, "early_stop_min_delta": 0.0005}'
```

- Whitelist: `max_steps` / `early_stop_patience` (positive int),
  `early_stop_min_delta` (>= 0), `stop: "graceful"` (checkpoint + clean exit —
  the SAFE stop; `POST .../stop` remains the hard kill). Unknown keys are 422,
  never silently dropped.
- Each POST REPLACES the whole control file (no merge): include every knob you
  want in force, not just the one you are changing.
- The server writes `<run_dir>/control.json`; a control-plane-aware trainer
  applies it at its next metrics boundary and logs a `control_applied` event.
  `GET .../control` shows requested vs applied — applied stays null for trainers
  that predate the control plane.
- `max_steps` also syncs `managed_run.max_step`, so daemon QC/lifecycle follow
  the new cap. Gate/evaluation/retention declarations in `managed_runs/<run>.yaml`
  were ALWAYS live-editable (the daemon re-reads them every tick).
- A graceful stop finalizes with derived status `stopped` (terminal event
  `stopped_by_control`).

## Stopping / resuming

- `POST .../runs/{run}/stop` — idempotent; the reconciler finalizes (retention +
  notification) on its next tick.
- Resume = a NEW submission (new `run_name`) that passes the checkpoint to YOUR
  trainer via `args` (e.g. `["--resume-checkpoint", "/path/in/container.pt"]`) —
  kikai does not inject it. Register the checkpoint as a data source first if you
  want its integrity verified at submit time. `resume.fresh_no_resume` is a registry
  annotation for validation, not a launch parameter.

## Error codes you will actually see

| code (tail) | HTTP | meaning → action |
|---|---|---|
| `*_not_found` / `*_missing` | 404 | reference typo or not registered → register it first |
| `*_exists` / `run.exists` | 409 | immutable record diverged → new id / read diff |
| `project.archived` | 409 | unarchive first (`POST .../unarchive`) |
| `operation.host_not_local` | 409 | this server only launches locally in v1 |
| `*_in_use` | 409 | container name already held — `details.next` points at the stop call |
| `*_invalid` (incl. schema 422s) | 422 | your body — `details.validation_errors` lists paths |
| `*_unverified` | 422 | input failed integrity re-check → re-register or investigate |
| `artifact.content_root_forbidden` | 403 | server has no `--content-root` for that file |
| `request.params_invalid` | 422 | query param typo |
| `route.not_found` | 404 | URL typo — check `/openapi.json` |

## Escape hatch

Anything without a typed endpoint: `POST .../operations` executes one operation
request (`{"adapter": "...", "operation": "...", ...}`) against this project —
`project_root` is pinned server-side. `?dry_run=1` first. Adapters and their fields:
`GET /openapi.json` won't list them; read the kikai-lab README's adapter table.

## Humans

Everything you register renders live in the dashboard at `GET /` — humans watch the
same project you are building. Keep summaries human-readable.
