# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/); this project is
pre-1.0, so minor versions may contain breaking API changes.

## [Unreleased]

### Added
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
