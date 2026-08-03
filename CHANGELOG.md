# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/); this project is
pre-1.0, so minor versions may contain breaking API changes.

## [Unreleased]

### Added
- `remote_docker_run` `detach` / `ports`: start a long-lived service container
  (`docker run -d`, no `--rm`, `name` required so it stays tearable-down) and
  publish `host:container` port pairs, so a resident service no longer needs a
  hand-written `docker run`.
- HTTP control server (`kikai server start`): project/experiment/run registry
  over one endpoint, typed run submission (agents never touch Docker/SSH),
  columnar metrics, artifact streaming, and a no-build web dashboard.
- Agent operator guide served at `GET /v1/skill.md` (cannot drift from the
  running version).
- `submit-from/{parent}` — differential submission with recorded lineage.
- `probe-from/{parent}` — checkpoint-warm-started offline probes;
  `metric_checks.window_steps_relative` for probe-relative gates.
- `retention.keep_milestones` — trajectory anchors for probes, protected
  alongside the rolling keep_latest/keep_best windows.
- Live control plane: `POST /runs/{run}/control` changes a running run's
  `max_steps` / early-stopping / graceful stop with no restart, via
  `<run_dir>/control.json`.
- Live QC config: `POST /runs/{run}/qc-config` updates a managed run's
  `probes` / `qc_op` (key-level partial update, `null` removes) with full
  submit-time validation of the merged record; the reconciler picks the new
  config up on its next tick.
- `brief` and `journal` endpoints for one-call session resume.
- `kikai remote` registry-write subcommands, so agents never hand-roll
  `curl` + heredoc quoting: `bundle-put` (tars a directory with Python's
  `tarfile` — no macOS AppleDouble/`.DS_Store`/`__MACOSX` junk — and uploads
  it), `container-put` (PUT a container record from a JSON/YAML file), and
  `qc-config` (live probes/qc_op update from a JSON file).
- **Run-dir inspection API (ssh-free)**: `GET .../runs/{run}/artifacts`
  lists files/dirs inside the run_dir (path/size/mtime/is_dir; client paths
  are sandboxed — traversal and symlink escapes are refused), and
  `GET .../runs/{run}/artifacts/file?path=...&max_bytes=...` returns small
  text/JSON content (`tail=true` for file tails; binary files return
  metadata only). CLI: `kikai remote artifacts <project> <run>
  [--path d --depth N | --file rel --tail]`.
- `GET .../runs/{run}/status` now exposes the full reconciler progress
  digest: `probes_done_steps`, `op_fail_counts`, `op_gave_up`, `last_error`,
  and recent `delivery_failures`.
- `delivery_summary` (`{total, expected, unrecorded, delivered, failed,
  skipped, unverified, reasons, reason_samples}`) on the `/status` digest and
  on `kikai remote run`, counted over EVERY recorded delivery outcome and
  measured against the QC/probe steps the reconciler actually completed.
  `delivery_failures` is a truncated tail (20 / CLI 5) with no denominator,
  which hid a run whose QC posts were unconfirmed at every one of 60
  checkpoints — a tail cannot express scale. `expected`/`unrecorded` cover the
  other silence: a run whose delivery records were never written at all
  (crash-restart replay, or a daemon predating delivery recording) used to
  report `total: 0`, identical to a healthy run. `failed` (confirmed not
  delivered) and `unverified` (kikai cannot confirm either way) are separate
  numbers on every surface. `reasons` is keyed by a fixed vocabulary
  (`skipped`, `post_failed`, `http_<code>`/`http_other`, `no_delivery_event`,
  `unknown`) so its cardinality never depends on script-supplied error text;
  that text survives as bounded `reason_samples`.
- `discord_post_failed` is now part of the delivery-event vocabulary and is
  recorded as `post_failed:<error>`. Previously a post that raised was filed
  as `no_delivery_event`, i.e. a KNOWN failure was indistinguishable from
  "the script emitted no event at all". Records written by a pre-upgrade
  daemon keep their `no_delivery_event` shape on disk and therefore read as
  `unverified`, while the same event recorded now reads as `failed` — see the
  vintage caveat in `server/SKILL.md` before diagnosing a straddling run.
- `kikai remote run` renders both delivery lines deterministically and marks
  every cut: `reasons` is ordered by count (so which buckets survive a cut no
  longer depends on dict insertion order) and clipped with the same `…` helper
  as the recorded text, and the failure tail prints compact
  `key=outcome(status):detail` rows instead of raw JSON — the same 300-char
  budget now shows 4-5 rows instead of 1-3 cut mid-object.
- Every delivery record and `delivery_failures` row carries an explicit
  `outcome` (`delivered` / `http_error` / `post_failed` / `skipped` /
  `no_delivery_event` / `unknown`); readers no longer classify from free text,
  and script-supplied text is truncated once, with a visible `…` marker.
- Delivery-outcome recording: after each QC/probe op the reconciler parses
  `{"event": "discord_post", "status": N}` / `discord_post_skipped` events
  from the op's captured stdout (and `artifact_delivery` step results) into
  `progress.delivery`, keyed like `op_fail_counts` (`qc:<step>` /
  `probe:<id>:<step>`) — "the video rendered but never arrived" is now
  diagnosable from the API. Extraction is fail-safe: a parsing surprise
  never breaks reconciliation.
- Run conclusions (verdict + evidence) recorded with the run.
- Declarative `evaluations` / `metric_checks` run by the reconciler, with
  gate-failure notifications.
- Optional shared **bearer-token auth** (`--auth-token` / `KIKAI_AUTH_TOKEN`);
  server binds `127.0.0.1` by default.
- Trainer contract documentation and a dependency-free reference toy trainer
  under `examples/toy_trainer/`.

### Security
- See [SECURITY.md](SECURITY.md): reaching the API means running code on the
  host; safe defaults, opt-in exposure.

[Unreleased]: https://github.com/zenryokukikai/kikai-lab/commits/main
