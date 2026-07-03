import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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


class RecordingMixedWebhookHandler(BaseHTTPRequestHandler):
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
        if self.path.endswith("/notify"):
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"artifact-message-1"}')

    def log_message(self, format, *args):  # noqa: A002
        return


def start_webhook_server():
    RecordingMixedWebhookHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingMixedWebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def write_delivery_targets(project_root):
    targets = project_root / "delivery_targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / "discord_progress.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "discord_progress",
                "kind": "discord_webhook",
                "webhook_url": "env:TEST_PROGRESS_WEBHOOK_URL",
            },
            indent=2,
        )
    )
    (targets / "discord_qc.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "discord_qc",
                "kind": "discord_webhook",
                "webhook_url": "env:TEST_QC_WEBHOOK_URL",
            },
            indent=2,
        )
    )


def write_sequence_operation(path, project_root, artifact_path, *, missing_artifact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    file_path = artifact_path if not missing_artifact else artifact_path.parent / "missing.mp4"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "example_run_checkpoint_qc_smoke",
                    "project_root": str(project_root),
                    "adapter": "operation_sequence",
                    "pipeline_run_id": "example_run_checkpoint_qc_smoke_001",
                    "steps": [
                        {
                            "step_id": "notify_started",
                            "request": {
                                "operation": "notify_progress",
                                "adapter": "webhook_notification",
                                "notification_id": "notice_started",
                                "delivery_target_id": "discord_progress",
                                "message": "example_run checkpoint QC started",
                                "severity": "info",
                                "run_name": "example_run",
                            },
                        },
                        {
                            "step_id": "deliver_preview",
                            "request": {
                                "operation": "deliver_artifact",
                                "adapter": "artifact_delivery",
                                "delivery_id": "preview_delivery",
                                "delivery_target_id": "discord_qc",
                                "artifact_id": "preview_artifact",
                                "file_path": str(file_path),
                                "message": "example_run checkpoint preview ready",
                            },
                        },
                    ],
                },
            },
            indent=2,
        )
    )


def test_operation_sequence_runs_notification_then_artifact_delivery_and_records_pipeline(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_targets(project_root)
    artifact = tmp_path / "preview.mp4"
    artifact.write_bytes(b"fake preview bytes")
    op = tmp_path / "ops" / "sequence.json"
    write_sequence_operation(op, project_root, artifact)
    server = start_webhook_server()
    notify_url = f"http://127.0.0.1:{server.server_port}/notify"
    artifact_url = f"http://127.0.0.1:{server.server_port}/artifact"
    try:
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli(
            "exec",
            str(op),
            env={
                "TEST_PROGRESS_WEBHOOK_URL": notify_url,
                "TEST_QC_WEBHOOK_URL": artifact_url,
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "operation_sequence_completed"
    assert [step["step_id"] for step in payload["data"]["steps"]] == [
        "notify_started",
        "deliver_preview",
    ]
    assert [step["status"] for step in payload["data"]["steps"]] == ["completed", "completed"]
    assert notify_url not in result.stdout
    assert artifact_url not in result.stdout

    assert len(RecordingMixedWebhookHandler.requests) == 2
    assert RecordingMixedWebhookHandler.requests[0]["path"] == "/notify"
    assert json.loads(RecordingMixedWebhookHandler.requests[0]["body"].decode("utf-8")) == {
        "content": "example_run checkpoint QC started"
    }
    assert RecordingMixedWebhookHandler.requests[1]["path"] == "/artifact"
    assert b"fake preview bytes" in RecordingMixedWebhookHandler.requests[1]["body"]

    pipeline_record = json.loads(
        (project_root / "pipeline_runs" / "example_run_checkpoint_qc_smoke_001.json").read_text()
    )
    assert pipeline_record["pipeline_run_id"] == "example_run_checkpoint_qc_smoke_001"
    assert pipeline_record["status"] == "completed"
    assert [step["status"] for step in pipeline_record["steps"]] == ["completed", "completed"]
    assert notify_url not in json.dumps(pipeline_record)
    assert artifact_url not in json.dumps(pipeline_record)


def test_operation_sequence_stops_on_failed_step_and_records_failure(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_targets(project_root)
    artifact = tmp_path / "preview.mp4"
    artifact.write_bytes(b"fake preview bytes")
    op = tmp_path / "ops" / "sequence.json"
    write_sequence_operation(op, project_root, artifact, missing_artifact=True)
    server = start_webhook_server()
    notify_url = f"http://127.0.0.1:{server.server_port}/notify"
    artifact_url = f"http://127.0.0.1:{server.server_port}/artifact"
    try:
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli(
            "exec",
            str(op),
            env={
                "TEST_PROGRESS_WEBHOOK_URL": notify_url,
                "TEST_QC_WEBHOOK_URL": artifact_url,
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.sequence_step_failed"
    assert payload["errors"][0]["details"]["failed_step_id"] == "deliver_preview"
    assert len(RecordingMixedWebhookHandler.requests) == 1
    assert RecordingMixedWebhookHandler.requests[0]["path"] == "/notify"
    assert not (project_root / "artifact_deliveries" / "preview_delivery.json").exists()

    pipeline_record = json.loads(
        (project_root / "pipeline_runs" / "example_run_checkpoint_qc_smoke_001.json").read_text()
    )
    assert pipeline_record["status"] == "failed"
    assert [step["status"] for step in pipeline_record["steps"]] == ["completed", "failed"]
    assert pipeline_record["steps"][1]["error"]["code"] == "operation.delivery_file_missing"
