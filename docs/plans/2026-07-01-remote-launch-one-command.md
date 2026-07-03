# Remote launch one-command plan

## Goal

Remove the per-launch JSON boilerplate an operator otherwise repeats every training iteration: writing the inner `script_bundle_run` op, listing every text payload file in the bundle tree, and wrapping it as a `remote_kikai_exec` op with the remote payload root / operation path / pipeline-run id fields.

## Scope

Implement `kikai remote-launch` (CLI) on top of pure builders in `kikai_lab/remote_launch.py`:

- `collect_bundle_payload_paths` — relative payload paths: caller `extra` (e.g. `current.json`, container yaml, inner op json) followed by every text file under `script_bundles/<bundle_id>/`, de-duplicated, order-preserving, skipping symlinks and `__pycache__`.
- `build_remote_kikai_exec_op` — a complete `remote_kikai_exec` op (local_operation_template branch) with sensible defaults for `remote_payload_project_root`, `remote_operation_path`, and `pipeline_run_id`.
- `build_script_bundle_launch_ops` — one call returning `(inner_op, remote_op, inner_rel, remote_rel)`.

The builders are pure (no I/O, no env reads); the CLI writes the returned dicts and prints the ready `kikai target run <remote_op>` command.

Only text suffixes ride the payload channel (`.py`, `.json`, `.yaml`, `.yml`, `.sh`, `.txt`, `.md`); binary files are pushed with `remote_file_push`.

Out of scope: shipping binaries, remote repo pulls, running the op (the operator runs `target dry-run` / `target run` themselves).

## CLI shape

```bash
kikai remote-launch \
  --project-root <root> \
  --operation-id <op-id> \
  --bundle-id <bundle> \
  --container-id <container> \
  --entrypoint train \
  --ssh-host env:KIKAI_REMOTE_SSH_HOST \
  --remote-project-root env:KIKAI_REMOTE_PROJECT_ROOT \
  --args-json '["--max-steps","100"]' \
  --env PYTHONUNBUFFERED=1
```

Flags: `--operation-id`, `--bundle-id`, `--container-id`, `--entrypoint`, `--ssh-host`, `--remote-project-root`, `--args-json` (or repeated `--arg`/`--arg=--flag`), `--env KEY=VALUE` (repeatable), `--no-detach`, `--container-yaml`, `--extra-payload` (defaults to `current.json`). Writes `ops/<op-id>.json` and `ops/remote_<op-id>.json`.

## Acceptance

- The builders are unit-tested in isolation (pure dicts in, pure dicts out).
- The collected payload includes `current.json`, the container yaml, the inner op json, and the whole text bundle tree, with symlinks excluded.
- The printed next action is `kikai target run ops/remote_<op-id>.json`.
- Full pytest and ruff pass; the public hygiene guard passes.
