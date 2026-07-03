import json
import os
import subprocess
import sys

from kikai_lab import reconcile
from kikai_lab.operation import OperationError


# --------------------------------------------------------------------------- #
# fixtures / fakes
# --------------------------------------------------------------------------- #
def make_registry(tmp_path, container_id="run_training", docker_name="train-ctr"):
    project_root = tmp_path / "registry"
    containers = project_root / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    (containers / f"{container_id}.yaml").write_text(
        "schema_version: 1\n"
        "kind: docker_container\n"
        f"container_id: {container_id}\n"
        "docker:\n"
        f"  name: {docker_name}\n"
        "  image: example:latest\n"
    )
    return project_root


def make_run_dir(tmp_path, checkpoints, metrics_rows=None):
    run_dir = tmp_path / "runs" / "r"
    ckpt = run_dir / "checkpoints"
    ckpt.mkdir(parents=True, exist_ok=True)
    for name in checkpoints:
        (ckpt / name).write_bytes(b"")
    if metrics_rows is not None:
        (ckpt / "metrics.jsonl").write_text(
            "\n".join(json.dumps(row) for row in metrics_rows) + "\n"
        )
    return run_dir


def managed_run(run_dir, container_id="run_training", **extra):
    mr = {
        "kind": "managed_run",
        "run_id": "r",
        "run_dir": str(run_dir),
        "training_container_id": container_id,
        "retention": {"keep_latest": 2, "keep_best": 2},
        "qc_op": {
            "kind": "kikai_operation",
            "request": {
                "adapter": "script_bundle_run",
                "operation": "qc",
                "bundle_id": "diag_bundle",
                "container_id": "qc_runner",
                "entrypoint": "generate",
                "args": [
                    "--cotrain-checkpoint",
                    "${CONTAINER_TRAINING_RUNS_ROOT}/r/checkpoints/{{checkpoint_name}}",
                    "--out-prefix",
                    "${CONTAINER_TRAINING_RUNS_ROOT}/r/qc/step{{step6}}",
                ],
            },
        },
    }
    mr.update(extra)
    return mr


class FakeExec:
    """Record every op request; optionally fail specific adapters."""

    def __init__(self, fail_adapters=(), retention_result=None):
        self.calls = []
        self.fail_adapters = set(fail_adapters)
        self.retention_result = retention_result or {
            "execution_status": "checkpoint_retention_applied",
            "deleted": [],
            "kept_latest": [],
            "kept_best": [],
        }

    def __call__(self, op):
        request = op["request"]
        adapter = request.get("adapter")
        self.calls.append(request)
        if adapter in self.fail_adapters:
            raise OperationError(f"test.{adapter}_failed", "forced failure")
        if adapter == "checkpoint_retention":
            return self.retention_result
        return {"execution_status": f"{adapter}_ok"}

    def adapters(self):
        return [call.get("adapter") for call in self.calls]

    def qc_calls(self):
        return [call for call in self.calls if call.get("adapter") == "script_bundle_run"]


def fake_inspect(running=True, found=True, exit_code=0, state=None):
    # state = docker State.Status (running|exited|dead|restarting|paused|created).
    if state is None:
        state = "running" if running else "exited"

    def _inspect(request, name):
        if not found:
            return False, [], ""
        return True, [{"State": {"Running": running, "ExitCode": exit_code, "Status": state}}], ""

    return _inspect


# --------------------------------------------------------------------------- #
# QC: new checkpoint -> one QC op, idempotent on re-tick
# --------------------------------------------------------------------------- #
def test_new_checkpoint_triggers_qc_once(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)

    ex = FakeExec()
    progress = reconcile.default_progress("r")
    s1 = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=True))

    assert s1["new_qc_steps"] == [1000]
    qc = ex.qc_calls()
    assert len(qc) == 1
    # placeholders fully substituted with the real (loss-tagged) basename + zero-padded step
    serialized = json.dumps(qc[0])
    assert "{{" not in serialized
    assert "checkpoint_step_001000_loss0p5.pt" in serialized
    assert "step001000" in serialized
    assert "checkpoint_retention" in ex.adapters()

    # second tick: no duplicate QC, retention still runs
    ex2 = FakeExec()
    progress2 = reconcile.load_progress(project_root, "r")
    s2 = reconcile.tick(project_root, mr, progress2, execute=ex2, inspect=fake_inspect(running=True))
    assert s2["new_qc_steps"] == []
    assert ex2.qc_calls() == []
    assert "checkpoint_retention" in ex2.adapters()


