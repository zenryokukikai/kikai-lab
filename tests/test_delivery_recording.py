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
POST_RAISED = '{"event": "discord_post_failed", "error": "URLError(timed out)"}'


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
    assert reconcile.delivery_entry({"stdout": POST_OK}) == {
        "status": 200,
        "outcome": "delivered",
    }
    assert reconcile.delivery_entry({"stdout": POST_FAIL}) == {
        "status": 429,
        "outcome": "http_error",
    }
    assert reconcile.delivery_entry({"stdout": SKIPPED}) == {
        "status": None,
        "outcome": "skipped",
        "skipped_reason": "no_webhook",
    }
    assert reconcile.delivery_entry({"stdout": "just logs\n"}) == {
        "status": None,
        "outcome": "no_delivery_event",
        "skipped_reason": "no_delivery_event",
    }
    # corrupt JSON-looking lines and non-int statuses degrade, never raise
    assert reconcile.delivery_entry({"stdout": '{"event": "discord_post", broken'}) == {
        "status": None,
        "outcome": "no_delivery_event",
        "skipped_reason": "no_delivery_event",
    }
    assert reconcile.delivery_entry(
        {"stdout": '{"event": "discord_post", "status": "weird"}'}
    ) == {"status": None, "outcome": "unknown"}


def test_redirect_status_is_not_counted_as_delivered():
    """3xx is an ANSWER, not a delivery: a webhook URL that 301s to a login page
    returns a perfectly healthy-looking status and posts nothing."""
    entry = reconcile.delivery_entry(
        {"stdout": '{"event": "discord_post", "status": 301}'}
    )
    assert entry == {"status": 301, "outcome": "http_error"}
    summary = reconcile.delivery_summary({"delivery": {"qc:1000": entry}})
    assert summary["delivered"] == 0
    assert summary["failed"] == 1
    assert summary["reasons"] == {"http_301": 1}


def test_truncation_is_marked_on_both_free_text_branches():
    """An unmarked cut reads as a complete message; an uncapped one lets a
    5000-char script reason into every read surface. Both branches, one helper."""
    long_skip = reconcile.delivery_entry(
        {"stdout": json.dumps({"event": "discord_post_skipped", "reason": "s" * 5000})}
    )
    assert len(long_skip["skipped_reason"]) == reconcile.DELIVERY_DETAIL_MAX
    assert long_skip["skipped_reason"].endswith("…")
    long_fail = reconcile.delivery_entry(
        {"stdout": json.dumps({"event": "discord_post_failed", "error": "e" * 5000})}
    )
    assert long_fail["skipped_reason"].endswith("…")
    assert len(long_fail["skipped_reason"]) == len("post_failed:") + reconcile.DELIVERY_DETAIL_MAX
    # short text is untouched (no marker on a complete message)
    assert reconcile.delivery_entry({"stdout": SKIPPED})["skipped_reason"] == "no_webhook"


def test_failed_post_is_not_filed_as_missing_event():
    """A post that RAISED is a known failure, not an unknown one.

    Regression: ``discord_post_failed`` was outside DELIVERY_EVENT_NAMES, so the
    only two producers of it landed in the same ``no_delivery_event`` bucket as
    scripts that never attempt a post at all."""
    entry = reconcile.delivery_entry({"stdout": f"rendered\n{POST_RAISED}\n"})
    assert entry == {
        "status": None,
        "outcome": "post_failed",
        "skipped_reason": "post_failed:URLError(timed out)",
    }
    # error detail is optional and bounded
    assert reconcile.delivery_entry(
        {"stdout": '{"event": "discord_post_failed"}'}
    ) == {"status": None, "outcome": "post_failed", "skipped_reason": "post_failed"}
    long = reconcile.delivery_entry(
        {"stdout": json.dumps({"event": "discord_post_failed", "error": "x" * 500})}
    )
    assert len(long["skipped_reason"]) <= len("post_failed:") + 160


def test_delivery_summary_counts_every_outcome():
    """The denominator the truncated failure tail cannot express.

    Legacy records (no ``outcome`` field, written before it existed) are
    classified from what they carry, so an upgrade mid-run does not blank the
    counts."""
    progress = {
        "qc_done_steps": [1000, 2000, 3000, 4000],
        "probes_done_steps": {"p1": [1000, 2000]},
        "delivery": {
            "qc:1000": {"status": 200, "outcome": "delivered"},
            "qc:2000": {"status": None, "skipped_reason": "no_delivery_event"},
            "qc:3000": {"status": None, "outcome": "no_delivery_event",
                        "skipped_reason": "no_delivery_event"},
            "qc:4000": {"status": 429, "outcome": "http_error"},
            "probe:p1:1000": {"status": None, "outcome": "skipped",
                              "skipped_reason": "no_webhook"},
            "probe:p1:2000": {"status": None},
            "junk": "not-a-dict",
        },
    }
    assert reconcile.delivery_summary(progress) == {
        "total": 6,
        "expected": 6,
        "unrecorded": 0,
        "delivered": 1,
        "failed": 1,
        "skipped": 1,
        "unverified": 3,
        "reasons": {
            "no_delivery_event": 2,
            "http_429": 1,
            "skipped": 1,
            "unknown": 1,
        },
        "reason_samples": {"skipped": ["no_webhook"]},
    }


