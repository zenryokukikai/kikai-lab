# Operation sequence adapter plan

## Goal

Let Kikai run a small ordered pipeline from one guarded operation JSON. This is required for dogfooding checkpoint QC because a checkpoint event must become a controlled sequence:

1. post progress notification;
2. run guarded generation/preflight steps;
3. deliver preview/diagnostic artifacts;
4. record which step completed or failed.

This commit implements the sequencing primitive only. It does not yet implement checkpoint polling or TRT preview generation.

## Scope

Implement `adapter: operation_sequence`.

In scope:

- one parent operation JSON, one parent guard receipt;
- `pipeline_run_id` records under `<project-root>/pipeline_runs/<pipeline_run_id>.json`;
- ordered `steps[]` with `step_id` and child `request` objects;
- child steps inherit parent `project_root` when omitted;
- execute existing adapters such as `webhook_notification` and `artifact_delivery`;
- stop at first failed step;
- record completed and failed step status;
- refuse nested `operation_sequence` and duplicate `step_id` values;
- refuse to overwrite an existing pipeline run record.

Out of scope:

- loops or checkpoint polling;
- concurrent steps;
- retry/backoff;
- variable substitution between steps;
- real TRT/QC generation.

## Operation shape

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "example_run_checkpoint_qc_smoke",
    "project_root": "examples/example_project",
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
          "delivery_id": "example_run_preview_delivery",
          "delivery_target_id": "discord_qc",
          "artifact_id": "example_run_preview",
          "file_path": "/path/to/preview.mp4",
          "message": "example_run preview ready"
        }
      }
    ]
  }
}
```

## Acceptance

- A local HTTP-server test verifies notification then artifact delivery order.
- Successful run writes a completed pipeline record.
- Failed second step records first step completed and second step failed.
- Failed sequence stops before later steps and does not create downstream delivery records.
- Full pytest, ruff, and example validation pass locally and on training-host.example.
