# kikai server — HTTP API + dashboard for agent-driven experiment management

## Goal

Give AI agents and humans one network endpoint (`http://<host>:<port>`) that covers project
management, training operations, and result inspection — replacing the ssh +
`remote_kikai_exec` payload flow for day-to-day work. Agents must never touch docker or
host paths directly; humans get a browsable dashboard over the same API.

## Why

Operating kikai over ssh requires the caller to maintain op + remote-wrapper JSON pairs,
enumerate payload file lists by hand, and know host-internal paths. Responses arrive as
JSON escaped inside `stdout_tail`, which agents misread. All of that is accidental
complexity the training host can absorb by running a server next to the registry and the
docker daemon.

## Architecture decisions

1. **The server is a trusted in-process caller** — the same precedent as the reconciler
   daemon (`kikai_lab/reconcile.py`): typed API requests are turned into operation
   requests by the server itself and executed via `execute_operation()` directly. The
   guard-receipt dance remains for human-edited operation *files*; it is not needed when
   the request never exists as an editable file.
2. **The file registry stays the only store.** Every endpoint reads/writes the same
   YAML/JSON/JSONL records the CLI and daemon use. No database. Metrics stay in
   `metrics.jsonl`, streamed and downsampled on demand.
3. **Multi-project = a projects root of sibling registries.** `--projects-root <dir>`;
   `project_id` = directory name (safe charset, single path segment). A directory is a
   project iff it contains `project.yaml`. Projects are archived, never hard-deleted.
4. **The envelope is the wire format.** Every JSON response body is the existing
   `kikai_lab.envelope.envelope()` shape (`ok/data/warnings/errors/next_actions`). HTTP
   status derives from `OperationError` code conventions (`*_missing|*_not_found` → 404,
   `*_exists|*_in_use` → 409, validation codes → 422, other operation errors → 400).
   `next_actions[].command` carries HTTP hints (`"GET /v1/..."`).
5. **Run submissions are recorded, statuses are derived.** A typed submission writes
   `runs/<run_name>.yaml` (canonical record + `request_sha256`), auto-creates
   `managed_runs/<run_name>.yaml` for the unmodified reconciler, and keeps an audit copy
   under `ops/`. Status is derived per request from docker inspect + the daemon's
   `progress.json` + the metrics terminal event — never stored authoritatively.
6. **Idempotent creation everywhere.** Client-supplied ids; PUT semantics. Re-PUT with
   identical canonical content → `200` + `already_exists: true`. Divergent content on an
   immutable resource → `409` with a diff summary. Agents can retry safely.
7. **Multi-host is design-only in v1.** The server takes `--host-id` (default `local`);
   submissions accept an optional `host_ref` and anything non-local is rejected with
   `operation.host_not_local`. Existing `host_ref` fields in project/artifact records are
   untouched. Later options (documented, not built): one server per GPU host with a thin
   routing client, or a coordinator proxying typed ops to per-host agents.
8. **No auth (internal deployment), but an auth seam**: the server binds `127.0.0.1` by
   default (`--host 0.0.0.0` must be explicit) and all `/v1` routes hang off one router
   where a token dependency can be added without route changes.
9. **Single-worker uvicorn.** Registry writes rely on process-local atomicity
   (tmp + `os.replace`, `O_EXCL` creates). One reconciler per registry: `--with-reconciler`
   must not run alongside an external `kikai serve` on the same project.

## Surface (prefix `/v1`)

- Meta: `GET /healthz`, `GET /v1/version`, `GET /v1/skill.md` (agent skill doc served by
  the same process), `GET /openapi.json`, dashboard at `/`.
- Projects: `GET /v1/projects`, `PUT /v1/projects/{id}`, `GET /v1/projects/{id}`,
  `POST .../archive|unarchive`, `GET .../report` (existing `build_project_report`),
  `GET .../validate` (existing validation suite).
- Experiments / decisions / containers / data-sources: `GET` list/detail + idempotent
  `PUT`; container registration enforces mount reproducibility; data-source integrity is
  always server-computed.
- Bundles: `PUT .../bundles/{id}` with a raw tar body containing a `kikai_bundle.json`
  manifest (entrypoints map); safe extraction (absolute paths, `..`, links rejected);
  reuses `create_script_bundle`.
- Runs: list/detail, `POST .../submit` (typed, `dry_run` supported), `POST .../stop`
  (idempotent), `GET .../status` (tiny polling payload), `GET .../logs?tail=`,
  `GET .../metrics?keys=&max_points=` (columnar, streamed, always includes the exact
  last row), `GET .../events?since_seq=` (derived, monotonically numbered).
- Artifacts: ledger queries + `GET .../content` streaming with fail-closed containment:
  resolved real paths must fall under an explicitly configured `--content-root`.
- Escape hatch: `POST .../operations` executes an arbitrary operation request in-process
  (dry-run supported) so every existing adapter stays reachable without new endpoints.

## Module layout

`kikai_lab/server/`: `app.py` (FastAPI factory, envelope exception handlers, config),
`registry.py` (project resolution, atomic writes, idempotency helpers), `projects.py`,
`resources.py`, `runs.py`, `submit.py`, `metrics.py`, `bundles.py`, `reconciler.py`
(background thread looping `reconcile_once` over all projects), `SKILL.md`, `static/`
(no-build dashboard). CLI: `kikai server start` (lazy import so other commands never pay
the FastAPI import cost). Dependencies: `fastapi`, `uvicorn` (main), `httpx` (dev).

## Milestones

1. Design doc + skeleton + read-only projects API + `kikai server start`.
2. Idempotent PUT machinery + project/experiment/decision/container/data-source CRUD.
3. Runs read plane + columnar metrics + artifact content streaming.
4. Bundle tar upload + data-source registration endpoints.
5. Typed run submission + stop + operations escape hatch + embedded reconciler.
6. Dashboard (static, no build step).
7. SKILL.md + polish (pagination/fields sweep, README, fixtures).

## Validation

- `fastapi.testclient.TestClient` per router (`tests/test_server_*.py`), tmp projects
  roots, exact envelope assertions; fake docker via `KIKAI_DOCKER_BIN`; pure-function
  units for the metrics downsampler, tar safety, idempotency hashing, and content-root
  containment; one subprocess smoke test booting real uvicorn on an ephemeral port.
- `scripts/check_public_hygiene.py` over all new files; fixtures use `example_*` naming.
