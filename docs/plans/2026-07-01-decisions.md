# In-project decision records plan

## Goal

Make kikai-lab own its decision log instead of pointing out to an external system. Previously a project's `current.must_read_external_ref_ids` had to be satisfied by an experiment's `external_refs` pointing at an external decision id. Decisions now live in the project, so validation passes with no external system.

## Scope

Implement decision records in `kikai_lab/decision.py` plus a `kikai decision` CLI subcommand.

Record shape (`decisions/<decision_id>.yaml`):

```yaml
schema_version: 1
kind: decision
decision_id: exp-001-pose-space
title: Align Stage A/B pose spaces
summary: Use one extractor for both renderer target and audio target.
status: decided          # open | decided | superseded
decided_at: 2026-07-01T00:00:00Z   # optional
links:                   # optional
  - {kind: experiment, id: exp-001}
```

- `create_decision` validates `decision_id` (safe charset), `status`, and a non-empty `title`, and refuses to overwrite an existing decision.
- `load_decisions` returns all `kind: decision` records sorted by id.
- Validation change: a must-read is satisfied by an internal decision id **or** a legacy experiment `external_ref` id; `kikai validate` only fails when a must-read id matches neither.

Out of scope: decision state machine enforcement, supersession graphs, cross-project decisions.

## CLI shape

```bash
kikai decision create <id> --project-root <root> --title <title> \
  [--summary <text>] [--status open|decided|superseded] \
  [--decided-at <ts>] [--link kind:id ...] --json

kikai decision list --project-root <root> --json
```

## Acceptance

- `decision create` writes `decisions/<id>.yaml` and refuses a duplicate.
- A `current.must_read_external_ref_ids` entry resolves against a matching in-project decision and `kikai validate` passes with no external system.
- `decision list` returns the records and a count.
- Full pytest and ruff pass; the public hygiene guard passes.
