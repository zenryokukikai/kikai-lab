# Kikai Lab Docker Exec Adapter Plan

> **For Hermes:** Use TDD. Write failing tests first, then minimal implementation.

**Goal:** Make `kikai exec <operation.json>` the one local command for Docker-container execution so agents never hand-compose `docker exec ...` with many args, heredocs, nested quoting, or ad-hoc SSH commands.

**Architecture:** Operation JSON stores the target `container_id` and structured `argv[]`. Kikai resolves `container_id` through the read-only `containers/*.yaml` registry to a canonical `docker.name`, then invokes Docker with a subprocess argument list. Kikai must never shell-join commands. Phase 1 implements Docker adapter execution, optional `docker_host` forwarding to Docker CLI for one-command local-to-remote Docker contexts, and testable fake-docker support; ad-hoc SSH/docker command composition remains forbidden.

**Tech Stack:** Python 3.11+, stdlib subprocess, JSON operation files, PyYAML registry loading, pytest, ruff.

---

## Problem

Agents repeatedly fail when asked to run commands inside Docker containers because they hand-compose commands like:

```bash
docker exec <container> bash -lc 'python ... many args ... <<EOF ... EOF'
```

Common failures:

- quote breakage,
- heredoc breakage,
- wrong container name,
- wrong workdir/mount assumptions,
- missing env values,
- accidental shell interpretation,
- inconsistent command reconstruction after context compaction.

This is a development-speed blocker and must be handled in Phase 1.

## Phase 1 rule

Agents must not directly compose Docker execution commands.

Allowed user/agent command shape:

```bash
kikai target dry-run ops/example_run_render_qc.json
kikai exec ops/example_run_render_qc.json
```

or:

```bash
kikai target run ops/example_run_render_qc.json
```

Forbidden operational patterns:

```bash
docker exec <container> ...many args...
docker exec <container> bash -lc '...'
ssh host 'docker exec <container> ...'
cat <<EOF | docker exec ...
```

## Operation shape

Example:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "render_qc",
    "project_root": "examples/example_project",
    "adapter": "docker_exec",
    "container_id": "example_run_training",
    "workdir": "env:CONTAINER_EXAMPLE_ENGINE_ROOT",
    "env": {
      "PYTHONUNBUFFERED": "1"
    },
    "argv": [
      "python",
      "scripts/render_qc.py",
      "--config",
      "configs/example_run_qc.yaml"
    ]
  }
}
```

`target dry-run` adds the guard receipt. `exec` verifies the guard receipt, resolves `container_id`, and executes:

```text
[docker, exec, --workdir, <workdir>, -e, PYTHONUNBUFFERED=1, <docker.name>, python, scripts/render_qc.py, --config, configs/example_run_qc.yaml]
```

as a subprocess argv list, not through a shell.

## Validation rules

For `adapter: docker_exec`:

- `request.argv` is required and must be a non-empty list of strings.
- `request.container_id` is required.
- `request.project_root` is required and must contain `containers/<container_id>.yaml`.
- The container record must have `docker.name`.
- String command fields such as `command`, `command_string`, `shell`, `heredoc`, or `script` are rejected for this adapter.
- Shell wrappers are rejected as a policy if `argv[0]` is `bash`, `sh`, or `zsh` and `-c` appears in argv.

## Output envelope

On success:

```json
{
  "ok": true,
  "data": {
    "execution_status": "docker_exec_completed",
    "container_id": "example_run_training",
    "container_name": "example-example_run-training",
    "returncode": 0,
    "stdout": "...",
    "stderr": "..."
  }
}
```

On non-zero container command exit:

- Kikai command returns non-zero.
- JSON envelope is still emitted.
- `data.returncode` records the Docker subprocess return code.
- `errors[0].code` is `operation.docker_exec_failed`.

## TDD tasks

### Task 1: docker_exec adapter invokes fake docker with structured argv

**Files:**
- Modify: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/operation.py`
- Modify: `kikai_lab/cli.py` if needed

Steps:
1. Write a test with temp registry `containers/run1_training.yaml` and operation JSON using `adapter: docker_exec`.
2. Use `KIKAI_DOCKER_BIN` pointing to a fake Python script that records `sys.argv[1:]` to a JSON file.
3. Run `target dry-run`, then `exec`.
4. Assert fake docker received `exec <container-name> <argv...>` with no shell string.
5. Implement minimal adapter.

### Task 2: reject shell/heredoc/string command shapes

**Files:**
- Modify: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/operation.py`

Steps:
1. Write tests for `command_string`, `heredoc`, and `argv: ["bash", "-lc", "..."]` rejection.
2. Implement validation errors.

### Task 3: support structured workdir/env

**Files:**
- Modify: `tests/test_side_effect_single_json.py`
- Modify: `kikai_lab/operation.py`

Steps:
1. Write a test with `workdir` and `env` object.
2. Assert fake docker receives `--workdir`, `-e KEY=VALUE` before container name.
3. Implement.

### Task 4: schema/docs/examples

**Files:**
- Modify: `schemas/operation.schema.json`
- Modify: `README.md`
- Optional create: `examples/ops/docker_exec_python_version.json`

Steps:
1. Extend operation schema with `container_id`, `workdir`, `env`.
2. Document `docker_exec` adapter.
3. Keep examples placeholder-only and receipt-free.

## Acceptance criteria

- Agents have a single command shape: `kikai exec <operation.json>`.
- Kikai executes Docker using subprocess argv arrays only.
- No shell-joined command, heredoc, or `bash -lc` path is accepted by `docker_exec`.
- `uv run pytest -q` passes.
- `uv run ruff check .` passes.
- training-host.example git-only pull and test/lint pass.
- No production Docker container is started during tests; fake docker is used.
