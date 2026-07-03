import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        cwd=REPO_ROOT,
    )


def write_fake_scp(tmp_path):
    """A fake scp that records argv and materializes the destination file."""
    argv_path = tmp_path / "scp_argv.json"
    fake = tmp_path / "fake_scp.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "argv = sys.argv[1:]\n"
        f"pathlib.Path({str(argv_path)!r}).write_text(json.dumps(argv))\n"
        "dest = pathlib.Path(argv[-1])\n"
        "dest.parent.mkdir(parents=True, exist_ok=True)\n"
        "dest.write_bytes(b'FETCHED-MEDIA-BYTES')\n"
    )
    fake.chmod(0o755)
    return fake, argv_path


def write_fetch_op(tmp_path, local_dest_root):
    op = {
        "kind": "kikai_operation",
        "schema_version": 1,
        "request": {
            "adapter": "remote_file_fetch",
            "operation": "fetch_example_diag",
            "ssh_host": "env:KIKAI_REMOTE_SSH_HOST",
            "remote_paths": [
                "/workspace/training_runs/run/qc/audio_to_pose_diagnostic.mp4"
            ],
            "local_dest_root": str(local_dest_root),
        },
    }
    op_path = tmp_path / "remote_file_fetch.json"
    op_path.write_text(json.dumps(op, indent=2))
    return op_path


def test_remote_file_fetch_copies_remote_media_to_local(tmp_path):
    fake_scp, argv_path = write_fake_scp(tmp_path)
    dest_root = tmp_path / "fetched"
    op_path = write_fetch_op(tmp_path, dest_root)
    env = {
        "KIKAI_REMOTE_SSH_HOST": "examplehost",
        "KIKAI_SCP_BIN": str(fake_scp),
    }

    dry = run_cli("target", "dry-run", str(op_path), env=env)
    assert dry.returncode == 0, dry.stderr or dry.stdout

    run = run_cli("target", "run", str(op_path), env=env)
    assert run.returncode == 0, run.stderr or run.stdout
    payload = json.loads(run.stdout)
    assert payload["ok"] is True, payload

    fetched = dest_root / "audio_to_pose_diagnostic.mp4"
    assert fetched.is_file(), "fetched media should exist locally"
    assert fetched.read_bytes() == b"FETCHED-MEDIA-BYTES"

    argv = json.loads(argv_path.read_text())
    assert "examplehost:/workspace/training_runs/run/qc/audio_to_pose_diagnostic.mp4" in argv


def test_remote_file_fetch_requires_guard_receipt_for_run(tmp_path):
    fake_scp, _ = write_fake_scp(tmp_path)
    dest_root = tmp_path / "fetched"
    op = json.loads(write_fetch_op(tmp_path, dest_root).read_text())
    op.pop("guard_receipt", None)
    op_path = tmp_path / "no_guard.json"
    op_path.write_text(json.dumps(op))
    env = {"KIKAI_REMOTE_SSH_HOST": "examplehost", "KIKAI_SCP_BIN": str(fake_scp)}
    run = run_cli("target", "run", str(op_path), env=env)
    assert run.returncode != 0
    assert "guard_receipt" in (run.stdout + run.stderr)
