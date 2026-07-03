"""Runs read plane: list / detail / status / logs / metrics / events.

A run's status is DERIVED per request — never stored authoritatively — from four
independent truths: the declared ``runs/<run>.yaml`` record, a live ``docker inspect``
of the training container, the reconciler's ``managed_runs/<run>.progress.json``, and
the metrics terminal event. That is what makes the endpoint safe to poll: it cannot go
stale, and it cannot disagree with what the daemon actually did.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from kikai_lab.operation import (
    OperationError,
    docker_inspect_by_name,
    docker_logs_by_name,
    docker_name_from_container,
    load_container_record,
    resolve_metrics_path,
    resolve_text_ref,
)
from kikai_lab.reconcile import (
    checkpoint_steps,
    load_managed_run,
    load_progress,
    read_terminal_event,
)
from kikai_lab.server.app import envelope_response
from kikai_lab.server.metrics import read_last_train_metrics, read_metrics_columnar
from kikai_lab.server.registry import (
    ServerConfig,
    load_yaml_record,
    require_project,
    require_safe_id,
    select_fields,
)
from kikai_lab.server.resources import list_yaml_records

DERIVED_STATUSES = (
    "submitted",
    "submitting",
    "submit_failed",
    "running",
    "exited_pending_finalize",
    "completed",
    "early_stopped",
    "stopped",
    "failed",
    "unknown",
)


def load_managed_run_optional(project_root: Path, run_name: str) -> dict[str, Any] | None:
    path = project_root / "managed_runs" / f"{run_name}.yaml"
    if not path.is_file():
        return None
    try:
        return load_managed_run(path)
    except OperationError:
        return None


def run_dir_contained(config: ServerConfig, run_dir: Path) -> bool:
    if not config.run_dir_roots:
        return True
    try:
        real = run_dir.resolve()
    except OSError:
        return False
    import os as _os

    return any(
        _os.path.commonpath([str(real), str(root.resolve())]) == str(root.resolve())
        for root in config.run_dir_roots
        if root.is_dir()
    )


def resolved_run_dir(
    project_root: Path,
    run_name: str,
    managed_run: dict[str, Any] | None,
    config: ServerConfig | None = None,
) -> Path | None:
    """The daemon-local run directory, if this run declares one that resolves (and,
    when run_dir_roots is configured, is contained by them — fail-closed)."""
    candidates = []
    if managed_run and managed_run.get("run_dir"):
        candidates.append(str(managed_run["run_dir"]))
    run_path = project_root / "runs" / f"{run_name}.yaml"
    if run_path.is_file():
        try:
            record = load_yaml_record(run_path, kind="run")
        except OperationError:
            record = {}
        submission = record.get("submission") or {}
        if isinstance(submission.get("run_dir"), str):
            candidates.append(submission["run_dir"])
    for candidate in candidates:
        try:
            resolved = Path(resolve_text_ref(candidate))
        except OperationError:
            continue
        if config is not None and not run_dir_contained(config, resolved):
            continue
        return resolved
    return None


def inspect_training_container(
    project_root: Path,
    managed_run: dict[str, Any] | None,
    fallback_container_id: str | None = None,
) -> dict[str, Any]:
    """Live container view; mirrors reconcile.poll_status but never raises.

    Falls back to the submission's container_id when no managed_run exists, so a run
    caught mid-submission (crash between docker run and the final record write) is
    still observable rather than reading as container-less.
    """
    container_id = (
        managed_run.get("training_container_id") if managed_run else fallback_container_id
    )
    if not container_id:
        return {"exists": False, "running": False, "managed": False}
    try:
        container = load_container_record(project_root, container_id)
        name = resolve_text_ref(docker_name_from_container(container, ""))
        found, data, _ = docker_inspect_by_name({"project_root": str(project_root)}, name)
    except OperationError as exc:
        return {
            "exists": False,
            "running": False,
            "managed": managed_run is not None,
            "inspect_error": exc.code,
        }
    state = (data[0].get("State") if found and data and isinstance(data[0], dict) else None) or {}
    return {
        "container_name": name,
        "exists": bool(found),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "state": state.get("Status"),
        "started_at": state.get("StartedAt"),
        "managed": managed_run is not None,
    }


def derive_status(
    *,
    declared: str | None,
    container: dict[str, Any],
    progress: dict[str, Any],
    terminal_event: str | None,
) -> str:
    if progress.get("finalized"):
        if terminal_event == "early_stop":
            return "early_stopped"
        if terminal_event == "done":
            return "completed"
        if terminal_event == "stopped_by_control":
            # an intentional, checkpointed stop via the control plane — success,
            # distinguished from the trainer's own natural/early endings
            return "stopped"
        # Finalized with NO terminal metrics row: the reconciler tears down any stably
        # exited container, including a crashed one — do not report that as success.
        return "failed"
    if container.get("running"):
        return "running"
    if container.get("exists") and container.get("state") in ("restarting",):
        return "running"
    if container.get("exists") and container.get("state") in ("created",):
        return "submitted"
    if container.get("exists") and container.get("state") in ("exited", "dead"):
        exit_code = container.get("exit_code")
        if terminal_event is None and exit_code not in (0, None):
            return "failed"
        return "exited_pending_finalize"
    if not container.get("managed"):
        return declared or "unknown"
    if not progress.get("seen_running") and not container.get("exists"):
        return "submitted"
    return "unknown"


def require_run_record(project_root: Path, run_name: str) -> dict[str, Any]:
    run_path = project_root / "runs" / f"{run_name}.yaml"
    if not run_path.is_file():
        raise OperationError(
            "run.not_found",
            f"no run '{run_name}' in project '{project_root.name}'",
            {"project_id": project_root.name, "run_name": run_name},
        )
    return load_yaml_record(run_path, kind="run")


def require_metrics_path(
    project_root: Path, run_name: str, config: ServerConfig | None = None
) -> Path:
    managed = load_managed_run_optional(project_root, run_name)
    run_dir = resolved_run_dir(project_root, run_name, managed, config)
    if run_dir is None:
        raise OperationError(
            "run.run_dir_missing",
            "run declares no resolvable run_dir (managed_run or submission required)",
            {"run_name": run_name},
        )
    metrics_path = resolve_metrics_path(run_dir)
    if not metrics_path.exists():
        raise OperationError(
            "run.metrics_missing",
            "run has no metrics.jsonl yet",
            {"run_name": run_name},
        )
    return metrics_path



def parse_flag_map(args: list[Any]) -> tuple[dict[str, list[str]], bool]:
    """Flatten an argv list into ({flag: [values]}, lossy). Values stay lists —
    joining would make ["foo bar"] and ["foo","bar"] indistinguishable. First
    occurrence of a repeated flag wins (matching args_set semantics) and marks the
    parse LOSSY, so the caller knows the map may hide a real argv difference.
    Leading positional tokens land under "_positional"."""
    flags: dict[str, list[str]] = {}
    positional: list[str] = []
    lossy = False
    current: str | None = None
    values: list[str] = []

    def flush() -> None:
        nonlocal current, values, lossy
        if current is not None:
            if current in flags:
                lossy = True  # repeated flag: later occurrence dropped from the map
            else:
                flags[current] = values
        current, values = None, []

    for token in [str(a) for a in args]:
        if token.startswith("--"):
            flush()
            current = token
        elif current is None:
            positional.append(token)
        else:
            values.append(token)
    flush()
    if positional:
        flags["_positional"] = positional
    return flags, lossy


def diff_across(values_by_run: dict[str, Any]) -> bool:
    canon = {json.dumps(v, sort_keys=True, default=str) for v in values_by_run.values()}
    return len(canon) > 1


def build_runs_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.get("/projects/{project_id}/compare")
    def runs_compare(
        project_id: str,
        runs: str = Query(..., description="comma-separated run names (2-6)"),
    ) -> JSONResponse:
        """Side-by-side of what CHANGED between runs: per-flag args diff, env diff,
        submission/managed key diff (each run's own name normalized to {run} so
        run_dir-style differences don't count), plus latest metrics and verdicts."""
        from kikai_lab.server.submit import rebind_run_name

        path = require_project(config, project_id)
        names = [require_safe_id(n.strip(), kind="run") for n in runs.split(",") if n.strip()]
        if len(set(names)) != len(names) or not 2 <= len(names) <= 6:
            raise OperationError(
                "run.compare_invalid",
                "compare needs 2-6 DISTINCT run names",
                {"runs": names},
            )
        summaries: dict[str, Any] = {}
        submissions: dict[str, dict[str, Any]] = {}
        raw_submissions: dict[str, dict[str, Any]] = {}
        manageds: dict[str, dict[str, Any]] = {}
        raw_manageds: dict[str, dict[str, Any]] = {}
        for name in names:
            record = require_run_record(path, name)
            submission = dict(record.get("submission") or {})
            managed_cfg = dict(submission.get("managed") or {})
            managed_record = load_managed_run_optional(path, name)
            latest = None
            run_dir = resolved_run_dir(path, name, managed_record, config)
            if run_dir is not None:
                metrics_path = resolve_metrics_path(run_dir)
                if metrics_path.exists():
                    latest = read_last_train_metrics(metrics_path)
            summaries[name] = {
                "status": record.get("status"),
                "verdict": record.get("verdict"),
                "experiment_id": record.get("experiment_id"),
                "parent_run": submission.get("parent_run"),
                "latest_step": latest.get("step") if latest else None,
                "latest_loss": latest.get("loss") if latest else None,
            }
            # normalize the run's own name away so cosmetic run_dir diffs vanish
            raw_submissions[name] = submission
            raw_manageds[name] = managed_cfg
            submissions[name] = rebind_run_name(submission, name, "{run}")
            manageds[name] = rebind_run_name(managed_cfg, name, "{run}")

        def table(
            section: dict[str, dict[str, Any]],
            raw_section: dict[str, dict[str, Any]] | None = None,
        ) -> dict[str, dict[str, Any]]:
            # a key is a diff only when it differs BOTH raw and rebound: rebinding
            # exists to suppress cosmetic own-name diffs, never to create phantom
            # ones (identical values embedding one run's name must not diverge)
            keys = {k for body in section.values() for k in body}
            out: dict[str, dict[str, Any]] = {}
            for key in sorted(keys):
                per_run = {name: section[name].get(key) for name in names}
                if not diff_across(per_run):
                    continue
                if raw_section is not None and not diff_across(
                    {name: raw_section[name].get(key) for name in names}
                ):
                    continue
                out[key] = per_run
            return out

        parsed = {n: parse_flag_map(submissions[n].get("args") or []) for n in names}
        args_by_run = {n: parsed[n][0] for n in names}
        any_lossy = any(parsed[n][1] for n in names)
        raw_args_by_run = {
            n: parse_flag_map(raw_submissions[n].get("args") or [])[0] for n in names
        }
        env_by_run = {n: dict(submissions[n].get("env") or {}) for n in names}
        raw_env_by_run = {n: dict(raw_submissions[n].get("env") or {}) for n in names}

        def top(section: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
            # server-managed metadata always differs between runs — pure diff noise
            excluded = (
                "args",
                "env",
                "managed",
                "parent_run",
                "overrides",
                "request_sha256",
                "at",
                "started_container_id",
            )
            return {
                n: {k: v for k, v in section[n].items() if k not in excluded}
                for n in names
            }

        args_diff = table(args_by_run, raw_args_by_run)
        raw_argvs = {n: [str(a) for a in raw_submissions[n].get("args") or []] for n in names}
        rebound_argvs = {
            n: [str(a) for a in submissions[n].get("args") or []] for n in names
        }
        if (
            (not args_diff or any_lossy)
            and diff_across(raw_argvs)
            and diff_across(rebound_argvs)  # same both-differ rule as table():
            # a cosmetic own-name argv difference must not resurface as _raw
        ):
            # the flag map cannot represent the difference (repeated flags,
            # positional ordering) -> fail open to the raw argv
            args_diff["_raw"] = raw_argvs
        return envelope_response(
            ok=True,
            data={
                "runs": summaries,
                "config_diff": {
                    "args": args_diff,
                    "env": table(env_by_run, raw_env_by_run),
                    "submission": table(top(submissions), top(raw_submissions)),
                    "managed": table(manageds, raw_manageds),
                },
            },
        )

    @router.get("/projects/{project_id}/runs")
    def runs_index(
        project_id: str,
        experiment_id: str | None = Query(None),
        status: str | None = Query(None),
        fields: str | None = Query(None),
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        path = require_project(config, project_id)
        field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
        summaries = []
        for record in list_yaml_records(path / "runs", kind="run"):
            if record.get("_invalid"):
                if not experiment_id and not status:
                    summaries.append(record)
                continue
            if experiment_id and record.get("experiment_id") != experiment_id:
                continue
            run_name = record.get("run_name")
            progress = load_progress(path, run_name) if run_name else {}
            summary = {
                "run_name": run_name,
                "experiment_id": record.get("experiment_id"),
                "status": record.get("status"),
                "verdict": record.get("verdict"),
                "lifecycle_state": progress.get("lifecycle_state")
                if progress.get("ticks")
                else None,
                "managed": (path / "managed_runs" / f"{run_name}.yaml").is_file()
                if run_name
                else False,
            }
            if status and summary["status"] != status:
                continue
            summaries.append(select_fields(summary, field_list))
        window = summaries[offset : offset + limit]
        return envelope_response(
            ok=True,
            data={"runs": window, "total": len(summaries), "offset": offset, "limit": limit},
        )

    @router.get("/projects/{project_id}/runs/{run_name}")
    def run_detail(project_id: str, run_name: str) -> JSONResponse:
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        record = require_run_record(path, run_name)
        managed = load_managed_run_optional(path, run_name)
        progress = load_progress(path, run_name)
        container = inspect_training_container(
            path, managed, (record.get("submission") or {}).get("container_id")
        )
        run_dir = resolved_run_dir(path, run_name, managed, config)
        terminal_event = None
        checkpoints: list[dict[str, Any]] = []
        latest_metrics = None
        if run_dir is not None:
            metrics_path = resolve_metrics_path(run_dir)
            terminal_event = read_terminal_event(metrics_path)
            latest_metrics = read_last_train_metrics(metrics_path)
            checkpoints = [
                {"step": step, "name": p.name} for step, p in checkpoint_steps(run_dir)
            ]
        return envelope_response(
            ok=True,
            data={
                "run": record,
                "managed_run": managed,
                "progress": progress if progress.get("ticks") else None,
                "container": container,
                "derived_status": derive_status(
                    declared=record.get("status"),
                    container=container,
                    progress=progress,
                    terminal_event=terminal_event,
                ),
                "terminal_event": terminal_event,
                "checkpoints": checkpoints,
                "latest_metrics": latest_metrics,
            },
        )

    @router.get("/projects/{project_id}/runs/{run_name}/status")
    async def run_status(
        project_id: str,
        run_name: str,
        wait: str | None = Query(
            None, description="'state_change' long-polls until derived_status changes"
        ),
        timeout: int = Query(120, ge=1, le=300),
        from_status: str | None = Query(
            None,
            alias="from",
            description="baseline derived_status (default: computed at request start)",
        ),
    ) -> JSONResponse:
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)  # 404 before we start holding the request

        def status_payload() -> dict[str, Any]:
            # re-read the record too: status is derived per POLL, not per request —
            # a resubmit mid-poll changes declared status and container_id
            record = require_run_record(path, run_name)
            managed = load_managed_run_optional(path, run_name)
            progress = load_progress(path, run_name)
            container = inspect_training_container(
                path, managed, (record.get("submission") or {}).get("container_id")
            )
            run_dir = resolved_run_dir(path, run_name, managed, config)
            terminal_event = None
            latest = None
            if run_dir is not None:
                metrics_path = resolve_metrics_path(run_dir)
                terminal_event = read_terminal_event(metrics_path)
                latest = read_last_train_metrics(metrics_path)
            return {
                "run_name": run_name,
                "derived_status": derive_status(
                    declared=record.get("status"),
                    container=container,
                    progress=progress,
                    terminal_event=terminal_event,
                ),
                "container": {
                    key: container.get(key)
                    for key in ("running", "exists", "state", "exit_code", "started_at")
                },
                "latest_step": latest.get("step") if latest else None,
                "latest_loss": latest.get("loss") if latest else None,
                "qc_done_steps": progress.get("qc_done_steps", []),
                "terminal_event": terminal_event,
            }

        if wait is not None and wait != "state_change":
            raise OperationError(
                "run.wait_invalid",
                "only wait=state_change is supported",
                {"wait": wait},
            )
        if from_status is not None and from_status not in DERIVED_STATUSES:
            # a typo'd baseline can never match -> every poll returns instantly and
            # long-poll silently degrades to busy-polling; fail closed instead
            raise OperationError(
                "run.from_invalid",
                "from= must be a derived status",
                {"from": from_status, "valid": list(DERIVED_STATUSES)},
            )
        data = await anyio.to_thread.run_sync(status_payload)
        if wait is None:
            return envelope_response(ok=True, data=data)
        # Long-poll: one held request instead of N client polls. The blocking
        # docker/file reads run in the threadpool, so the event loop stays free.
        baseline = from_status or data["derived_status"]
        waited = 0.0
        while data["derived_status"] == baseline and waited < timeout:
            step_s = min(5.0, timeout - waited)
            await asyncio.sleep(step_s)
            waited += step_s
            data = await anyio.to_thread.run_sync(status_payload)
        data["changed"] = data["derived_status"] != baseline
        data["baseline"] = baseline
        data["waited_sec"] = round(waited, 1)
        return envelope_response(ok=True, data=data)

    @router.get("/projects/{project_id}/runs/{run_name}/logs")
    def run_logs(
        project_id: str, run_name: str, tail: int = Query(200, ge=1, le=5000)
    ) -> JSONResponse:
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)
        managed = load_managed_run_optional(path, run_name)
        if not managed:
            raise OperationError(
                "run.not_managed",
                "run has no managed_run record, so no container to read logs from",
                {"run_name": run_name},
            )
        container = load_container_record(path, managed["training_container_id"])
        name = resolve_text_ref(docker_name_from_container(container, ""))
        found, stdout, stderr = docker_logs_by_name(
            {"project_root": str(path)}, name, tail=tail
        )
        if not found:
            raise OperationError(
                "run.container_not_found",
                "training container does not exist (torn down or never started)",
                {"run_name": run_name, "container_name": name},
            )
        lines = (stdout.splitlines() + stderr.splitlines())[-tail:]
        return envelope_response(
            ok=True, data={"run_name": run_name, "lines": lines, "tail": tail}
        )

    @router.get("/projects/{project_id}/runs/{run_name}/metrics")
    def run_metrics(
        project_id: str,
        run_name: str,
        keys: str | None = Query(None, description="comma-separated series names"),
        since_step: int = Query(0, ge=0),
        max_points: int = Query(500, ge=2, le=20000),
        every: int | None = Query(None, ge=1),
    ) -> JSONResponse:
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)
        metrics_path = require_metrics_path(path, run_name, config)
        key_list = [k.strip() for k in keys.split(",") if k.strip()] if keys else None
        payload = read_metrics_columnar(
            metrics_path,
            keys=key_list,
            since_step=since_step,
            max_points=max_points,
            every=every,
        )
        return envelope_response(
            ok=True, data={"run_name": run_name, "source": "metrics.jsonl", **payload}
        )

    @router.get("/projects/{project_id}/experiments/{experiment_id}/metrics")
    def experiment_metrics(
        project_id: str,
        experiment_id: str,
        keys: str = Query("loss"),
        max_points: int = Query(200, ge=2, le=5000),
    ) -> JSONResponse:
        path = require_project(config, project_id)
        experiment_id = require_safe_id(experiment_id, kind="experiment")
        if not (path / "experiments" / f"{experiment_id}.yaml").is_file():
            raise OperationError(
                "experiment.not_found",
                f"no experiment '{experiment_id}' in project '{path.name}'",
                {"experiment_id": experiment_id},
            )
        key_list = [k.strip() for k in keys.split(",") if k.strip()]
        runs_payload: dict[str, Any] = {}
        for record in list_yaml_records(path / "runs", kind="run"):
            if record.get("experiment_id") != experiment_id:
                continue
            run_name = record.get("run_name")
            if not run_name:
                continue
            try:
                metrics_path = require_metrics_path(path, run_name, config)
            except OperationError:
                runs_payload[run_name] = None
                continue
            runs_payload[run_name] = read_metrics_columnar(
                metrics_path, keys=key_list, max_points=max_points
            )
        return envelope_response(
            ok=True,
            data={"experiment_id": experiment_id, "keys": key_list, "runs": runs_payload},
        )

    @router.get("/projects/{project_id}/runs/{run_name}/events")
    def run_events(
        project_id: str, run_name: str, since_seq: int = Query(0, ge=0)
    ) -> JSONResponse:
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)
        progress = load_progress(path, run_name)
        managed = load_managed_run_optional(path, run_name)
        run_dir = resolved_run_dir(path, run_name, managed, config)
        terminal_event = (
            read_terminal_event(resolve_metrics_path(run_dir)) if run_dir is not None else None
        )
        # STRUCTURAL seqs, not positional ones: qc_done_steps keeps growing after the
        # terminal row exists (the reconciler QCs the final checkpoint after the trainer
        # writes 'done'), so enumeration indexes would shift — duplicating the terminal
        # event and losing late QC events for a since_seq poller. seq = the step itself
        # for QC events; terminal/finalized get fixed sentinels above any real step.
        terminal_seq = 10**9 + 1
        finalized_seq = 10**9 + 2
        numbered: list[dict[str, Any]] = [
            {"seq": step, "kind": "qc_delivered", "step": step}
            for step in progress.get("qc_done_steps", [])
        ]
        if terminal_event:
            numbered.append({"seq": terminal_seq, "kind": "terminal", "event": terminal_event})
        if progress.get("finalized"):
            numbered.append({"seq": finalized_seq, "kind": "finalized"})
        numbered.sort(key=lambda e: e["seq"])
        fresh = [event for event in numbered if event["seq"] > since_seq]
        # last_seq is the SAFE resume cursor. Between the trainer writing the terminal
        # row and the reconciler QCing the final checkpoint, later qc_delivered events
        # will appear with seqs BELOW the terminal sentinel — so while that window is
        # open, the cursor must stay at the highest QC step or a poller using
        # since_seq=last_seq would never see the final QC.
        qc_seqs = [e["seq"] for e in numbered if e["kind"] == "qc_delivered"]
        if terminal_event and not progress.get("finalized"):
            last_seq = max(qc_seqs) if qc_seqs else 0
        else:
            last_seq = numbered[-1]["seq"] if numbered else 0
        return envelope_response(
            ok=True,
            data={
                "run_name": run_name,
                "events": fresh,
                "last_seq": last_seq,
            },
        )

    return router