def test_delivery_summary_reasons_cardinality_is_bounded_by_vocabulary():
    """60 distinct error strings must NOT become 60 buckets.

    The bucket key used to be the whole free-text reason, so a per-attempt error
    carrying a path/step blew up the one field /status re-serializes on every
    long-poll."""
    progress = {
        "qc_done_steps": list(range(1000, 61000, 1000)),
        "delivery": {
            f"qc:{s}": {
                "status": None,
                "outcome": "post_failed",
                "skipped_reason": f"post_failed:URLError(/runs/r/qc/step{s}/out.mp4)",
            }
            for s in range(1000, 61000, 1000)
        },
    }
    summary = reconcile.delivery_summary(progress)
    assert summary["reasons"] == {"post_failed": 60}
    assert summary["failed"] == 60 and summary["unverified"] == 0
    # the free text survives as a bounded sample, not as keys
    samples = summary["reason_samples"]["post_failed"]
    assert len(samples) == reconcile.DELIVERY_SAMPLE_MAX
    assert all(len(s) <= reconcile.DELIVERY_SAMPLE_LEN for s in samples)
    assert len(json.dumps(summary)) < 1000


def test_delivery_summary_flags_qc_steps_with_no_delivery_record():
    """`total: 0` used to be the same shape for "healthy" and "nothing recorded".

    record_delivery is skipped on the idempotent-replay path (crash restart) and
    by daemons predating delivery recording, so 60 QC steps can produce zero
    records — the field operators are told to read first must say so."""
    progress = {
        "qc_done_steps": list(range(1000, 61000, 1000)),
        "probes_done_steps": {"preview": [1000, 2000]},
        "delivery": {},
    }
    assert reconcile.delivery_summary(progress) == {
        "total": 0,
        "expected": 62,
        "unrecorded": 62,
        "delivered": 0,
        "failed": 0,
        "skipped": 0,
        "unverified": 0,
        "reasons": {},
        "reason_samples": {},
    }


def test_delivery_summary_never_claims_failure_for_unverified():
    """`no_delivery_event` is *cannot confirm*, not *confirmed not delivered*."""
    progress = {
        "qc_done_steps": [1000, 2000],
        "delivery": {
            "qc:1000": {"status": None, "outcome": "no_delivery_event",
                        "skipped_reason": "no_delivery_event"},
            "qc:2000": {"status": 500, "outcome": "http_error"},
        },
    }
    summary = reconcile.delivery_summary(progress)
    assert summary["unverified"] == 1
    assert summary["failed"] == 1  # the 500 only — the missing event is NOT a failure
    assert summary["delivered"] + summary["failed"] + summary["skipped"] + summary[
        "unverified"
    ] == summary["total"]


def test_delivery_summary_on_empty_progress():
    assert reconcile.delivery_summary({}) == {
        "total": 0,
        "expected": 0,
        "unrecorded": 0,
        "delivered": 0,
        "failed": 0,
        "skipped": 0,
        "unverified": 0,
        "reasons": {},
        "reason_samples": {},
    }


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
    assert saved["delivery"]["qc:1000"] == {"status": 200, "outcome": "delivered"}


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
        "outcome": "skipped",
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
                "qc:200": {"status": 200, "outcome": "delivered"},
                "probe:preview:200": {"status": None, "outcome": "skipped",
                                      "skipped_reason": "no_webhook"},
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
        {"key": "probe:preview:200", "status": None, "outcome": "skipped",
         "skipped_reason": "no_webhook"}
    ]
    assert data["delivery_summary"] == {
        "total": 2,
        "expected": 2,
        "unrecorded": 0,
        "delivered": 1,
        "failed": 0,
        "skipped": 1,
        "unverified": 0,
        "reasons": {"skipped": 1},
        "reason_samples": {"skipped": ["no_webhook"]},
    }


def test_status_delivery_summary_survives_failure_tail_truncation(tmp_path):
    """The incident shape: MORE non-deliveries than the failure tail can show.

    ``delivery_failures`` caps at 20 rows, so 60 consecutive unconfirmed QC
    posts looked exactly like 20 — the summary is what carries the scale."""
    from tests.test_server_projects import make_client
    from tests.test_server_runs import make_run_fixture

    project = make_run_fixture(tmp_path, qc_done=[200])
    progress_path = project / "managed_runs" / "example_run.progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["qc_done_steps"] = list(range(1000, 61000, 1000))
    progress["delivery"] = {
        f"qc:{step}": {"status": None, "outcome": "no_delivery_event",
                       "skipped_reason": "no_delivery_event"}
        for step in range(1000, 61000, 1000)
    }
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    data = make_client(tmp_path).get(
        "/v1/projects/example_a/runs/example_run/status"
    ).json()["data"]
    assert len(data["delivery_failures"]) == 20  # tail, unchanged
    assert data["delivery_summary"] == {
        "total": 60,
        "expected": 60,
        "unrecorded": 0,
        "delivered": 0,
        "failed": 0,  # NOT failed: kikai cannot confirm either way
        "skipped": 0,
        "unverified": 60,
        "reasons": {"no_delivery_event": 60},
        "reason_samples": {},
    }


