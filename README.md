# Kikai Lab

**An ML experiment registry and control server built for AI agents as first-class
operators.** Most experiment trackers are dashboards a human reads. kikai is an
HTTP control plane an agent *drives* — it launches training, watches it, judges
it, and iterates, over a plain-file registry you can read with `cat` and diff in
git.

```
agent ──HTTP──> kikai server ──> Docker training containers
                    │                    │ writes
                    ▼                    ▼
          file registry (YAML/JSONL)   run_dir/{metrics.jsonl, checkpoints/, control.json}
                    ▲                    │ reads
                    └──── reconciler ────┘  (QC · gates · retention · finalize)
```

The agent never touches Docker or SSH. It registers pieces by id and delegates
management to kikai; the server ships its own agent guide at `GET /v1/skill.md`,
so the instructions can never drift from the running version.

> Status: **beta.** The HTTP API and trainer contract are stabilizing; expect
> occasional breaking changes before 1.0. AGPL-3.0-or-later.

## Why it's different

- **Agent-ergonomic API.** Every response is one envelope
  (`{ok, data, errors[], next_actions[]}`) with stable machine-readable error
  codes and a suggested next call — built to be consumed by a model, not scraped
  from HTML.
- **Differential submission.** `submit-from/{parent}` inherits a parent run's
  whole config and records lineage, so "the last run with one variable changed"
  is one call, not a 60-line body.
- **Offline probes.** `probe-from/{parent}` warm-starts from a checkpoint to
  answer one question cheaply before committing to a full run; `keep_milestones`
  retention preserves the entry points probes need.
- **Live control plane.** Change a *running* run's termination policy —
  `max_steps`, early-stopping, or a graceful checkpointed stop — with no restart.
- **Declarative gates + self-driving ops.** Metric checks, QC renders,
  checkpoint retention, and finalize/alerting run from the run's own
  declarations; results and human conclusions live *with the run*.
- **One-call session resume.** `brief` and `journal` hand a fresh agent the whole
  decision context, so there is no external hand-off doc to keep in sync.

The framework is trainer-agnostic: it reads two files your training loop writes
(`metrics.jsonl` and step-tagged checkpoints). See
[docs/TRAINER_CONTRACT.md](docs/TRAINER_CONTRACT.md).

## Install

```bash
pip install kikai-lab          # or: uv add kikai-lab
```

Python 3.11+. Requires Docker on the host for the training lifecycle; the
registry/CLI parts work without it.

## Quickstart

Run the dependency-free [toy trainer](examples/toy_trainer/) under a kikai
server, end to end:

```bash
# 1. start the server over a fresh registry (localhost-only by default)
mkdir -p /tmp/kikai-demo
kikai server start --projects-root /tmp/kikai-demo &

# 2. an agent's view of how to drive it
curl -s localhost:8300/v1/skill.md

# 3. register a project and inspect it in the dashboard
curl -X PUT localhost:8300/v1/projects/demo \
  -H 'content-type: application/json' -d '{"summary": "demo project"}'
open http://localhost:8300/          # project → experiment → run, metrics, artifacts
```

The full golden path — experiment, container profile, bundle, submit with gates,
QC, retention, finalize — is in `GET /v1/skill.md` and exercised end to end by
the toy trainer's [README](examples/toy_trainer/README.md).

## Security

kikai launches containers on its host: **reaching the API means running code on
the host.** It binds `127.0.0.1` by default; exposing it (`--host 0.0.0.0`) is
opt-in, with an optional shared bearer token (`--auth-token` / `KIKAI_AUTH_TOKEN`)
and filesystem containment flags. Read [SECURITY.md](SECURITY.md) before exposing
it anywhere.

## Documentation

- [docs/TRAINER_CONTRACT.md](docs/TRAINER_CONTRACT.md) — the two files your
  trainer writes; the whole integration surface.
- `GET /v1/skill.md` — the agent operator guide, served by the running server.
- [SECURITY.md](SECURITY.md) — deployment security model.
- [CHANGELOG.md](CHANGELOG.md) — release history.
- Reference sections below cover the CLI, registry layout, operation-JSON safety
  model, reconciler, decisions, and report/dashboard in depth.

---

## Reference

`examples/` ships the dependency-free [toy trainer](examples/toy_trainer/) (a full trainer-contract reference) plus a couple of standalone operation JSON samples. Real adopter state should live under an adopter-owned registry root such as `<adopter-repo>/.kikai/`; the `path/to/project` in the walkthroughs below is a placeholder for that root.

