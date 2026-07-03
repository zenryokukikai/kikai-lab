import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def _make_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "target_project"
    (project_root / "ops").mkdir(parents=True)
    (project_root / "containers").mkdir()
    bundle_root = project_root / "script_bundles" / "bundle1" / "root" / "scripts"
    bundle_root.mkdir(parents=True)
    (project_root / "current.json").write_text(json.dumps({"schema_version": 1}))
    (project_root / "containers" / "runner.yaml").write_text("container_id: runner\n")
    (project_root / "script_bundles" / "bundle1" / "bundle.json").write_text(
        json.dumps({"kind": "kikai_script_bundle", "bundle_id": "bundle1"})
    )
    (bundle_root / "run.py").write_text("print('ok')\n")
    return project_root


def test_remote_launch_cli_writes_ops_and_dry_runs(tmp_path):
    project_root = _make_project(tmp_path)
    result = run_cli(
        "remote-launch",
        "--project-root", str(project_root),
        "--operation-id", "run_x_op",
        "--bundle-id", "bundle1",
        "--container-id", "runner",
        "--entrypoint", "run",
        "--ssh-host", "training-host.example",
        "--remote-project-root", "/remote/kikai-lab",
        "--args-json", json.dumps(["--max-steps", "10"]),
        "--env", "KIKAI_RUN_ID=run_x",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)["data"]
    inner_path = Path(data["inner_operation"])
    remote_path = Path(data["remote_operation"])
    assert inner_path.exists() and remote_path.exists()
    # inner op carries the args + env; remote op bundles the payload
    inner = json.loads(inner_path.read_text())
    assert inner["request"]["args"] == ["--max-steps", "10"]
    assert inner["request"]["env"] == {"KIKAI_RUN_ID": "run_x"}
    remote = json.loads(remote_path.read_text())
    assert "script_bundles/bundle1/root/scripts/run.py" in (
        remote["request"]["local_project_payload_paths"]
    )
    # the written wrapper passes structural validation
    dry = run_cli("target", "dry-run", str(remote_path))
    assert dry.returncode == 0, dry.stdout + dry.stderr


def test_remote_launch_cli_rejects_bad_env(tmp_path):
    project_root = _make_project(tmp_path)
    result = run_cli(
        "remote-launch",
        "--project-root", str(project_root),
        "--operation-id", "run_x_op",
        "--bundle-id", "bundle1",
        "--container-id", "runner",
        "--entrypoint", "run",
        "--ssh-host", "h",
        "--remote-project-root", "/remote/kikai-lab",
        "--env", "NOEQUALS",
    )
    assert result.returncode == 2
    assert "env_invalid" in result.stdout
