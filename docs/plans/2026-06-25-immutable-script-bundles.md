# Kikai Lab Immutable Script Bundles Implementation Plan

> **For Hermes:** Use TDD. Write failing tests first, then minimal implementation.

**Goal:** Preserve past experiment reproducibility by executing from immutable script/config snapshots stored and verified by Kikai Lab, not from mutable live script paths.

**Architecture:** Add a `script_bundles/<bundle_id>/bundle.json` registry format that stores a manifest of required snapshot files and their SHA-256 hashes. A new `script_bundle_exec` adapter validates the bundle before execution, expands a named entrypoint to structured argv, and delegates to the existing Docker execution path. Past experiments reference a bundle id and entrypoint; if any file is missing or changed, Kikai fails closed before execution.

**Tech Stack:** Python 3.11+, JSON bundle manifests, SHA-256 file hashing, existing Docker subprocess argv execution, pytest, ruff.

---

## Problem

The current `docker_exec` adapter solves hand-composed Docker commands, but operation JSON still points at mutable repo paths such as:

```json
"argv": ["python", "scripts/training/launch_example_run.py", "--config", "configs/training/example_run.yaml"]
```

This does not protect past experiments if:

- a script is deleted,
- only some helper scripts remain,
- config and launcher versions drift,
- a future commit changes CLI compatibility,
- a remote worktree is not the same revision used at launch,
- dry-run and exec see different file contents.

For reproducible experiments, Kikai must protect the exact script/config bundle used by the experiment. Git-only sync is necessary, but not sufficient: past launches must not depend on mutable live script paths continuing to work.

## Decision

Kikai Lab must treat experiment launch code as an immutable, content-addressed bundle.

Source-of-truth for editing remains the repo. But once a bundle is created for an experiment, Kikai stores a snapshot under the registry, verifies hashes, and executes from that snapshot path.

The important distinction:

- **editable development code:** normal repo files, e.g. `scripts/training/launch.py`;
- **experiment bundle snapshot:** immutable copied files under `script_bundles/<bundle_id>/root/...` plus `bundle.json` hashes.

An operation for a past experiment should reference the bundle, not live scripts.

## Bundle layout

Example:

```text
examples/example_project/script_bundles/example_run_train/
  bundle.json
  root/
    scripts/training/launch_example_run.py
    scripts/training/common.py
    configs/training/example_run.yaml
```

`bundle.json`:

```json
{
  "schema_version": 1,
  "kind": "kikai_script_bundle",
  "bundle_id": "example_run_train",
  "immutable": true,
  "entrypoints": {
    "train": {
      "argv": [
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch_example_run.py",
        "--config",
        "script_bundles/example_run_train/root/configs/training/example_run.yaml"
      ]
    }
  },
  "files": [
    {
      "path": "root/scripts/training/launch_example_run.py",
      "sha256": "..."
    },
    {
      "path": "root/scripts/training/common.py",
      "sha256": "..."
    },
    {
      "path": "root/configs/training/example_run.yaml",
      "sha256": "..."
    }
  ]
}
```

Rules:

- `bundle_id` must match its directory name.
- `kind` must be `kikai_script_bundle`.
- `immutable` must be `true` for experiment execution bundles.
- All manifest file paths must be relative and stay inside the bundle directory.
- Kikai computes SHA-256 over actual files and rejects missing or mismatched files.
- Entry point argv must be a structured list of strings.
- Entry point argv must not be a shell wrapper such as `bash -lc`.

## Operation shape

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "example_run_train",
    "project_root": "examples/example_project",
    "target_id": "example_run_train",
    "adapter": "script_bundle_exec",
    "bundle_id": "example_run_train",
    "entrypoint": "train",
    "container_id": "example_run_training",
    "docker_host": "env:KIKAI_DOCKER_HOST",
    "workdir": "env:CONTAINER_EXAMPLE_ENGINE_ROOT",
    "env": {
      "PYTHONUNBUFFERED": "1"
    },
    "args": []
  }
}
```

Execution flow:

1. Validate guard receipt.
2. Load `script_bundles/<bundle_id>/bundle.json`.
3. Validate bundle identity and immutability.
4. Validate every file hash.
5. Expand `entrypoints[entrypoint].argv + request.args`.
6. Reuse the existing Docker execution path with structured argv.
7. Return `bundle_id`, `entrypoint`, and expanded argv in the JSON result.

## Fail-closed behavior

Kikai must refuse execution if:

- bundle manifest is missing;
- bundle id mismatches directory/request;
- any bundle file is missing;
- any bundle file hash mismatches;
- entrypoint is missing;
- entrypoint argv is invalid;
- request tries to pass raw `argv` to `script_bundle_exec`;
- request `args` is not a list of strings;
- entrypoint uses shell wrappers.

Expected error codes:

```text
operation.script_bundle_missing
operation.script_bundle_invalid
operation.script_bundle_id_mismatch
operation.script_bundle_not_immutable
operation.script_bundle_file_missing
operation.script_bundle_hash_mismatch
operation.script_bundle_entrypoint_missing
operation.script_bundle_entrypoint_invalid
operation.script_bundle_args_invalid
operation.script_bundle_raw_argv_forbidden
operation.shell_wrapper_forbidden
```

## TDD tasks

### Task 1: Bundle integrity validates before exec

**Files:**
- Modify: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/operation.py`

Steps:

1. Add a helper to create a temp bundle with a launcher file and matching hash.
2. Write a test for `adapter: script_bundle_exec` using fake Docker.
3. Verify RED: test fails with `operation.adapter_not_implemented`.
4. Implement minimal bundle load/hash validation and argv expansion.
5. Verify fake Docker receives `python script_bundles/<id>/root/...`.

### Task 2: Missing and modified bundle files fail closed

**Files:**
- Modify: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/operation.py`

Steps:

1. Write a missing-file test expecting `operation.script_bundle_file_missing`.
2. Write a hash-mismatch test expecting `operation.script_bundle_hash_mismatch`.
3. Verify RED.
4. Implement file validation.
5. Verify GREEN.

### Task 3: Reject mutable/unsafe script bundle shapes

**Files:**
- Modify: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/operation.py`

Steps:

1. Write tests for `immutable: false`, raw request `argv`, invalid `args`, and `bash -lc` entrypoint.
2. Verify RED.
3. Implement validation.
4. Verify GREEN.

### Task 4: Docs/schema/examples

**Files:**
- Modify: `README.md`
- Modify: `schemas/operation.schema.json`
- Optional create: `schemas/script_bundle.schema.json`
- Optional create: `examples/example_project/script_bundles/...` fixture if small.

Steps:

1. Document immutable script bundles and `script_bundle_exec`.
2. Extend operation schema with `bundle_id`, `entrypoint`, and `args`.
3. Add schema file for bundle manifest if useful.
4. Run `uv run pytest -q`, `uv run ruff check .`, and `uv run kikai validate --project-root examples/example_project --json`.

## Acceptance criteria

- Past experiment operations can reference immutable script bundles instead of mutable live script paths.
- Kikai refuses to execute if any bundle file is missing or modified.
- Kikai refuses mutable bundles for experiment execution.
- Kikai rejects raw argv on `script_bundle_exec`; entrypoint argv must come from the bundle manifest.
- Docker execution still uses subprocess argv arrays only.
- Tests prove fake Docker receives expanded argv from the bundle snapshot.
- No production Docker container is started during tests.
- Local tests/lint/validate pass.
- Git-only push/pull and training-host.example tests/lint/validate pass before completion.
