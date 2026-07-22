"""Typed run submission, stop, and the generic operations escape hatch.

Submission is the heart of the agent workflow: the agent names registered pieces
(container profile, bundle, entrypoint, data sources) and the server constructs the
``script_bundle_run`` operation itself and executes it in-process (trusted caller, same
precedent as the reconciler — no guard-receipt dance, because the request never exists
as a human-editable file). ``runs/<run_name>.yaml`` records the submission with its
canonical ``request_sha256``; ``managed_runs/<run_name>.yaml`` is auto-created so the
unmodified reconciler takes over QC / retention / finalize. The agent never sees docker.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from kikai_lab.envelope import error, next_action
from kikai_lab.operation import (
    OperationError,
    docker_inspect_by_name,
    docker_name_from_container,
    docker_ps_all,
    docker_rm_force,
    ephemeral_child_name_regex,
    execute_operation,
    load_container_record,
    load_script_bundle,
    operation_data_source_ref_preflight,
    request_sha256,
    resolve_text_ref,
    script_bundle_entrypoint_argv,
)
from kikai_lab.reconcile import control_path, progress_path, read_control
from kikai_lab.server.app import envelope_response
from kikai_lab.server.registry import (
    WRITE_LOCK,
    ServerConfig,
    append_journal,
    atomic_write_json,
    atomic_write_yaml,
    load_yaml_record,
    require_active_project,
    require_project,
    require_safe_id,
    utc_now_text,
    validate_record_schema,
)
from kikai_lab.server.runs import load_managed_run_optional, require_run_record

# Declared statuses a same-body resubmit may relaunch/adopt from: a failed launch,
# or a crash between docker starting and the final record write (H2 recovery).
SUBMISSION_RETRYABLE_DECLARED = ("submit_failed", "submitting")


def build_submit_op(
    project_root: Path, run_name: str, body: dict[str, Any]
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "adapter": "script_bundle_run",
        "operation": f"{run_name}_submit",
        "project_root": str(project_root),
        "bundle_id": body["bundle_id"],
        "container_id": body["container_id"],
        "entrypoint": body["entrypoint"],
        "detach": True,
        "args": list(body.get("args") or []),
        "env": dict(body.get("env") or {}),
    }
    request["data_source_refs"] = list(body.get("data_source_refs") or [])
    return {"kind": "kikai_operation", "schema_version": 1, "request": request}


def preserve_analysis(run_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Carry conclusions/verdict from the existing record into a rebuilt one.

    submission_record() rebuilds the run record from the request body; without this,
    a retry or the post-launch status write would silently erase the append-only
    analysis trail — the exact value the conclusion endpoint exists to keep."""
    if run_path.is_file():
        try:
            existing = load_yaml_record(run_path, kind="run")
        except OperationError:
            return record
        for key in ("conclusions", "verdict"):
            if key in existing and key not in record:
                record[key] = existing[key]
    return record


def submission_record(
    run_name: str,
    body: dict[str, Any],
    sha: str,
    *,
    status: str,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": 1,
        "run_name": run_name,
        "status": status,
        "submission": {
            "at": utc_now_text(),
            "request_sha256": sha,
            "bundle_id": body["bundle_id"],
            "container_id": body["container_id"],
            "entrypoint": body["entrypoint"],
            "args": list(body.get("args") or []),
            "env": dict(body.get("env") or {}),
            "host_ref": body.get("host_ref") or "local",
        },
    }
    if body.get("experiment_id"):
        record["experiment_id"] = body["experiment_id"]
    if lineage:
        record["submission"]["parent_run"] = lineage.get("parent_run")
        record["submission"]["overrides"] = lineage.get("overrides") or {}
        if lineage.get("probe"):
            record["probe"] = lineage["probe"]
    if body.get("run_dir"):
        record["submission"]["run_dir"] = body["run_dir"]
    if body.get("resume") is not None:
        record["fresh_no_resume"] = bool(body["resume"].get("fresh_no_resume", False))
    record["data_source_refs"] = list(body.get("data_source_refs") or [])
    return record


def _validate_bundle_entrypoint_ref(
    path: Path, bundle_id: str, entrypoint: str, *, where: str
) -> None:
    """Wrap load_script_bundle + script_bundle_entrypoint_argv with a context tag
    on the error so a typo in a qc_op or probe reports which reference failed —
    a common accident where an entrypoint typo in qc_op only surfaced at the
    reconciler's first tick (many checkpoints of silent QC failure) is prevented
    by running this at submit time."""
    try:
        bundle, _ = load_script_bundle(path, bundle_id)
        script_bundle_entrypoint_argv(bundle, bundle_id, entrypoint)
    except OperationError as exc:
        details = dict(exc.details or {})
        # preserve an inner 'where' if the inner code already set one (rare but
        # possible in nested validation) — this outer context becomes 'outer_where'
        if "where" in details:
            details.setdefault("outer_where", where)
        else:
            details["where"] = where
        raise OperationError(exc.code, f"[{where}] {exc.message}", details) from exc


def _validate_probes_field(path: Path, managed: dict[str, Any]) -> None:
    """Static checks for managed.probes[]. Structural (types/uniqueness) + reference
    checks (each probe's bundle/entrypoint/container must exist). Missing references
    are the class of failure the reconciler discovers per-checkpoint; catching them
    here means a bad probe blows up the submit, not 60 downstream QC ticks."""
    probes = managed.get("probes")
    if probes is None:
        return
    if not isinstance(probes, list):
        raise OperationError(
            "run.record_invalid", "managed.probes must be an array",
            {"got_type": type(probes).__name__},
        )
    seen_ids: set[str] = set()
    for i, probe in enumerate(probes):
        if not isinstance(probe, dict):
            raise OperationError(
                "run.record_invalid",
                f"managed.probes[{i}] must be an object",
                {"index": i},
            )
        pid = probe.get("id")
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", pid):
            raise OperationError(
                "run.record_invalid",
                f"managed.probes[{i}].id must match [A-Za-z0-9._-]+",
                {"index": i, "id": pid},
            )
        if pid in seen_ids:
            raise OperationError(
                "run.record_invalid",
                f"managed.probes has duplicate id {pid!r} at index {i}",
                {"index": i, "id": pid},
            )
        seen_ids.add(pid)
        for req in ("bundle_id", "entrypoint", "container_id", "project_root"):
            if not probe.get(req):
                raise OperationError(
                    "run.record_invalid",
                    f"managed.probes[{i}].{req} is required (probe id={pid!r})",
                    {"index": i, "id": pid, "missing": req},
                )
        if not isinstance(probe.get("args"), list):
            raise OperationError(
                "run.record_invalid",
                f"managed.probes[{i}].args must be an array (probe id={pid!r})",
                {"index": i, "id": pid},
            )
        every = probe.get("every_steps")
        if every is not None and (not isinstance(every, int) or every <= 0):
            raise OperationError(
                "run.record_invalid",
                f"managed.probes[{i}].every_steps must be a positive integer",
                {"index": i, "id": pid, "every_steps": every},
            )
        # A user-supplied operation string MUST vary with step. Two ticks writing the
        # same operation id collide in the ops ledger and the second is silently
        # rejected — silently missing QC is the very failure mode probes[] exists to
        # eliminate. Omit `operation` to get the safe default (run_id + probe_id + step6).
        op_tpl = probe.get("operation")
        if isinstance(op_tpl, str) and (
            "{{step}}" not in op_tpl and "{{step6}}" not in op_tpl
        ):
            raise OperationError(
                "run.record_invalid",
                f"managed.probes[{i}].operation must embed {{step}} or {{step6}} to "
                "vary per checkpoint (omit for the safe default)",
                {"index": i, "id": pid, "operation": op_tpl},
            )
        # reference checks
        _validate_bundle_entrypoint_ref(
            path, str(probe["bundle_id"]), str(probe["entrypoint"]),
            where=f"managed.probes[{i}] id={pid!r}",
        )
        container_id = str(probe["container_id"])
        container = load_container_record(path, container_id)
        docker_name_from_container(container, container_id)