Docker container definitions live under `containers/*.yaml`. These records define the canonical desired container names, images, roles, mounts, GPU expectation, and healthcheck hints so agents do not guess which training, watcher, or TensorBoard container to use. Docker lifecycle actions are side-effect operations driven by one operation JSON (see below).

## Remote command safety

Kikai Lab side-effect commands must not accept long hand-composed argument lists. Each side-effect operation is driven by exactly one positional operation JSON file. All execution parameters live inside that JSON file, including project root, target ID, adapter parameters, environment references, and the guard receipt.

Allowed shape for side-effect operations:

```bash
kikai target dry-run ops/example_run_qc.json
kikai target run ops/example_run_qc.json
kikai exec ops/example_run_qc.json
```

An operation file may be authored as **JSON, YAML, or TOML** — the format is chosen by extension (`.json` / `.yaml` / `.yml` / `.toml`) and loads the same operation object. Containers and experiments are already YAML, so operations no longer have to be hand-escaped JSON. The guard-receipt digest is computed on the request object, so it is **format-agnostic** (a YAML operation and the equivalent JSON operation share a digest), and `dry-run` writes the receipt back in the file's own format. Example `ops/tail_train.yaml`:

```yaml
kind: kikai_operation
schema_version: 1
request:
  adapter: remote_docker_logs
  operation: tail_train
  ssh_host: env:KIKAI_REMOTE_SSH_HOST
  container_name: run-train
  tail: 50
```

### Generation templates

A recurring generation task (rendering a preview, launching a QC pass) has a known-good command
shape — which entrypoint, which caches, which mask source. Re-deriving that shape by hand each time
invites silent mistakes. A **template** captures the recipe once as a reviewable artifact and renders
it to a normal operation:

```bash
kikai template list --project-root .
kikai template render templates/fullframe_preview.yaml \
  --set operation_id=preview_001 --set source_run_dir=... --set input_dir=... \
  --set out=preview.mp4 --set max_frames=250 --out ops/preview_001.yaml
kikai target dry-run ops/preview_001.yaml   # then `target run`
```

A template is a `kind: kikai_operation_template` file (JSON/YAML/TOML) with a `parameters:` list and a
`request:` carrying `{{name}}` placeholders (see the module docstring of `kikai_lab/template.py` for a complete example).
`render` substitutes declared defaults + `--set` overrides; a missing required parameter, an unknown
`--set` key, or any unresolved placeholder is a hard error, so the emitted operation is always complete
and still flows through the normal guard path (`dry-run` → `run`).

Rules:

- One operation uses one JSON file.
- The operation JSON path is the only positional CLI argument for side-effect commands.
- Side-effect commands reject free-form passthrough args and extra control flags.
- Side-effect commands output JSON by default; no `--json` flag is required for them.
- The operation file contains both `request` and `guard_receipt` sections.
- `target run` / `exec` refuse execution if the same file lacks a valid `guard_receipt` for its current `request` content.
- Adapters execute structured argv arrays from the validated JSON, never shell-joined strings.
- Agents should not hand-compose `docker exec`, heredocs, or `bash -lc` wrappers. Use `adapter: docker_exec` in the operation JSON and run `kikai exec <operation.json>`.
- Past experiment operations should not depend on mutable live script paths. Use immutable script bundles under `script_bundles/<bundle_id>/` and `adapter: script_bundle_exec` when a launch must remain reproducible after scripts/configs are edited later.

For container execution, the operation references a canonical `container_id`; Kikai resolves it through `containers/<container_id>.yaml` and invokes Docker with subprocess argv arrays:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "render_qc",
    "project_root": "path/to/project",
    "adapter": "docker_exec",
    "container_id": "example_run_training",
    "docker_host": "env:KIKAI_DOCKER_HOST",
    "workdir": "env:CONTAINER_EXAMPLE_ENGINE_ROOT",
    "env": {"PYTHONUNBUFFERED": "1"},
    "argv": ["python", "scripts/render_qc.py", "--config", "configs/example_run_qc.yaml"]
  }
}
```

For reproducible experiment launches, snapshot scripts/configs/helpers into a script bundle and execute the bundle entrypoint instead of mutable live paths:

```bash
kikai script-bundle create example_run_train \
  --project-root path/to/project \
  --source-root . \
  --entrypoint train \
  --include-dir scripts/training \
  --include-dir configs/training \
  --argv python \
  --argv scripts/training/launch_example_run.py \
  --argv=--config \
  --argv configs/training/example_run.yaml \
  --json
```

This writes:

```text
script_bundles/example_run_train/
  bundle.json
  root/scripts/training/launch_example_run.py
  root/scripts/training/<helpers...>
  root/configs/training/example_run.yaml