def test_retention_runs_after_qc_each_tick(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    ex = FakeExec()
    reconcile.tick(project_root, mr, reconcile.default_progress("r"), execute=ex, inspect=fake_inspect())
    # QC precedes retention (never delete a checkpoint before its diagnostic renders)
    assert ex.adapters().index("script_bundle_run") < ex.adapters().index("checkpoint_retention")


def test_max_step_skips_qc_but_retention_runs(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_005000_loss0p1.pt"])
    mr = managed_run(run_dir, max_step=1000)
    ex = FakeExec()
    s = reconcile.tick(project_root, mr, reconcile.default_progress("r"), execute=ex, inspect=fake_inspect())
    assert s["new_qc_steps"] == []
    assert ex.qc_calls() == []
    assert "checkpoint_retention" in ex.adapters()


def test_qc_failure_leaves_step_unmarked_for_retry(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_004000_loss0p4.pt"])
    mr = managed_run(run_dir)

    ex = FakeExec(fail_adapters=["script_bundle_run"])
    progress = reconcile.default_progress("r")
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect())
    assert s["new_qc_steps"] == []
    assert s["qc_errors"] and s["qc_errors"][0]["step"] == 4000
    assert progress["qc_done_steps"] == []  # NOT marked -> retriable

    # retry next tick with a healthy executor
    ex2 = FakeExec()
    p2 = reconcile.load_progress(project_root, "r")
    s2 = reconcile.tick(project_root, mr, p2, execute=ex2, inspect=fake_inspect())
    assert s2["new_qc_steps"] == [4000]


def test_substitute_preserves_special_characters(tmp_path):
    # M1: values with quotes/backslashes must survive substitution intact -- the old
    # json.dumps->replace->json.loads round-trip would raise JSONDecodeError.
    template = {"request": {"args": ["--label", "{{run_id}}", "--ckpt", "{{checkpoint_name}}"]}}
    out = reconcile._substitute(
        template, {"run_id": 'a"b\\c', "checkpoint_name": "ck_loss0p5.pt"}
    )
    assert out["request"]["args"] == ["--label", 'a"b\\c', "--ckpt", "ck_loss0p5.pt"]
    assert isinstance(out["request"], dict)


def test_qc_record_exists_is_idempotent_success(tmp_path):
    # M3: a crashed prior attempt already delivered -> re-run raises *_record_exists; the
    # daemon must treat that as done (mark it), not an infinite-retry failure.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)

    class RecordExistsExec(FakeExec):
        def __call__(self, op):
            adapter = op["request"].get("adapter")
            self.calls.append(op["request"])
            if adapter == "script_bundle_run":
                raise OperationError("operation.sequence_record_exists", "already recorded")
            if adapter == "checkpoint_retention":
                return self.retention_result
            return {"execution_status": f"{adapter}_ok"}

    ex = RecordExistsExec()
    progress = reconcile.default_progress("r")
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect())
    assert s["qc_errors"] == []               # not a failure
    assert progress["qc_done_steps"] == [1000]  # marked done -> no infinite retry
    assert s["new_qc_steps"] == []            # already delivered, not counted as new


def test_operation_sequence_qc_template_requires_step_varying_pipeline_run_id(tmp_path):
    # C1/C2: an operation_sequence QC op with a constant pipeline_run_id would wedge on
    # sequence_record_exists after the first checkpoint -> reject at build time; in a tick
    # it is a per-step error, NOT a whole-tick abort (retention still runs).
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    mr["qc_op"] = {
        "kind": "kikai_operation",
        "request": {
            "adapter": "operation_sequence",
            "operation": "qc",
            "pipeline_run_id": "constant_id",  # NOT step-varying -> invalid
            "project_root": "examples/example_project",
            "steps": [],
        },
    }
    ex = FakeExec()
    s = reconcile.tick(project_root, mr, reconcile.default_progress("r"), execute=ex, inspect=fake_inspect())
    assert s["new_qc_steps"] == []
    assert s["qc_errors"]
    assert s["qc_errors"][0]["error"] == "reconcile.qc_template_pipeline_run_id_not_step_varying"
    assert "checkpoint_retention" in ex.adapters()  # tick NOT aborted

    # a step-varying pipeline_run_id is accepted and substituted
    mr["qc_op"]["request"]["pipeline_run_id"] = "qc_step{{step6}}"
    op = reconcile.build_qc_op(
        project_root, mr, 1000, run_dir / "checkpoints" / "checkpoint_step_001000_loss0p5.pt"
    )
    assert op["request"]["pipeline_run_id"] == "qc_step001000"


def test_clear_incomplete_qc_record_removes_failed_keeps_completed(tmp_path):
    project_root = make_registry(tmp_path)
    (project_root / "pipeline_runs").mkdir(parents=True, exist_ok=True)

    def rec(pid, status):
        (project_root / "pipeline_runs" / f"{pid}.json").write_text(json.dumps({"status": status}))

    rec("qc_failed", "failed")
    rec("qc_done", "completed")
    reconcile._clear_incomplete_qc_record(
        project_root, {"request": {"adapter": "operation_sequence", "pipeline_run_id": "qc_failed"}}
    )
    reconcile._clear_incomplete_qc_record(
        project_root, {"request": {"adapter": "operation_sequence", "pipeline_run_id": "qc_done"}}
    )
    assert not (project_root / "pipeline_runs" / "qc_failed.json").exists()  # failed -> removed
    assert (project_root / "pipeline_runs" / "qc_done.json").exists()        # completed -> kept


def test_failed_qc_sequence_is_retried_not_marked_done(tmp_path):
    # N1: a failed QC operation_sequence writes a failed pipeline record. On the next tick
    # the daemon must NOT read the resulting sequence_record_exists as success (which would
    # let retention prune an un-QC'd checkpoint); it clears the failed record and re-runs.
    project_root = make_registry(tmp_path)
    (project_root / "pipeline_runs").mkdir(parents=True, exist_ok=True)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    mr["qc_op"] = {
        "kind": "kikai_operation",
        "request": {
            "adapter": "operation_sequence",
            "operation": "qc",
            "pipeline_run_id": "qc_step{{step6}}",
            "project_root": "x",
            "steps": [],
        },
    }

    class SeqExec(FakeExec):
        def __init__(self):
            super().__init__()
            self.qc_attempts = 0

        def __call__(self, op):
            req = op["request"]
            self.calls.append(req)
            if req.get("adapter") == "operation_sequence":
                recp = project_root / "pipeline_runs" / f"{req['pipeline_run_id']}.json"
                if recp.exists():
                    raise OperationError("operation.sequence_record_exists", "exists")
                self.qc_attempts += 1
                if self.qc_attempts == 1:
                    recp.write_text(json.dumps({"status": "failed"}))  # first attempt fails
                    raise OperationError("operation.sequence_step_failed", "failed")
                recp.write_text(json.dumps({"status": "completed"}))  # retry completes
                return {"execution_status": "operation_sequence_completed"}
            if req.get("adapter") == "checkpoint_retention":
                return self.retention_result
            return {"execution_status": "ok"}

    ex = SeqExec()
    p = reconcile.default_progress("r")
    s1 = reconcile.tick(project_root, mr, p, execute=ex, inspect=fake_inspect())
    assert s1["new_qc_steps"] == []          # failed -> not delivered
    assert p["qc_done_steps"] == []          # NOT marked done
    assert s1["qc_errors"]

    p2 = reconcile.load_progress(project_root, "r")
    s2 = reconcile.tick(project_root, mr, p2, execute=ex, inspect=fake_inspect())
    assert s2["new_qc_steps"] == [1000]      # stale failed record cleared -> re-ran -> done
    assert p2["qc_done_steps"] == [1000]


def test_qc_already_delivered_inner_step_is_idempotent_success(tmp_path):
    # N4: crash after the inner delivery succeeded but before the completed pipeline record.
    # Re-run re-hits delivery_record_exists -> sequence_step_failed carrying an inner
    # *_record_exists code -> daemon treats it as delivered (done), not a permanent wedge.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    mr["qc_op"] = {
        "kind": "kikai_operation",
        "request": {
            "adapter": "operation_sequence",
            "operation": "qc",
            "pipeline_run_id": "qc_step{{step6}}",
            "project_root": "x",
            "steps": [
                {"step_id": "generate_qc", "request": {"adapter": "script_bundle_run"}},
                {"step_id": "deliver_diagnostic", "request": {"adapter": "artifact_delivery"}},
            ],
        },
    }

    class DeliveredExec(FakeExec):
        def __call__(self, op):
            req = op["request"]
            self.calls.append(req)
            if req.get("adapter") == "operation_sequence":
                raise OperationError(
                    "operation.sequence_step_failed",
                    "stopped after a failed step",
                    {
                        "failed_step_id": "deliver_diagnostic",  # the LAST step
                        "step_error": {"code": "operation.delivery_record_exists", "message": "dup"},
                    },
                )
            if req.get("adapter") == "checkpoint_retention":
                return self.retention_result
            return {"execution_status": "ok"}

    ex = DeliveredExec()
    p = reconcile.default_progress("r")
    s = reconcile.tick(project_root, mr, p, execute=ex, inspect=fake_inspect())
    assert s["qc_errors"] == []             # not a failure
    assert p["qc_done_steps"] == [1000]     # marked done (already delivered) -> no wedge
    assert s["new_qc_steps"] == []          # not counted as newly posted


