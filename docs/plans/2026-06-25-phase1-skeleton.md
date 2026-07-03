# Kikai Lab Phase 1 Skeleton Implementation Plan

> **For Hermes:** Use TDD. Write failing tests first, then minimal implementation.

**Goal:** Build the initial `kikai` CLI, schemas, file-backed registry reader, and example_run fixture records.

**Architecture:** A small Python package with stdlib `argparse`, stable JSON envelopes, YAML/JSON registry loading, and validator functions. `examples/example_project` is fixture data only; production users pass their own registry root.

**Tech Stack:** Python 3.11+, uv, argparse, PyYAML, jsonschema, pytest, ruff.

---

## Fixed implementation decisions

- Repo name: `kikai-lab`.
- Python package: `kikai_lab`.
- CLI entrypoint: `kikai_lab.cli:main`.
- JSON envelope shape: `ok`, `schema_version`, `data`, `warnings`, `errors`, `next_actions`.
- `current.json` is the only current pointer.
- Stale current blocks validation/guard after `staleness_block_after_hours`.
- `must_read_external_ref_ids` must be a subset of current experiment `external_refs[].id`; reverse inclusion is not required.
- Production registry root is adopter-owned; examples are fixtures.
- Remote/container side-effect operations must not depend on hand-built long argv, nested quoting, heredocs, or multiple control files. Kikai operations use exactly one positional operation JSON file; all parameters and guard receipt data live inside that file.

## Remote command safety rule

Hermes agents frequently make quoting mistakes when composing long SSH/docker commands. Therefore Kikai Lab must make long operational argv impossible for side-effect commands:

- Do not allow long training/render/QC argument lists over SSH, `docker exec`, or local shell.
- Do not split execution state across `--args-file`, `--approval-token`, `--file`, or similar multi-file/control-flag schemes.
- Use one operation JSON path as the only side-effect CLI argument, for example `kikai target run ops/example_run_qc.json`.
- Put all parameters in the operation JSON: `project_root`, `target_id`, operation kind, adapter name, environment refs, inputs, outputs, structured argv arrays, side-effect list, and `guard_receipt`.
- Side-effect commands output JSON by default and should not require `--json`.
- `kikai target dry-run` updates or emits the same operation JSON with a `guard_receipt` over the current `request` content.
- `kikai target run` and `kikai exec` refuse execution unless the same operation JSON contains a valid `guard_receipt` matching its current `request` content.
- Adapters receive structured data from the operation JSON and execute argv lists directly, never shell-joined strings.
- If an operation cannot be represented in one operation JSON file, implementation must stop and extend the operation schema instead of adding another free-form SSH/docker command.

## Non-blocking open questions deferred

- Exact stale override policy for read-only exploration.
- Reusable package vs adopter-local target definitions.
- Remote status SSH/helper implementation.

## Task 1: Project skeleton and CLI envelope

**Files:**
- Create: `pyproject.toml`
- Create: `kikai_lab/cli.py`
- Create: `kikai_lab/envelope.py`
- Test: `tests/test_cli_envelope.py`

**Steps:**
1. Write failing tests that invoke `python -m kikai_lab.cli validate --project-root <tmp> --json`.
2. Verify failure because CLI module is missing.
3. Implement JSON envelope and minimal command parser.
4. Verify tests pass.

## Task 2: Current pointer loading and staleness

**Files:**
- Create: `kikai_lab/store.py`
- Modify: `kikai_lab/cli.py`
- Test: `tests/test_current_validate.py`

**Steps:**
1. Write failing tests for fresh, warn, stale, and missing current.
2. Implement `current` command and current staleness computation.
3. Validate stale current as blocking.
4. Verify tests pass.

## Task 3: Experiment/run loading and subset validation

**Files:**
- Create: `kikai_lab/validation.py`
- Test: `tests/test_validate_links.py`

**Steps:**
1. Write failing tests for `must_read_external_ref_ids` subset success/failure.
2. Implement YAML loading for `experiments/*.yaml` and `runs/*.yaml`.
3. Validate current run/model/checkpoint links.
4. Verify tests pass.

## Task 4: Show and next commands

**Files:**
- Modify: `kikai_lab/cli.py`
- Test: `tests/test_show_next.py`

**Steps:**
1. Write failing tests for `show experiment`, `show run`, and `next` ordering.
2. Implement read-only show commands.
3. Implement deterministic `next`: validation blockers first, stale current next, then proposed actions.
4. Verify tests pass.

## Task 5: example_run fixture backfill

**Files:**
- Create: `examples/example_project/current.json`
- Create: `examples/example_project/project.yaml`
- Create: `examples/example_project/experiments/example_experiment.yaml`
- Create: `examples/example_project/runs/example_run.yaml`
- Test: `tests/test_examples_validate.py`

**Steps:**
1. Write failing test that example registry validates.
2. Add fixture records using placeholders only.
3. Verify no absolute local/remote paths or raw IPs in fixture records.
4. Verify tests pass.

## Task 6: Single operation JSON for side-effect commands

**Files:**
- Create: `schemas/operation.schema.json`
- Create: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/cli.py`

**Steps:**
1. Write failing tests proving `kikai exec ops/example_run_qc.json` accepts exactly one operation JSON path.
2. Write failing tests proving `kikai exec --file ops/example_run_qc.json`, `kikai exec --args-file ...`, `kikai exec ... --approval-token ...`, and extra passthrough args are rejected.
3. Define operation JSON with `request` and `guard_receipt` sections.
4. Implement parser restrictions for side-effect commands.
5. Verify side-effect commands return JSON by default without `--json`.
6. Verify all tests pass.

## Verification commands

```bash
uv run pytest -q
uv run ruff check .
uv run kikai validate --project-root examples/example_project --json
uv run kikai current --project-root examples/example_project --json
```