```

`bundle.json` records every snapshot file and SHA-256 hash. Prefer `--include-dir` for scripts/config directories so helper imports and payload files enter the immutable snapshot automatically; `--file` remains available for one-off explicit files. `kikai validate` and `kikai exec` fail closed if any bundle file is missing or modified. Operation JSON uses `script_bundle_exec`:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "example_run_train",
    "project_root": "path/to/project",
    "adapter": "script_bundle_exec",
    "bundle_id": "example_run_train",
    "entrypoint": "train",
    "container_id": "example_run_training",
    "docker_host": "env:KIKAI_DOCKER_HOST",
    "workdir": "env:CONTAINER_EXAMPLE_ENGINE_ROOT",
    "env": {"PYTHONUNBUFFERED": "1"},
    "args": []
  }
}
```

`script_bundle_exec` forbids raw request `argv`; the executable argv must come from the immutable bundle entrypoint, with optional structured `args` appended.

For immutable code mounts, register source trees as Kikai-managed source snapshots instead of mounting mutable live worktrees. A source snapshot copies selected files into `<project-root>/source_snapshots/<source_snapshot_id>/root/`, records SHA-256 hashes in `snapshot.json`, and is validated before container execution:

```bash
kikai source-snapshot create example_project_v1 \
  --project-root path/to/project \
  --source-root /path/to/source/worktree \
  --include-dir scripts \
  --include-dir configs \
  --json
```

Container code mounts must reference the registered snapshot id. At execution time Kikai mounts the snapshot root from the registry, not the mutable `source` env path:

```yaml
mounts:
  - source: env:HOST_LEGACY_SOURCE_ROOT  # retained only for operator context
    target: env:CONTAINER_EXAMPLE_PROJECT_ROOT
    source_kind: kikai_managed_source_snapshot
    source_snapshot_id: example_project_v1
    mode: ro
```

`kikai validate` and `kikai exec` fail closed when the referenced source snapshot is missing, was not generated by `kikai source-snapshot create`, has missing files, or has hash mismatches.

### Data source registry for non-code inputs

Non-code inputs belong in first-class data source records under `data_sources/<data_source_id>.yaml`. Use data sources for datasets, manifests, caches, media, checkpoints, metrics logs, model artifacts, and external datasets; keep source code in `source_snapshots/`, executable scripts/configs in `script_bundles/`, and produced outputs in artifact records until they are explicitly promoted as inputs. Runs and launch-like operation JSON declare those inputs with `data_source_refs` using canonical roles such as `train_manifest`, `face_cache`, `source_audio`, `initial_checkpoint`, and `metrics_log`. Kikai validates status, storage shape, immutability, canonical role compatibility, lineage cycles, and launch-time integrity where applicable.

#### Register an immutable file data source

Register file-like immutable data sources through Kikai so the sha256 is calculated by Kikai Lab, not supplied by the caller. There is intentionally no `--sha256` input flag.

```bash
kikai data-source create-file example_manifest_v1 \
  --project-root path/to/project \
  --source-type dataset_manifest \
  --path fixtures/manifests/example_manifest_v1.yaml \
  --host-ref example_training_host \
  --role train_manifest \
  --role eval_manifest \
  --summary 'Pose training manifest for the example project fixture.' \
  --json
```

For `host_path` file sources, relative `--path` / `storage.path` values are resolved only against `--project-root`. They are never resolved against the caller's current working directory. In the example above, Kikai hashes `path/to/project/fixtures/manifests/example_manifest_v1.yaml` regardless of where the CLI is invoked from. Use an absolute path only when the record should intentionally point outside the project root.

#### Register an immutable directory data source

Register directory-like immutable data sources through Kikai so the manifest digest is calculated by Kikai Lab. Directory manifests sort POSIX relative paths, record directories plus regular-file size and sha256 entries, and reject symlinks/special files. There is intentionally no caller-supplied digest flag.

```bash
kikai data-source create-directory example_face_cache_v1 \
  --project-root path/to/project \
  --source-type cache_directory \
  --path fixtures/cache/example_face_cache_v1 \
  --host-ref example_training_host \
  --role face_cache \
  --summary 'Example generated face cache directory.' \
  --json
```

Inspect and re-validate the registered record:

```bash
kikai data-source validate example_manifest_v1 --project-root path/to/project --json
kikai data-source show example_manifest_v1 --project-root path/to/project --json
```

The created record is written to `path/to/project/data_sources/example_manifest_v1.yaml` and can be referenced from a run:

```yaml
data_source_refs:
  - role: train_manifest
    data_source_id: example_manifest_v1
    required: true
  - role: initial_checkpoint
    data_source_id: null
    required: false
```

