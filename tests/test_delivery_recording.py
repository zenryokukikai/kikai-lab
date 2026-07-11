"""Delivery-outcome recording: QC/probe op stdout -> progress['delivery'] -> status API.

The failure mode this covers: a QC video renders fine, the Discord post fails
(or is skipped), and nothing anywhere says so without ssh-reading the op's
stdout on the host. The reconciler now parses the delivery events it already
holds and the status endpoint exposes the outcomes."""
from __future__ import annotations

import json

from kikai_lab import reconcile
from kikai_lab.operation import OperationError
from tests.test_reconcile import FakeExec, fake_inspect, make_registry, make_run_dir
from tests.test_reconcile import managed_run as make_managed_run

POST_OK = '{"event": "discord_post", "status": 200}'
POST_FAIL = '{"event": "discord_post", "status": 429}'
SKIPPED = '{"event": "discord_post_skipped", "reason": "no_webhook"}'


class StdoutExec(FakeExec):
    """FakeExec whose script_bundle_run results carry a canned stdout."""

    def __init__(self, stdout: str, **kwargs):
        super().__init__(**kwargs)
        self.stdout = stdout

    def __call__(self, op):
        result = super().__call__(op)
        if op["request"].get("adapter") == "script_bundle_run":
            result["stdout"] = self.stdout
        return result


# ------------------------------------------------------------ pure extraction
def test_extract_events_from_flat_stdout():
    result = {"stdout": f"noise\n{POST_OK}\nmore noise\n"}
    events = reconcile.extract_delivery_events(result)
    assert events == [{"event": "discord_post", "status": 200}]


def test_extract_events_from_nested_sequence_result():
    result = {
        "execution_status": "operation_sequence_completed",
        "steps": [
            {"step_id": "render", "result": {"stdout": "rendered\n"}},
            {"step_id": "post", "result": {"stdout": f"{POST_FAIL}\n"}},
        ],
    }
    events = reconcile.extract_delivery_events(result)
    assert events == [{"event": "discord_post", "status": 429}]


def test_extract_events_recognizes_artifact_delivery_adapter_result():
    result = {
        "steps": [
            {
                "step_id": "deliver",
                "result": {
                    "execution_status": "artifact_delivery_completed",
                    "http_status": 204,
                },
            }
        ]
    }
    assert reconcile.extract_delivery_events(result) == [
        {"event": "discord_post", "status": 204}
    ]


def test_delivery_entry_vocabulary():
    assert reconcile.delivery_entry({"stdout": POST_OK}) == {"status": 200}
    assert reconcile.delivery_entry({"stdout": SKIPPED}) == {
        "status": None,
        "skipped_reason": "no_webhook",
    }
    assert reconcile.delivery_entry({"stdout": "just logs\n"}) == {
        "status": None,
        "skipped_reason": "no_delivery_event",
    }
    # corrupt JSON-looking lines and non-int statuses degrade, never raise
    assert reconcile.delivery_entry({"stdout": '{"event": "discord_post", broken'}) == {
        "status": None,
        "skipped_reason": "no_delivery_event",
    }
    assert reconcile.delivery_entry(
        {"stdout": '{"event": "discord_post", "status": "weird"}'}
    ) == {"status": None}


def test_record_delivery_never_raises():
    class Evil(dict):
        def setdefault(self, *args):
            raise RuntimeError("boom")

    reconcile.record_delivery(Evil(), "qc:1000", {"stdout": POST_OK})  # must not raise


# ------------------------------------------------------------- tick recording
def test_tick_records_qc_delivery_outcome(tmp_path):
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = make_managed_run(run_dir)
    ex = StdoutExec(f"render done\n{POST_OK}\n")
    progress = reconcile.default_progress("r")
    reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=True))
    saved = reconcile.load_progress(project_root, "r")
    assert saved["delivery"]["qc:1000"] == {"status": 200}


def test_tick_records_probe_skip_and_missing_event(tmp_path):
    from tests.test_reconcile import _probe

    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = make_managed_run(run_dir, probes=[_probe("p1")])
    del mr["qc_op"]
    ex = StdoutExec(f"{SKIPPED}\n")
    progress = reconcile.default_progress("r")
    reconcile.tick(project_root, mr, progress, execute=ex, inspect=fake_inspect(running=True))
    saved = reconcile.load_progress(project_root, "r")
    assert saved["delivery"]["probe:p1:1000"] == {
        "status": None,
        "skipped_reason": "no_webhook",
    }


def test_tick_failed_qc_records_no_delivery_entry(tmp_path):
    """A failed op is a QC failure (op_fail_counts), not a delivery outcome."""
    project_root = make_registry(tmp_path)
    run_dir = make_run_dir(tmp_path, ["checkpoint_step_001000_loss0p5.pt"])
    mr = make_managed_run(run_dir)

    class FailExec(FakeExec):
        def __call__(self, op):
            request = op["request"]
            self.calls.append(request)
            if request.get("adapter") == "script_bundle_run":
                raise OperationError("operation.docker_run_failed", "boom")
            if request.get("adapter") == "checkpoint_retention":
                return self.retention_result
            return {"execution_status": "ok"}

    progress = reconcile.default_progress("r")
    reconcile.tick(
        project_root, mr, progress, execute=FailExec(), inspect=fake_inspect(running=True)
    )
    saved = reconcile.load_progress(project_root, "r")
    assert "qc:1000" not in (saved.get("delivery") or {})
    assert saved["op_fail_counts"]["qc:1000"] == 1


# ------------------------------------------------------------- status surface
def test_status_endpoint_exposes_progress_and_delivery(tmp_path):
    from tests.test_server_projects import make_client
    from tests.test_server_runs import make_run_fixture

    project = make_run_fixture(tmp_path, qc_done=[200])
    progress_path = project / "managed_runs" / "example_run.progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        {
            "probes_done_steps": {"preview": [200]},
            "op_fail_counts": {"qc:300": 2},
            "op_gave_up": ["probe:preview:300"],
            "last_error": "qc step 300: operation.docker_run_failed",
            "delivery": {
                "qc:200": {"status": 200},
                "probe:preview:200": {"status": None, "skipped_reason": "no_webhook"},
            },
        }
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    client = make_client(tmp_path)
    response = client.get("/v1/projects/example_a/runs/example_run/status")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["probes_done_steps"] == {"preview": [200]}
    assert data["op_fail_counts"] == {"qc:300": 2}
    assert data["op_gave_up"] == ["probe:preview:300"]
    assert data["last_error"] == "qc step 300: operation.docker_run_failed"
    assert data["delivery_failures"] == [
        {"key": "probe:preview:200", "status": None, "skipped_reason": "no_webhook"}
    ]
