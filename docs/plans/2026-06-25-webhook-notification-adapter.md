# Webhook notification adapter plan

## Goal

Add a message-only Discord webhook operation so Kikai can send progress notifications independently from artifact delivery.

This is required for dogfooding because training/watcher pipelines need to post lifecycle events such as start, checkpoint detected, preview generation started, upload completed, and failure.

## Scope

Implement `adapter: webhook_notification`.

In scope:

- reuse delivery target records under `<project-root>/delivery_targets/<target_id>.json|yaml`;
- support `kind: discord_webhook` with `webhook_url: env:<NAME>`;
- send a Discord-compatible JSON payload `{ "content": message }`;
- write `<project-root>/notifications/<notification_id>.json` only after successful HTTP response;
- fail closed when the env ref is missing or the HTTP call fails;
- never print or record the webhook URL;
- keep the one-operation-JSON side-effect command shape.

Out of scope:

- retry/backoff;
- rich Discord embeds;
- pipeline sequencing;
- live checkpoint watcher integration.

## Operation shape

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "notify_progress",
    "project_root": "examples/example_project",
    "adapter": "webhook_notification",
    "notification_id": "example_run_checkpoint_watcher_started",
    "delivery_target_id": "discord_qc",
    "message": "example_run checkpoint watcher started",
    "severity": "info",
    "run_name": "example_run"
  }
}
```

## Acceptance

- Local HTTP-server test receives JSON Discord payload.
- Success writes a notification record.
- Missing webhook env ref fails before record creation.
- Webhook URL never appears in stdout or records.
- Full pytest, ruff, and example validation pass.
