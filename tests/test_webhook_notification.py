import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_env_run_root_backfill_operation(*, project_root, notification_id):
    """Generic training_progress_backfill op that resolves its run root from an env ref.

    Mirrors the structure of a checked-in operation whose paths point at a
    training runs root supplied via the ``HOST_TRAINING_RUNS_ROOT`` environment
    variable, so the env-driven backfill path stays covered without shipping a
    project-specific fixture file.
    """
    return {
        "schema_version": 1,
        "kind": "kikai_operation",
        "request": {
            "operation": "example_run_training_progress_backfill",
            "adapter": "training_progress_backfill",
            "project_root": str(project_root),
            "notification_id": notification_id,
            "delivery_target_id": "discord_progress",
            "run_name": "example_run",
            "run_dir": "${HOST_TRAINING_RUNS_ROOT}/example_run",
            "metrics_path": "${HOST_TRAINING_RUNS_ROOT}/example_run/metrics.jsonl",
            "checkpoint_dir": "${HOST_TRAINING_RUNS_ROOT}/example_run/checkpoints",
            "tensorboard_dir": "${HOST_TRAINING_RUNS_ROOT}/example_run/tensorboard",
            "model_arch": "example_model_v1",
            "max_steps": 20000,
            "metrics_tail_rows": 40,
            "message_prefix": "example_run Kikai progress backfill",
            "severity": "warning",
        },
    }


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


class RecordingJsonWebhookHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        return


def start_webhook_server():
    RecordingJsonWebhookHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingJsonWebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def write_delivery_target(project_root, *, webhook_url="env:TEST_WEBHOOK_URL"):
    targets = project_root / "delivery_targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / "discord_progress.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "discord_progress",
                "kind": "discord_webhook",
                "webhook_url": webhook_url,
                "summary": "test Discord progress webhook",
            },
            indent=2,
        )
    )


def write_notification_operation(path, project_root, *, notification_id="notice1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "notify_progress",
                    "project_root": str(project_root),
                    "adapter": "webhook_notification",
                    "notification_id": notification_id,
                    "delivery_target_id": "discord_progress",
                    "message": "example_run checkpoint watcher started",
                    "severity": "info",
                    "run_name": "example_run",
                },
            },
            indent=2,
        )
    )


def test_webhook_notification_posts_discord_message_and_records_notification(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    op = tmp_path / "ops" / "notify.json"
    write_notification_operation(op, project_root)
    server = start_webhook_server()
    webhook_url = f"http://127.0.0.1:{server.server_port}/progress/token"
    try:
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli("exec", str(op), env={"TEST_WEBHOOK_URL": webhook_url})
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "webhook_notification_completed"
    assert payload["data"]["notification_id"] == "notice1"
    assert payload["data"]["http_status"] == 204
    assert webhook_url not in result.stdout

    assert len(RecordingJsonWebhookHandler.requests) == 1
    request = RecordingJsonWebhookHandler.requests[0]
    assert request["path"] == "/progress/token"
    assert request["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(request["body"].decode("utf-8")) == {
        "content": "example_run checkpoint watcher started"
    }

    record_path = project_root / "notifications" / "notice1.json"
    record = json.loads(record_path.read_text())
    assert record["schema_version"] == 1
    assert record["notification_id"] == "notice1"
    assert record["target_id"] == "discord_progress"
    assert record["severity"] == "info"
    assert record["run_name"] == "example_run"
    assert record["status"] == "delivered"
    assert record["http_status"] == 204
    assert webhook_url not in record_path.read_text()


def test_webhook_notification_fails_closed_when_target_env_missing(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    op = tmp_path / "ops" / "notify.json"
    write_notification_operation(op, project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.env_ref_missing"
    assert not (project_root / "notifications" / "notice1.json").exists()


def write_training_progress_backfill_operation(
    path, project_root, run_dir, *, notification_id="backfill1"
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "training_progress_backfill",
                    "project_root": str(project_root),
                    "adapter": "training_progress_backfill",
                    "notification_id": notification_id,
                    "delivery_target_id": "discord_progress",
                    "run_name": "example_run",
                    "run_dir": str(run_dir),
                    "model_arch": "example_model_v1",
                    "max_steps": 20000,
                    "metrics_tail_rows": 5,
                    "message_prefix": "example_run Kikai progress backfill",
                    "severity": "warning",
                },
            },
            indent=2,
        )
    )


