# Kikai Lab Script Bundle Create Implementation Plan

> **For Hermes:** Use TDD. Write failing tests first, then minimal implementation.

**Goal:** Add a CLI command that creates immutable script bundles from live repo files so past experiments can be reproduced without hand-writing `bundle.json`.

**Architecture:** Add `kikai script-bundle create <bundle_id>` as a local registry-writing command. It copies declared files or recursive `--include-dir` payloads from a source root into `script_bundles/<bundle_id>/root/...`, computes SHA-256 hashes, rewrites entrypoint argv references to bundled snapshot paths, writes `bundle.json`, and refuses to overwrite an existing bundle.

**Tech Stack:** Python 3.11+, argparse, shutil copy, SHA-256 hashing, existing envelope output, pytest, ruff.

---

## CLI shape

```bash
kikai script-bundle create example_run_train \
  --project-root .kikai \
  --source-root . \
  --entrypoint train \
  --include-dir scripts/training \
  --include-dir configs/training \
  --argv python \
  --argv scripts/training/launch_example_run.py \
  --argv=--config \
  --argv configs/training/example_run.yaml \
  --json
```

Rules:

- `--project-root` is the Kikai registry root where `script_bundles/` is written.
- `--source-root` is the repo/worktree root from which `--file` paths are copied.
- `--file` values are relative paths under `--source-root` and become `root/<same-relative-path>` inside the bundle.
- `--include-dir` values are relative directories under `--source-root`; all regular files below them are copied recursively, excluding generated caches such as `__pycache__`, `.pytest_cache`, and `.pyc`/`.pyo` files.
- `--entrypoint` names the entrypoint in `bundle.json`.
- `--argv` repeats to form the structured entrypoint argv.
- Any argv item exactly matching a bundled source path is rewritten to `script_bundles/<bundle_id>/root/<path>`.
- Existing bundle dirs are never overwritten in this first version.
- No shell wrappers are allowed in created entrypoint argv.

## Output

On success:

```json
{
  "ok": true,
  "data": {
    "bundle_id": "example_run_train",
    "bundle_dir": ".../script_bundles/example_run_train",
    "bundle_manifest": ".../script_bundles/example_run_train/bundle.json",
    "entrypoint": "train",
    "file_count": 2,
    "entrypoint_argv": [
      "python",
      "script_bundles/example_run_train/root/scripts/training/launch_example_run.py",
      "--config",
      "script_bundles/example_run_train/root/configs/training/example_run.yaml"
    ]
  }
}
```

## Fail-closed errors

```text
script_bundle.create_project_root_missing
script_bundle.create_source_root_missing
script_bundle.create_bundle_exists
script_bundle.create_file_path_invalid
script_bundle.create_file_missing
script_bundle.create_file_duplicate
script_bundle.create_entrypoint_invalid
script_bundle.create_argv_invalid
operation.shell_wrapper_forbidden
```

## TDD tasks

### Task 1: Create a bundle with copied files and rewritten argv

Files:

- Create: `tests/test_script_bundle_create.py`
- Modify: `kikai_lab/cli.py`
- Modify: `kikai_lab/operation.py` or a helper module

Steps:

1. Write a test that creates source files under a temp source root.
2. Run `kikai script-bundle create ...`.
3. Assert `bundle.json` exists, files are copied, hashes match, and argv paths are rewritten to `script_bundles/<id>/root/...`.
4. Verify RED: command is currently unknown.
5. Implement minimal create command.
6. Verify GREEN.

### Task 2: Refuse unsafe/missing/duplicate inputs

Files:

- Modify: `tests/test_script_bundle_create.py`
- Modify: implementation.

Steps:

1. Test existing target bundle is rejected.
2. Test missing source file is rejected.
3. Test duplicate file path is rejected.
4. Test absolute or `..` file path is rejected.
5. Test `bash -lc` entrypoint argv is rejected.
6. Verify RED, implement, verify GREEN.

### Task 3: End-to-end validate and exec compatibility

Files:

- Modify: `tests/test_script_bundle_create.py`.

Steps:

1. After create, run `kikai validate --project-root <root> --json` and expect ok.
2. Create a `script_bundle_exec` operation referencing the generated bundle and fake Docker; ensure fake Docker receives bundled snapshot paths.
3. Run all tests/lint.

## Acceptance criteria

- Users can create immutable script bundles without manually writing `bundle.json`.
- Bundle creation copies files and records SHA-256 hashes.
- Entry point argv uses bundled snapshot paths for declared files.
- Existing bundles are protected from accidental overwrite.
- Created bundles pass `kikai validate`.
- Created bundles work with `script_bundle_exec` and fake Docker tests.
- Local and training-host.example pytest/ruff/validate pass.