def test_early_guarded_step_record_exists_is_not_marked_done(tmp_path):
    # N5: a _record_exists on an EARLY step (not the terminal delivery) must NOT be read as
    # "delivered" -- the later delivery step never ran on this re-run, so marking done would
    # let retention prune an un-QC'd checkpoint. It stays a visible, retried failure.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    mr["qc_op"] = {
        "kind": "kikai_operation",
        "request": {
            "adapter": "operation_sequence",
            "operation": "qc",
            "pipeline_run_id": "qc_step{{step6}}",
            "project_root": "x",
            "steps": [
                {"step_id": "notify_start", "request": {"adapter": "webhook_notification"}},
                {"step_id": "generate_qc", "request": {"adapter": "script_bundle_run"}},
                {"step_id": "deliver_diagnostic", "request": {"adapter": "artifact_delivery"}},
            ],
        },
    }

    class EarlyGuardExec(FakeExec):
        def __call__(self, op):
            req = op["request"]
            self.calls.append(req)
            if req.get("adapter") == "operation_sequence":
                raise OperationError(
                    "operation.sequence_step_failed",
                    "stopped after a failed step",
                    {
                        "failed_step_id": "notify_start",  # the FIRST step, not the delivery
                        "step_error": {"code": "operation.notification_record_exists", "message": "dup"},
                    },
                )
            if req.get("adapter") == "checkpoint_retention":
                return self.retention_result
            return {"execution_status": "ok"}

    ex = EarlyGuardExec()
    p = reconcile.default_progress("r")
    s = reconcile.tick(project_root, mr, p, execute=ex, inspect=fake_inspect())
    assert p["qc_done_steps"] == []  # NOT falsely marked done (diagnostic never delivered)
    assert s["qc_errors"] and s["qc_errors"][0]["error"] == "operation.sequence_step_failed"
    assert "checkpoint_retention" in ex.adapters()


def test_qc_sequence_step_failed_with_real_error_is_retried(tmp_path):
    # a sequence_step_failed whose INNER error is not a record-exists is a genuine failure
    # -> left unmarked, retried; the tick still runs retention.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    mr["qc_op"] = {
        "kind": "kikai_operation",
        "request": {
            "adapter": "operation_sequence",
            "operation": "qc",
            "pipeline_run_id": "qc_step{{step6}}",
            "project_root": "x",
            "steps": [],
        },
    }

    class FailExec(FakeExec):
        def __call__(self, op):
            req = op["request"]
            self.calls.append(req)
            if req.get("adapter") == "operation_sequence":
                raise OperationError(
                    "operation.sequence_step_failed",
                    "stopped after a failed step",
                    {
                        "failed_step_id": "generate_qc",
                        "step_error": {"code": "operation.script_bundle_run_failed", "message": "boom"},
                    },
                )
            if req.get("adapter") == "checkpoint_retention":
                return self.retention_result
            return {"execution_status": "ok"}

    ex = FailExec()
    p = reconcile.default_progress("r")
    s = reconcile.tick(project_root, mr, p, execute=ex, inspect=fake_inspect())
    assert p["qc_done_steps"] == []  # NOT marked done
    assert s["qc_errors"] and s["qc_errors"][0]["error"] == "operation.sequence_step_failed"
    assert "checkpoint_retention" in ex.adapters()


def test_vanished_container_finalizes_after_seen_running(tmp_path):
    # N3: a container removed out-of-band (found=False, docker WORKING) after we saw it
    # running -> treat as ended -> finalize once (don't poll a ghost forever).
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_003000_loss0p3.pt"])
    mr = managed_run(run_dir)
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    ex = FakeExec()
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(found=False))
    assert s["finalized"] is True
    assert "docker_container_restart" in ex.adapters()


def test_docker_unreachable_does_not_finalize(tmp_path):
    # inspect error (docker down) with seen_running must NOT finalize -- unknown != gone.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, [])
    mr = managed_run(run_dir)
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True

    def boom(request, name):
        raise OperationError("operation.docker_not_found", "no docker")

    ex = FakeExec()
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=boom)
    assert s["finalized"] is False
    assert "docker_container_restart" not in ex.adapters()


def test_retention_only_run_without_qc(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)
    mr.pop("qc_op")  # retention-only managed run
    ex = FakeExec()
    s = reconcile.tick(project_root, mr, reconcile.default_progress("r"), execute=ex, inspect=fake_inspect())
    assert s["new_qc_steps"] == []
    assert ex.qc_calls() == []
    assert "checkpoint_retention" in ex.adapters()


# --------------------------------------------------------------------------- #
# terminal detection -> finalize (notify + teardown)
# --------------------------------------------------------------------------- #
def test_early_stop_event_triggers_teardown_then_noop(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(
        tmp_path,
        ["checkpoint_step_002000_loss0p2.pt"],
        metrics_rows=[
            {"event": "train_metrics", "step": 2000, "loss": 0.2},
            {"event": "early_stop", "step": 2000},
        ],
    )
    mr = managed_run(run_dir)

    # container has EXITED (early stop -> trainer exited); we observed it running earlier.
    ex = FakeExec()
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=False))
    assert s["terminal_event"] == "early_stop"
    assert s["finalized"] is True
    assert "docker_container_restart" in ex.adapters()
    assert progress["lifecycle_state"] == "done"

    # once finalized, subsequent ticks are pure no-ops
    ex2 = FakeExec()
    p2 = reconcile.load_progress(project_root, "r")
    s2 = reconcile.tick(project_root, mr, p2, execute=ex2, inspect=fake_inspect(running=False))
    assert s2["lifecycle_state"] == "done"
    assert ex2.calls == []


def test_stale_terminal_event_does_not_finalize_a_running_container(tmp_path):
    # H1: a resumed run appends to metrics.jsonl, so a prior segment's early_stop/done row
    # is present while the container is legitimately RUNNING again. Must NOT tear it down.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(
        tmp_path,
        ["checkpoint_step_030000_loss0p1.pt"],
        metrics_rows=[
            {"event": "early_stop", "step": 2000},          # stale, prior segment
            {"event": "resume", "step": 2000},
            {"event": "train_metrics", "step": 30000, "loss": 0.1},
        ],
    )
    mr = managed_run(run_dir)
    ex = FakeExec()
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=True))
    assert s["terminal_event"] == "early_stop"           # detected...
    assert s["finalized"] is False                        # ...but NOT acted on (still running)
    assert "docker_container_restart" not in ex.adapters()


