"""Experiment reconciler daemon.

kikai-lab has, until now, been a one-shot CLI over a file registry: guarded ops are
executed by a human invoking ``kikai target run``. The reconciler turns that "someone
who periodically drives the ops" role into a long-running process.

For each *active* training run (declared as a ``managed_runs/<id>.yaml`` desired-state
artifact) a reconcile *tick* is the smallest idempotent unit of work:

  1. poll the training container's status (local ``docker inspect``);
  2. for every NEW ``checkpoint_step_*.pt`` (up to ``max_step``) run the run's QC op --
     a checkpoint diagnostic that renders + posts a video to Discord -- exactly once;
  3. run ``checkpoint_retention`` (the trainer no longer self-prunes, so this is what
     keeps the two independent keep-latest / keep-best windows bounded);
  4. when training terminates (a terminal ``early_stop``/``done`` row in metrics.jsonl,
     or the container exiting after we have seen it run) notify + tear the container down.

The daemon is TRUSTED and IN-PROCESS: it constructs op requests itself and calls
``execute_operation`` directly (no receipt dance, which only exists to gate untrusted
CLI-loaded op files). All docker work reuses the local-docker helpers already in
``operation.py`` -- no new docker code.

Progress/idempotency state lives beside the desired-state file in
``managed_runs/<id>.progress.json`` and is written atomically (tmp + ``os.replace``) so a
crash mid-tick never corrupts it and the next tick resumes without re-posting QC.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from kikai_lab.operation import (
    OperationError,
    checkpoint_step_from_name,
    docker_inspect_by_name,
    docker_name_from_container,
    execute_operation,
    load_container_record,
    resolve_metrics_path,
    resolve_text_ref,
)

MANAGED_RUN_KIND = "managed_run"
# Terminal training-lifecycle events a cooperating trainer writes to metrics.jsonl:
#   - 'early_stop' : patience exhausted (written once when early stopping fires)
#   - 'done'       : max-steps reached / normal completion
# NB: 'early_stop_eval' is the PER-EVAL row, not terminal -- do not treat it as done.
TERMINAL_TRAINING_EVENTS = frozenset({"early_stop", "done", "stopped_by_control"})
DEFAULT_POLL_INTERVAL_SEC = 60

ExecuteFn = Callable[[dict[str, Any]], dict[str, Any]]
InspectFn = Callable[[dict[str, Any], str], "tuple[bool, list[dict[str, Any]], str]"]


# --------------------------------------------------------------------------- #
# managed_runs/<id>.yaml desired-state loading
# --------------------------------------------------------------------------- #
def managed_runs_dir(project_root: Path) -> Path:
    return Path(project_root) / "managed_runs"


def load_managed_run(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict) or data.get("kind") != MANAGED_RUN_KIND:
        raise OperationError(
            "reconcile.managed_run_invalid",
            "managed_run must be a mapping with kind: managed_run",
            {"path": str(path)},
        )
    for key in ("run_id", "run_dir", "training_container_id"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise OperationError(
                "reconcile.managed_run_field_missing",
                f"managed_run requires a non-empty string field '{key}'",
                {"path": str(path), "field": key},
            )
    return data


def load_managed_runs(project_root: Path, run_id: str | None = None) -> list[dict[str, Any]]:
    directory = managed_runs_dir(project_root)
    if not directory.exists():
        return []
    runs: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        managed_run = load_managed_run(path)
        if run_id is None or managed_run.get("run_id") == run_id:
            runs.append(managed_run)
    return runs


# --------------------------------------------------------------------------- #
# progress / idempotency state (daemon-owned, atomic)
# --------------------------------------------------------------------------- #
def progress_path(project_root: Path, run_id: str) -> Path:
    return managed_runs_dir(project_root) / f"{run_id}.progress.json"


def default_progress(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "qc_done_steps": [],
        "eval_done": {},
        "check_verdicts": {},
        "last_retention_step": None,
        "lifecycle_state": "running",
        "finalized": False,
        "finalize_notified": False,
        "seen_running": False,
        "last_error": None,
        "ticks": 0,
    }


def load_progress(project_root: Path, run_id: str) -> dict[str, Any]:
    path = progress_path(project_root, run_id)
    merged = default_progress(run_id)
    if not path.exists():
        return merged
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Corrupt/partial state -> resume from a clean default rather than crash. Atomic
        # writes make this practically unreachable; the cost of the fallback is at-most a
        # re-QC of already-delivered checkpoints (accepted at-least-once trade-off).
        return merged
    if not isinstance(data, dict):
        return merged
    merged.update(data)
    merged["qc_done_steps"] = sorted(
        {int(s) for s in merged.get("qc_done_steps", []) if isinstance(s, int)}
    )
    return merged


def write_progress(project_root: Path, run_id: str, progress: dict[str, Any]) -> None:
    path = progress_path(project_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(progress, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# checkpoint + metrics inspection (daemon-local filesystem view)
# --------------------------------------------------------------------------- #
def checkpoint_steps(run_dir: Path) -> list[tuple[int, Path]]:
    """(step, path) for each ``checkpoint_step_*.pt`` in ``<run_dir>/checkpoints``, ascending.

    Only the periodic ``checkpoint_step_*`` family is QC'd (mirrors the watcher). The
    curated ``best_step_*`` family is a retention concern, not a QC one.
    """
    directory = Path(run_dir) / "checkpoints"
    if not directory.exists():
        return []
    out: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("checkpoint_step_*.pt")):
        step = checkpoint_step_from_name(path)
        if step is not None:
            out.append((step, path))
    out.sort(key=lambda item: item[0])
    return out


def read_terminal_event(metrics_path: Path) -> str | None:
    """Last terminal training event ('early_stop'|'done'|'stopped_by_control'), else None."""
    path = Path(metrics_path)
    if not path.exists():
        return None
    last: str | None = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("event") in TERMINAL_TRAINING_EVENTS:
                last = row.get("event")
    return last


def poll_status(
    project_root: Path, training_container_id: str, *, inspect: InspectFn
) -> dict[str, Any]:
    container = load_container_record(Path(project_root), training_container_id)
    name = resolve_text_ref(docker_name_from_container(container, training_container_id))
    request = {"project_root": str(project_root)}
    try:
        found, data, _ = inspect(request, name)
    except OperationError as exc:
        # docker missing/unreachable -> unknown status; never treat as "exited".
        return {"container_name": name, "exists": False, "running": False, "inspect_error": exc.code}
    running = False
    exit_code: Any = None
    docker_state: str | None = None
    if found and data:
        state = data[0].get("State") if isinstance(data[0], dict) else None
        if isinstance(state, dict):
            running = bool(state.get("Running"))
            exit_code = state.get("ExitCode")
            status_value = state.get("Status")
            docker_state = status_value if isinstance(status_value, str) else None
    return {
        "container_name": name,
        "exists": bool(found),
        "running": running,
        "exit_code": exit_code,
        # docker State.Status: created|running|paused|restarting|removing|exited|dead.
        # Only exited/dead are terminal; running/restarting/paused/created are NOT.
        "state": docker_state,
    }


# --------------------------------------------------------------------------- #
# op builders (all reuse existing execute_operation adapters)
# --------------------------------------------------------------------------- #
def _substitute(template: Any, mapping: dict[str, str]) -> Any:
    """Replace ``{{key}}`` placeholders in every string of a nested dict/list structure.

    Walks the PARSED structure (not the serialised JSON text), so a value containing a
    quote or backslash cannot corrupt the document — the substitution happens on Python
    strings and re-serialisation (by ``execute_operation``) escapes correctly.
    """
    if isinstance(template, str):
        result = template
        for key, value in mapping.items():
            result = result.replace("{{" + key + "}}", value)
        return result
    if isinstance(template, list):
        return [_substitute(item, mapping) for item in template]
    if isinstance(template, dict):
        return {k: _substitute(v, mapping) for k, v in template.items()}
    return template


def _prepend_run_label(op: Any, run_id: str) -> Any:
    """Force every ``--post-label`` value in the op to lead with ``[<run name>]``.

    QC/eval posts (e.g. Discord uploads) are labeled by author-written strings.
    When a run is derived (submit-from/probe-from), a hand-written label that
    embeds an INFORMAL ancestor name (``exp7`` for parent ``exp7_baseline``)
    survives rebinding — rebind only rewrites the full run name — and the posts
    get attributed to the wrong run. The run name is system truth, so prepend
    it unconditionally instead of trusting authors. Idempotent: an already-prefixed
    label is left alone. Handles both ``--post-label VALUE`` and
    ``--post-label=VALUE`` arg forms, at any nesting depth (operation_sequence
    steps included).
    """
    prefix = f"[{run_id}]"

    def tag(value: str) -> str:
        # boundary-aware idempotency: "run_OLD ..." must NOT count as already
        # tagged for run_id "r" (bare startswith would match any shared prefix)
        if value.startswith(prefix) or value == run_id or value.startswith(f"{run_id} "):
            return value
        return f"{prefix} {value}"

    if isinstance(op, list):
        out = []
        expect_value = False
        for item in op:
            if expect_value and isinstance(item, str) and not item.startswith("--"):
                out.append(tag(item))
                expect_value = False
                continue
            expect_value = False
            if isinstance(item, str) and item == "--post-label":
                expect_value = True
                out.append(item)
            elif isinstance(item, str) and item.startswith("--post-label="):
                out.append("--post-label=" + tag(item[len("--post-label="):]))
            else:
                out.append(_prepend_run_label(item, run_id))
        return out
    if isinstance(op, dict):
        return {k: _prepend_run_label(v, run_id) for k, v in op.items()}
    return op


def load_qc_template(project_root: Path, managed_run: dict[str, Any]) -> dict[str, Any] | None:
    """The QC op template, inline (``qc_op``) or from a file (``qc_op_template`` path)."""
    inline = managed_run.get("qc_op")
    if isinstance(inline, dict):
        return inline
    template_ref = managed_run.get("qc_op_template")
    if not template_ref:
        return None
    path = Path(template_ref)
    if not path.is_absolute():
        path = Path(project_root) / template_ref
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_qc_op(
    project_root: Path, managed_run: dict[str, Any], step: int, checkpoint_path: Path
) -> dict[str, Any] | None:
    """Fill the QC op template for one checkpoint.

    The QC op runs in a SEPARATE container (its own mounts), so we never inject the
    daemon-local absolute path. We inject the checkpoint *basename* (which is identical
    across container views, and carries the ``_loss`` tag we cannot reconstruct from the
    step alone) plus the step; the template composes the container-space path via its own
    ``${CONTAINER_TRAINING_RUNS_ROOT}`` refs.
    """
    template = load_qc_template(project_root, managed_run)
    if template is None:
        return None
    _validate_qc_template(template)
    mapping = {
        "step": str(step),
        "step6": f"{step:06d}",
        "checkpoint_name": Path(checkpoint_path).name,
        "run_id": str(managed_run.get("run_id", "")),
    }
    rendered = _substitute(template, mapping)
    run_id = str(managed_run.get("run_id", ""))
    return _prepend_run_label(rendered, run_id) if run_id else rendered


def _validate_qc_template(template: dict[str, Any]) -> None:
    """An ``operation_sequence`` QC op writes a one-shot ``pipeline_runs/<id>.json`` record
    and refuses to overwrite it, so its ``pipeline_run_id`` MUST vary per checkpoint (embed
    ``{{step}}``/``{{step6}}``) or the second checkpoint's QC wedges forever on
    ``sequence_record_exists``. Fail loudly at build time instead of at runtime."""
    request = template.get("request") if isinstance(template, dict) else None
    if not isinstance(request, dict) or request.get("adapter") != "operation_sequence":
        return
    pipeline_run_id = request.get("pipeline_run_id")
    if not isinstance(pipeline_run_id, str) or (
        "{{step}}" not in pipeline_run_id and "{{step6}}" not in pipeline_run_id
    ):
        raise OperationError(
            "reconcile.qc_template_pipeline_run_id_not_step_varying",
            "an operation_sequence QC template must set a pipeline_run_id containing "
            "{{step}} or {{step6}} so each checkpoint gets a unique, non-colliding record",
            {"pipeline_run_id": pipeline_run_id},
        )


def _clear_incomplete_qc_record(project_root: Path, qc_op: dict[str, Any]) -> None:
    """Remove a non-completed ``operation_sequence`` pipeline record so a previously-FAILED
    QC re-runs fresh on the next attempt.

    ``execute_operation_sequence`` writes a ``status: "failed"`` record when an inner step
    errors, and thereafter refuses to run (``sequence_record_exists``) regardless of status.
    Without this, a failed QC would either wedge forever OR be mis-read as delivered. We
    KEEP a ``completed`` record in place: its re-run raises ``sequence_record_exists``, which
    the caller treats as idempotent success (a genuine replay after a crash between the QC
    finishing and the progress commit).
    """
    request = qc_op.get("request") if isinstance(qc_op, dict) else None
    if not isinstance(request, dict) or request.get("adapter") != "operation_sequence":
        return
    pipeline_run_id = request.get("pipeline_run_id")
    if not isinstance(pipeline_run_id, str) or not pipeline_run_id:
        return
    record_path = Path(project_root) / "pipeline_runs" / f"{pipeline_run_id}.json"
    if not record_path.exists():
        return
    try:
        status = json.loads(record_path.read_text(encoding="utf-8")).get("status")
    except (OSError, json.JSONDecodeError):
        status = None
    if status != "completed":
        record_path.unlink(missing_ok=True)


def _inner_step_already_recorded(exc: OperationError, qc_op: dict[str, Any]) -> bool:
    """True when a QC ``operation_sequence`` failed on its FINAL step because that step hit
    its one-shot ``*_record_exists`` guard -- i.e. the terminal delivery already succeeded on
    a prior attempt (crash after delivery, before the completed pipeline record was written).

    Reaching the LAST step means every prior step (incl. whatever posts the diagnostic)
    re-ran to completion on this attempt, so the QC genuinely went out -> mark it done rather
    than wedge on permanent re-delivery. We deliberately do NOT trust a ``_record_exists`` on
    an EARLIER step: later steps (including the delivery) did not run on this re-run, so the
    diagnostic may never have been delivered -- treating that as done could let retention
    prune an un-QC'd checkpoint. Such a template just surfaces a visible, retried failure.
    """
    details = exc.details if isinstance(exc.details, dict) else {}
    step_error = details.get("step_error")
    code = step_error.get("code") if isinstance(step_error, dict) else None
    if not (isinstance(code, str) and code.endswith("_record_exists")):
        return False
    request = qc_op.get("request") if isinstance(qc_op, dict) else None
    steps = request.get("steps") if isinstance(request, dict) else None
    if not isinstance(steps, list) or not steps:
        return False
    last_step = steps[-1]
    last_step_id = last_step.get("step_id") if isinstance(last_step, dict) else None
    return last_step_id is not None and details.get("failed_step_id") == last_step_id


QC_ARTIFACT_MEDIA_SUFFIXES = (".mp4", ".webm", ".png", ".jpg", ".jpeg")
# Keep in sync with kikai_lab.server.registry.SAFE_ID (reconcile must not import the
# server package): ids the content plane will accept.
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _qc_artifact_id(run_id: str, step: int, media: Path) -> str | None:
    """A SAFE_ID-conformant, suffix-disambiguated id for one QC media file.

    The suffix is part of the identity (a same-stem video+thumbnail pair must not
    shadow each other), the stem is sanitized to the safe charset, and the whole id
    is capped at 64 chars with a short name-hash keeping truncated stems unique."""
    import hashlib

    suffix = media.suffix.lstrip(".").lower()
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", media.stem)
    base = f"{run_id}_qc_step{step:06d}_"
    tail_budget = 64 - len(base) - len(suffix) - 1
    if tail_budget < 8:
        return None  # run_id alone nearly fills the cap; skip rather than emit junk
    if len(stem) > tail_budget:
        digest = hashlib.sha1(media.name.encode("utf-8")).hexdigest()[:6]
        stem = f"{stem[: tail_budget - 7]}-{digest}"
    artifact_id = f"{base}{stem}_{suffix}"
    return artifact_id if _SAFE_ARTIFACT_ID.match(artifact_id) else None


def record_qc_artifacts(
    project_root: Path, managed_run: dict[str, Any], step: int
) -> list[str]:
    """Append this checkpoint's QC outputs to the artifact ledger so they are
    browsable/playable (dashboard gallery + /content), not just Discord posts.

    Requires ``qc_artifacts_dir`` on the managed run: a daemon-local directory
    template with {{step}}/{{step6}} placeholders (refs allowed). Best-effort:
    a missing dir records nothing (the QC op may write elsewhere or only post)."""
    template = managed_run.get("qc_artifacts_dir")
    if not isinstance(template, str) or not template:
        return []
    resolved = resolve_text_ref(
        template.replace("{{step6}}", f"{step:06d}").replace("{{step}}", str(step))
    )
    directory = Path(resolved)
    if not directory.is_dir():
        return []
    run_id = str(managed_run["run_id"])
    ledger = Path(project_root) / "artifacts" / f"{run_id}.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    recorded: list[str] = []
    with ledger.open("a", encoding="utf-8") as f:
        for media in sorted(directory.iterdir()):
            if not media.is_file() or media.suffix.lower() not in QC_ARTIFACT_MEDIA_SUFFIXES:
                continue
            artifact_id = _qc_artifact_id(run_id, step, media)
            if artifact_id is None:
                logging.getLogger("kikai_lab.reconcile").warning(
                    "qc artifact skipped (id not SAFE_ID-conformant): %s", media.name
                )
                continue
            row = {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "run_name": run_id,
                "kind": "qc_video"
                if media.suffix.lower() in (".mp4", ".webm")
                else "qc_image",
                "artifact_class": "visual_only_renderer_qc",
                "locations": [
                    {"kind": "host_path", "path": str(media), "host_ref": "local"}
                ],
            }
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            recorded.append(artifact_id)
    return recorded


def build_retention_op(project_root: Path, managed_run: dict[str, Any]) -> dict[str, Any]:
    """checkpoint_retention runs IN-PROCESS on the daemon's own rw mount, so ``run_dir``
    is the daemon-local path. keep_latest/keep_best come from the managed_run's
    ``retention`` block, else the experiment yaml (``experiment_id``)."""
    request: dict[str, Any] = {
        "adapter": "checkpoint_retention",
        "operation": f"{managed_run['run_id']}_checkpoint_retention",
        "project_root": str(project_root),
        "run_dir": str(managed_run["run_dir"]),
    }
    if managed_run.get("experiment_id"):
        request["experiment_id"] = managed_run["experiment_id"]
    retention = managed_run.get("retention") or {}
    for key in ("keep_latest", "keep_best", "keep_milestones", "metric_key", "metric_mode"):
        if key in retention:
            request[key] = retention[key]
    return {"kind": "kikai_operation", "schema_version": 1, "request": request}


def record_checkpoint_artifacts(
    project_root: Path, managed_run: dict[str, Any], run_dir: Path
) -> list[str]:
    """At finalize, ledger the SURVIVING checkpoints (best_checkpoint.pt, best_step_*,
    retained checkpoint_step_*) as kind=checkpoint artifacts.

    Only at finalize: intermediate checkpoints churn under retention, and an
    append-only ledger must not fill with rows pointing at deleted files."""
    directory = Path(run_dir) / "checkpoints"
    if not directory.is_dir():
        return []
    run_id = str(managed_run["run_id"])
    ledger = Path(project_root) / "artifacts" / f"{run_id}.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    recorded: list[str] = []
    with ledger.open("a", encoding="utf-8") as f:
        for ckpt in sorted(directory.glob("*.pt")):
            artifact_id = _qc_artifact_id(run_id, 0, ckpt)
            if artifact_id is None:
                logging.getLogger("kikai_lab.reconcile").warning(
                    "checkpoint artifact skipped (id not SAFE_ID-conformant): %s",
                    ckpt.name,
                )
                continue
            artifact_id = artifact_id.replace("_qc_step000000_", "_ckpt_")
            if not _SAFE_ARTIFACT_ID.match(artifact_id):
                continue
            row = {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "run_name": run_id,
                "kind": "checkpoint",
                "artifact_class": "model_checkpoint",
                "locations": [
                    {"kind": "host_path", "path": str(ckpt), "host_ref": "local"}
                ],
            }
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            recorded.append(artifact_id)
    return recorded


# --------------------------------------------------------------------------- #
# declarative evaluations: measurement ops + pure metric assertions the daemon
# runs so nobody has to babysit a hypothesis by hand.
# --------------------------------------------------------------------------- #
def evaluation_due_steps(
    evaluation: dict[str, Any], steps: list[int], done: list[int]
) -> list[int]:
    """Checkpoint steps this evaluation should run at and has not yet.

    Semantics scoped deliberately: only CURRENTLY EXISTING checkpoints are considered
    (a step whose checkpoint retention pruned before the eval succeeded never
    retries — same contract as QC), and every_n_steps means 'checkpoint steps
    divisible by N' (a cadence not dividing the checkpoint interval never fires)."""
    trigger = evaluation.get("trigger") or {}
    if not any(k in trigger for k in ("every_n_steps", "at_steps", "on_finalize")):
        # a typo'd trigger silently never firing is the worst failure mode for a
        # declare-once feature — fail loudly instead.
        raise OperationError(
            "reconcile.evaluation_invalid",
            "trigger must declare every_n_steps, at_steps, or on_finalize",
            {"eval_id": evaluation.get("eval_id"), "trigger": trigger},
        )
    due: list[int] = []
    every = trigger.get("every_n_steps")
    at_steps = trigger.get("at_steps")
    for step in steps:
        if step in done:
            continue
        if isinstance(every, int) and every > 0 and step % every == 0:
            due.append(step)
        elif isinstance(at_steps, list) and step in at_steps:
            due.append(step)
    return due