Use `required: false` with `data_source_id: null` to explicitly document an omitted optional input such as a no-resume initial checkpoint. Required refs must resolve to `data_sources/<data_source_id>.yaml`, and the requested role must be listed in the data source's `contract.role_compatibility`.

Remote host-side registration can also be expressed as an operation when the data path is only available in the target environment:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "create_example_face_cache_data_source",
    "project_root": "path/to/project",
    "adapter": "data_source_create",
    "data_source_kind": "directory",
    "data_source_id": "example_face_cache_v1",
    "source_type": "cache_directory",
    "path": "env:HOST_EXAMPLE_CACHE_ROOT",
    "host_ref": "example_training_host",
    "roles": ["face_cache"],
    "summary": "Example generated face cache directory."
  }
}
```

Run `kikai target dry-run <op.json>` first, then `kikai exec <op.json>`. The adapter computes the digest on the host running the operation, writes `data_sources/<id>.yaml`, and is useful before a later launch operation in the same remote payload project references the created id.

Launch-like operation requests should also declare the data sources they depend on:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "example_train",
    "project_root": "path/to/project",
    "adapter": "script_bundle_run",
    "bundle_id": "example_run_train",
    "entrypoint": "train",
    "data_source_refs": [
      {"role": "train_manifest", "data_source_id": "example_manifest_v1"}
    ]
  }
}
```

Run `kikai validate --project-root <project-root> --json` before committing registry changes, and run `kikai target dry-run <operation.json>` before side effects. Launch-like dry-runs fail before side effects when a declared data source is missing, blocked, mutable-live, role-incompatible, or not integrity-verifiable. `append_only` metrics logs are explicitly recorded as `append_only_not_rehashed`; immutable file-like inputs with Kikai-calculated `file_sha256` are re-hashed during dry-run preflight and recorded in the guard receipt.

For remote execution where the target adopter registry or newly created bundle is local and should not require an ad-hoc remote repo pull before the run, `remote_kikai_exec` can materialize a bounded local Kikai project payload on the remote host. Provide `local_operation_template`, `local_project_root`, `local_project_payload_paths`, and `remote_payload_project_root`; Kikai reads those local files, sends them through the structured remote adapter, rewrites the operation `project_root` to the remote payload root, runs remote `target dry-run`, then runs remote `exec`. Include required `source_snapshots/<id>/snapshot.json` and `source_snapshots/<id>/root/...` files in the payload list when containers mount Kikai-managed source snapshots.

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "remote_bundle_gate",
    "adapter": "remote_kikai_exec",
    "ssh_host": "env:KIKAI_REMOTE_SSH_HOST",
    "remote_project_root": "env:KIKAI_REMOTE_PROJECT_ROOT",
    "uv_bin": "env:KIKAI_REMOTE_UV_BIN",
    "local_operation_template": "/path/to/adopter/ops/gate.json",
    "local_project_root": "/path/to/adopter",
    "local_project_payload_paths": [
      "current.json",
      "containers/gate_runner.yaml",
      "source_snapshots/example_project_v1/snapshot.json",
      "source_snapshots/example_project_v1/root/scripts/gate.py",
      "script_bundles/gate_bundle/bundle.json",
      "script_bundles/gate_bundle/root/scripts/gate.py"
    ],
    "remote_payload_project_root": "/tmp/kikai_gate_payload_project",
    "remote_operation_path": "/tmp/kikai_gate_payload_project/ops/gate.json",
    "pipeline_run_id": "gate_001"
  }
}
```

For Discord/QC artifact delivery, register the webhook URL as a local server secret, then reference it from the delivery target:

```bash
kikai server secret set KIKAI_DISCORD_QC_WEBHOOK_URL \
  --value 'https://example.invalid/discord-webhook/...' \
  --json
```

Non-secret server settings such as host names, project roots, container roots, and image names can be registered the same way with `server setting set`:

```bash
kikai server setting set KIKAI_REMOTE_SSH_HOST --value training-host.example --json
```

Operation references keep using `env:NAME` or `${NAME}`. Kikai resolves those from the process environment first, then from registered server settings/secrets. Secret values are stored under the local server config directory with mode `0600` and are not printed by the registration command.

```json
{
  "schema_version": 1,
  "target_id": "discord_qc",
  "kind": "discord_webhook",
  "webhook_url": "env:KIKAI_DISCORD_QC_WEBHOOK_URL"
}
```

Then use one guarded operation JSON with `adapter: artifact_delivery`:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "deliver_artifact",
    "project_root": "path/to/project",
    "adapter": "artifact_delivery",
    "delivery_id": "example_run_step003000_preview_delivery",
    "delivery_target_id": "discord_qc",
    "artifact_id": "example_run_step003000_preview",
    "file_path": "/path/to/preview.mp4",
    "message": "example_run checkpoint step003000 preview ready"
  }
}
```