def test_training_progress_backfill_posts_metrics_checkpoint_and_tensorboard_summary(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    run_dir = tmp_path / "training_runs" / "example_run"
    checkpoint_dir = run_dir / "checkpoints"
    tensorboard_dir = run_dir / "tensorboard"
    checkpoint_dir.mkdir(parents=True)
    tensorboard_dir.mkdir(parents=True)
    (checkpoint_dir / "checkpoint_step_019500.pt").write_bytes(b"ckpt")
    (checkpoint_dir / "checkpoint_step_020000.pt").write_bytes(b"ckpt")
    (tensorboard_dir / "events.out.tfevents.fake").write_text("event")
    (run_dir / "metrics.jsonl").write_text(
        json.dumps({"event": "train_metrics", "step": 19990, "loss": 0.1234})
        + "\n"
        + json.dumps({"event": "eval_metrics", "step": 20000, "eval_loss": 0.2345})
        + "\n"
        + json.dumps(
            {
                "event": "checkpoint",
                "step": 20000,
                "path": str(checkpoint_dir / "checkpoint_step_020000.pt"),
            }
        )
        + "\n"
    )
    op = tmp_path / "ops" / "backfill.json"
    write_training_progress_backfill_operation(op, project_root, run_dir)
    server = start_webhook_server()
    webhook_url = f"http://127.0.0.1:{server.server_port}/progress/token"
    try:
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli("exec", str(op), env={"TEST_WEBHOOK_URL": webhook_url})
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "training_progress_backfill_completed"
    assert payload["data"]["notification_id"] == "backfill1"
    assert payload["data"]["latest_step"] == 20000
    assert payload["data"]["latest_checkpoint_step"] == 20000
    assert payload["data"]["tensorboard_event_count"] == 1
    assert webhook_url not in result.stdout

    assert len(RecordingJsonWebhookHandler.requests) == 1
    body = json.loads(RecordingJsonWebhookHandler.requests[0]["body"].decode("utf-8"))
    assert "example_run Kikai progress backfill" in body["content"]
    assert "step 20000/20000" in body["content"]
    assert "checkpoint_step_020000.pt" in body["content"]
    assert "TensorBoard events=1" in body["content"]
    assert "eval_loss=0.2345" in body["content"]

    record_path = project_root / "notifications" / "backfill1.json"
    record = json.loads(record_path.read_text())
    assert record["notification_id"] == "backfill1"
    assert record["target_id"] == "discord_progress"
    assert record["status"] == "delivered"
    assert record["latest_step"] == 20000
    assert record["latest_checkpoint"]["step"] == 20000
    assert record["tensorboard_event_count"] == 1
    assert webhook_url not in record_path.read_text()


def test_checked_in_training_progress_backfill_fixture_posts_from_env_run_root(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    training_runs_root = tmp_path / "training_runs"
    run_dir = training_runs_root / "example_run"
    checkpoint_dir = run_dir / "checkpoints"
    tensorboard_dir = run_dir / "tensorboard"
    checkpoint_dir.mkdir(parents=True)
    tensorboard_dir.mkdir(parents=True)
    (checkpoint_dir / "checkpoint_step_020000.pt").write_bytes(b"ckpt")
    (tensorboard_dir / "events.out.tfevents.fake").write_text("event")
    (run_dir / "metrics.jsonl").write_text(
        json.dumps({"event": "eval_metrics", "step": 20000, "eval_loss": 0.2345}) + "\n"
    )
    operation = build_env_run_root_backfill_operation(
        project_root=project_root, notification_id="fixture_backfill1"
    )
    op = tmp_path / "ops" / "fixture_backfill.json"
    op.parent.mkdir(parents=True)
    op.write_text(json.dumps(operation, indent=2) + "\n")
    server = start_webhook_server()
    webhook_url = f"http://127.0.0.1:{server.server_port}/progress/token"
    try:
        dry_run = run_cli(
            "target", "dry-run", str(op), env={"HOST_TRAINING_RUNS_ROOT": str(training_runs_root)}
        )
        assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

        result = run_cli(
            "exec",
            str(op),
            env={
                "HOST_TRAINING_RUNS_ROOT": str(training_runs_root),
                "TEST_WEBHOOK_URL": webhook_url,
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "training_progress_backfill_completed"
    body = json.loads(RecordingJsonWebhookHandler.requests[0]["body"].decode("utf-8"))
    assert "example_run Kikai progress backfill" in body["content"]
    assert "step 20000/20000" in body["content"]
    assert "eval_loss=0.2345" in body["content"]
