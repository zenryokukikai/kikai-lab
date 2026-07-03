import hashlib
import json
import os
import subprocess
import sys


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


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_container(root, *, source_snapshot_id="example_project_v1"):
    containers = root / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    (containers / "run1_training.yaml").write_text(
        f"""schema_version: 1
kind: docker_container
container_id: run1_training
role: training
docker:
  name: example-run1-training
  image: env:EXAMPLE_TRAINING_IMAGE
workdir: /workspace/example_project
mounts:
  - source: env:HOST_SHOULD_NOT_BE_USED_FOR_SNAPSHOT
    target: /workspace/example_project
    source_kind: kikai_managed_source_snapshot
    source_snapshot_id: {source_snapshot_id}
    mode: ro
  - source: env:HOST_TRAINING_RUNS_ROOT
    target: /workspace/training_runs
    mode: rw
"""
    )


def write_minimal_registry(root, *, source_snapshot_id="example_project_v1"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    write_container(root, source_snapshot_id=source_snapshot_id)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "demo",
                "current_experiment_id": "exp1",
                "current_run_name": "run1",
                "current_checkpoint": "checkpoint.pt",
                "current_model_arch": "arch1",
                "must_read_external_ref_ids": ["EXAMPLE-REF-001"],
                "verified_by": "test-agent",
                "last_verified_at": "2026-06-26T00:00:00Z",
                "staleness_warn_after_hours": 999999,
                "staleness_block_after_hours": 1000000,
                "established_by_decision_id": "decision-old",
                "next_decision_id": "decision-next",
                "next_decision_required": False,
                "required_container_ids": ["run1_training"],
            }
        )
    )
    (root / "experiments" / "exp1.yaml").write_text(
        """experiment_id: exp1
status: active
external_refs:
  - provider: example_design_registry
    id: EXAMPLE-REF-001
    kind: design
    required: true
"""
    )
    (root / "runs" / "run1.yaml").write_text(
        """run_name: run1
experiment_id: exp1
status: active
model_arch: arch1
checkpoint:
  latest: checkpoint.pt
"""
    )


def write_script_bundle(project_root, bundle_id="example_run_train"):
    launcher_text = "print('bundle launcher')\n"
    bundle_root = project_root / "script_bundles" / bundle_id
    launcher_path = bundle_root / "root" / "scripts" / "training" / "launch.py"
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(launcher_text)
    bundle = {
        "schema_version": 1,
        "kind": "kikai_script_bundle",
        "bundle_id": bundle_id,
        "immutable": True,
        "generated_by": {"tool": "kikai script-bundle create", "schema_version": 1},
        "entrypoints": {
            "train": {
                "argv": [
                    "python",
                    f"script_bundles/{bundle_id}/root/scripts/training/launch.py",
                ]
            }
        },
        "files": [
            {
                "path": "root/scripts/training/launch.py",
                "sha256": hashlib.sha256(launcher_text.encode()).hexdigest(),
            }
        ],
    }
    (bundle_root / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n")


def write_script_bundle_run_operation(path, project_root):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "example_run_train",
                    "project_root": str(project_root),
                    "target_id": "example_run_train",
                    "adapter": "script_bundle_run",
                    "bundle_id": "example_run_train",
                    "entrypoint": "train",
                    "container_id": "run1_training",
                },
            },
            indent=2,
        )
    )


def write_fake_docker(tmp_path):
    output_path = tmp_path / "docker_argv.json"
    fake = tmp_path / "fake_docker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(output_path)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "raise SystemExit(0)\n"
    )
    fake.chmod(0o755)
    return fake, output_path


def test_source_snapshot_create_copies_files_hashes_and_records_provenance(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    helper = source_root / "scripts" / "training" / "helper.py"
    config = source_root / "configs" / "run.yaml"
    pycache = source_root / "scripts" / "training" / "__pycache__" / "helper.pyc"
    launcher.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    pycache.parent.mkdir(parents=True)
    launcher.write_text("from helper import main\nmain()\n")
    helper.write_text("def main():\n    print('train')\n")
    config.write_text("run_name: run1\n")
    pycache.write_bytes(b"compiled bytecode should not enter source snapshots")

    result = run_cli(
        "source-snapshot",
        "create",
        "example_project_v1",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--include-dir",
        "scripts",
        "--file",
        "configs/run.yaml",
        "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["source_snapshot_id"] == "example_project_v1"
    assert payload["data"]["file_count"] == 3
    snapshot_root = project_root / "source_snapshots" / "example_project_v1"
    copied_launcher = snapshot_root / "root" / "scripts" / "training" / "launch.py"
    copied_helper = snapshot_root / "root" / "scripts" / "training" / "helper.py"
    copied_config = snapshot_root / "root" / "configs" / "run.yaml"
    assert copied_launcher.read_text() == "from helper import main\nmain()\n"
    assert copied_helper.read_text() == "def main():\n    print('train')\n"
    assert copied_config.read_text() == "run_name: run1\n"
    assert not (snapshot_root / "root" / "scripts" / "training" / "__pycache__").exists()
    manifest = json.loads((snapshot_root / "snapshot.json").read_text())
    assert manifest["kind"] == "kikai_source_snapshot"
    assert manifest["source_snapshot_id"] == "example_project_v1"
    assert manifest["immutable"] is True
    assert manifest["generated_by"] == {
        "tool": "kikai source-snapshot create",
        "schema_version": 1,
    }
    assert manifest["files"] == [
        {"path": "root/configs/run.yaml", "sha256": sha256_file(copied_config)},
        {"path": "root/scripts/training/helper.py", "sha256": sha256_file(copied_helper)},
        {"path": "root/scripts/training/launch.py", "sha256": sha256_file(copied_launcher)},
    ]


def test_validate_rejects_snapshot_mount_without_registered_snapshot(tmp_path):
    write_minimal_registry(tmp_path, source_snapshot_id="missing_snapshot")

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "container.source_snapshot_missing"


def test_script_bundle_run_mounts_registered_source_snapshot_not_live_env(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("print('source snapshot')\n")
    write_minimal_registry(project_root)
    create = run_cli(
        "source-snapshot",
        "create",
        "example_project_v1",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--include-dir",
        "scripts",
        "--json",
    )
    assert create.returncode == 0, create.stdout + create.stderr
    write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_run.json"
    write_script_bundle_run_operation(op, project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
            "HOST_TRAINING_RUNS_ROOT": str(tmp_path / "training_runs"),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    docker_argv = json.loads(docker_argv_path.read_text())
    assert (
        f"{project_root / 'source_snapshots' / 'example_project_v1' / 'root'}"
        ":/workspace/example_project:ro"
    ) in docker_argv
    assert "HOST_SHOULD_NOT_BE_USED_FOR_SNAPSHOT" not in json.dumps(docker_argv)
