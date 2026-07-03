# Project report & dashboard plan

## Goal

Give an operator one offline view of the whole project — concept, current state, decisions, per-experiment descriptions, and the run ledger — without standing up a server. Aggregate the local records into one report and optionally render a self-contained HTML dashboard.

## Scope

Implement `build_project_report` and `render_report_html` in `kikai_lab/report.py` plus a `kikai report` CLI subcommand.

`build_project_report` is a pure aggregation of local records:

- `current.json` → project block (id, stage, summary, current experiment/run, verification staleness/age, next-decision pointer).
- `decisions/*.yaml` → decision cards.
- `experiments/*.yaml` → experiment cards (title, summary, external_refs, is_current).
- `containers/*.yaml` → the run ledger (`container_id`, docker name/image, role, status, summary, `metrics: null`).

Metrics/artifacts are layered on separately (fetched from the training host on demand and merged into `runs[].metrics`); the report itself reads no remote state.

`render_report_html` inlines the report JSON into a single static HTML page (no server, no fetch) with decision cards, experiment cards, and a filterable run table.

Out of scope: live metrics fetch, auth, hosting, write-back.

## CLI shape

```bash
kikai report --project-root <root> [--out report.json] [--html dashboard.html] [--json]
```

With no `--out`/`--html`, the report JSON is returned in the envelope. `--out` writes the JSON; `--html` writes the offline dashboard.

## Acceptance

- The report aggregates decisions, experiments, and the run ledger from local records only.
- `--html` produces a self-contained page that renders with no network access.
- `--out` and `--html` write their files and the envelope reports the paths.
- Full pytest and ruff pass; the public hygiene guard passes.
