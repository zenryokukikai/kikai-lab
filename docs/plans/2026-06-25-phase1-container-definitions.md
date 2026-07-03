# Kikai Lab Phase 1 Container Definitions Plan

> **For Hermes:** Use TDD. Write failing tests first, then minimal implementation.

**Goal:** Add first-class read-only Docker container/service definitions to Phase 1 so agents stop guessing which container should be used.

**Architecture:** Phase 1 will manage desired container definitions as registry records under `containers/*.yaml`. It will validate and display these definitions, but it will not start, stop, restart, or reconcile Docker containers yet. Side-effect lifecycle operations remain future work and must use the one-operation-JSON rule.

**Tech Stack:** Python 3.11+, uv, argparse, PyYAML, jsonschema schema files, pytest, ruff.

---

## Why this is Phase 1, not Phase 2

Kikai Lab's first purpose is to reduce agent confusion and prevent wrong operational actions. In example operations, repeated confusion came from using the wrong Docker container, watcher sidecar, TensorBoard process, or bind mount lineage. Therefore Phase 1 must include canonical container definitions even if it does not yet perform Docker lifecycle management.

Without this, `current`, `validate`, `next`, and operation JSON can still point agents at ambiguous runtime state.

## Phase 1 scope

Implement read-only desired-state management:

- Add `schemas/docker_container.schema.json`.
- Add registry records under `<project-root>/containers/*.yaml`.
- Add `kikai show container <container_id> --project-root <registry-root> --json`.
- Extend `kikai validate` to validate current required containers.
- Add example example-project container definitions using placeholders only, with no machine-local absolute paths or raw IPs.

Do not implement in Phase 1:

- `docker run`, `docker exec`, `docker compose`, restart, stop, remove, or reconcile.
- SSH-based live inspect.
- Drift detection against actual Docker state.
- Healthcheck execution.

Those are Phase 2+ and must be represented as one operation JSON per side-effect.

## Registry shape

Add a new directory:

```text
<project-root>/containers/
  <container_id>.yaml
```

Minimum record:

```yaml
schema_version: 1
kind: docker_container
container_id: example_run_training
host_id: training_host
role: training
status: desired_running
summary: Canonical training container for example_run.

docker:
  name: example-example_run-training
  image: env:EXAMPLE_TRAINING_IMAGE
  restart_policy: unless-stopped
  network_mode: host
  gpus: all

workdir: /workspace/example_engine
mounts:
  - source: env:EXAMPLE_ENGINE_WORKTREE
    target: /workspace/example_engine
    mode: rw
  - source: env:TRAINING_RUNS_ROOT
    target: /workspace/training_runs
    mode: rw

env_refs:
  TRAINING_RUNS_ROOT: env:TRAINING_RUNS_ROOT

healthcheck:
  type: command
  argv: ["python", "-c", "import torch; assert torch.cuda.is_available()"]

related_runs:
  - example_run
```

## Current pointer integration

`current.json` may declare required containers:

```json
{
  "required_container_ids": [
    "example_run_training",
    "example_run_checkpoint_watcher",
    "example_run_tensorboard"
  ]
}
```

Validation rules:

- Every `required_container_ids[]` entry must have `containers/<id>.yaml`.
- Container record `container_id` must match filename stem.
- Container `kind` must be `docker_container`.
- Container must include `docker.name` and `docker.image`.
- Container should include at least one of `role`, `status`, or `summary`; Phase 1 can warn later, but tests should enforce essential required fields only.

## CLI

Read-only command:

```bash
kikai show container example_run_training --project-root examples/example_project --json
```

Output envelope:

```json
{
  "ok": true,
  "data": {
    "container": {"container_id": "example_run_training"}
  }
}
```

Missing container:

```json
{
  "ok": false,
  "errors": [
    {"code": "show.container_missing"}
  ]
}
```

## TDD tasks

### Task 1: Current required containers validate missing records

**Files:**
- Modify: `tests/test_validate_links.py`
- Modify: `kikai_lab/validation.py`

Steps:
1. Write a failing test where `current.json` includes `required_container_ids: ["training"]` but `containers/training.yaml` is missing.
2. Expected failure after implementation: `validate` returns `current.container_missing`.
3. Implement minimal validation.
4. Run the focused test and full suite.

### Task 2: Container record identity and docker essentials

**Files:**
- Modify: `tests/test_validate_links.py`
- Modify: `kikai_lab/validation.py`

Steps:
1. Write failing tests for filename/id mismatch and missing `docker.image`/`docker.name`.
2. Implement minimal validation.
3. Run focused tests and full suite.

### Task 3: Show container command

**Files:**
- Modify: `tests/test_show_next.py`
- Modify: `kikai_lab/cli.py`

Steps:
1. Write failing test for `kikai show container training --project-root <root> --json`.
2. Implement `show container` alongside existing experiment/run.
3. Add missing container error.
4. Run focused tests and full suite.

### Task 4: Example example-project container fixtures

**Files:**
- Create: `examples/example_project/containers/*.yaml`
- Modify: `examples/example_project/current.json`
- Modify: `tests/test_examples_validate.py`

Steps:
1. Write failing test that examples expose required container IDs and `show container` succeeds for one fixture.
2. Add placeholder-only container records for training, checkpoint watcher, and TensorBoard.
3. Ensure no raw local/remote paths or IPs leak into examples.
4. Run examples tests and full suite.

### Task 5: Schema file

**Files:**
- Create: `schemas/docker_container.schema.json`
- Modify: `tests/test_schema_files.py`

Steps:
1. Add `docker_container.schema.json` to expected schema list.
2. Watch schema test fail.
3. Add minimal schema.
4. Run schema tests and full suite.

## Acceptance criteria

- `uv run pytest -q` passes.
- `uv run ruff check .` passes.
- `kikai validate --project-root examples/example_project --json` succeeds.
- `kikai show container <example-id> --project-root examples/example_project --json` succeeds.
- No Docker side effects are executed.
- All source sync remains git-only.