`artifact_delivery` sends a Discord-compatible multipart webhook request and writes `<project-root>/artifact_deliveries/<delivery_id>.json` only after a successful HTTP response. The webhook URL is not echoed in CLI output or saved in the delivery record.

For progress notifications without files, use the same delivery target with `adapter: webhook_notification`:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "notify_progress",
    "project_root": "path/to/project",
    "adapter": "webhook_notification",
    "notification_id": "example_run_checkpoint_watcher_started",
    "delivery_target_id": "discord_qc",
    "message": "example_run checkpoint watcher started",
    "severity": "info",
    "run_name": "example_run"
  }
}
```

`webhook_notification` posts a Discord-compatible JSON message and writes `<project-root>/notifications/<notification_id>.json` on success.

To chain operations from one guarded JSON, use `adapter: operation_sequence`. Child step requests inherit the parent `project_root` when omitted, and the parent guard receipt covers the whole sequence:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "example_run_checkpoint_qc_smoke",
    "project_root": "path/to/project",
    "adapter": "operation_sequence",
    "pipeline_run_id": "example_run_checkpoint_qc_smoke_001",
    "steps": [
      {
        "step_id": "notify_started",
        "request": {
          "operation": "notify_progress",
          "adapter": "webhook_notification",
          "notification_id": "example_run_checkpoint_qc_started",
          "delivery_target_id": "discord_qc",
          "message": "example_run checkpoint QC started"
        }
      },
      {
        "step_id": "deliver_preview",
        "request": {
          "operation": "deliver_artifact",
          "adapter": "artifact_delivery",
          "delivery_id": "example_run_step003000_preview_delivery",
          "delivery_target_id": "discord_qc",
          "artifact_id": "example_run_step003000_preview",
          "file_path": "/path/to/preview.mp4",
          "message": "example_run checkpoint preview ready"
        }
      }
    ]
  }
}
```

`operation_sequence` stops at the first failed step, writes `<project-root>/pipeline_runs/<pipeline_run_id>.json` with completed/failed step records, and refuses to overwrite an existing pipeline run record.

Before generating or delivering checkpoint QC artifacts, use `adapter: checkpoint_guard` to ensure the requested run/checkpoint/model/artifact class matches `current.json`:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "checkpoint_qc_guard",
    "project_root": "path/to/project",
    "adapter": "checkpoint_guard",
    "guard_id": "example_run_step003000_qc_guard",
    "run_name": "example_run",
    "checkpoint": "${CONTAINER_TRAINING_RUNS_ROOT}/example_run/checkpoints/checkpoint_step_003000.pt",
    "model_arch": "example_arch_v1",
    "artifact_class": "example_qc"
  }
}
```

`checkpoint_guard` reads `current.json`, rejects forbidden runs and artifact classes, checks exact current run/checkpoint/model match, and writes `<project-root>/guard_records/<guard_id>.json` only on success.

To prune a run's checkpoint directory without hand-composing `rm`, use `adapter: checkpoint_retention`. It protects the newest `keep_latest` checkpoints **and** the best `keep_best` by a training metric, then deletes the rest of the `checkpoint_step_*.pt` / `best_step_*.pt` families (`best_checkpoint.pt` is never deleted):

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "checkpoint_retention",
    "project_root": "path/to/project",
    "adapter": "checkpoint_retention",
    "run_dir": "${CONTAINER_TRAINING_RUNS_ROOT}/example_run",
    "experiment_id": "example_experiment",
    "dry_run": true
  }
}
```

The retention counts are configured **per experiment** in the `checkpoint_retention` section of `experiments/<experiment_id>.yaml` (`keep_latest`, `keep_best`, `metric_key`, `metric_mode`); explicit `keep_latest` / `keep_best` / `metric_key` / `metric_mode` request fields override the experiment values. Each checkpoint's metric is read from the `_loss` filename tag (e.g. `checkpoint_step_021500_loss20p6986.pt` -> `20.6986`, with `p`->`.` and `m`->`-`), falling back to the nearest step in the run's `metrics.jsonl` (`train_metrics.loss` / `early_stop_eval.mean_train_loss`). `dry_run: true` returns the kept/deleted plan without deleting anything, and any filename not matching the `_loss` convention is surfaced in the result `warnings`. Because it deletes files it is side-effecting: run `kikai target dry-run <op.json>` then `kikai target run <op.json>` (or `kikai exec`).