def test_restarting_container_is_not_finalized(tmp_path):
    # H2: 'restarting'/'paused' is briefly not-Running but NOT terminal -> no teardown.
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_003000_loss0p3.pt"])
    mr = managed_run(run_dir)
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    ex = FakeExec()
    s = reconcile.tick(
        project_root, mr, progress, execute=ex,
        inspect=fake_inspect(running=False, state="restarting"),
    )
    assert s["finalized"] is False
    assert "docker_container_restart" not in ex.adapters()


def test_done_event_triggers_teardown(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(
        tmp_path,
        ["checkpoint_step_060000_loss0p1.pt"],
        metrics_rows=[{"event": "done", "step": 60000}],
    )
    mr = managed_run(run_dir)
    ex = FakeExec()
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=False))
    assert s["terminal_event"] == "done"
    assert "docker_container_restart" in ex.adapters()


def test_terminal_event_read_from_run_dir_metrics(tmp_path):
    # the trainer writes metrics.jsonl at run_dir/metrics.jsonl (sibling of checkpoints/),
    # NOT inside checkpoints/ -- the daemon must read it there for terminal detection
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_002000_loss0p2.pt"])
    (run_dir / "metrics.jsonl").write_text(json.dumps({"event": "early_stop", "step": 2000}) + "\n")
    mr = managed_run(run_dir)
    ex = FakeExec()
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=False))
    assert s["terminal_event"] == "early_stop"  # read from run_dir/metrics.jsonl (not checkpoints/)
    assert "docker_container_restart" in ex.adapters()  # exited -> finalized


def test_resolve_metrics_path_prefers_run_dir_then_falls_back(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "checkpoints").mkdir(parents=True)
    # only the nested (legacy) file exists -> fallback
    (run_dir / "checkpoints" / "metrics.jsonl").write_text("{}\n")
    assert reconcile.resolve_metrics_path(run_dir) == run_dir / "checkpoints" / "metrics.jsonl"
    # the run_dir file exists -> preferred
    (run_dir / "metrics.jsonl").write_text("{}\n")
    assert reconcile.resolve_metrics_path(run_dir) == run_dir / "metrics.jsonl"


def test_container_exit_finalizes_after_seen_running(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_003000_loss0p3.pt"])
    mr = managed_run(run_dir)
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True  # observed running on an earlier tick
    ex = FakeExec()
    s = reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=False, found=True))
    assert s["finalized"] is True
    assert "docker_container_restart" in ex.adapters()


def test_not_running_but_never_seen_does_not_finalize(tmp_path):
    # a not-yet-started / slow-first-checkpoint container must NOT be torn down
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, [])
    mr = managed_run(run_dir)
    ex = FakeExec()
    s = reconcile.tick(project_root, mr, reconcile.default_progress("r"), execute=ex, inspect=fake_inspect(running=False, found=True))
    assert s["finalized"] is False
    assert "docker_container_restart" not in ex.adapters()


def test_finalize_notification_sent_once(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(
        tmp_path,
        ["checkpoint_step_002000_loss0p2.pt"],
        metrics_rows=[{"event": "early_stop", "step": 2000}],
    )
    mr = managed_run(run_dir, delivery_target_id="discord_progress")
    # container EXITED (early stop); teardown fails -> stays 'finalizing', notify once only
    ex = FakeExec(fail_adapters=["docker_container_restart"])
    progress = reconcile.default_progress("r")
    progress["seen_running"] = True
    reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=False))
    assert progress["finalized"] is False
    assert progress["finalize_notified"] is True
    assert progress["lifecycle_state"] == "finalizing"
    notifs = [c for c in ex.calls if c.get("adapter") == "webhook_notification"]
    assert len(notifs) == 1

    # next tick retries teardown but does NOT re-notify
    ex2 = FakeExec()
    p2 = reconcile.load_progress(project_root, "r")
    reconcile.tick(project_root, mr, p2, execute=ex2, inspect=fake_inspect(running=False))
    assert [c for c in ex2.calls if c.get("adapter") == "webhook_notification"] == []
    assert "docker_container_restart" in ex2.adapters()
    assert p2["finalized"] is True


# --------------------------------------------------------------------------- #
# status tolerance
# --------------------------------------------------------------------------- #
def test_inspect_error_is_tolerated(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = managed_run(run_dir)

    def boom(request, name):
        raise OperationError("operation.docker_not_found", "no docker")

    ex = FakeExec()
    s = reconcile.tick(project_root, mr, reconcile.default_progress("r"), execute=ex, inspect=boom)
    assert s["status"]["inspect_error"] == "operation.docker_not_found"
    assert s["finalized"] is False  # unknown status never finalizes
    assert "checkpoint_retention" in ex.adapters()


# --------------------------------------------------------------------------- #
# progress persistence (atomic roundtrip + corrupt recovery)
# --------------------------------------------------------------------------- #
def test_progress_roundtrip_and_corrupt_recovery(tmp_path):
    project_root = make_registry(tmp_path)
    p = reconcile.default_progress("r")
    p["qc_done_steps"] = [1000, 2000]
    reconcile.write_progress(project_root, "r", p)
    assert reconcile.load_progress(project_root, "r")["qc_done_steps"] == [1000, 2000]

    # a corrupt/partial file -> clean default (never crash the tick)
    reconcile.progress_path(project_root, "r").write_text("{ not json")
    assert reconcile.load_progress(project_root, "r") == reconcile.default_progress("r")


# --------------------------------------------------------------------------- #
# whole-pass + serve --once (loading managed_runs/*.yaml)
# --------------------------------------------------------------------------- #
def write_managed_run_yaml(project_root, run_dir, run_id="r", container_id="run_training", retention=(2, 2)):
    directory = project_root / "managed_runs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}.yaml").write_text(
        "schema_version: 1\n"
        "kind: managed_run\n"
        f"run_id: {run_id}\n"
        f"run_dir: {run_dir}\n"
        f"training_container_id: {container_id}\n"
        "retention:\n"
        f"  keep_latest: {retention[0]}\n"
        f"  keep_best: {retention[1]}\n"
    )


def test_reconcile_once_over_registry(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    write_managed_run_yaml(project_root, run_dir)
    ex = FakeExec()
    result = reconcile.reconcile_once(project_root, execute=ex, inspect=fake_inspect(running=True))
    assert result["managed_runs"] == 1
    assert result["results"][0]["run_id"] == "r"
    assert "checkpoint_retention" in ex.adapters()


def test_serve_once_returns_single_pass(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, [])
    write_managed_run_yaml(project_root, run_dir)
    ex = FakeExec()
    result = reconcile.serve(project_root, once=True, execute=ex, inspect=fake_inspect(running=True))
    assert result["managed_runs"] == 1


def test_load_managed_run_rejects_bad_kind(tmp_path):
    project_root = tmp_path / "registry"
    directory = project_root / "managed_runs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bad.yaml").write_text("kind: not_a_managed_run\nrun_id: r\n")
    try:
        reconcile.load_managed_runs(project_root)
        raise AssertionError("expected OperationError")
    except OperationError as exc:
        assert exc.code == "reconcile.managed_run_invalid"


