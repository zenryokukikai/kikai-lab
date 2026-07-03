# Remote training lifecycle adapters plan

## Goal

Own the full remote-GPU training lifecycle through guarded operation JSON, so an operator never hand-composes raw `ssh host '...'` or `docker run` strings. A detached run, its logs, its teardown, image builds, file sync, and one-off remote runs all become structured adapters with the same guard-receipt flow as every other side effect.

## Scope

Implement the remote/detached adapters in `kikai_lab/operation.py`:

- `script_bundle_run` with `detach: true` → `docker run -d --name <docker.name>`; lifecycle owned by the remote docker daemon, returns the started container id, refuses to start when a same-named container already exists.
- `remote_docker_logs` — `docker logs --tail N <name>` over a guarded SSH channel; combines stdout+stderr.
- `remote_docker_teardown` — list `docker ps -a`, select by explicit `container_names` and/or `name_pattern`, `docker rm -f` each (unless `list_only`).
- `docker_container_restart` — force-remove a named container resolved from `containers/<container_id>.yaml`; with `mode: restart` re-run a `status: service` container detached.
- `remote_file_push` / `remote_file_fetch` — scp local↔remote (dirs via `scp -r`).
- `remote_docker_build` — build a docker image on the remote host from an inline `dockerfile_content` piped over ssh.
- `remote_docker_run` — one-off `docker run --rm <image> <argv...>` on the remote host.
- `tensorboard_service` — `status` / `ensure-running` for a TensorBoard container.

Out of scope: live log streaming, multi-host orchestration, retry/backoff, scheduling.

## Security validation

Every remote adapter validates `ssh_host` with `require_safe_ssh_host`: strict charset and an explicit reject of a leading `-` (an ssh/scp arg starting with `-` is parsed as an option, e.g. `-oProxyCommand=...` → local command execution).

- `remote_docker_run`: `gpus` matched against `all`/`none`/`<int>`/`device=<ids>`; `image`, `network`, `name`, `workdir`, `volumes` regex-validated; env keys regex-validated, env values `shlex`-quoted; `command` is a list of argv strings, each `shlex`-quoted.
- `remote_docker_teardown`: `name_pattern` matched with `re.fullmatch` (anchored, whole-name) and length-capped at 200; each selected name re-checked against the safe-name regex before removal.
- `remote_docker_build`: `image_tag` and `remote_build_dir` regex-validated; `build_args` keys regex-validated and each `k=v` token `shlex`-quoted.
- Remote dest/build/workdir paths must match a safe absolute-path regex and contain no `..` segments.

## Operation shape (detached run)

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "run_train",
    "project_root": "examples/example_project",
    "adapter": "script_bundle_run",
    "bundle_id": "example_run_train",
    "entrypoint": "train",
    "container_id": "example_run_training",
    "detach": true,
    "args": ["--max-steps", "100"]
  }
}
```

Logs and stop are then separate guarded ops (`remote_docker_logs`, `remote_docker_teardown`).

## Acceptance

- A detached run returns the started container id and refuses a duplicate name.
- `remote_docker_logs` returns the combined stdout+stderr tail.
- `remote_docker_teardown` `name_pattern` cannot substring-select all containers.
- ssh_host beginning with `-` is rejected by every remote adapter.
- Full pytest and ruff pass; the public hygiene guard passes.
