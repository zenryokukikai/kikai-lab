# Checkpoint guard adapter plan

## Goal

Prevent Kikai checkpoint QC pipelines from rendering, validating, or delivering artifacts for the wrong run/checkpoint/model. This directly addresses the dogfood No-Go risk: an agent must not accidentally generate Discord-visible preview/diagnostic artifacts from stale or cross-lineage assets.

## Scope

Implement `adapter: checkpoint_guard`.

In scope:

- read `<project-root>/current.json`;
- require exact match for:
  - `run_name` -> `current_run_name`;
  - `checkpoint` -> `current_checkpoint`;
  - `model_arch` -> `current_model_arch`;
- reject `run_name` when it appears in `do_not_use_as_current`;
- reject `artifact_class` when it appears in `artifact_class_forbidden_next`;
- require `artifact_class` to be in `artifact_class_allowed_next` when that list is present and non-empty;
- write `<project-root>/guard_records/<guard_id>.json` only on success;
- fail closed without writing records on mismatch;
- refuse to overwrite an existing guard record.

Out of scope:

- file existence checks for remote/container checkpoint paths;
- artifact registry lookup;
- external design registry readback;
- TRT cache validation;
- preview/diagnostic generation.

## Operation shape

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "checkpoint_qc_guard",
    "project_root": "examples/example_project",
    "adapter": "checkpoint_guard",
    "guard_id": "example_run_step003000_visual_qc_guard",
    "run_name": "example_run",
    "checkpoint": "${CONTAINER_TRAINING_RUNS_ROOT}/example_run/checkpoints/checkpoint_step_003000.pt",
    "model_arch": "example_arch_v1",
    "artifact_class": "visual_only_renderer_qc"
  }
}
```

## Acceptance

- Correct current run/checkpoint/model/artifact class passes and writes a guard record.
- Wrong checkpoint fails without a guard record.
- Forbidden artifact class fails without a guard record.
- `do_not_use_as_current` run fails without a guard record.
- Full pytest, ruff, and example validation pass locally and on training-host.example.