# --------------------------------------------------------------------------- #
# CLI: `kikai reconcile --once` drives real execute_operation(checkpoint_retention)
# --------------------------------------------------------------------------- #
def run_cli(*args, env=None):
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
        env=run_env,
    )


def test_cli_reconcile_once_applies_retention(tmp_path):
    # retention-only run so the CLI path exercises the REAL execute_operation end to end
    # (docker inspect is tolerated whether or not docker exists on the test host).
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(
        tmp_path,
        [
            "checkpoint_step_001000_loss0p3.pt",
            "checkpoint_step_002000_loss0p2.pt",
            "checkpoint_step_003000_loss0p1.pt",
        ],
    )
    write_managed_run_yaml(project_root, run_dir, retention=(2, 0))

    result = run_cli("reconcile", "--project-root", str(project_root), "--once")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert data["managed_runs"] == 1
    ckpt_dir = run_dir / "checkpoints"
    # keep_latest=2 -> oldest of the three periodic checkpoints was pruned
    assert not (ckpt_dir / "checkpoint_step_001000_loss0p3.pt").exists()
    assert (ckpt_dir / "checkpoint_step_003000_loss0p1.pt").exists()
    # progress state was persisted
    assert reconcile.progress_path(project_root, "r").exists()


def test_clean_tick_clears_stale_last_error(tmp_path):
    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "managed_runs").mkdir()
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    managed = {"run_id": "r1", "run_dir": str(run_dir), "training_container_id": "c1"}
    progress = r.default_progress("r1")
    progress["last_error"] = "qc step 1000: operation.docker_run_failed"

    def fake_execute(op):
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def fake_inspect(request, name):
        return False, [], "gone"

    summary = r.tick(project, managed, progress, execute=fake_execute, inspect=fake_inspect)
    assert summary["qc_errors"] == []
    assert progress["last_error"] is None


def test_foreground_docker_run_clears_exited_leftover(tmp_path, monkeypatch):
    import json as _json

    from kikai_lab import operation as op

    (tmp_path / "containers").mkdir()
    (tmp_path / "containers" / "qc.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: qc\n"
        "docker:\n  name: qc-ctr\n  image: img\n",
        encoding="utf-8",
    )
    control = tmp_path / "ctl.json"
    control.write_text(_json.dumps({"state": {"Running": False, "Status": "exited"}}))
    log = tmp_path / "argv.jsonl"
    fake = tmp_path / "fake_docker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"c = pathlib.Path({str(control)!r})\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "ctl = json.loads(c.read_text())\n"
        "log.open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'inspect':\n"
        "    if ctl.get('state') is None:\n"
        "        sys.stderr.write('no such object'); raise SystemExit(1)\n"
        "    print(json.dumps([{'State': ctl['state']}])); raise SystemExit(0)\n"
        "if cmd == 'rm':\n"
        "    ctl['state'] = None; c.write_text(json.dumps(ctl)); raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    request = {
        "adapter": "docker_run",
        "operation": "qc_once",
        "project_root": str(tmp_path),
        "container_id": "qc",
        "argv": ["python3", "x.py"],
    }
    result = op.execute_docker_run_operation(request)
    argvs = [_json.loads(line) for line in log.read_text().splitlines()]
    assert any(a[:2] == ["rm", "-f"] for a in argvs)  # exited leftover cleared
    assert any(a[0] == "run" for a in argvs)
    assert result.get("execution_status")

    # A RUNNING holder is a precise 409-class error, not docker stderr soup.
    import pytest

    control.write_text(_json.dumps({"state": {"Running": True, "Status": "running"}}))
    with pytest.raises(op.OperationError) as excinfo:
        op.execute_docker_run_operation(request)
    assert excinfo.value.code == "operation.docker_run_name_in_use"



def test_teardown_error_keeps_last_error_breadcrumb(tmp_path):
    from kikai_lab import reconcile as r
    from kikai_lab.operation import OperationError

    project = tmp_path
    (project / "managed_runs").mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    managed = {"run_id": "r1", "run_dir": str(run_dir), "training_container_id": "c1"}
    progress = r.default_progress("r1")
    progress["seen_running"] = True  # container observed running earlier

    def failing_execute(op):
        if op["request"]["adapter"] == "docker_container_restart":
            raise OperationError("operation.docker_teardown_failed", "boom", {})
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def exited_inspect(request, name):
        return True, [{"State": {"Running": False, "Status": "exited", "ExitCode": 0}}], ""

    summary = r.tick(project, managed, progress, execute=failing_execute, inspect=exited_inspect)
    assert summary["teardown"] == {"error": "operation.docker_teardown_failed"}
    # the clean-tick clear must NOT wipe a same-tick teardown failure breadcrumb
    assert progress["last_error"] == "teardown: operation.docker_teardown_failed"


def test_preflight_never_removes_created_or_unsafe_names(tmp_path, monkeypatch):
    import json as _json

    import pytest

    from kikai_lab import operation as op

    (tmp_path / "containers").mkdir()
    control = tmp_path / "ctl.json"
    log = tmp_path / "argv.jsonl"
    fake = tmp_path / "fake_docker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"c = pathlib.Path({str(control)!r})\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "ctl = json.loads(c.read_text())\n"
        "log.open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'inspect':\n"
        "    print(json.dumps([{'State': ctl['state']}])); raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    (tmp_path / "containers" / "qc.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: qc\n"
        "docker:\n  name: qc-ctr\n  image: img\n",
        encoding="utf-8",
    )
    request = {
        "adapter": "docker_run",
        "operation": "qc_once",
        "project_root": str(tmp_path),
        "container_id": "qc",
        "argv": ["python3", "x.py"],
    }
    # created (Running=false but NOT terminally stopped): never rm — precise conflict
    control.write_text(_json.dumps({"state": {"Running": False, "Status": "created"}}))
    with pytest.raises(op.OperationError) as excinfo:
        op.execute_docker_run_operation(request)
    assert excinfo.value.code == "operation.docker_run_name_in_use"
    argvs = [_json.loads(line) for line in log.read_text().splitlines()]
    assert not any(a[0] == "rm" for a in argvs)

    # unsafe declared name: the run won't pass --name, so the preflight must not
    # inspect/rm either (docker rm accepts ids/prefixes — could hit a stranger)
    (tmp_path / "containers" / "bad.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: bad\n"
        "docker:\n  name: 'bad/../name'\n  image: img\n",
        encoding="utf-8",
    )
    log.write_text("")
    op.execute_docker_run_operation({**request, "container_id": "bad", "operation": "b"})
    argvs = [_json.loads(line) for line in log.read_text().splitlines()]
    assert not any(a[0] in ("inspect", "rm") for a in argvs)