def build_evaluation_op(
    project_root: Path, managed_run: dict[str, Any], evaluation: dict[str, Any], step: int
) -> dict[str, Any]:
    template = evaluation.get("op")
    if not isinstance(template, dict):
        raise OperationError(
            "reconcile.evaluation_invalid",
            "evaluation requires an inline op template",
            {"eval_id": evaluation.get("eval_id")},
        )
    _validate_qc_template(template)
    mapping = {
        "step": str(step),
        "step6": f"{step:06d}",
        "run_id": str(managed_run.get("run_id", "")),
        "eval_id": str(evaluation.get("eval_id", "")),
    }
    rendered = _substitute(template, mapping)
    run_id = str(managed_run.get("run_id", ""))
    return _prepend_run_label(rendered, run_id) if run_id else rendered


def run_metric_checks(
    metrics_path: Path, checks: list[dict[str, Any]], latest_step: int
) -> list[dict[str, Any]]:
    """Pure assertions over metrics.jsonl — the cheap hypothesis gates.

    expect vocabulary:
      decreasing — the mean of `key` over the last third of the window is lower than
                   over the first third by at least min_delta (default 0.0)
      below/above — the latest value of `key` compares against threshold
    A check only reports once its window is reachable (window end <= latest_step).
    window_steps_relative: window offsets are added to the run's FIRST metrics step —
    the natural frame for a PROBE resumed from a parent checkpoint, where absolute
    steps depend on where the parent happened to stop."""
    rows: list[dict[str, Any]] = []
    values: dict[int, dict[str, Any]] = {}
    try:
        with Path(metrics_path).open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("event") == "train_metrics":
                    step = row.get("step")
                    if isinstance(step, int):
                        values[step] = row
    except OSError:
        return []
    for check in checks:
        key = check.get("key")
        check_id = check.get("check_id") or key
        try:
            window = check.get("window_steps") or [0, latest_step]
            expect = check.get("expect")
            if not key or not isinstance(window, list) or len(window) != 2:
                rows.append({"check_id": check_id, "verdict": "invalid_check"})
                continue
            lo, hi = int(window[0]), int(window[1])
            if check.get("window_steps_relative"):
                if not check.get("window_steps"):
                    # defaulted-to-[0, latest] offsets from a base > 0 can NEVER be
                    # reached -> the check would pend forever, silently
                    rows.append({"check_id": check_id, "verdict": "invalid_check"})
                    continue
                if not values:
                    rows.append({"check_id": check_id, "verdict": "pending"})
                    continue
                base = min(values.keys())
                lo, hi = base + lo, base + hi
        except (TypeError, ValueError):
            # one malformed declaration must never disable its siblings
            rows.append({"check_id": check_id, "verdict": "invalid_check"})
            continue
        if latest_step < hi:
            rows.append({"check_id": check_id, "verdict": "pending"})
            continue
        series = [
            (s, v[key]) for s, v in sorted(values.items())
            if lo <= s <= hi
            and isinstance(v.get(key), int | float)
            and not isinstance(v.get(key), bool)
        ]
        if len(series) < 6:
            rows.append({"check_id": check_id, "verdict": "no_data"})
            continue
        third = max(1, len(series) // 3)
        head = sum(v for _, v in series[:third]) / third
        tail = sum(v for _, v in series[-third:]) / third
        latest = series[-1][1]
        try:
            if expect == "decreasing":
                ok = (head - tail) >= float(check.get("min_delta", 0.0))
                detail = {"head_mean": round(head, 6), "tail_mean": round(tail, 6)}
            elif expect == "below":
                ok = latest < float(check.get("threshold", 0.0))
                detail = {"latest": round(latest, 6)}
            elif expect == "above":
                ok = latest > float(check.get("threshold", 0.0))
                detail = {"latest": round(latest, 6)}
            else:
                rows.append({"check_id": check_id, "verdict": "invalid_expect"})
                continue
        except (TypeError, ValueError):
            rows.append({"check_id": check_id, "verdict": "invalid_check"})
            continue
        rows.append({"check_id": check_id, "verdict": "pass" if ok else "fail", **detail})
    return rows


def build_check_notification(
    project_root: Path,
    managed_run: dict[str, Any],
    failed: list[dict[str, Any]],
    notify_seq: int,
) -> dict[str, Any]:
    run_id = managed_run["run_id"]
    names = ", ".join(str(c["check_id"]) for c in failed)
    # notification_id embeds a per-run monotonically increasing sequence so each fail
    # TRANSITION gets its own one-shot record (webhook_notification refuses
    # duplicates) — fail->pass->fail notifies again instead of erroring against the
    # first record, even within the same checkpoint window.
    return {
        "kind": "kikai_operation",
        "schema_version": 1,
        "request": {
            "adapter": "webhook_notification",
            "operation": f"{run_id}_metric_check_failed",
            "project_root": str(project_root),
            "notification_id": f"{run_id}_metric_check_{names[:40]}_{notify_seq}",
            "delivery_target_id": managed_run["delivery_target_id"],
            "message": f"[{run_id}] metric check FAILED: {names} — "
            "the declared hypothesis gate did not hold; inspect the metrics.",
            "severity": "warning",
            "run_name": run_id,
        },
    }


def build_teardown_op(project_root: Path, managed_run: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "kikai_operation",
        "schema_version": 1,
        "request": {
            "adapter": "docker_container_restart",
            "operation": f"{managed_run['run_id']}_training_teardown",
            "project_root": str(project_root),
            "container_id": managed_run["training_container_id"],
            "mode": "teardown",
        },
    }


def run_has_conclusion(project_root: Path, run_id: str) -> bool:
    """Broadly guarded: an unreadable run record must never break a finalize."""
    try:
        record = yaml.safe_load(
            (Path(project_root) / "runs" / f"{run_id}.yaml").read_text(encoding="utf-8")
        )
        return bool(isinstance(record, dict) and record.get("conclusions"))
    except FileNotFoundError:
        return False  # no record at all -> certainly no conclusion
    except Exception:
        return True  # unreadable/corrupt -> can't tell, don't nag


def build_run_dir_chown_op(
    project_root: Path, managed_run: dict[str, Any]
) -> dict[str, Any]:
    """One-shot repair for the root-write trap: a root-running trainer leaves
    checkpoints the daemon user cannot delete. Chown the run_dir to the daemon's
    own uid/gid using the training image (guaranteed present)."""
    run_id = managed_run["run_id"]
    return {
        "kind": "kikai_operation",
        "schema_version": 1,
        "request": {
            "adapter": "run_dir_chown",
            "operation": f"{run_id}_run_dir_chown",
            "project_root": str(project_root),
            "container_id": managed_run["training_container_id"],
            "run_dir": str(managed_run["run_dir"]),
            "uid": os.getuid(),
            "gid": os.getgid(),
        },
    }


def build_finalize_notification(
    project_root: Path,
    managed_run: dict[str, Any],
    reason: str,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = managed_run["run_id"]
    lines = [
        f"[{run_id}] training finished ({reason}); reconciler ran final "
        "retention and released the training container."
    ]
    verdicts = (progress or {}).get("check_verdicts") or {}
    gate_failed = any(v == "fail" for v in verdicts.values())
    if verdicts:
        gates = ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
        lines.append(f"gates: {gates}")
    if not run_has_conclusion(project_root, run_id):
        # close-the-loop as a mechanism, not a habit: every finalize without a
        # recorded analysis asks for one, with the exact call to make.
        lines.append(
            "no conclusion recorded yet -> POST "
            f"/v1/projects/{Path(project_root).name}/runs/{run_id}/conclusion "
            "(verdict + summary; see /v1/skill.md)"
        )
    return {
        "kind": "kikai_operation",
        "schema_version": 1,
        "request": {
            "adapter": "webhook_notification",
            "operation": f"{run_id}_reconcile_finalized",
            "project_root": str(project_root),
            "notification_id": f"{run_id}_reconcile_finalized",
            "delivery_target_id": managed_run["delivery_target_id"],
            "message": "\n".join(lines),
            "severity": "warning" if gate_failed else "info",
            "run_name": run_id,
        },
    }


# --------------------------------------------------------------------------- #
# one reconcile tick for a single managed run
# --------------------------------------------------------------------------- #
def tick(
    project_root: Path,
    managed_run: dict[str, Any],
    progress: dict[str, Any],
    *,
    execute: ExecuteFn | None = None,
    inspect: InspectFn | None = None,
) -> dict[str, Any]:
    execute = execute or execute_operation
    inspect = inspect or docker_inspect_by_name
    project_root = Path(project_root)
    run_id = managed_run["run_id"]
    run_dir = Path(resolve_text_ref(str(managed_run["run_dir"])))
    max_step = managed_run.get("max_step")

    summary: dict[str, Any] = {
        "run_id": run_id,
        "status": None,
        "new_qc_steps": [],
        "qc_errors": [],
        "retention": None,
        "terminal_event": None,
        "teardown": None,
        "finalized": bool(progress.get("finalized")),
        "lifecycle_state": progress.get("lifecycle_state", "running"),
    }

    progress["ticks"] = int(progress.get("ticks", 0)) + 1
    tick_had_error = False

    # Already finalized -> idempotent no-op (keep reporting terminal state).
    if progress.get("finalized"):
        summary["status"] = {"finalized": True}
        summary["lifecycle_state"] = progress.get("lifecycle_state")
        write_progress(project_root, run_id, progress)
        return summary

    # 1. training container status
    try:
        status = poll_status(project_root, managed_run["training_container_id"], inspect=inspect)
    except OperationError as exc:
        status = {"exists": False, "running": False, "inspect_error": exc.code}
    summary["status"] = status
    if status.get("running"):
        progress["seen_running"] = True

    # 2. QC every new checkpoint exactly once (commit each step BEFORE the next -> no dup post)
    qc_configured = ("qc_op" in managed_run) or bool(managed_run.get("qc_op_template"))
    done_steps = set(progress.get("qc_done_steps", []))
    for step, path in checkpoint_steps(run_dir):
        if step in done_steps:
            continue
        if max_step is not None and step > int(max_step):
            continue
        if not qc_configured:
            continue
        try:
            qc_op = build_qc_op(project_root, managed_run, step, path)
        except OperationError as exc:
            summary["qc_errors"].append({"step": step, "error": exc.code})
            tick_had_error = True
            progress["last_error"] = f"qc step {step}: {exc.code}"
            continue
        except Exception as exc:  # bad/missing template file -> isolate, don't abort the tick
            summary["qc_errors"].append({"step": step, "error": "reconcile.qc_build_failed"})
            tick_had_error = True
            progress["last_error"] = f"qc step {step}: build failed: {str(exc)[:160]}"
            continue
        # Drop a stale FAILED sequence record so the QC re-runs fresh (COMPLETED ones stay,
        # so a genuine replay still raises _record_exists -> idempotent success below).
        _clear_incomplete_qc_record(project_root, qc_op)
        already_recorded = False
        try:
            execute(qc_op)
        except OperationError as exc:
            if exc.code.endswith("_record_exists"):
                # only COMPLETED records survive the clear above -> genuinely delivered.
                already_recorded = True
            elif exc.code == "operation.sequence_step_failed" and _inner_step_already_recorded(exc, qc_op):
                # re-run hit an inner delivery/notification that was ALREADY recorded on a
                # prior attempt (crash after delivery, before the completed pipeline record)
                # -> the QC was delivered; idempotent success, not a permanent re-deliver wedge.
                already_recorded = True
            else:
                summary["qc_errors"].append({"step": step, "error": exc.code})
                tick_had_error = True
                progress["last_error"] = f"qc step {step}: {exc.code}"
                continue  # leave unmarked -> retried next tick
        except Exception as exc:  # adapter bug / unexpected -> isolate, keep the type visible
            summary["qc_errors"].append(
                {"step": step, "error": f"reconcile.qc_exec_failed:{type(exc).__name__}"}
            )
            tick_had_error = True
            progress["last_error"] = f"qc step {step}: exec failed: {str(exc)[:160]}"
            continue
        done_steps.add(step)
        progress["qc_done_steps"] = sorted(done_steps)
        if not already_recorded:
            summary["new_qc_steps"].append(step)
            try:
                # NB: a crash after QC delivery but before this append loses the rows
                # permanently (no retry) — accepted best-effort contract.
                summary.setdefault("qc_artifacts_recorded", []).extend(
                    record_qc_artifacts(project_root, managed_run, step)
                )
            except Exception:  # ledger writing must never fail a QC that succeeded
                logging.getLogger("kikai_lab.reconcile").exception(
                    "qc artifact ledger write failed"
                )
        write_progress(project_root, run_id, progress)

    # 2b. declarative evaluations (measurement ops) + metric checks — the agent
    # declares once; the daemon babysits the hypothesis.
    evaluations = managed_run.get("evaluations") or []
    all_steps = [s for s, _ in checkpoint_steps(run_dir)]
    summary["eval_errors"] = []
    for evaluation in evaluations:
        eval_id = str(evaluation.get("eval_id") or "")
        if not eval_id or not isinstance(evaluation.get("op"), dict):
            continue
        done = list(progress.get("eval_done", {}).get(eval_id, []))
        try:
            due = evaluation_due_steps(evaluation, all_steps, done)
        except OperationError as exc:
            summary["eval_errors"].append({"eval_id": eval_id, "error": exc.code})
            tick_had_error = True
            progress["last_error"] = f"eval {eval_id}: {exc.code}"
            continue
        for step in due:
            try:
                eval_op = build_evaluation_op(project_root, managed_run, evaluation, step)
            except OperationError as exc:
                summary["eval_errors"].append(
                    {"eval_id": eval_id, "step": step, "error": exc.code}
                )
                tick_had_error = True
                progress["last_error"] = f"eval {eval_id}@{step}: {exc.code}"
                continue
            # Same record hygiene as QC: drop a stale FAILED sequence record so a
            # previously-failed measurement re-runs fresh — only COMPLETED records
            # survive, so _record_exists below is a genuine replay, never a failed
            # run masquerading as done.
            _clear_incomplete_qc_record(project_root, eval_op)
            try:
                execute(eval_op)
            except OperationError as exc:
                if exc.code.endswith("_record_exists"):
                    pass  # completed record survived the clear -> genuinely done
                elif exc.code == "operation.sequence_step_failed" and _inner_step_already_recorded(
                    exc, eval_op
                ):
                    pass  # terminal delivery already recorded on a prior attempt
                else:
                    summary["eval_errors"].append(
                        {"eval_id": eval_id, "step": step, "error": exc.code}
                    )
                    tick_had_error = True
                    progress["last_error"] = f"eval {eval_id}@{step}: {exc.code}"
                    continue
            except Exception as exc:
                summary["eval_errors"].append(
                    {"eval_id": eval_id, "step": step, "error": type(exc).__name__}
                )
                tick_had_error = True
                progress["last_error"] = f"eval {eval_id}@{step}: {str(exc)[:120]}"
                continue
            done.append(step)
            progress.setdefault("eval_done", {})[eval_id] = sorted(done)
            artifacts_dir = evaluation.get("artifacts_dir")
            if artifacts_dir:
                try:
                    record_qc_artifacts(
                        project_root,
                        {**managed_run, "qc_artifacts_dir": artifacts_dir},
                        step,
                    )
                except Exception:
                    logging.getLogger("kikai_lab.reconcile").exception(
                        "evaluation artifact ledger write failed"
                    )
            write_progress(project_root, run_id, progress)

    checks = managed_run.get("metric_checks") or []
    if checks and all_steps:
        try:
            verdicts = run_metric_checks(
                resolve_metrics_path(run_dir), checks, max(all_steps)
            )
        except OSError:
            verdicts = []  # unreadable metrics file; per-check errors never raise
        newly_failed = []
        for verdict in verdicts:
            check_id = str(verdict["check_id"])
            previous = progress.get("check_verdicts", {}).get(check_id)
            progress.setdefault("check_verdicts", {})[check_id] = verdict["verdict"]
            if verdict["verdict"] == "fail" and previous != "fail":
                newly_failed.append(verdict)
        summary["metric_checks"] = verdicts
        if newly_failed:
            try:
                from kikai_lab.server.registry import append_journal

                append_journal(
                    project_root,
                    "metric_check_failed",
                    {
                        "run_name": run_id,
                        "checks": [str(c["check_id"]) for c in newly_failed],
                    },
                )
            except Exception:
                pass
        if newly_failed and managed_run.get("delivery_target_id"):
            notify_seq = int(progress.get("check_notify_seq", 0)) + 1
            progress["check_notify_seq"] = notify_seq
            try:
                execute(
                    build_check_notification(
                        project_root, managed_run, newly_failed, notify_seq
                    )
                )
            except OperationError as exc:
                if not exc.code.endswith("_record_exists"):  # duplicate = benign
                    tick_had_error = True
                    progress["last_error"] = f"check_notify: {exc.code}"

    # 3. retention AFTER QC (never delete a checkpoint before its diagnostic renders).
    # OSError (e.g. root-owned checkpoints the daemon user cannot unlink) is an
    # OPERATIONAL failure: it must surface as a retention error breadcrumb and let the
    # tick continue to finalize — not abort the whole tick as reconcile.tick_failed.
    try:
        retention_result = execute(build_retention_op(project_root, managed_run))
        summary["retention"] = {
            "status": retention_result.get("execution_status"),
            "deleted": retention_result.get("deleted"),
            "kept_latest": retention_result.get("kept_latest"),
            "kept_best": retention_result.get("kept_best"),
        }
        steps_now = [s for s, _ in checkpoint_steps(run_dir)]
        if steps_now:
            progress["last_retention_step"] = max(steps_now)
    except OperationError as exc:
        summary["retention"] = {"error": exc.code}
        tick_had_error = True
        progress["last_error"] = f"retention: {exc.code}"
    except OSError as exc:
        repaired = False
        repair_reported = False
        if isinstance(exc, PermissionError):
            # the root-write trap (hit twice in production): repair ownership with a
            # one-shot docker chown and retry retention ONCE, this tick
            try:
                execute(build_run_dir_chown_op(project_root, managed_run))
                retention_result = execute(build_retention_op(project_root, managed_run))
                summary["retention"] = {
                    "status": retention_result.get("execution_status"),
                    "deleted": retention_result.get("deleted"),
                    "kept_latest": retention_result.get("kept_latest"),
                    "kept_best": retention_result.get("kept_best"),
                    "repaired_via_chown": True,
                }
                steps_now = [s for s, _ in checkpoint_steps(run_dir)]
                if steps_now:
                    progress["last_retention_step"] = max(steps_now)
                repaired = True
            except (OperationError, OSError) as repair_exc:
                summary["retention"] = {
                    "error": f"retention.chown_repair_failed:{type(repair_exc).__name__}"
                }
                tick_had_error = True
                repair_reported = True
                code = getattr(repair_exc, "code", type(repair_exc).__name__)
                progress["last_error"] = f"retention chown repair: {code}"
        # gate on the LOCAL repair flag, not tick_had_error: an earlier QC/eval error
        # must not swallow this retention breadcrumb (that combo — flaky QC + disk
        # pressure — is exactly when the operator needs it)
        if not repaired and not repair_reported:
            summary["retention"] = {"error": f"retention.os_error:{type(exc).__name__}"}
            tick_had_error = True
            progress["last_error"] = (
                f"retention: {type(exc).__name__}: {str(exc)[:120]} "
                "(hint: checkpoints written by a root container need the run dir "
                "chowned to the daemon user)"
            )

    # 4. finalize (notify once + teardown) ONLY when the container has actually, stably
    #    EXITED. We deliberately do NOT finalize on a terminal metrics row alone:
    #      - a RESUMED run appends to the same metrics.jsonl, so a prior segment's
    #        early_stop/done row is still present and would tear down a live, training
    #        container on the very first tick;
    #      - a container in 'restarting'/'paused'/'created' is briefly not-Running without
    #        being done -- state must be 'exited'/'dead', not merely Running==false;
    #      - waiting for the real exit lets the trainer flush its final checkpoint/artifacts
    #        before we docker rm -f it.
    #    terminal_event is kept only to LABEL why it ended.
    try:
        terminal_event = read_terminal_event(resolve_metrics_path(run_dir))
    except OSError as exc:
        # The read flavor of the root-write trap (umask-077 trainer): an unreadable
        # metrics.jsonl must not abort the tick pre-finalize.
        terminal_event = None
        tick_had_error = True
        progress["last_error"] = (
            f"terminal_event: {type(exc).__name__}: {str(exc)[:120]} "
            "(hint: run dir files unreadable by the daemon user)"
        )
    summary["terminal_event"] = terminal_event
    # A container removed out-of-band (docker works, but the container is GONE) after we saw
    # it running is also "ended" -> finalize/notify once instead of polling a ghost forever.
    # An inspect ERROR (docker unreachable) is NOT "gone": status stays unknown, never final.
    container_gone = (not status.get("exists")) and (not status.get("inspect_error"))
    terminally_exited = bool(progress.get("seen_running")) and (
        (status.get("exists") and status.get("state") in ("exited", "dead")) or container_gone
    )
    if terminally_exited and not progress.get("finalized"):
        reason = terminal_event or ("container_gone" if container_gone else "container_exited")
        progress["lifecycle_state"] = "finalizing"
        if managed_run.get("delivery_target_id") and not progress.get("finalize_notified"):
            try:
                execute(
                    build_finalize_notification(
                        project_root, managed_run, reason, progress
                    )
                )
                progress["finalize_notified"] = True
            except OperationError as exc:
                tick_had_error = True
                progress["last_error"] = f"finalize_notify: {exc.code}"
        try:
            execute(build_teardown_op(project_root, managed_run))
            summary["teardown"] = "ok"
            # on_finalize evaluations run AFTER teardown (the container is gone —
            # ops must be self-contained) and exactly once: a failure here keeps its
            # breadcrumb but the run still finalizes, and the finalized short-circuit
            # means the measurement is NOT retried — re-run manually if needed.
            for evaluation in managed_run.get("evaluations") or []:
                if not (evaluation.get("trigger") or {}).get("on_finalize"):
                    continue
                eval_id = str(evaluation.get("eval_id") or "")
                final_step = max(all_steps) if all_steps else 0
                if final_step in progress.get("eval_done", {}).get(eval_id, []):
                    continue  # the periodic trigger already measured this exact step
                try:
                    final_op = build_evaluation_op(
                        project_root, managed_run, evaluation, final_step
                    )
                    _clear_incomplete_qc_record(project_root, final_op)
                    try:
                        execute(final_op)
                    except OperationError as exc:
                        if not exc.code.endswith("_record_exists"):
                            raise  # completed record = genuine replay, benign
                    done_list = progress.setdefault("eval_done", {}).setdefault(eval_id, [])
                    if final_step not in done_list:
                        done_list.append(final_step)
                        done_list.sort()
                except Exception as exc:
                    tick_had_error = True
                    progress["last_error"] = f"eval {eval_id}@finalize: {str(exc)[:120]}"
            try:
                summary["checkpoint_artifacts_recorded"] = record_checkpoint_artifacts(
                    project_root, managed_run, run_dir
                )
            except Exception:  # ledger writing must never fail a finalize
                logging.getLogger("kikai_lab.reconcile").exception(
                    "checkpoint artifact ledger write failed"
                )
            progress["finalized"] = True
            progress["lifecycle_state"] = "done"
            summary["finalized"] = True
            try:
                from kikai_lab.server.registry import append_journal

                append_journal(
                    project_root,
                    "run_finalized",
                    {"run_name": run_id, "terminal_event": terminal_event},
                )
            except Exception:
                pass
        except OperationError as exc:
            # Leave lifecycle_state='finalizing' so the next tick retries teardown.
            summary["teardown"] = {"error": exc.code}
            tick_had_error = True
            progress["last_error"] = f"teardown: {exc.code}"

    if not tick_had_error:
        # A FULLY clean tick (qc, retention, finalize notify, teardown) clears the
        # sticky last_error so operators see current truth, not the ghost of a
        # long-fixed failure — and a still-failing teardown keeps its breadcrumb.
        progress["last_error"] = None
    summary["lifecycle_state"] = progress.get("lifecycle_state")
    write_progress(project_root, run_id, progress)
    return summary


# --------------------------------------------------------------------------- #
# a full pass over every managed run (the reconcile --once unit; serve loops it)
# --------------------------------------------------------------------------- #
def reconcile_once(
    project_root: Path,
    run_id: str | None = None,
    *,
    execute: ExecuteFn | None = None,
    inspect: InspectFn | None = None,
) -> dict[str, Any]:
    project_root = Path(project_root)
    runs = load_managed_runs(project_root, run_id)
    results: list[dict[str, Any]] = []
    for managed_run in runs:
        current_run_id = managed_run.get("run_id")
        try:
            progress = load_progress(project_root, current_run_id)
            results.append(
                tick(project_root, managed_run, progress, execute=execute, inspect=inspect)
            )
        except OperationError as exc:
            results.append({"run_id": current_run_id, "error": exc.code, "error_message": exc.message})
        except Exception as exc:  # pragma: no cover - defensive: one run never kills the pass
            results.append(
                {"run_id": current_run_id, "error": "reconcile.tick_failed", "error_message": str(exc)}
            )
    return {"managed_runs": len(runs), "results": results}


def serve(
    project_root: Path,
    *,
    interval: int = DEFAULT_POLL_INTERVAL_SEC,
    once: bool = False,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    execute: ExecuteFn | None = None,
    inspect: InspectFn | None = None,
) -> dict[str, Any]:
    """Reconcile every ``interval`` seconds until interrupted. ``once=True`` = a single pass."""
    interval = max(1, int(interval))  # never busy-loop on a 0/negative interval
    while True:
        result = reconcile_once(project_root, run_id, execute=execute, inspect=inspect)
        if once:
            return result
        sleep(interval)