For TRT-backed preview/diagnostic generation, use `adapter: trt_cache_guard` to fail closed when cache use is not explicit:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "trt_cache_guard",
    "project_root": "path/to/project",
    "adapter": "trt_cache_guard",
    "guard_id": "example_run_step003000_trt_cache_guard",
    "model_arch": "example_arch_v1",
    "trt_cache_dir": "/workspace/trt_cache/example_run",
    "compile_mode": "reuse_cache",
    "require_compile_cache": true
  }
}
```

`trt_cache_guard` checks `model_arch` against `current.current_model_arch`, rejects disabled/no-cache compile modes, requires `require_compile_cache: true`, and writes a guard record only on success. It does not inspect remote/container filesystem contents; use a bundled container preflight for that.

A checkpoint-QC smoke fixture is available at `path/to/project/ops/example_run_checkpoint_qc_smoke.json`. It chains:

```text
checkpoint_guard -> trt_cache_guard -> webhook_notification(start) -> script_bundle_exec(TRT cache preflight) -> script_bundle_exec(fake generator) -> artifact_summary_guard -> artifact_delivery(preview) -> artifact_delivery(diagnostic) -> webhook_notification(done)
```

This fixture is intentionally **not** real TensorRT inference. The preflight bundle runs inside the checkpoint-watcher container and fails closed unless `KIKAI_TRT_CACHE_DIR` points at an existing readable/writable cache directory and required runtime modules are importable. The fake generator then writes deterministic placeholder MP4 bytes and a renderer-like summary; `artifact_summary_guard` validates `optimize=trt`, TRT cache paths, preview/diagnostic file existence, frame counts, audio presence, and audible volume before Discord delivery. This proves ordering, guard records, Discord progress, and preview/diagnostic artifact delivery before a real example-project TRT preview bundle replaces it. Copy the operation JSON before dry-run so the checked-in fixture remains receipt-free:

```bash
cp path/to/project/ops/example_run_checkpoint_qc_smoke.json /tmp/example_run_checkpoint_qc_smoke.json
kikai target dry-run /tmp/example_run_checkpoint_qc_smoke.json
KIKAI_TRT_CACHE_DIR=... KIKAI_DISCORD_PROGRESS_WEBHOOK_URL=... KIKAI_DISCORD_QC_WEBHOOK_URL=... kikai exec /tmp/example_run_checkpoint_qc_smoke.json
```

A tracked noop fixture is available for local or remote smoke tests. Copy it before dry-run so the checked-in fixture remains receipt-free:

```bash
cp examples/ops/noop_render_qc.json /tmp/kikai_noop_render_qc.json
kikai target dry-run /tmp/kikai_noop_render_qc.json
kikai exec /tmp/kikai_noop_render_qc.json
```

## Remote training lifecycle

Long-running training runs on a separate GPU host. Kikai owns that lifecycle through a family of guarded SSH/scp adapters so an operator never hand-composes raw `ssh host '...'` or `docker run` strings. Each of these is selected from one operation JSON via `kikai target dry-run <op.json>` then `kikai target run <op.json>` / `kikai exec <op.json>`.

| Adapter | Purpose |
| --- | --- |
| `script_bundle_run` (with `detach: true`) | Start a training container detached (`docker run -d --name <docker.name>`) so its lifecycle is owned by the remote docker daemon, not the local/ssh caller. Returns immediately with the started container id; refuses to start when a same-named container already exists. |
| `remote_docker_logs` | Fetch `docker logs --tail N <name>` for a detached run over a guarded SSH channel; combines stdout and stderr (training often logs to stderr). |
| `remote_docker_teardown` | List `docker ps -a` and `docker rm -f` orphaned containers selected by explicit `container_names` and/or a `name_pattern`. Frees a GPU/name held by a dead run. |
| `docker_container_restart` | Force-remove a named container (resolved from `containers/<container_id>.yaml`) so the next ephemeral run starts clean; with `mode: restart` it re-runs a `status: service` container detached. |
| `remote_file_push` | Push local files/dirs to the remote host over scp (dirs use `scp -r`). Used to sync the kikai-lab package to the remote checkout so `remote_kikai_exec` runs the latest adapters. |
| `remote_file_fetch` | Pull remote files back to a local destination root over scp. |
| `remote_docker_build` | Build a docker image on the remote host; the full Dockerfile is supplied inline (`dockerfile_content`) and piped over ssh. Bakes a derived training image so per-run `pip install` is unnecessary. |
| `remote_docker_run` | One-off `docker run --rm` of a given image plus an argv `command` list on the remote host (benchmarks, NGC containers) without a kikai checkout there. |
| `tensorboard_service` | `status` / `ensure-running` for a TensorBoard container resolved from `containers/<container_id>.yaml`; (re)starts it detached when the port/logdir does not match. |
| `remote_kikai_exec` | Ship a bounded local Kikai project payload to the remote host and run a local operation template there (see [One-command launch](#one-command-launch)). |

### Remote command safety validation

These adapters interpolate values into a remote shell, so the request fields are validated before any subprocess runs (claims below match the code exactly):

- `ssh_host` is validated in **every** remote adapter: it must match a strict charset and must not begin with `-`, because an ssh/scp argument that starts with `-` is parsed as an option (e.g. `-oProxyCommand=...` → local command execution).
- `remote_docker_run` `gpus` is validated against `all` / `none` / `<int>` / `device=<ids>`; `image`, `network`, `name`, `workdir`, and `volumes` are regex-validated, env keys are regex-validated and env values are `shlex`-quoted, and the `command` is a list of argv strings (each `shlex`-quoted), not a shell string.
- `remote_docker_teardown` `name_pattern` is matched with `re.fullmatch` (anchored — it must match the **whole** container name, not a substring, so e.g. `.` cannot select every container) and is length-capped at 200 chars; each selected name is re-checked against the safe-name regex before `docker rm -f`.
- `remote_docker_build` `image_tag` and `remote_build_dir` are regex-validated, `build_args` keys are regex-validated and each `k=v` token is `shlex`-quoted.
- Remote destination/build/workdir paths are containment-checked: they must match a safe absolute-path regex and must not contain `..` segments. Payload collection skips symlinks so a payload entry cannot point outside the bundle/project root.

A detached run started via `script_bundle_run` requires the container to define `docker.name`, and that name must pass the safe-container-name regex.

### One-command launch

`kikai remote-launch` absorbs the per-launch JSON boilerplate. It writes the inner `script_bundle_run` operation, walks the bundle tree to collect the text payload (auto-adding `current.json`, the container yaml, and the inner op json), wraps it in a `remote_kikai_exec` op, and prints the ready `kikai target run <remote_op>` command:

```bash
kikai remote-launch \
  --project-root <root> \
  --operation-id <op-id> \
  --bundle-id <bundle> \
  --container-id <container> \
  --entrypoint train \
  --ssh-host env:KIKAI_REMOTE_SSH_HOST \
  --remote-project-root env:KIKAI_REMOTE_PROJECT_ROOT \
  --args-json '["--max-steps","100"]' \
  --env PYTHONUNBUFFERED=1 \
  --json
