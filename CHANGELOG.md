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
- `brief` and `journal` endpoints for one-call session resume.
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