def test_successful_qc_records_artifact_ledger_rows(tmp_path):
    import json as _json

    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "managed_runs").mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "checkpoints" / "checkpoint_step_000500.pt").write_bytes(b"x")
    qc_dir = run_dir / "qc" / "reconcile_step000500"
    qc_dir.mkdir(parents=True)
    (qc_dir / "preview.mp4").write_bytes(b"video")
    (qc_dir / "preview.png").write_bytes(b"img")  # same stem: must NOT shadow the video
    (qc_dir / ("x" * 80 + ".mp4")).write_bytes(b"long")  # sanitized+hashed, <=64 chars
    (qc_dir / "notes.txt").write_text("skip me")

    managed = {
        "run_id": "example_run",
        "run_dir": str(run_dir),
        "training_container_id": "c1",
        "qc_op": {"kind": "kikai_operation", "schema_version": 1,
                  "request": {"adapter": "noop", "operation": "qc_{{step6}}"}},
        "qc_artifacts_dir": str(run_dir / "qc" / "reconcile_step{{step6}}"),
    }

    def fake_execute(op):
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def fake_inspect(request, name):
        return False, [], "gone"

    summary = r.tick(project, managed, r.default_progress("example_run"),
                     execute=fake_execute, inspect=fake_inspect)
    assert summary["new_qc_steps"] == [500]
    rows = [_json.loads(line) for line in
            (project / "artifacts" / "example_run.jsonl").read_text().splitlines()]
    ids = {row["artifact_id"]: row for row in rows}
    assert "example_run_qc_step000500_preview_mp4" in ids
    assert "example_run_qc_step000500_preview_png" in ids  # suffix disambiguates
    video = ids["example_run_qc_step000500_preview_mp4"]
    assert video["kind"] == "qc_video"
    assert video["locations"][0]["path"].endswith("preview.mp4")
    import re as _re

    safe = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    assert all(safe.match(i) for i in ids)  # every id is fetchable via the API
    assert any(i.endswith("_mp4") and "-" in i for i in ids)  # long name got hashed
    assert all("notes" not in i for i in ids)  # non-media skipped

    # replayed/already-recorded QCs do not duplicate ledger rows
    progress = r.load_progress(project, "example_run")
    r.tick(project, managed, progress, execute=fake_execute, inspect=fake_inspect)
    rows2 = (project / "artifacts" / "example_run.jsonl").read_text().splitlines()
    assert len(rows2) == len(rows)


def test_retention_oserror_is_breadcrumb_not_tick_abort(tmp_path):
    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "managed_runs").mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    managed = {"run_id": "r1", "run_dir": str(run_dir), "training_container_id": "c1"}

    seen = []

    def execute(op):
        seen.append(op["request"]["adapter"])
        if op["request"]["adapter"] == "checkpoint_retention":
            raise PermissionError(13, "Permission denied", "checkpoint_step_000500.pt")
        return {"execution_status": "ok"}

    def inspect(request, name):
        return False, [], "gone"

    progress = r.default_progress("r1")
    summary = r.tick(project, managed, progress, execute=execute, inspect=inspect)
    # tick completed (no reconcile.tick_failed): a chown repair was attempted, the
    # retry still hit EPERM -> actionable breadcrumb, not an abort
    assert seen.count("run_dir_chown") == 1
    assert summary["retention"]["error"].startswith("retention.chown_repair_failed")
    assert "chown repair" in progress["last_error"]

    # a NON-permission OSError never triggers the docker repair
    seen.clear()

    def execute_enospc(op):
        seen.append(op["request"]["adapter"])
        if op["request"]["adapter"] == "checkpoint_retention":
            raise OSError(28, "No space left on device")
        return {"execution_status": "ok"}

    progress2 = r.default_progress("r1")
    summary2 = r.tick(project, managed, progress2, execute=execute_enospc, inspect=inspect)
    assert "run_dir_chown" not in seen
    assert summary2["retention"]["error"].startswith("retention.os_error")
    assert "chowned" in progress2["last_error"]

    # an EARLIER tick error (flaky QC) must not swallow the retention breadcrumb
    managed_qc = dict(
        managed,
        qc_op_template={
            "kind": "kikai_operation",
            "schema_version": 1,
            "request": {"adapter": "noop", "operation": "r1_qc_{step6}"},
        },
    )
    (run_dir / "checkpoints").mkdir(exist_ok=True)
    (run_dir / "checkpoints" / "checkpoint_step_000500.pt").write_bytes(b"x")

    def execute_qc_fail_then_enospc(op):
        adapter = op["request"]["adapter"]
        if adapter == "noop":
            raise r.OperationError("operation.docker_run_failed", "qc broke", {})
        if adapter == "checkpoint_retention":
            raise OSError(28, "No space left on device")
        return {"execution_status": "ok"}

    progress3 = r.default_progress("r1")
    summary3 = r.tick(
        project, managed_qc, progress3, execute=execute_qc_fail_then_enospc, inspect=inspect
    )
    assert summary3["qc_errors"]  # the earlier error really happened
    assert summary3["retention"]["error"].startswith("retention.os_error")


def test_retention_permission_error_repaired_via_chown(tmp_path):
    import os as _os

    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "managed_runs").mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    managed = {"run_id": "r1", "run_dir": str(run_dir), "training_container_id": "c1"}

    calls = []

    def execute(op):
        calls.append(op["request"])
        retentions = [c for c in calls if c["adapter"] == "checkpoint_retention"]
        if op["request"]["adapter"] == "checkpoint_retention" and len(retentions) == 1:
            raise PermissionError(13, "Permission denied", "checkpoint_step_000500.pt")
        return {
            "execution_status": "ok",
            "deleted": ["old.pt"],
            "kept_latest": ["new.pt"],
            "kept_best": [],
        }

    def inspect(request, name):
        return False, [], "gone"

    progress = r.default_progress("r1")
    summary = r.tick(project, managed, progress, execute=execute, inspect=inspect)
    chowns = [c for c in calls if c["adapter"] == "run_dir_chown"]
    assert len(chowns) == 1
    assert chowns[0]["uid"] == _os.getuid() and chowns[0]["gid"] == _os.getgid()
    assert chowns[0]["container_id"] == "c1" and chowns[0]["run_dir"] == str(run_dir)
    assert summary["retention"]["repaired_via_chown"] is True
    assert summary["retention"]["deleted"] == ["old.pt"]
    assert progress["last_error"] is None  # repaired -> no breadcrumb