def _validate_qc_op_refs(path: Path, managed: dict[str, Any]) -> None:
    """The primary qc_op has the same class of typo risk as probes — a wrong
    entrypoint here silently fails on every checkpoint. Validate at submit."""
    qc_op = managed.get("qc_op")
    if not isinstance(qc_op, dict):
        return
    request = qc_op.get("request")
    if not isinstance(request, dict):
        return
    # only script_bundle_run / script_bundle_exec have bundle+entrypoint pairs at the
    # top level; nested operation_sequence steps are the author's responsibility (they
    # ship in bundles that MAY not be locally resolvable — same convention as today).
    adapter = request.get("adapter")
    if adapter in ("script_bundle_run", "script_bundle_exec"):
        bundle_id = request.get("bundle_id")
        entrypoint = request.get("entrypoint")
        if bundle_id and entrypoint:
            _validate_bundle_entrypoint_ref(
                path, str(bundle_id), str(entrypoint), where="managed.qc_op"
            )


def validate_submission_pieces(
    config: ServerConfig, path: Path, body: dict[str, Any]
) -> None:
    """Fail-closed reference checks so a typo dies at submit time, not launch time."""
    host_ref = body.get("host_ref")
    if host_ref not in (None, "local", config.host_id):
        raise OperationError(
            "operation.host_not_local",
            "this server only launches on its own host in v1; "
            "use the ssh fallback for other hosts",
            {"host_ref": host_ref, "host_id": config.host_id},
        )
    container = load_container_record(path, body["container_id"])
    docker_name_from_container(container, body["container_id"])
    bundle, _ = load_script_bundle(path, body["bundle_id"])
    script_bundle_entrypoint_argv(bundle, body["bundle_id"], body["entrypoint"])
    managed_for_refs = body.get("managed") or {}
    _validate_qc_op_refs(path, managed_for_refs)
    _validate_probes_field(path, managed_for_refs)
    experiment_id = body.get("experiment_id")
    if experiment_id and not (path / "experiments" / f"{experiment_id}.yaml").is_file():
        raise OperationError(
            "experiment.not_found",
            f"submission names an unregistered experiment '{experiment_id}'",
            {"experiment_id": experiment_id},
        )
    managed = body.get("managed")
    if managed is not None and not body.get("run_dir"):
        raise OperationError(
            "run.record_invalid",
            "managed submissions require run_dir (the daemon-local run directory)",
            {"run_name": body.get("run_name", "")},
        )


def managed_run_record(run_name: str, body: dict[str, Any]) -> dict[str, Any]:
    managed = body.get("managed") or {}
    record: dict[str, Any] = {
        "schema_version": 1,
        "kind": "managed_run",
        "run_id": run_name,
        "run_dir": body["run_dir"],
        "training_container_id": body["container_id"],
    }
    if body.get("experiment_id"):
        # checkpoint_retention falls back to the experiment's retention block via
        # managed_run.experiment_id — omit it and inheritance silently never happens.
        record["experiment_id"] = body["experiment_id"]
    for key in (
        "max_step",
        "poll_interval_sec",
        "lifecycle",
        "delivery_target_id",
        "qc_artifacts_dir",
    ):
        if managed.get(key) is not None:
            record[key] = managed[key]
    for key in ("retention", "qc_op_template", "qc_op", "evaluations", "metric_checks", "probes"):
        if managed.get(key) is not None:
            record[key] = managed[key]
    validate_record_schema(record, "managed_run", kind="managed_run")
    return record


def reconstruct_submit_body(
    project_root: Path, parent_run: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The parent's full submit body, rebuilt from its run record + managed_run,
    plus wire-level warnings about anything that could not be faithfully rebuilt."""
    run_path = project_root / "runs" / f"{parent_run}.yaml"
    if not run_path.is_file():
        raise OperationError(
            "run.not_found",
            f"no parent run '{parent_run}' to inherit from",
            {"run_name": parent_run},
        )
    record = load_yaml_record(run_path, kind="run")
    submission = record.get("submission") or {}
    if not submission.get("bundle_id"):
        raise OperationError(
            "run.record_invalid",
            "parent run has no submission block (hand-written record?) — "
            "submit-from needs an API-submitted parent",
            {"run_name": parent_run},
        )
    warnings: list[dict[str, Any]] = []
    if "env" not in submission:
        # records written before env persistence: "inherit everything" would be
        # silently false for the environment — say so on the wire.
        warnings.append(
            error(
                "run.parent_env_unrecorded",
                "parent predates env recording; env reconstructed as empty — "
                "pass env in overrides if the parent used one",
                blocking=False,
                details={"parent_run": parent_run},
            )
        )
    body: dict[str, Any] = {
        "container_id": submission.get("container_id"),
        "bundle_id": submission.get("bundle_id"),
        "entrypoint": submission.get("entrypoint"),
        "args": list(submission.get("args") or []),
        "env": dict(submission.get("env") or {}),
        "host_ref": submission.get("host_ref") or "local",
    }
    if record.get("experiment_id"):
        body["experiment_id"] = record["experiment_id"]
    if record.get("data_source_refs"):
        body["data_source_refs"] = record["data_source_refs"]
    if "fresh_no_resume" in record:
        body["resume"] = {"fresh_no_resume": bool(record["fresh_no_resume"])}
    if submission.get("run_dir"):
        body["run_dir"] = submission["run_dir"]
    managed_path = project_root / "managed_runs" / f"{parent_run}.yaml"
    if managed_path.is_file():
        managed = load_yaml_record(managed_path, kind="managed_run")
        body["managed"] = {
            k: managed[k]
            for k in (
                "max_step",
                "poll_interval_sec",
                "lifecycle",
                "delivery_target_id",
                "retention",
                "qc_op_template",
                "qc_op",
                "qc_artifacts_dir",
                "evaluations",
                "metric_checks",
            )
            if k in managed
        }
    return body, warnings


ID_TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")


def rebind_run_name(
    value: Any, parent: str, child: str, *, sibling_runs: frozenset[str] = frozenset()
) -> Any:
    """Replace the parent run's name with the child's in every string of the body —
    run_dir, QC out-prefixes, operation names, labels.

    Plain substring replacement corrupts references to OTHER runs whose names embed
    the parent's (run_1 inside run_10's checkpoint path). So the parent name only
    matches when not flanked by alphanumerics: run_1 never matches inside run_10,
    while derived names (run_1_qc_000500) still rebind. The remaining ambiguity —
    a string token that is itself ANOTHER registered run extending the parent with a
    delimiter (parent example_run vs a referenced sibling example_run_v2) — is fail-closed:
    422, override that field explicitly."""
    occurrence = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(parent)}(?![A-Za-z0-9])"
    )
    # only siblings whose own name embeds the parent can be corrupted by the
    # substitution — guard those as boundary-occurrences INSIDE the token, so a
    # sibling's derived artifacts (run_1-old_ckpt for sibling run_1-old) are
    # protected too, not just exact-name tokens
    risky_siblings = [
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])")
        for s in sibling_runs
        if s != parent and occurrence.search(s)
    ]

    def rebind_token(match: re.Match[str]) -> str:
        token = match.group()
        if not occurrence.search(token):
            return token
        for sibling_pattern in risky_siblings:
            if sibling_pattern.search(token):
                raise OperationError(
                    "run.rebind_invalid",
                    "inherited body references another registered run whose name "
                    "embeds the parent's — rebinding would corrupt it; override "
                    "that field explicitly in overrides",
                    {"token": token, "parent_run": parent},
                )
        return occurrence.sub(child, token)

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return ID_TOKEN.sub(rebind_token, node)
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    return walk(value)


def apply_overrides(body: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge caller overrides onto the inherited body.

    Special forms: ``args_set`` upserts flag values ({"--vgg-weight": "5.0"}; a null
    value REMOVES the flag and ALL its values, "" strips values leaving a bare flag,
    a list sets a multi-value (nargs) flag), everything else replaces the top-level
    key (``managed`` merges one level deep). Only the first occurrence of a repeated
    flag is touched; values that themselves start with "--" cannot be expressed —
    replace ``args`` wholesale for those."""

    def as_arg(item: Any) -> str:
        if item is True:
            return "true"
        if item is False:
            return "false"
        return str(item)

    def as_values(flag_value: Any) -> list[str]:
        if flag_value == "":
            return []
        if isinstance(flag_value, list):
            return [as_arg(v) for v in flag_value]
        return [as_arg(flag_value)]

    merged = dict(body)
    for key, value in overrides.items():
        if key == "args_set":
            args = list(merged.get("args") or [])
            for flag, flag_value in (value or {}).items():
                # equals-form occurrences (--flag=value) are superseded first, so an
                # upsert/remove owns the flag regardless of the parser's precedence
                # (otherwise "the server's injection wins" is parser-conditional)
                args = [a for a in args if not str(a).startswith(f"{flag}=")]
                if flag in args:
                    idx = args.index(flag)
                    end = idx + 1  # consume ALL of a nargs flag's values, not just one
                    while end < len(args) and not str(args[end]).startswith("--"):
                        end += 1
                    if flag_value is None:
                        del args[idx:end]
                    else:
                        args[idx + 1 : end] = as_values(flag_value)
                elif flag_value is not None:
                    args.append(flag)
                    args.extend(as_values(flag_value))
            merged["args"] = args
        elif (
            key == "managed"
            and isinstance(value, dict)
            and isinstance(merged.get("managed"), dict)
        ):
            merged["managed"] = {**merged["managed"], **value}
        else:
            merged[key] = value
    return merged