```

Flags: `--project-root`, `--operation-id`, `--bundle-id`, `--container-id`, `--entrypoint`, `--ssh-host`, `--remote-project-root`, `--args-json` (a JSON list of strings; or repeat `--arg`/`--arg=--flag`), `--env KEY=VALUE` (repeatable), `--no-detach` (default is detached), `--container-yaml` (override the default `containers/<container-id>.yaml`), and `--extra-payload` (extra relative payload files; defaults to `current.json`). It writes `ops/<op-id>.json` and `ops/remote_<op-id>.json` and then you run `kikai target dry-run` / `kikai target run` on the remote op.

Binary files cannot ride the text payload channel (only `.py`, `.json`, `.yaml`, `.yml`, `.sh`, `.txt`, `.md` are shipped); push those separately with `remote_file_push`.

### Monitoring detached runs

`kikai_lab.log_parse.summarize_training_logs(op_result_or_logs)` decodes a `remote_docker_logs` op result (or raw log text), extracts JSONL `train_metrics` rows, finds the last step, and scans for error/ready markers — so a monitor loop does not re-write that regex every run.

## Reconciler daemon (`kikai serve`)

Everything above is one-shot: a human (or script) invokes an op and it runs once. The **reconciler daemon** turns the "someone who periodically drives the ops" role into a long-running process. It is intended to run **on the training host itself**, next to the local docker daemon and a registry that lives on that host — so it drives local docker directly rather than over ssh.

Each *active* run is declared as a desired-state artifact `managed_runs/<run_id>.yaml` (schema: `schemas/managed_run.schema.json`; example: `path/to/project/managed_runs/example_run.yaml`). For every such run, one reconcile **tick** is the smallest idempotent unit of work:

1. **poll** the training container's status via a local `docker inspect` (`docker_inspect_by_name`);
2. **QC** every *new* `checkpoint_step_*.pt` (up to `max_step`) exactly once by executing the run's `qc_op_template` — the daemon substitutes `{{step}}`, `{{step6}}` and `{{checkpoint_name}}` and runs it (typically a `script_bundle_run` that renders a checkpoint diagnostic and delivers it to Discord). This subsumes the standalone checkpoint-watcher sidecar;
3. run **`checkpoint_retention`** (the cooperating trainer no longer self-prunes, so this is what keeps the two independent keep-latest / keep-best windows bounded) — always *after* QC, so a checkpoint is never deleted before its diagnostic renders;
4. on a **terminal** training event (`early_stop` / `done` / `stopped_by_control` in `metrics.jsonl`) or the container exiting after it was seen running, send a finalize notification and tear the training container down (`docker_container_restart` mode `teardown`).

Progress/idempotency state lives beside the desired-state file in `managed_runs/<run_id>.progress.json` and is written atomically (temp + `os.replace`), so a crash mid-tick never corrupts it and the next tick resumes without re-posting QC. The daemon is trusted and in-process: it builds op requests itself and calls `execute_operation` directly (no guard-receipt dance, which only exists to gate untrusted CLI-loaded op files).

```bash
# one deterministic pass over every managed_runs/*.yaml (the unit test / cron fallback)
kikai reconcile --project-root <registry> --once

