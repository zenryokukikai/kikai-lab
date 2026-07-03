# 2026-06-26 Kikai reproducible watcher delivery and source snapshot gate

## Purpose

Make the approved example_run step020000 fullaudio QC artifact postable by the checkpoint watcher/Discord path, while tightening Kikai Lab reproducibility rules so future operations do not depend on mutable existing source repository mounts or hand-authored SSH/docker commands.

## Decisions

1. **QC status**
   - The step020000 fullaudio v18 full-frame QC artifact is approved by the user.
   - It should be posted to Discord through the watcher/Kikai delivery path, not held for further human QC before delivery.

2. **Watcher Discord delivery**
   - Add `examples/example_project/ops/example_run_checkpoint_watcher_discord_post_20000.json`.
   - The operation does not regenerate media.
   - It runs `artifact_summary_guard` against the existing summary and then delivers both preview and diagnostic media to `discord_qc`.
   - It sends start/done progress notifications to `discord_progress`.
   - Add remote wrapper `examples/example_project/ops/remote_example_run_checkpoint_watcher_discord_post_20000.json` so the local invocation remains one Kikai operation JSON.

3. **No direct operational SSH**
   - Direct SSH should be reserved for remote repository sync/update only.
   - Container operations, artifact generation, watcher delivery, guards, and notifications must go through Kikai adapters and one operation JSON.
   - Add `scripts/sync_remote_kikai_repo.py` as the checked-in sync command. Operators should run this script instead of hand-writing SSH git commands.

4. **No mutable source repo mounts for reproducible container work**
   - Kikai container definitions must not mount live worktrees or existing source repos as code roots.
   - Code mounts such as `/workspace/example_project` and `/workspace/example_engine` must declare `source_kind: kikai_managed_source_snapshot`.
   - Live repo envs such as `HOST_EXAMPLE_PROJECT_ROOT`, `HOST_EXAMPLE_ENGINE_ROOT`, and `*_WORKTREE` are rejected for code mounts.
   - Example example_run containers now reference `HOST_KIKAI_EXAMPLE_PROJECT_SOURCE_SNAPSHOT_ROOT` and `HOST_KIKAI_EXAMPLE_ENGINE_SOURCE_SNAPSHOT_ROOT` instead of live repo roots.

## Implementation files

- `kikai_lab/validation.py`
- `tests/test_container_reproducibility.py`
- `examples/example_project/containers/example_run_training.yaml`
- `examples/example_project/containers/example_run_checkpoint_watcher.yaml`
- `examples/example_project/containers/example_run_example_project_training_runner.yaml`
- `examples/example_project/containers/example_run_qc_runner.yaml`
- `examples/example_project/ops/example_run_checkpoint_watcher_discord_post_20000.json`
- `examples/example_project/ops/remote_example_run_checkpoint_watcher_discord_post_20000.json`
- `examples/example_project/ops/remote_example_run_*` env mapping updates
- `scripts/sync_remote_kikai_repo.py`

## Verification

Required verification before claiming done:

- `uv run pytest tests/test_container_reproducibility.py -q`
- `uv run pytest -q`
- `uv run kikai validate --project-root examples/example_project --json`
- `python scripts/sync_remote_kikai_repo.py --host dummy --remote-root /srv/kikai-lab-example --dry-run`

## Operational notes

- Posting the media to Discord still requires valid `KIKAI_DISCORD_PROGRESS_WEBHOOK_URL`, `KIKAI_DISCORD_QC_WEBHOOK_URL`, and the artifact path envs on the execution side.
- This change does not copy or rsync source files. Source sync must remain git-only.
- If a future operation needs a new code snapshot, it must create/register a Kikai-managed immutable source snapshot rather than mounting an arbitrary existing checkout.
