# Artifact delivery adapter plan

## Goal

Make Kikai able to deliver generated QC artifacts to Discord from a single guarded operation JSON, so checkpoint preview/diagnostic workflows can become dogfoodable without hand-written webhook commands.

This is the first step toward the Go condition:

- progress and QC artifacts reach Discord;
- delivery is driven by Kikai operation JSON;
- webhook secrets are kept in environment variables;
- successful sends are recorded as artifact delivery records.

## Scope

Implement a minimal `artifact_delivery` operation adapter for `discord_webhook` delivery targets.

In scope:

- load delivery target records from `<project-root>/delivery_targets/<target_id>.json|yaml`;
- support `webhook_url: env:<NAME>` secret references;
- send one artifact file with a Discord-compatible multipart request;
- write `<project-root>/artifact_deliveries/<delivery_id>.json` on success;
- fail closed before sending when the file is missing or env ref is missing;
- never include webhook URL in CLI output or delivery records;
- preserve the existing one-operation-JSON side-effect shape.

Out of scope for this commit:

- checkpoint watcher/poller;
- TRT cache inference pipeline;
- preview/diagnostic generation;
- retry/backoff policy;
- multiple attachments;
- real Discord smoke, because this commit uses local HTTP-server tests and does not require a webhook secret.

## Operation shape

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "deliver_artifact",
    "project_root": "examples/example_project",
    "adapter": "artifact_delivery",
    "delivery_id": "example_run_step003000_preview_delivery",
    "delivery_target_id": "discord_qc",
    "artifact_id": "example_run_step003000_preview",
    "file_path": "/path/to/preview.mp4",
    "message": "example_run checkpoint step003000 preview ready"
  }
}
```

Target record:

```json
{
  "schema_version": 1,
  "target_id": "discord_qc",
  "kind": "discord_webhook",
  "webhook_url": "env:KIKAI_DISCORD_QC_WEBHOOK_URL"
}
```

Execution remains:

```bash
kikai target dry-run ops/deliver.json
kikai exec ops/deliver.json
```

## Acceptance

- The adapter posts multipart `payload_json` and `files[0]` to the configured webhook URL.
- A delivery record is written only after a successful HTTP response.
- Missing env refs and missing files fail before creating a delivery record.
- The webhook secret never appears in stdout or the delivery record.
- All existing tests, ruff, and example registry validation pass.
