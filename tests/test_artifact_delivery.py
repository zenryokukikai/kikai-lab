import io
import json
import os
import subprocess
import sys
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from kikai_lab.operation import post_discord_webhook


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


class RecordingResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._body


class RecordingWebhookHandler(BaseHTTPRequestHandler):
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
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"discord-message-1"}')

    def log_message(self, format, *args):  # noqa: A002
        return


def start_webhook_server():
    RecordingWebhookHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingWebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def write_delivery_target(
    project_root,
    *,
    target_id="discord_qc",
    webhook_url="env:TEST_WEBHOOK_URL",
):
    targets = project_root / "delivery_targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / f"{target_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": target_id,
                "kind": "discord_webhook",
                "webhook_url": webhook_url,
                "summary": "test Discord QC webhook",
            },
            indent=2,
        )
    )


def write_delivery_operation(
    path,
    project_root,
    artifact_path,
    *,
    delivery_id="delivery1",
    delivery_target_id="discord_qc",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "deliver_artifact",
                    "project_root": str(project_root),
                    "adapter": "artifact_delivery",
                    "delivery_id": delivery_id,
                    "delivery_target_id": delivery_target_id,
                    "artifact_id": "artifact1",
                    "file_path": str(artifact_path),
                    "message": "checkpoint step 000100 preview ready",
                },
            },
            indent=2,
        )
    )


def test_artifact_delivery_posts_file_to_discord_webhook_and_records_delivery(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    artifact = tmp_path / "preview.mp4"
    artifact.write_bytes(b"fake video bytes")
    op = tmp_path / "ops" / "deliver.json"
    write_delivery_operation(op, project_root, artifact)
    server = start_webhook_server()
    webhook_url = f"http://127.0.0.1:{server.server_port}/webhook/token"
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
    assert payload["data"]["execution_status"] == "artifact_delivery_completed"
    assert payload["data"]["delivery_id"] == "delivery1"
    assert payload["data"]["target_id"] == "discord_qc"
    assert payload["data"]["http_status"] == 200
    assert webhook_url not in result.stdout

    assert len(RecordingWebhookHandler.requests) == 1
    request = RecordingWebhookHandler.requests[0]
    assert request["path"] == "/webhook/token"
    content_type = request["headers"]["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    body = request["body"]
    assert b"payload_json" in body
    assert b"checkpoint step 000100 preview ready" in body
    assert b"preview.mp4" in body
    assert b"fake video bytes" in body

    record_path = project_root / "artifact_deliveries" / "delivery1.json"
    record = json.loads(record_path.read_text())
    assert record["schema_version"] == 1
    assert record["delivery_id"] == "delivery1"
    assert record["artifact_id"] == "artifact1"
    assert record["target_id"] == "discord_qc"
    assert record["status"] == "delivered"
    assert record["http_status"] == 200
    assert record["response_body"] == '{"id":"discord-message-1"}'
    assert webhook_url not in record_path.read_text()


def test_artifact_delivery_retries_transient_503_before_recording_success(tmp_path, monkeypatch):
    artifact = tmp_path / "preview.mp4"
    artifact.write_bytes(b"fake retry video bytes")
    attempts = []
    sleeps = []

    def fake_urlopen(request, timeout):
        attempts.append((request, timeout))
        if len(attempts) <= 3:
            raise HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                hdrs=Message(),
                fp=io.BytesIO(b"temporary discord outage"),
            )
        return RecordingResponse(200, b'{"id":"discord-message-after-retry"}')

    monkeypatch.setattr("kikai_lab.operation.urllib.request.urlopen", fake_urlopen)

    result = post_discord_webhook(
        webhook_url="https://discord.test/webhook/token",
        message="checkpoint step 000100 preview ready",
        file_path=artifact,
        retry_sleep=sleeps.append,
    )

    assert result["http_status"] == 200
    assert result["response_body"] == '{"id":"discord-message-after-retry"}'
    assert len(attempts) == 4
    assert sleeps == [10, 10, 10]


def test_artifact_delivery_resolves_file_path_env_ref(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    artifact = tmp_path / "preview_env.mp4"
    artifact.write_bytes(b"fake env video bytes")
    op = tmp_path / "ops" / "deliver_env_path.json"
    write_delivery_operation(op, project_root, "env:TEST_ARTIFACT_PATH")
    server = start_webhook_server()
    webhook_url = f"http://127.0.0.1:{server.server_port}/webhook/token"
    try:
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli(
            "exec",
            str(op),
            env={"TEST_WEBHOOK_URL": webhook_url, "TEST_ARTIFACT_PATH": str(artifact)},
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["file_path"] == str(artifact)
    record = json.loads((project_root / "artifact_deliveries" / "delivery1.json").read_text())
    assert record["file_path"] == str(artifact)
    assert b"fake env video bytes" in RecordingWebhookHandler.requests[0]["body"]


def test_artifact_delivery_fails_closed_when_webhook_env_is_missing(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    artifact = tmp_path / "preview.mp4"
    artifact.write_bytes(b"fake video bytes")
    op = tmp_path / "ops" / "deliver.json"
    write_delivery_operation(op, project_root, artifact)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.env_ref_missing"
    assert not (project_root / "artifact_deliveries" / "delivery1.json").exists()


def test_artifact_delivery_fails_closed_when_file_is_missing(tmp_path):
    project_root = tmp_path / "registry"
    write_delivery_target(project_root)
    artifact = tmp_path / "missing.mp4"
    op = tmp_path / "ops" / "deliver.json"
    write_delivery_operation(op, project_root, artifact)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op), env={"TEST_WEBHOOK_URL": "http://127.0.0.1:1/webhook"})

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.delivery_file_missing"
    assert not (project_root / "artifact_deliveries" / "delivery1.json").exists()