def test_finalize_notification_gates_and_conclusion_reminder(tmp_path):
    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "runs").mkdir()
    managed = {"run_id": "r1", "delivery_target_id": "discord"}

    # no conclusion + a failed gate -> reminder line + warning severity
    progress = {"check_verdicts": {"highpass_must_decline": "fail", "style": "pass"}}
    op = r.build_finalize_notification(project, managed, "done", progress)
    msg = op["request"]["message"]
    assert "gates: highpass_must_decline=fail, style=pass" in msg
    assert "/runs/r1/conclusion" in msg
    assert op["request"]["severity"] == "warning"

    # conclusion recorded + all gates pass -> no reminder, info severity
    (project / "runs" / "r1.yaml").write_text(
        "schema_version: 1\nrun_name: r1\nconclusions:\n"
        "  - verdict: adopted\n    summary: ok\n",
        encoding="utf-8",
    )
    op = r.build_finalize_notification(
        project, managed, "done", {"check_verdicts": {"style": "pass"}}
    )
    assert "conclusion" not in op["request"]["message"]
    assert op["request"]["severity"] == "info"

    # progress omitted entirely -> still valid (backward-compatible callers)
    op = r.build_finalize_notification(project, managed, "done")
    assert op["request"]["severity"] == "info"


def test_finalize_records_surviving_checkpoints_as_artifacts(tmp_path):
    import json as _json

    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "managed_runs").mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    ckpts = run_dir / "checkpoints"
    ckpts.mkdir(parents=True)
    (ckpts / "best_checkpoint.pt").write_bytes(b"b")
    (ckpts / "best_step_000400_loss1p0.pt").write_bytes(b"b")
    (ckpts / "checkpoint_step_000500_loss1p1.pt").write_bytes(b"c")
    managed = {"run_id": "r1", "run_dir": str(run_dir), "training_container_id": "c1"}

    def execute(op):
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def gone_inspect(request, name):
        return False, [], "gone"

    progress = r.default_progress("r1")
    progress["seen_running"] = True  # container ended out-of-band -> finalize path
    summary = r.tick(project, managed, progress, execute=execute, inspect=gone_inspect)
    assert summary["finalized"] is True
    recorded = summary["checkpoint_artifacts_recorded"]
    assert any(i.startswith("r1_ckpt_best_checkpoint") for i in recorded)
    rows = [
        _json.loads(line)
        for line in (project / "artifacts" / "r1.jsonl").read_text().splitlines()
    ]
    assert all(row["kind"] == "checkpoint" for row in rows)
    assert len(rows) == 3


def test_declared_evaluations_and_metric_checks_run_automatically(tmp_path):
    import json as _json

    from kikai_lab import reconcile as r

    project = tmp_path
    (project / "managed_runs").mkdir()
    (project / "containers").mkdir()
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    ckpts = run_dir / "checkpoints"
    ckpts.mkdir(parents=True)
    for step in (1000, 2000):
        (ckpts / f"checkpoint_step_{step:06d}.pt").write_bytes(b"x")
    # metrics: value flat in [0, 2000] -> 'decreasing' check must FAIL
    rows = [
        {"event": "train_metrics", "step": s, "loss": 5.0, "sharpness": 0.0025}
        for s in range(100, 2100, 100)
    ]
    (run_dir / "metrics.jsonl").write_text(
        "\n".join(_json.dumps(x) for x in rows) + "\n", encoding="utf-8"
    )
    (project / "delivery_targets").mkdir()

    managed = {
        "run_id": "r1",
        "run_dir": str(run_dir),
        "training_container_id": "c1",
        "delivery_target_id": "dt",
        "evaluations": [
            {
                "eval_id": "teeth",
                "trigger": {"every_n_steps": 2000},
                "op": {
                    "kind": "kikai_operation",
                    "schema_version": 1,
                    "request": {"adapter": "noop", "operation": "teeth_{{step6}}"},
                },
            }
        ],
        "metric_checks": [
            {
                "check_id": "highpass_must_decline",
                "key": "sharpness",
                "expect": "decreasing",
                "window_steps": [100, 2000],
                "min_delta": 0.0001,
            }
        ],
    }

    executed = []

    def execute(op):
        executed.append(op["request"])
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def inspect(request, name):
        return True, [{"State": {"Running": True, "Status": "running"}}], ""

    progress = r.default_progress("r1")
    summary = r.tick(project, managed, progress, execute=execute, inspect=inspect)

    # evaluation ran exactly once at step 2000 (not 1000), tracked idempotently
    eval_ops = [q for q in executed if q["operation"].startswith("teeth_")]
    assert [q["operation"] for q in eval_ops] == ["teeth_002000"]
    assert progress["eval_done"]["teeth"] == [2000]

    # flat metric -> decreasing check fails, verdict recorded, notification sent once
    assert summary["metric_checks"][0]["verdict"] == "fail"
    assert progress["check_verdicts"]["highpass_must_decline"] == "fail"
    notes = [q for q in executed if q["adapter"] == "webhook_notification"]
    assert len(notes) == 1 and "FAILED" in notes[0]["message"]

    # second tick: no re-run, no duplicate notification
    executed.clear()
    r.tick(project, managed, progress, execute=execute, inspect=inspect)
    assert [q for q in executed if q["operation"].startswith("teeth_")] == []
    assert [q for q in executed if q["adapter"] == "webhook_notification"] == []