def resolve_parent_checkpoint(
    config: ServerConfig, project_root: Path, parent_run: str, which: Any
) -> tuple[str, int]:
    """(filename, step) of the parent checkpoint a probe warm-starts from.

    which: "best" (newest best_step_*; the curated family carries the step in its
    name — best_checkpoint.pt does not, and the server will not unpickle weights;
    FALLS BACK to the newest periodic checkpoint_step_* when no best_step_* exists),
    "latest" (newest checkpoint_step_*), or an int step (exact checkpoint_step match).
    Resolution is server-side and fail-closed: no checkpoint, no launch; filenames
    outside the safe charset never reach argv."""
    from kikai_lab.operation import checkpoint_step_from_name
    from kikai_lab.server.runs import resolved_run_dir

    managed = load_managed_run_optional(project_root, parent_run)
    run_dir = resolved_run_dir(project_root, parent_run, managed, config)
    ckpt_dir = Path(run_dir) / "checkpoints" if run_dir is not None else None
    if ckpt_dir is None or not ckpt_dir.is_dir():
        raise OperationError(
            "run.probe_checkpoint_missing",
            "parent run has no resolvable checkpoints directory",
            {"parent_run": parent_run},
        )

    def family(prefix: str) -> list[tuple[int, str]]:
        out = []
        for f in ckpt_dir.glob(f"{prefix}_*.pt"):
            step = checkpoint_step_from_name(f)
            if step is not None:
                out.append((step, f.name))
        return sorted(out)

    candidates: list[tuple[int, str]]
    if which in (None, "best"):
        candidates = family("best_step") or family("checkpoint_step")
    elif which == "latest":
        candidates = family("checkpoint_step")
    elif isinstance(which, int) and not isinstance(which, bool):
        candidates = [(s, n) for s, n in family("checkpoint_step") if s == int(which)]
    else:
        raise OperationError(
            "run.probe_checkpoint_invalid",
            "checkpoint must be 'best', 'latest', or an integer step",
            {"checkpoint": which},
        )
    if not candidates:
        raise OperationError(
            "run.probe_checkpoint_missing",
            "no matching parent checkpoint for the probe to warm-start from",
            {"parent_run": parent_run, "checkpoint": which},
        )
    step, name = candidates[-1]
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", name):
        raise OperationError(
            "run.probe_checkpoint_invalid",
            "checkpoint filename contains characters unsafe for argv/env expansion",
            {"name": name},
        )
    return name, step


def arg_value_after(args: list[Any], flag: str) -> str | None:
    """Value of ``--flag value`` or ``--flag=value`` (first occurrence)."""
    items = [str(a) for a in args]
    for i, item in enumerate(items):
        if item == flag:
            if i + 1 < len(items) and not items[i + 1].startswith("--"):
                return items[i + 1]
            return None
        if item.startswith(f"{flag}="):
            return item[len(flag) + 1 :]
    return None


def ensure_run_dir_relocated(
    parent_body: dict[str, Any],
    child_body: dict[str, Any],
    run_name: str,
    run_dir_arg: str = "--run-dir",
) -> None:
    """Fail closed if the child would write into the PARENT's run_dir.

    rebind_run_name only rewrites occurrences of the parent's RUN NAME. When a
    run_dir uses a different naming scheme than the run name (e.g. run name
    ``example_run_v2`` but run_dir ``.../example_renderer/run``), the
    rebind is a no-op and the child silently inherits the parent's run_dir —
    appending to its metrics, overwriting its checkpoints, and (via the child's
    retention) DELETING them. Require an explicit run_dir relocation instead."""
    parent_rd = parent_body.get("run_dir")
    if parent_rd and child_body.get("run_dir") == parent_rd:
        raise OperationError(
            "run.run_dir_relocation_invalid",
            "the child run_dir equals the parent's — the parent's run_dir naming "
            "does not contain its run name, so rebinding could not relocate it. "
            "Writing there would corrupt the parent (metrics/checkpoints/retention). "
            "Pass an explicit run_dir override (and the matching trainer run-dir arg).",
            {"run_name": run_name, "run_dir": parent_rd},
        )
    parent_arg = arg_value_after(parent_body.get("args") or [], run_dir_arg)
    if parent_arg and arg_value_after(child_body.get("args") or [], run_dir_arg) == parent_arg:
        raise OperationError(
            "run.run_dir_relocation_invalid",
            f"the child's {run_dir_arg} still points at the parent's run dir; "
            "rebinding could not relocate it (run_dir naming != run name). Override "
            f"{run_dir_arg} (and run_dir) to a fresh path.",
            {"run_name": run_name, run_dir_arg: parent_arg},
        )


def notify_run_started(
    path: Path,
    run_name: str,
    body: dict[str, Any],
    lineage: dict[str, Any] | None,
) -> str | None:
    """Post a 'training started' notification to the run's delivery target.

    Best-effort by design: the container is already running, so a notification
    failure must never fail the submit — it returns a warning code instead.
    notification_id is per-run, so a crash-window resubmit does not double-post
    (*_record_exists is benign)."""
    target = (body.get("managed") or {}).get("delivery_target_id")
    if not target:
        return None
    args = [str(a) for a in body.get("args") or []]
    resume_ckpt = None
    if "--resume-checkpoint" in args:
        idx = args.index("--resume-checkpoint")
        if idx + 1 < len(args):
            resume_ckpt = Path(args[idx + 1]).name
    lines = [f"[{run_name}] training started"]
    detail = []
    if body.get("experiment_id"):
        detail.append(f"experiment={body['experiment_id']}")
    detail.append(f"start={'resume:' + resume_ckpt if resume_ckpt else 'fresh'}")
    max_step = (body.get("managed") or {}).get("max_step")
    if max_step:
        detail.append(f"max_step={max_step}")
    if body.get("bundle_id"):
        detail.append(f"bundle={body['bundle_id']}")
    lines.append(" ".join(detail))
    if lineage and lineage.get("parent_run"):
        probe = lineage.get("probe") or {}
        if probe.get("question"):
            q = str(probe["question"])[:200]
            lines.append(f'probe of {lineage["parent_run"]}: "{q}"')
        else:
            lines.append(f"derived from {lineage['parent_run']}")
    op = {
        "kind": "kikai_operation",
        "schema_version": 1,
        "request": {
            "adapter": "webhook_notification",
            "operation": f"{run_name}_submit_notify",
            "project_root": str(path),
            "notification_id": f"{run_name}_submit_notify",
            "delivery_target_id": target,
            "message": "\n".join(lines)[:1900],  # discord hard limit is 2000
            "severity": "info",
            "run_name": run_name,
        },
    }
    try:
        execute_operation(op)
    except OperationError as exc:
        if exc.code.endswith("_record_exists"):
            return None  # crash-window resubmit: already announced
        return exc.code
    except Exception as exc:  # noqa: BLE001 — the container is already running;
        # a corrupt hand-edited target record or a full disk must not 500 the submit
        return f"unexpected:{type(exc).__name__}"
    return None