def test_status_delivery_summary_reports_qc_steps_with_no_record(tmp_path):
    """A progress.json with QC steps but an EMPTY delivery map (crash-restart
    replay, or a daemon predating delivery recording) must not read as silence:
    `delivery_summary` is what SKILL.md tells the operator to check first."""
    from tests.test_server_projects import make_client
    from tests.test_server_runs import make_run_fixture

    project = make_run_fixture(tmp_path, qc_done=[200])
    progress_path = project / "managed_runs" / "example_run.progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["qc_done_steps"] = list(range(1000, 61000, 1000))
    progress.pop("delivery", None)
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    data = make_client(tmp_path).get(
        "/v1/projects/example_a/runs/example_run/status"
    ).json()["data"]
    assert data["delivery_failures"] == []  # the tail has nothing to show — the bug
    summary = data["delivery_summary"]
    assert summary["total"] == 0
    assert summary["expected"] == 60 and summary["unrecorded"] == 60


def test_status_delivery_summary_buckets_post_failed(tmp_path):
    """`post_failed:` records reach /status as ONE bucket with bounded samples."""
    from tests.test_server_projects import make_client
    from tests.test_server_runs import make_run_fixture

    project = make_run_fixture(tmp_path, qc_done=[200])
    progress_path = project / "managed_runs" / "example_run.progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["qc_done_steps"] = [1000, 2000, 3000]
    progress["delivery"] = {
        f"qc:{step}": {
            "status": None,
            "outcome": "post_failed",
            "skipped_reason": f"post_failed:URLError(step{step})",
        }
        for step in (1000, 2000, 3000)
    }
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    data = make_client(tmp_path).get(
        "/v1/projects/example_a/runs/example_run/status"
    ).json()["data"]
    assert data["delivery_summary"]["reasons"] == {"post_failed": 3}
    assert data["delivery_summary"]["failed"] == 3
    assert len(data["delivery_summary"]["reason_samples"]["post_failed"]) == 3
    assert data["delivery_failures"][0]["outcome"] == "post_failed"


def test_summary_survives_a_corrupt_progress_json():
    """The summary must never be the thing that breaks a status read.

    Regression: `_delivery_expected` called `.values()` on `probes_done_steps`
    with no type guard, so a truncated / hand-edited / older-shape progress.json
    raised AttributeError on the `/status` hot path and in `kikai remote run` —
    a diagnostics field taking the diagnosis down with it."""
    corrupt = [
        {"probes_done_steps": ["preview:1000"]},          # list where a map is expected
        {"probes_done_steps": "preview"},                 # string
        {"probes_done_steps": {"preview": "1000"}},       # steps not a list
        {"probes_done_steps": {"preview": None}},
        {"qc_done_steps": {"1000": True}},                # map where a list is expected
        {"qc_done_steps": 1000},                          # scalar
        {"delivery": ["qc:1000"]},                        # list where a map is expected
        {"delivery": "qc:1000"},
        {"delivery": {"qc:1000": "200"}},                 # record not a map
    ]
    for progress in corrupt:
        summary = reconcile.delivery_summary(progress)
        # the documented invariants hold on every shape, not just valid ones
        assert (
            summary["delivered"] + summary["failed"]
            + summary["skipped"] + summary["unverified"] == summary["total"]
        )
        assert summary["total"] + summary["unrecorded"] == summary["expected"]
    # nothing usable in there, so nothing is claimed
    assert reconcile.delivery_summary({"probes_done_steps": "preview"})["expected"] == 0
    # a valid neighbour is still counted when its sibling is garbage
    mixed = reconcile.delivery_summary(
        {"qc_done_steps": [1000, 2000, 2000], "probes_done_steps": {"p": [1000], "q": 5}}
    )
    assert mixed["expected"] == 3 and mixed["unrecorded"] == 3
    # booleans are ints in Python but are not steps
    assert reconcile.delivery_summary({"qc_done_steps": [True, False]})["expected"] == 0
    # ...and the whole payload can be the wrong type
    assert reconcile.delivery_summary("garbage")["expected"] == 0


def test_status_survives_a_corrupt_progress_json(tmp_path):
    """End to end: the corrupt shape reaches /status and the read still answers."""
    from tests.test_server_projects import make_client
    from tests.test_server_runs import make_run_fixture

    project = make_run_fixture(tmp_path, qc_done=[200])
    progress_path = project / "managed_runs" / "example_run.progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["probes_done_steps"] = ["preview:1000"]  # list, not a map
    progress["delivery"] = ["qc:1000"]
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    resp = make_client(tmp_path).get("/v1/projects/example_a/runs/example_run/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["delivery_summary"]["total"] == 0
    assert data["delivery_failures"] == []