# long-running: reconcile every --interval seconds until interrupted
kikai serve --project-root <registry> --interval 60

# scope either command to a single run
kikai reconcile --project-root <registry> --run-id <run_id> --once
```

`kikai serve` is a thin loop over the same single-pass logic (`kikai_lab.reconcile.reconcile_once`); `--once` makes them equivalent. Per-run errors are isolated — one failing run records its error and the pass continues to the next.

## Decisions

Decisions are first-class records managed inside the project as `decisions/<decision_id>.yaml` (`schema_version`, `kind: decision`, `decision_id`, `title`, `summary`, `status` one of `open` / `decided` / `superseded`, optional `decided_at` and `links`). kikai-lab owns the decision log; no external system is required.

A project's `current.must_read_external_ref_ids` entry is satisfied by **either** an internal decision (`decisions/<id>.yaml`) **or** a legacy experiment `external_refs` entry, so `kikai validate` passes once the referenced decision exists in-project.

```bash
kikai decision create exp-001-pose-space \
  --project-root <root> \
  --title 'Align Stage A/B pose spaces' \
  --summary 'Use one extractor for both renderer target and audio target.' \
  --status decided \
  --link experiment:exp-001 \
  --json

kikai decision list --project-root <root> --json
```

`--status` defaults to `open`; `--link kind:id` is repeatable. `decision create` refuses to overwrite an existing decision.

## Project report & dashboard

`kikai report` aggregates the local project records — `current.json`, `decisions/`, `experiments/`, and `containers/` (the run ledger) — into one report. With no flags it returns the report JSON in the envelope; `--out <path>` writes the report JSON to disk, and `--html <path>` writes a self-contained offline HTML dashboard (the report JSON is inlined; no server and no fetch). Metrics/artifacts are layered on separately and merged into `runs[].metrics` on demand.

```bash
kikai report --project-root <root> --out report.json --html dashboard.html
```

The dashboard shows the project concept and current state, decision cards, per-experiment descriptions, and a filterable run table.

## HTTP server + dashboard (`kikai server start`)

Everything above is also reachable over HTTP: `kikai server start --projects-root <dir>
--port 8300` serves every project registry under one endpoint — project CRUD, typed run
submission (agents never touch docker/ssh), columnar metrics, artifact streaming, and a
no-build web dashboard at `/`. Beyond basic CRUD the API includes differential
submission (`submit-from/{parent}`), offline probes (`probe-from/{parent}`), a live
control plane (`POST .../control`), run comparison (`compare?runs=`), long-poll status,
and one-call session resume (`brief` / `journal`) — all documented in the served skill
guide. Agents should start from `GET /v1/skill.md` (served by the same process, so it
cannot drift). Every JSON response is the CLI envelope; errors
carry stable codes. Single-worker only; registry writes rely on process-local atomicity.
Optional flags: `--host 0.0.0.0` (must be explicit — see [SECURITY.md](SECURITY.md)),
`--auth-token` / `KIKAI_AUTH_TOKEN` (require a bearer token on every request but
`/healthz`), `--content-root` (enables artifact `/content`, fail-closed), `--path-map
CONTAINER_PREFIX=HOST_PREFIX`, `--run-dir-root` (contains metrics/checkpoint reads),
`--with-reconciler` (embed the reconcile loop; ONE reconciler per registry — do not
combine with an external `kikai serve` on the same project). Design doc:
`docs/plans/2026-07-02-kikai-http-server.md`.