def build_submit_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["submit"])

    @router.post("/projects/{project_id}/runs/{run_name}/submit")
    def run_submit(
        project_id: str,
        run_name: str,
        body: Annotated[dict[str, Any], Body()] = ...,
    ) -> JSONResponse:
        return do_submit(project_id, run_name, body, None)

    def do_submit(
        project_id: str,
        run_name: str,
        body: dict[str, Any],
        _lineage: dict[str, Any] | None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> JSONResponse:
        path = require_active_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        if not isinstance(body, dict):
            raise OperationError("run.record_invalid", "body must be a JSON object", {})
        validate_record_schema(body, "api_run_submission", kind="run")
        validate_submission_pieces(config, path, body)
        op = build_submit_op(path, run_name, body)
        # Guarded CLI ops get this preflight from the receipt machinery; the trusted
        # in-process path must run it explicitly or data_source_refs silently become
        # decoration (unknown ids / integrity mismatches must die BEFORE docker).
        operation_data_source_ref_preflight(op["request"])
        sha = request_sha256(op)

        if body.get("dry_run"):
            preview = {k: v for k, v in op["request"].items() if k != "project_root"}
            if _lineage:  # plain resubmit would drop lineage — point back at submit-from
                submit_cmd = (
                    f"POST /v1/projects/{path.name}/runs/{run_name}"
                    f"/submit-from/{_lineage['parent_run']}"
                )
            else:
                submit_cmd = f"POST /v1/projects/{path.name}/runs/{run_name}/submit"
            return envelope_response(
                ok=True,
                data={
                    "run_name": run_name,
                    "dry_run": True,
                    "request_sha256": sha,
                    "op_request": preview,
                },
                warnings=warnings or [],
                next_actions=[
                    next_action(
                        "submit",
                        "http_request",
                        "resend without dry_run to launch",
                        blocking=False,
                        command=submit_cmd,
                    )
                ],
            )

        run_path = path / "runs" / f"{run_name}.yaml"
        is_retry = False
        with WRITE_LOCK:
            require_active_project(config, project_id)
            if run_path.exists():
                existing = load_yaml_record(run_path, kind="run")
                existing_sha = (existing.get("submission") or {}).get("request_sha256")
                if existing_sha != sha:
                    raise OperationError(
                        "run.exists",
                        "run exists with a different submission; runs are immutable — "
                        "submit under a new run_name",
                        {"run_name": run_name},
                    )
                existing_sub = existing.get("submission") or {}
                if (
                    _lineage is None
                    and existing_sub.get("parent_run")
                    and existing_sub.get("request_sha256") == sha
                ):
                    # identical-body retry of a submit-from/probe child (e.g. after a
                    # crash window) must not erase its recorded lineage — nor the probe
                    # metadata that marks the run as budget-limited and question-bound
                    _lineage = {
                        "parent_run": existing_sub.get("parent_run"),
                        "overrides": existing_sub.get("overrides") or {},
                    }
                    if existing.get("probe"):
                        _lineage["probe"] = existing["probe"]
                if existing.get("status") not in SUBMISSION_RETRYABLE_DECLARED:
                    return envelope_response(
                        ok=True,
                        data={
                            "run_name": run_name,
                            "already_exists": True,
                            "declared_status": existing.get("status"),
                        },
                        next_actions=[status_next_action(path.name, run_name)],
                    )
                is_retry = True
            atomic_write_yaml(
                run_path,
                preserve_analysis(
                    run_path,
                    submission_record(
                        run_name, body, sha, status="submitting", lineage=_lineage
                    )
                ),
            )
            managed_created = False
            if body.get("managed") is not None:
                # BEFORE docker: if we crash mid-launch, the read plane and the
                # reconciler can still see/adopt the container via the managed_run.
                atomic_write_yaml(
                    path / "managed_runs" / f"{run_name}.yaml",
                    managed_run_record(run_name, body),
                )
                # A fresh submit must not inherit a previous life's daemon state:
                # a stale progress.json finalized=true short-circuits every tick
                # (no QC, no teardown, ever), and a stale control.json
                # force_finalize=true would cancel the new run's backfill and
                # finalize it the moment training exits. Mirror of the run_dir
                # control cleanup below, on the managed_runs side.
                (path / "managed_runs" / f"{run_name}.progress.json").unlink(missing_ok=True)
                (path / "managed_runs" / f"{run_name}.control.json").unlink(missing_ok=True)
                managed_created = True

        adopted = False
        try:
            # stale control (M2): a leftover control.json from the run_dir's
            # previous life (e.g. a graceful stop before a resume relaunch) would
            # be applied by the fresh trainer at its first metrics boundary — a
            # resumed run could silently stop itself. Clear it BEFORE docker;
            # if it cannot be removed, fail closed INSIDE this try so the failure
            # takes the same submit_failed cleanup (record + managed_run unlink +
            # journal) as any other pre-container launch error — never a ghost.
            # (Unlinked outside WRITE_LOCK: a concurrent control POST can recreate
            # the file before the container starts; ordering a control write
            # against an in-flight relaunch is inherently racy client-side.)
            from kikai_lab.server.runs import resolved_run_dir

            stale_managed = load_managed_run_optional(path, run_name)
            stale_run_dir = resolved_run_dir(path, run_name, stale_managed, config)
            if stale_run_dir is not None:
                stale_control = Path(stale_run_dir) / "control.json"
                if stale_control.exists():
                    try:
                        stale_control.unlink()
                    except OSError as exc:
                        raise OperationError(
                            "run.control_stale_unremovable",
                            "a stale control.json exists in the run_dir and could "
                            "not be removed (root-owned run dirs need the chown "
                            "repair); launching would let it drive the fresh "
                            "trainer",
                            {"run_name": run_name, "error": type(exc).__name__},
                        ) from exc
            result = execute_operation(op)
        except OperationError as exc:
            if exc.code == "operation.script_bundle_run_name_in_use" and is_retry:
                # An identical submission already started this container (crash after
                # docker run, before the final record write) — adopt it instead of
                # wedging on permanent retry. Adoption keys on (same sha + retryable
                # status + name collision); two runs sharing one container profile
                # alias the same docker name, which is a pre-existing constraint of
                # profile-named containers.
                adopted = True
                result = {"execution_status": "adopted_existing_container"}
            else:
                with WRITE_LOCK:
                    atomic_write_yaml(
                        run_path,
                        preserve_analysis(
                            run_path,
                            {
                                **submission_record(
                                    run_name,
                                    body,
                                    sha,
                                    status="submit_failed",
                                    lineage=_lineage,
                                ),
                                "submit_error": exc.code,
                            },
                        ),
                    )
                    # No container started: the pre-written managed_run would make the
                    # reconciler tick a ghost forever and the read plane would report
                    # 'submitted' over the declared failure.
                    (path / "managed_runs" / f"{run_name}.yaml").unlink(missing_ok=True)
                append_journal(
                    path,
                    "run_submit_failed",
                    {
                        "run_name": run_name,
                        "error": exc.code,
                        "parent_run": (_lineage or {}).get("parent_run"),
                    },
                )
                if exc.code == "operation.script_bundle_run_name_in_use":
                    raise OperationError(
                        exc.code,
                        exc.message + " (stop the run holding this container first)",
                        {
                            **exc.details,
                            "next": f"POST /v1/projects/{path.name}/runs/{run_name}/stop",
                        },
                    ) from exc
                raise

        with WRITE_LOCK:
            record = submission_record(
                run_name, body, sha, status="running", lineage=_lineage
            )
            # the DOCKER id from `docker run -d` stdout — result["container_id"] is
            # the container PROFILE id, already stored as submission.container_id
            record["submission"]["started_container_id"] = result.get(
                "started_container_id"
            )
            atomic_write_yaml(run_path, preserve_analysis(run_path, record))
            atomic_write_json(
                path / "ops" / f"{run_name}_submit.json",
                {**op, "result_summary": {"execution_status": result.get("execution_status")}},
            )
        append_journal(
            path,
            "run_submitted",
            {
                "run_name": run_name,
                "adopted": adopted,
                "parent_run": (_lineage or {}).get("parent_run"),
            },
        )
        notify_error = notify_run_started(path, run_name, body, _lineage)
        if notify_error:
            (warnings := warnings or []).append(
                error(
                    "run.start_notification_failed",
                    "the run launched but its start notification could not be "
                    "delivered",
                    blocking=False,
                    details={"error": notify_error},
                )
            )
        return envelope_response(
            ok=True,
            data={
                "run_name": run_name,
                "submitted": True,
                "adopted_existing_container": adopted,
                "request_sha256": sha,
                "managed_run_created": managed_created,
                "derived_status": "running",
            },
            warnings=warnings or [],
            next_actions=[
                status_next_action(path.name, run_name),
                next_action(
                    "watch_metrics",
                    "http_request",
                    "poll loss once training is stepping",
                    blocking=False,
                    command=(
                        f"GET /v1/projects/{path.name}/runs/{run_name}/metrics"
                        "?keys=loss&max_points=200"
                    ),
                ),
            ],
            status_code=201,
        )

    @router.post("/projects/{project_id}/runs/{run_name}/submit-from/{parent_run}")
    def run_submit_from(
        project_id: str,
        run_name: str,
        parent_run: str,
        body: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> JSONResponse:
        """Differential submission: inherit the parent run's full configuration,
        rebind every occurrence of the parent's name to the new run (run_dir, QC
        paths/labels...), apply the caller's overrides, and record lineage — one
        small call instead of resending a 60-line body, with the one-variable
        discipline enforced by construction."""
        path = require_active_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        parent_run = require_safe_id(parent_run, kind="run")
        body = body if isinstance(body, dict) else {}
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise OperationError(
                "run.record_invalid", "overrides must be a JSON object", {}
            )
        parent_body, reconstruct_warnings = reconstruct_submit_body(path, parent_run)
        siblings = frozenset(
            f.stem for f in (path / "runs").glob("*.yaml") if f.stem != parent_run
        )
        # rebind ONLY run-derived fields; identity keys (bundle_id, container_id,
        # experiment_id, data_source_refs) name registered resources, never the run
        rebound = dict(parent_body)
        for field in ("args", "env", "run_dir", "managed"):
            if field in rebound:
                rebound[field] = rebind_run_name(
                    rebound[field], parent_run, run_name, sibling_runs=siblings
                )
        merged = apply_overrides(rebound, overrides)
        # submit-from only knows the conventional "--run-dir" flag for the arg-level
        # check; other trainer flag names rely on the managed-run_dir check (which
        # catches the primary retention/reconciler danger).
        ensure_run_dir_relocated(parent_body, merged, run_name)
        merged["dry_run"] = bool(body.get("dry_run"))
        return do_submit(
            project_id,
            run_name,
            merged,
            {"parent_run": parent_run, "overrides": overrides},
            warnings=reconstruct_warnings,
        )

    @router.post("/projects/{project_id}/runs/{run_name}/probe-from/{parent_run}")
    def run_probe_from(
        project_id: str,
        run_name: str,
        parent_run: str,
        body: Annotated[dict[str, Any], Body()] = ...,
    ) -> JSONResponse:
        """PROBE: a short, checkpoint-warm-started run that answers ONE question
        before anyone pays for a fresh full run. Inherits the parent's config
        (submit-from machinery), auto-injects --resume-checkpoint pointing at the
        parent's best/latest checkpoint (container path) and --max-steps =
        resume_step + probe_steps, defaults retention to 1+1, and records
        probe metadata (question, budget) on the run. Declare metric_checks with
        window_steps_relative: true so gates judge offsets from the resume point."""
        path = require_active_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        parent_run = require_safe_id(parent_run, kind="run")
        if not isinstance(body, dict):
            raise OperationError("run.record_invalid", "body must be a JSON object", {})
        question = str(body.get("question") or "").strip()
        if not question:
            raise OperationError(
                "run.probe_invalid",
                "a probe MUST state its question (that is the point of a probe)",
                {"run_name": run_name},
            )
        probe_steps = body.get("probe_steps")
        if not isinstance(probe_steps, int) or isinstance(probe_steps, bool) or not (
            0 < probe_steps <= 20000
        ):
            raise OperationError(
                "run.probe_invalid",
                "probe_steps must be an integer in 1..20000 — longer is not a probe",
                {"probe_steps": probe_steps},
            )
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise OperationError(
                "run.record_invalid", "overrides must be a JSON object", {}
            )
        if overrides.get("managed") is not None and not isinstance(
            overrides["managed"], dict
        ):
            raise OperationError(
                "run.record_invalid", "overrides.managed must be a JSON object", {}
            )
        resume_arg = str(body.get("resume_arg") or "--resume-checkpoint")
        max_steps_arg = str(body.get("max_steps_arg") or "--max-steps")
        run_dir_arg = str(body.get("run_dir_arg") or "--run-dir")

        ckpt_name, resume_step = resolve_parent_checkpoint(
            config, path, parent_run, body.get("checkpoint", "best")
        )
        parent_body, reconstruct_warnings = reconstruct_submit_body(path, parent_run)
        # the parent's CONTAINER run dir (pre-rebind!) anchors the checkpoint path
        parent_container_run_dir = arg_value_after(
            parent_body.get("args") or [], run_dir_arg
        )
        if not parent_container_run_dir:
            raise OperationError(
                "run.probe_invalid",
                f"parent args carry no '{run_dir_arg}' value to anchor the container "
                "checkpoint path — pass run_dir_arg naming your trainer's flag",
                {"parent_run": parent_run},
            )
        container_ckpt = f"{parent_container_run_dir}/checkpoints/{ckpt_name}"

        siblings = frozenset(
            f.stem for f in (path / "runs").glob("*.yaml") if f.stem != parent_run
        )
        rebound = dict(parent_body)
        for field in ("args", "env", "run_dir", "managed"):
            if field in rebound:
                rebound[field] = rebind_run_name(
                    rebound[field], parent_run, run_name, sibling_runs=siblings
                )
        merged = apply_overrides(rebound, overrides)
        # probe-authoritative knobs LAST — the endpoint owns resume/budget/lifecycle
        merged = apply_overrides(
            merged,
            {
                "args_set": {
                    resume_arg: container_ckpt,
                    max_steps_arg: str(resume_step + probe_steps),
                }
            },
        )
        managed = dict(merged.get("managed") or {})
        managed["max_step"] = resume_step + probe_steps
        if "retention" not in (overrides.get("managed") or {}):
            managed["retention"] = {"keep_latest": 1, "keep_best": 1}
        merged["managed"] = managed
        merged["resume"] = {"fresh_no_resume": False}
        # a probe that writes into the parent's run_dir would corrupt the very
        # checkpoint it resumed from — and probe retention (1+1) would prune it.
        ensure_run_dir_relocated(parent_body, merged, run_name, run_dir_arg)
        merged["dry_run"] = bool(body.get("dry_run"))
        return do_submit(
            project_id,
            run_name,
            merged,
            {
                "parent_run": parent_run,
                "overrides": overrides,
                "probe": {
                    "parent_run": parent_run,
                    "question": question,
                    "budget_steps": probe_steps,
                    "resume_step": resume_step,
                    "resume_checkpoint": container_ckpt,
                },
            },
            warnings=reconstruct_warnings,
        )

    @router.post("/projects/{project_id}/runs/{run_name}/control")
    def run_control_write(
        project_id: str,
        run_name: str,
        body: Annotated[dict[str, Any], Body()] = ...,
    ) -> JSONResponse:
        """Live control plane: change a RUNNING training's termination policy
        without a restart. Writes <run_dir>/control.json atomically; a
        control-plane-aware trainer applies it on its next metrics boundary and
        logs a control_applied event. Keys: max_steps / early_stop_patience
        (int > 0), early_stop_min_delta (number >= 0), stop: "graceful"
        (checkpoint + clean exit). max_steps also syncs managed_run.max_step so
        the daemon lifecycle follows the new cap."""
        from kikai_lab.server.runs import resolved_run_dir

        path = require_active_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)
        if not isinstance(body, dict) or not body:
            raise OperationError(
                "run.control_invalid",
                "control body must be a non-empty JSON object",
                {"run_name": run_name},
            )
        control: dict[str, Any] = {}
        for key in ("max_steps", "early_stop_patience"):
            if key in body:
                value = body[key]
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise OperationError(
                        "run.control_invalid",
                        f"{key} must be a positive integer",
                        {key: value},
                    )
                control[key] = value
        if "early_stop_min_delta" in body:
            value = body["early_stop_min_delta"]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise OperationError(
                    "run.control_invalid",
                    "early_stop_min_delta must be a non-negative number",
                    {"early_stop_min_delta": value},
                )
            control["early_stop_min_delta"] = float(value)
        if "stop" in body:
            if body["stop"] != "graceful":
                raise OperationError(
                    "run.control_invalid",
                    "stop supports only 'graceful' (checkpoint + clean exit); "
                    "for a hard kill use POST .../stop",
                    {"stop": body["stop"]},
                )
            control["stop"] = "graceful"
        unknown = sorted(set(body) - set(control))
        if unknown:
            raise OperationError(
                "run.control_invalid",
                "unknown control keys (whitelist: max_steps, early_stop_patience, "
                "early_stop_min_delta, stop)",
                {"unknown": unknown},
            )

        with WRITE_LOCK:
            managed = load_managed_run_optional(path, run_name)
            run_dir = resolved_run_dir(path, run_name, managed, config)
            if run_dir is None:
                raise OperationError(
                    "run.run_dir_missing",
                    "run declares no resolvable run_dir to carry a control file",
                    {"run_name": run_name},
                )
            control_path = Path(run_dir) / "control.json"
            tmp = control_path.with_suffix(".json.tmp")
            try:
                tmp.write_text(
                    json.dumps(control, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                tmp.replace(control_path)
            except FileNotFoundError as exc:
                raise OperationError(
                    "run.run_dir_missing",
                    "the run_dir does not exist yet — nothing is training there "
                    "to control",
                    {"run_name": run_name},
                ) from exc
            except OSError as exc:
                raise OperationError(
                    "run.control_write_failed",
                    "could not write control.json into the run_dir "
                    "(root-owned run dirs need the chown repair first)",
                    {"run_name": run_name, "error": type(exc).__name__},
                ) from exc
            if "max_steps" in control and managed is not None:
                managed_path = path / "managed_runs" / f"{run_name}.yaml"
                managed_record = load_yaml_record(managed_path, kind="managed_run")
                managed_record["max_step"] = control["max_steps"]
                atomic_write_yaml(managed_path, managed_record)
        control_warnings: list[dict[str, Any]] = []
        if "max_steps" in control:
            from kikai_lab.operation import resolve_metrics_path
            from kikai_lab.server.metrics import read_last_train_metrics

            metrics_path = resolve_metrics_path(Path(run_dir))
            latest = read_last_train_metrics(metrics_path) if metrics_path.exists() else None
            latest_step = latest.get("step") if latest else None
            if isinstance(latest_step, int) and control["max_steps"] <= latest_step:
                # the trainer will fall out of its loop labeled 'done' — an
                # operator truncation masquerading as natural completion
                control_warnings.append(
                    error(
                        "run.control_truncates",
                        "max_steps is at or below the last known step; the run "
                        "will end as 'completed' — use stop:'graceful' if an "
                        "intentional stop should read as one",
                        blocking=False,
                        details={
                            "max_steps": control["max_steps"],
                            "latest_step": latest_step,
                        },
                    )
                )
        append_journal(path, "run_control", {"run_name": run_name, **control})
        return envelope_response(
            ok=True,
            warnings=control_warnings,
            data={
                "run_name": run_name,
                "control": control,
                "managed_max_step_synced": "max_steps" in control
                and managed is not None,
                "note": "a control-plane-aware trainer applies this on its next "
                "metrics boundary; confirm via GET .../control (applied)",
            },
            next_actions=[
                next_action(
                    "confirm_applied",
                    "http_request",
                    "check the trainer acknowledged the change",
                    blocking=False,
                    command=f"GET /v1/projects/{path.name}/runs/{run_name}/control",
                )
            ],
            status_code=201,
        )

    @router.get("/projects/{project_id}/runs/{run_name}/control")
    def run_control_read(project_id: str, run_name: str) -> JSONResponse:
        """Requested control (control.json) vs what the trainer ACTUALLY applied
        (last control_applied event in metrics.jsonl). applied=null with a
        non-null requested means the trainer has not reached a metrics boundary
        yet — or predates the control plane entirely."""
        from kikai_lab.operation import resolve_metrics_path
        from kikai_lab.server.runs import resolved_run_dir

        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)
        managed = load_managed_run_optional(path, run_name)
        run_dir = resolved_run_dir(path, run_name, managed, config)
        requested = None
        applied = None
        if run_dir is not None:
            control_path = Path(run_dir) / "control.json"
            if control_path.is_file():
                try:
                    requested = json.loads(control_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    requested = {"_unreadable": True}
            metrics_path = resolve_metrics_path(Path(run_dir))
            if metrics_path.exists():
                try:
                    with metrics_path.open("r", encoding="utf-8") as f:
                        for line in f:
                            stripped = line.strip()
                            if not stripped or '"control_applied"' not in stripped:
                                continue
                            try:
                                row = json.loads(stripped)
                            except ValueError:
                                continue
                            if row.get("event") == "control_applied":
                                applied = {
                                    "step": row.get("step"),
                                    "applied": row.get("applied"),
                                    "ignored": row.get("ignored"),
                                }
                except OSError:
                    pass
        return envelope_response(
            ok=True,
            data={"run_name": run_name, "requested": requested, "applied": applied},
        )

    @router.post("/projects/{project_id}/runs/{run_name}/qc-config")
    def run_qc_config_update(
        project_id: str,
        run_name: str,
        body: Annotated[dict[str, Any], Body()] = ...,
    ) -> JSONResponse:
        """Update a managed run's per-checkpoint QC configuration (``probes`` /
        ``qc_op``) without a restart and without hand-editing the yaml. Partial
        update: only the keys present in the body are replaced (``null`` removes
        the key). The same submit-time validation applies — bad bundle /
        entrypoint / container references die here, not across 60 silent QC
        ticks. The reconciler reloads ``managed_runs/<run>.yaml`` every tick, so
        the new config drives the NEXT cycle; checkpoints whose QC/probe work is
        already recorded in progress.json are not re-run."""
        path = require_active_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        require_run_record(path, run_name)
        if not isinstance(body, dict) or not body:
            raise OperationError(
                "run.qc_config_invalid",
                "qc-config body must be a non-empty JSON object",
                {"run_name": run_name},
            )
        allowed = ("probes", "qc_op")
        unknown = sorted(set(body) - set(allowed))
        if unknown:
            raise OperationError(
                "run.qc_config_invalid",
                "unknown qc-config keys (whitelist: probes, qc_op)",
                {"unknown": unknown},
            )
        if body.get("qc_op") is not None and not isinstance(body["qc_op"], dict):
            raise OperationError(
                "run.qc_config_invalid",
                "qc_op must be an object (a kikai_operation) or null to remove",
                {"got_type": type(body["qc_op"]).__name__},
            )

        with WRITE_LOCK:
            require_active_project(config, project_id)
            managed_path = path / "managed_runs" / f"{run_name}.yaml"
            if not managed_path.is_file():
                raise OperationError(
                    "run.managed_run_missing",
                    "run has no managed_run record — QC config only applies to "
                    "managed submissions",
                    {"run_name": run_name},
                )
            record = load_yaml_record(managed_path, kind="managed_run")
            previous_probe_ids = {
                p.get("id")
                for p in (record.get("probes") or [])
                if isinstance(p, dict)
            }
            updated: list[str] = []
            removed: list[str] = []
            for key in allowed:
                if key not in body:
                    continue
                if body[key] is None:
                    if key in record:
                        del record[key]
                        removed.append(key)
                else:
                    record[key] = body[key]
                    updated.append(key)
            # Validate the MERGED record, same checks as submit time: a partial
            # update must not be able to write a managed_run that submit would
            # have rejected.
            _validate_qc_op_refs(path, record)
            _validate_probes_field(path, record)
            validate_record_schema(record, "managed_run", kind="managed_run")
            atomic_write_yaml(managed_path, record)

        qc_warnings: list[dict[str, Any]] = []
        new_probe_ids = sorted(
            {
                str(p["id"])
                for p in (record.get("probes") or [])
                if isinstance(p, dict) and p.get("id") is not None
            }
            - {str(pid) for pid in previous_probe_ids if pid is not None}
        )
        if new_probe_ids:
            # progress.json tracks done steps per probe id — an id the reconciler
            # has never seen has zero done steps, so it backfills every retained
            # checkpoint on the next tick. Intentional, but say so on the wire.
            qc_warnings.append(
                error(
                    "run.qc_config_probe_backfill",
                    "new probe ids run against every retained checkpoint not "
                    "yet probed (backfill on the next reconcile tick)",
                    blocking=False,
                    details={"new_probe_ids": new_probe_ids},
                )
            )
        append_journal(
            path,
            "run_qc_config_updated",
            {"run_name": run_name, "updated": updated, "removed": removed},
        )
        return envelope_response(
            ok=True,
            warnings=qc_warnings,
            data={
                "run_name": run_name,
                "updated": updated,
                "removed": removed,
                "probes": record.get("probes"),
                "qc_op": record.get("qc_op"),
                "note": "the reconciler reloads managed_runs/<run>.yaml every "
                "tick — the new QC config drives the next cycle; already-QCed "
                "checkpoints are not re-run",
            },
            status_code=201,
        )

    @router.post("/projects/{project_id}/runs/{run_name}/conclusion")
    def run_conclusion(
        project_id: str,
        run_name: str,
        body: Annotated[dict[str, Any], Body()] = ...,
    ) -> JSONResponse:
        """Append a result analysis (考察) to the run record — verdict + summary +
        evidence. Append-only: later conclusions supersede but never erase earlier
        ones, so the reasoning trail stays with the run (dashboard + API), not in
        chat history."""
        path = require_active_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        if not isinstance(body, dict):
            raise OperationError("run.record_invalid", "body must be a JSON object", {})
        verdict = body.get("verdict")
        if verdict not in ("adopted", "rejected", "superseded", "inconclusive"):
            raise OperationError(
                "run.record_invalid",
                "verdict must be adopted|rejected|superseded|inconclusive",
                {"verdict": verdict},
            )
        summary = body.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise OperationError(
                "run.record_invalid", "summary (the analysis text) is required", {}
            )
        entry: dict[str, Any] = {
            "at": utc_now_text(),
            "verdict": verdict,
            "summary": summary.strip(),
        }
        if isinstance(body.get("evidence"), list):
            entry["evidence"] = [str(e) for e in body["evidence"]][:20]
        if isinstance(body.get("next_run"), str) and body["next_run"]:
            entry["next_run"] = body["next_run"]
        run_path = path / "runs" / f"{run_name}.yaml"
        with WRITE_LOCK:
            require_active_project(config, project_id)
            if not run_path.is_file():
                raise OperationError(
                    "run.not_found",
                    f"no run '{run_name}' in project '{path.name}'",
                    {"run_name": run_name},
                )
            record = load_yaml_record(run_path, kind="run")
            record.setdefault("conclusions", []).append(entry)
            record["verdict"] = verdict  # latest verdict wins for list badges
            atomic_write_yaml(run_path, record)
        append_journal(
            path,
            "conclusion",
            {"run_name": run_name, "verdict": verdict, "summary": summary.strip()[:200]},
        )
        return envelope_response(
            ok=True,
            data={
                "run_name": run_name,
                "recorded": True,
                "verdict": verdict,
                "conclusion_count": len(record["conclusions"]),
            },
            status_code=201,
        )

    @router.post("/projects/{project_id}/runs/{run_name}/stop")
    def run_stop(project_id: str, run_name: str) -> JSONResponse:
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        managed = load_managed_run_optional(path, run_name)
        container_id = None
        if managed:
            container_id = managed.get("training_container_id")
        else:
            run_path = path / "runs" / f"{run_name}.yaml"
            if run_path.is_file():
                record = load_yaml_record(run_path, kind="run")
                container_id = (record.get("submission") or {}).get("container_id")
        if not container_id:
            raise OperationError(
                "run.not_found",
                "run has no managed_run or submission record to stop",
                {"run_name": run_name},
            )
        container = load_container_record(path, container_id)
        name = resolve_text_ref(docker_name_from_container(container, container_id))
        request = {"project_root": str(path)}
        found, _, _ = docker_inspect_by_name(request, name)
        if not found:
            return envelope_response(
                ok=True,
                data={"run_name": run_name, "already_stopped": True},
            )
        docker_rm_force(request, name)
        append_journal(path, "run_stopped", {"run_name": run_name})
        return envelope_response(
            ok=True,
            data={
                "run_name": run_name,
                "stopped": True,
                "note": "the reconciler finalizes (retention + notification) on its next tick",
            },
        )

    @router.post("/projects/{project_id}/runs/{run_name}/finalize")
    def run_finalize(
        project_id: str, run_name: str, cancel: bool = Query(False)
    ) -> JSONResponse:
        """Request force-finalize: cancel the QC/probe backfill and finalize.

        The server does NOT write progress.json — the reconciler daemon owns it,
        and a second writer means a mid-tick force request gets clobbered by the
        tick's stale write-back (last-writer-wins). Instead the request goes
        into managed_runs/<run>.control.json (the server is its only writer);
        the daemon re-reads it before each backfill op, cancels the remaining
        backlog, and runs its REGULAR terminal block — teardown, finalize
        notification, on_finalize evaluations, checkpoint ledger — none of
        which a server-side imitation could safely reproduce. Running
        probe/eval containers of this run are removed immediately, matched with
        the same truncation/mangling their runtime names actually got.
        A still-running training container is NOT killed by this endpoint (POST
        .../stop first); until it exits, its QC keeps running and the request
        stays pending. on_finalize evaluations DO run as part of the regular
        closure. `?cancel=true` withdraws a pending request.
        """
        path = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        managed = load_managed_run_optional(path, run_name)
        if not managed:
            raise OperationError(
                "run.not_found",
                "finalize requires a managed run (managed_runs/<run>.yaml)",
                {"run_name": run_name},
            )
        progress: dict[str, Any] = {}
        progress_file = progress_path(path, run_name)
        if progress_file.is_file():
            try:
                loaded = json.loads(progress_file.read_text(encoding="utf-8"))
                progress = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                progress = {}
        already = bool(progress.get("finalized"))
        if cancel:
            with WRITE_LOCK:
                control = read_control(path, run_name)
                control["force_finalize"] = False
                atomic_write_json(control_path(path, run_name), control)
            append_journal(path, "run_force_finalize_cancelled", {"run_name": run_name})
            return envelope_response(
                ok=True,
                data={"run_name": run_name, "finalize_requested": False,
                      "cancelled": True, "already_finalized": already},
            )
        if not already:
            with WRITE_LOCK:
                control = read_control(path, run_name)
                control["force_finalize"] = True
                control.setdefault("force_finalize_requested_at", utc_now_text())
                atomic_write_json(control_path(path, run_name), control)

        # Immediately remove RUNNING step-suffixed children of this run's
        # probe/eval declarations (the current in-flight render). Bare qc
        # containers (`<base>__stepNNNNNN`) carry no per-run tag and are left
        # to finish; the cancelled backlog stops any further ones.
        suffix_ids: list[tuple[str, str]] = []  # (tag, container_id)
        for probe in managed.get("probes") or []:
            pid = str(probe.get("id") or "")
            cid = str(probe.get("container_id") or "")
            if pid and cid:
                suffix_ids.append((f"probe_{pid}", cid))
        for evaluation in managed.get("evaluations") or []:
            eid = str(evaluation.get("eval_id") or "")
            cid = str(
                (evaluation.get("op") or {}).get("container_id")
                or evaluation.get("container_id")
                or ""
            )
            if eid and cid:
                suffix_ids.append((f"eval_{eid}", cid))
        request = {"project_root": str(path)}
        stopped: list[str] = []
        patterns = []
        for tag, cid in suffix_ids:
            try:
                container = load_container_record(path, cid)
            except OperationError:
                continue
            pattern = ephemeral_child_name_regex(container, cid, tag)
            if pattern is not None:
                patterns.append(pattern)
        # reap even when already finalized — a leftover child rendering after
        # teardown is exactly what an operator re-POSTs finalize to remove
        if patterns:
            for row in docker_ps_all(request):
                if row.get("state") != "running":
                    continue
                name = row["name"]
                if any(p.match(name) for p in patterns):
                    docker_rm_force(request, name)
                    stopped.append(name)
        append_journal(
            path,
            "run_force_finalize_requested",
            {"run_name": run_name, "stopped_containers": stopped, "already_finalized": already},
        )
        return envelope_response(
            ok=True,
            data={
                "run_name": run_name,
                "finalize_requested": True,
                "already_finalized": already,
                "stopped_containers": stopped,
                "note": "reconciler cancels the remaining backfill and runs its "
                        "regular terminal block (teardown/notify/evals/ledger) on "
                        "its next tick — no daemon restart needed",
            },
        )

    @router.get("/projects/{project_id}/docker/ps")
    def project_docker_ps(project_id: str) -> JSONResponse:
        """ssh-free `docker ps` scoped to this project's kikai-managed containers.

        Attribution is label-first: containers launched by kikai carry
        `kikai.container_id` / `kikai.suffix` labels (docker_attribution_labels),
        which survive any change to the name convention and any --name
        truncation. Containers from before the labels existed fall back to name
        matching against each registered record's docker name + `__<suffix>`
        convention. Anything not attributable is not kikai's and not reported.
        """
        path = require_project(config, project_id)
        request = {"project_root": str(path)}
        bases: list[tuple[str, str]] = []  # (docker base name, container_id)
        known_ids: set[str] = set()
        containers_dir = path / "containers"
        if containers_dir.is_dir():
            for cpath in sorted(containers_dir.glob("*.yaml")):
                cid = cpath.stem
                known_ids.add(cid)
                try:
                    record = load_container_record(path, cid)
                    base = resolve_text_ref(docker_name_from_container(record, cid))
                except OperationError:
                    continue
                bases.append((base, cid))
        # longest base first so `foo-bar` wins over `foo` for name `foo-bar__x`
        bases.sort(key=lambda item: len(item[0]), reverse=True)

        def classify(cid: str, suffix: str) -> dict[str, Any]:
            origin: dict[str, Any] = {"container_id": cid, "kind": "container"}
            if (m := re.match(r"^step(\d+)__probe_(.+)$", suffix)):
                origin.update(kind="probe", step=int(m.group(1)), probe_id=m.group(2))
            elif (m := re.match(r"^step(\d+)__eval_(.+)$", suffix)):
                origin.update(kind="evaluation", step=int(m.group(1)), eval_id=m.group(2))
            elif (m := re.match(r"^step(\d+)$", suffix)):
                origin.update(kind="qc", step=int(m.group(1)))
            elif suffix:
                origin.update(kind="op", suffix=suffix)
            return origin

        out: list[dict[str, Any]] = []
        for row in docker_ps_all(request):
            name = row["name"]
            labels = row.get("labels") or {}
            label_cid = labels.get("kikai.container_id")
            if label_cid and label_cid in known_ids:
                origin = classify(label_cid, labels.get("kikai.suffix") or "")
            else:
                match = next(
                    ((base, cid) for base, cid in bases
                     if name == base or name.startswith(base + "__")),
                    None,
                )
                if not match:
                    continue
                base, cid = match
                origin = classify(cid, name[len(base) + 2:] if name != base else "")
            row = {k: v for k, v in row.items() if k != "labels"}
            out.append({**row, "origin": origin})
        return envelope_response(ok=True, data={"containers": out, "count": len(out)})

    # Training entrypoints launched as raw detached ops bypass the managed-run
    # machinery entirely: no run record, no per-checkpoint QC/delivery, no
    # retention protection, and the inspect API cannot see them (2026-07-14
    # incident: a detached "train" op ran to completion with zero Discord QC).
    # The escape hatch therefore refuses them; the submit-from path executes
    # in-process and never passes through this route.
    MANAGED_REQUIRED_ENTRYPOINTS = {"train", "stageb"}

    def _reject_unmanaged_training_op(request: dict[str, Any]) -> None:
        if request.get("adapter") not in ("script_bundle_run", "script_bundle_exec"):
            return
        if not request.get("detach"):
            return
        entrypoint = str(request.get("entrypoint") or "")
        if entrypoint in MANAGED_REQUIRED_ENTRYPOINTS:
            raise OperationError(
                "operation.training_requires_managed_run",
                "detached training entrypoints must be launched as managed runs "
                "(kikai remote submit-from), not raw operations — raw launches "
                "get no QC cadence, retention protection, or run inspection",
                {"entrypoint": entrypoint},
            )

    @router.post("/projects/{project_id}/operations")
    def operations_escape_hatch(
        project_id: str,
        dry_run: bool = Query(False),
        body: Annotated[dict[str, Any], Body()] = ...,
    ) -> JSONResponse:
        """Execute an arbitrary operation request in-process (trusted caller).

        Keeps every existing adapter reachable without new endpoint work. The server
        pins ``project_root`` to this project — a body cannot point elsewhere.
        """
        path = require_active_project(config, project_id)
        request = body.get("request") if isinstance(body.get("request"), dict) else body
        if not isinstance(request, dict) or not request.get("adapter"):
            raise OperationError(
                "operation.request_invalid",
                "body must be an operation request object with an adapter",
                {},
            )
        request = {**request, "project_root": str(path)}
        _reject_unmanaged_training_op(request)
        operation_name = str(request.get("operation") or "operation")
        op = {"kind": "kikai_operation", "schema_version": 1, "request": request}
        sha = request_sha256(op)
        if dry_run:
            preview = {k: v for k, v in request.items() if k != "project_root"}
            return envelope_response(
                ok=True,
                data={"dry_run": True, "request_sha256": sha, "op_request": preview},
            )
        result = execute_operation(op)
        with WRITE_LOCK:
            atomic_write_json(
                path / "ops" / f"{operation_name}_{sha[:8]}.json",
                {**op, "result_summary": {"execution_status": result.get("execution_status")}},
            )
        return envelope_response(
            ok=True, data={"request_sha256": sha, "result": result}
        )

    return router


def status_next_action(project_id: str, run_name: str) -> dict[str, Any]:
    return next_action(
        "poll_status",
        "http_request",
        "poll the derived run status",
        blocking=False,
        command=f"GET /v1/projects/{project_id}/runs/{run_name}/status",
    )