def _eval_project(tmp_path, metric_rows=None):
    import json as _json

    project = tmp_path
    (project / "managed_runs").mkdir(exist_ok=True)
    (project / "containers").mkdir(exist_ok=True)
    (project / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: img\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "rd"
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoints" / "checkpoint_step_002000.pt").write_bytes(b"x")
    if metric_rows:
        (run_dir / "metrics.jsonl").write_text(
            "\n".join(_json.dumps(x) for x in metric_rows) + "\n", encoding="utf-8"
        )
    return project, run_dir


def test_failed_evaluation_is_retried_not_faked_done(tmp_path):
    """A failed sequence eval writes a failed pipeline record; the retry must clear it
    and re-run — never convert _record_exists into false success (reviewer HIGH)."""
    import json as _json

    from kikai_lab import reconcile as r
    from kikai_lab.operation import OperationError

    project, run_dir = _eval_project(tmp_path)
    managed = {
        "run_id": "r1",
        "run_dir": str(run_dir),
        "training_container_id": "c1",
        "evaluations": [
            {
                "eval_id": "teeth",
                "trigger": {"every_n_steps": 2000},
                "op": {
                    "kind": "kikai_operation",
                    "schema_version": 1,
                    "request": {
                        "adapter": "operation_sequence",
                        "operation": "teeth_{{step6}}",
                        "pipeline_run_id": "teeth_{{step6}}",
                        "steps": [{"step_id": "s1", "request": {"adapter": "noop"}}],
                    },
                },
            }
        ],
    }
    record_path = project / "pipeline_runs" / "teeth_002000.json"
    record_path.parent.mkdir()

    calls = {"n": 0}

    def execute(op):
        if op["request"].get("adapter") == "operation_sequence":
            calls["n"] += 1
            if calls["n"] == 1:
                # failed run leaves a failed pipeline record (adapter contract)
                record_path.write_text(_json.dumps({"status": "failed"}))
                raise OperationError(
                    "operation.sequence_step_failed", "boom",
                    {"failed_step_id": "s1", "step_error": {"code": "operation.script_bundle_run_failed"}},
                )
            record_path.write_text(_json.dumps({"status": "completed"}))
            return {"execution_status": "ok"}
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def inspect(request, name):
        return True, [{"State": {"Running": True, "Status": "running"}}], ""

    progress = r.default_progress("r1")
    s1 = r.tick(project, managed, progress, execute=execute, inspect=inspect)
    assert s1["eval_errors"], "first tick must surface the failure"
    assert progress["eval_done"].get("teeth", []) == []
    assert progress["last_error"].startswith("eval teeth@2000")

    s2 = r.tick(project, managed, progress, execute=execute, inspect=inspect)
    assert calls["n"] == 2, "retry must clear the failed record and re-run"
    assert progress["eval_done"]["teeth"] == [2000]
    assert not s2["eval_errors"]


def test_fail_pass_fail_notifies_each_transition(tmp_path):
    import json as _json

    from kikai_lab import reconcile as r

    rows_flat = [
        {"event": "train_metrics", "step": s, "sharpness": 0.0025}
        for s in range(100, 2100, 100)
    ]
    project, run_dir = _eval_project(tmp_path, metric_rows=rows_flat)
    managed = {
        "run_id": "r1",
        "run_dir": str(run_dir),
        "training_container_id": "c1",
        "delivery_target_id": "dt",
        "metric_checks": [
            {"check_id": "hp", "key": "sharpness", "expect": "decreasing",
             "window_steps": [100, 2000], "min_delta": 0.0001}
        ],
    }
    notified = []

    def execute(op):
        if op["request"]["adapter"] == "webhook_notification":
            notified.append(op["request"]["notification_id"])
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def inspect(request, name):
        return True, [{"State": {"Running": True, "Status": "running"}}], ""

    progress = r.default_progress("r1")
    r.tick(project, managed, progress, execute=execute, inspect=inspect)
    assert len(notified) == 1  # first fail

    # improve -> pass
    rows_decline = rows_flat + [
        {"event": "train_metrics", "step": s, "sharpness": 0.001}
        for s in range(1400, 2100, 100)
    ]
    (run_dir / "metrics.jsonl").write_text(
        "\n".join(_json.dumps(x) for x in rows_decline) + "\n", encoding="utf-8"
    )
    r.tick(project, managed, progress, execute=execute, inspect=inspect)
    assert progress["check_verdicts"]["hp"] == "pass"

    # regress -> fail again: a SECOND notification with a distinct id
    (run_dir / "metrics.jsonl").write_text(
        "\n".join(_json.dumps(x) for x in rows_flat) + "\n", encoding="utf-8"
    )
    r.tick(project, managed, progress, execute=execute, inspect=inspect)
    assert len(notified) == 2 and notified[0] != notified[1]


def test_malformed_check_never_disables_siblings_and_typo_trigger_is_loud(tmp_path):

    from kikai_lab import reconcile as r

    rows = [
        {"event": "train_metrics", "step": s, "loss": 5.0 - s * 0.001}
        for s in range(100, 2100, 100)
    ]
    project, run_dir = _eval_project(tmp_path, metric_rows=rows)
    managed = {
        "run_id": "r1",
        "run_dir": str(run_dir),
        "training_container_id": "c1",
        "metric_checks": [
            {"check_id": "broken", "key": "loss", "expect": "decreasing",
             "window_steps": ["oops", 2000]},
            {"check_id": "valid", "key": "loss", "expect": "decreasing",
             "window_steps": [100, 2000], "min_delta": 0.0001},
        ],
        "evaluations": [
            {"eval_id": "typo", "trigger": {"every_n_step": 1000},
             "op": {"kind": "kikai_operation", "schema_version": 1,
                    "request": {"adapter": "noop", "operation": "x"}}}
        ],
    }

    def execute(op):
        return {"execution_status": "ok", "deleted": [], "kept_latest": [], "kept_best": []}

    def inspect(request, name):
        return True, [{"State": {"Running": True, "Status": "running"}}], ""

    progress = r.default_progress("r1")
    summary = r.tick(project, managed, progress, execute=execute, inspect=inspect)
    verdicts = {v["check_id"]: v["verdict"] for v in summary["metric_checks"]}
    assert verdicts["broken"] == "invalid_check"
    assert verdicts["valid"] == "pass"  # sibling unaffected
    assert summary["eval_errors"][0]["error"] == "reconcile.evaluation_invalid"


def test_metric_checks_relative_windows(tmp_path):
    import json as _json

    from kikai_lab import reconcile as r

    metrics = tmp_path / "metrics.jsonl"
    # a probe segment resumed at step 60: rows 61..85, key declines steadily
    with metrics.open("w") as f:
        for step in range(61, 86):
            f.write(_json.dumps({
                "event": "train_metrics", "step": step, "tex": 1.0 - 0.01 * step,
            }) + "\n")
    checks = [
        {"check_id": "rel_decline", "key": "tex", "expect": "decreasing",
         "window_steps": [0, 20], "window_steps_relative": True},
        {"check_id": "rel_pending", "key": "tex", "expect": "decreasing",
         "window_steps": [0, 60], "window_steps_relative": True},
        {"check_id": "abs_pending", "key": "tex", "expect": "decreasing",
         "window_steps": [0, 20]},  # absolute [0,20] precedes the segment
        {"check_id": "rel_no_window", "key": "tex", "expect": "decreasing",
         "window_steps_relative": True},  # relative w/o window = eternal pending trap
    ]
    rows = {c["check_id"]: c for c in r.run_metric_checks(metrics, checks, 85)}
    # relative [0,20] -> absolute [61,81]: reachable and declining
    assert rows["rel_decline"]["verdict"] == "pass"
    # relative [0,60] -> absolute [61,121]: beyond latest -> pending
    assert rows["rel_pending"]["verdict"] == "pending"
    # absolute [0,20] with no rows in-window stays a data problem, not a pass
    assert rows["abs_pending"]["verdict"] in ("no_data", "invalid_check", "fail")
    # relative without explicit window is rejected, not eternally pending
    assert rows["rel_no_window"]["verdict"] == "invalid_check"
