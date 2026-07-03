import json
import os
import subprocess
import sys
from pathlib import Path

from kikai_lab.remote_launch import (
    build_remote_kikai_exec_op,
    build_script_bundle_launch_ops,
    collect_bundle_payload_paths,
)


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
    (project_root / "current.json").write_text(
        json.dumps({"schema_version": 1, "last_verified_at": "2026-06-26T00:00:00Z"})
    )
    (project_root / "containers" / "runner.yaml").write_text("container_id: runner\n")
    (project_root / "script_bundles" / "bundle1" / "bundle.json").write_text(
        json.dumps({"kind": "kikai_script_bundle", "bundle_id": "bundle1"})
    )
    (bundle_root / "run.py").write_text("print('ok')\n")
    # a __pycache__ artifact that must be excluded from the payload
    (bundle_root / "__pycache__").mkdir()
    (bundle_root / "__pycache__" / "run.cpython-311.pyc").write_text("junk")
    return project_root


def test_collect_bundle_payload_paths_lists_text_files_and_excludes_pycache(tmp_path):
    project_root = _make_project(tmp_path)
    paths = collect_bundle_payload_paths(
        project_root, "bundle1", extra=["current.json", "containers/runner.yaml"]
    )
    assert paths[0] == "current.json"
    assert paths[1] == "containers/runner.yaml"
    assert "script_bundles/bundle1/bundle.json" in paths
    assert "script_bundles/bundle1/root/scripts/run.py" in paths
    assert all("__pycache__" not in p for p in paths)
    # de-dup: passing a bundle file as extra does not duplicate it
    again = collect_bundle_payload_paths(
        project_root, "bundle1", extra=["script_bundles/bundle1/bundle.json"]
    )
    assert again.count("script_bundles/bundle1/bundle.json") == 1


def test_build_remote_kikai_exec_op_fields_and_defaults(tmp_path):
    op = build_remote_kikai_exec_op(
        operation_id="remote_run_x",
        ssh_host="env:KIKAI_REMOTE_SSH_HOST",
        remote_project_root="/remote/kikai-lab",
        local_project_root=str(tmp_path),
        local_operation_template=str(tmp_path / "ops" / "inner.json"),
        local_project_payload_paths=["current.json"],
        env={"KIKAI_RUN_ID": "run_x"},
    )
    req = op["request"]
    assert op["kind"] == "kikai_operation"
    assert req["adapter"] == "remote_kikai_exec"
    assert req["operation"] == "remote_run_x"
    assert req["pipeline_run_id"] == "remote_run_x"
    assert req["remote_payload_project_root"] == "/tmp/kikai_remote_run_x_project"
    assert req["remote_operation_path"] == "/tmp/kikai_remote_run_x_project/ops/remote_run_x.json"
    assert req["env"] == {"KIKAI_RUN_ID": "run_x"}


def test_build_script_bundle_launch_ops_passes_target_dry_run(tmp_path):
    project_root = _make_project(tmp_path)
    inner_op, remote_op, inner_rel, remote_rel = build_script_bundle_launch_ops(
        operation_id="run_x_op",
        project_root=project_root,
        bundle_id="bundle1",
        container_id="runner",
        entrypoint="run",
        args=["--max-steps", "10"],
        ssh_host="training-host.example",
        remote_project_root="/remote/kikai-lab",
        env={"KIKAI_RUN_ID": "run_x"},
    )
    # inner op is a script_bundle_run referencing the bundle/container
    assert inner_op["request"]["adapter"] == "script_bundle_run"
    assert inner_op["request"]["bundle_id"] == "bundle1"
    assert inner_op["request"]["detach"] is True
    assert inner_op["request"]["args"] == ["--max-steps", "10"]
    # the remote payload bundles current.json + container yaml + inner op json + bundle tree
    payload = remote_op["request"]["local_project_payload_paths"]
    assert "current.json" in payload
    assert "containers/runner.yaml" in payload
    assert inner_rel in payload
    assert "script_bundles/bundle1/root/scripts/run.py" in payload
    # write both ops and confirm the wrapper passes structural validation end-to-end
    (project_root / inner_rel).write_text(json.dumps(inner_op))
    (project_root / remote_rel).write_text(json.dumps(remote_op))
    dry = run_cli("target", "dry-run", str(project_root / remote_rel))
    assert dry.returncode == 0, dry.stdout + dry.stderr
