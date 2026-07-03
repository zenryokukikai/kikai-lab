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


def load_json(path):
    return json.loads(path.read_text())


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_container(root, container_id="run1_training", docker_name="example-run1-training"):
    containers = root / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    (containers / f"{container_id}.yaml").write_text(
        f"""schema_version: 1
kind: docker_container
container_id: {container_id}
docker:
  name: {docker_name}
  image: env:EXAMPLE_TRAINING_IMAGE
"""
    )


def write_minimal_registry(root):
    (root / "experiments").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
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
                "last_verified_at": "2026-06-25T00:00:00Z",
                "staleness_warn_after_hours": 999999,
                "staleness_block_after_hours": 999999,
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


def write_operation(path, project_root, *, bundle_id="example_run_train"):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "example_run_train",
                    "project_root": str(project_root),
                    "target_id": "example_run_train",
                    "adapter": "script_bundle_exec",
                    "bundle_id": bundle_id,
                    "entrypoint": "train",
                    "container_id": "run1_training",
                    "args": ["--dry-run"],
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


def test_script_bundle_create_copies_files_hashes_and_rewrites_entrypoint_argv(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    config = source_root / "configs" / "training" / "example_run.yaml"
    launcher.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    launcher.write_text("print('train')\n")
    config.write_text("run_name: example_run\n")

    result = run_cli(
        "script-bundle",
        "create",
        "example_run_train",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--file",
        "configs/training/example_run.yaml",
        "--argv",
        "python",
        "--argv",
        "scripts/training/launch.py",
        "--argv=--config",
        "--argv",
        "configs/training/example_run.yaml",
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["bundle_id"] == "example_run_train"
    assert payload["data"]["file_count"] == 2
    assert payload["data"]["entrypoint_argv"] == [
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--config",
        "script_bundles/example_run_train/root/configs/training/example_run.yaml",
    ]

    bundle_root = project_root / "script_bundles" / "example_run_train"
    copied_launcher = bundle_root / "root" / "scripts" / "training" / "launch.py"
    copied_config = bundle_root / "root" / "configs" / "training" / "example_run.yaml"
    assert copied_launcher.read_text() == "print('train')\n"
    assert copied_config.read_text() == "run_name: example_run\n"
    manifest = load_json(bundle_root / "bundle.json")
    assert manifest["kind"] == "kikai_script_bundle"
    assert manifest["immutable"] is True
    assert manifest["generated_by"] == {
        "tool": "kikai script-bundle create",
        "schema_version": 1,
    }
    assert manifest["entrypoints"]["train"]["argv"] == payload["data"]["entrypoint_argv"]
    assert manifest["files"] == [
        {
            "path": "root/configs/training/example_run.yaml",
            "sha256": sha256_file(copied_config),
        },
        {
            "path": "root/scripts/training/launch.py",
            "sha256": sha256_file(copied_launcher),
        },
    ]


def test_script_bundle_create_recursively_includes_directories_and_generates_hashes(
    tmp_path,
):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    helper = source_root / "scripts" / "training" / "helper.py"
    pycache = source_root / "scripts" / "training" / "__pycache__" / "helper.cpython-311.pyc"
    config = source_root / "configs" / "training" / "example_run.yaml"
    launcher.parent.mkdir(parents=True)
    pycache.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    launcher.write_text("from helper import main\nmain()\n")
    helper.write_text("def main():\n    print('train')\n")
    pycache.write_bytes(b"compiled bytecode should not enter immutable bundles")
    config.write_text("run_name: example_run\n")

    result = run_cli(
        "script-bundle",
        "create",
        "example_run_train",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--include-dir",
        "scripts",
        "--include-dir",
        "configs",
        "--argv",
        "python",
        "--argv",
        "scripts/training/launch.py",
        "--argv=--config",
        "--argv",
        "configs/training/example_run.yaml",
        "--json",
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["file_count"] == 3
    assert payload["data"]["entrypoint_argv"] == [
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--config",
        "script_bundles/example_run_train/root/configs/training/example_run.yaml",
    ]

    bundle_root = project_root / "script_bundles" / "example_run_train"
    copied_launcher = bundle_root / "root" / "scripts" / "training" / "launch.py"
    copied_helper = bundle_root / "root" / "scripts" / "training" / "helper.py"
    copied_config = bundle_root / "root" / "configs" / "training" / "example_run.yaml"
    assert copied_launcher.read_text() == "from helper import main\nmain()\n"
    assert copied_helper.read_text() == "def main():\n    print('train')\n"
    assert copied_config.read_text() == "run_name: example_run\n"
    assert not (bundle_root / "root" / "scripts" / "training" / "__pycache__").exists()
    manifest = load_json(bundle_root / "bundle.json")
    assert manifest["files"] == [
        {
            "path": "root/configs/training/example_run.yaml",
            "sha256": sha256_file(copied_config),
        },
        {
            "path": "root/scripts/training/helper.py",
            "sha256": sha256_file(copied_helper),
        },
        {
            "path": "root/scripts/training/launch.py",
            "sha256": sha256_file(copied_launcher),
        },
    ]


def test_script_bundle_create_refuses_overwrite_missing_duplicate_and_unsafe_paths(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("print('train')\n")

    create_args = [
        "script-bundle",
        "create",
        "example_run_train",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--argv",
        "python",
        "--argv",
        "scripts/training/launch.py",
        "--json",
    ]
    first = run_cli(*create_args)
    assert first.returncode == 0

    overwrite = run_cli(*create_args)
    assert overwrite.returncode != 0
    assert json.loads(overwrite.stdout)["errors"][0]["code"] == "script_bundle.create_bundle_exists"

    missing = run_cli(
        "script-bundle",
        "create",
        "missing_bundle",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/missing.py",
        "--argv",
        "python",
        "--argv",
        "scripts/training/missing.py",
        "--json",
    )
    assert missing.returncode != 0
    assert json.loads(missing.stdout)["errors"][0]["code"] == "script_bundle.create_file_missing"

    duplicate = run_cli(
        "script-bundle",
        "create",
        "duplicate_bundle",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--file",
        "scripts/training/launch.py",
        "--argv",
        "python",
        "--argv",
        "scripts/training/launch.py",
        "--json",
    )
    assert duplicate.returncode != 0
    duplicate_payload = json.loads(duplicate.stdout)
    assert duplicate_payload["errors"][0]["code"] == "script_bundle.create_file_duplicate"

    unsafe = run_cli(
        "script-bundle",
        "create",
        "unsafe_bundle",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "../outside.py",
        "--argv",
        "python",
        "--argv",
        "../outside.py",
        "--json",
    )
    assert unsafe.returncode != 0
    unsafe_payload = json.loads(unsafe.stdout)
    assert unsafe_payload["errors"][0]["code"] == "script_bundle.create_file_path_invalid"


def test_script_bundle_create_rejects_unmanaged_source_paths_in_entrypoint_argv(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    unbundled = source_root / "scripts" / "training" / "unbundled_helper.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("print('train')\n")
    unbundled.write_text("print('unmanaged')\n")

    relative = run_cli(
        "script-bundle",
        "create",
        "relative_unmanaged_bundle",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--argv",
        "python",
        "--argv",
        "scripts/training/launch.py",
        "--argv",
        "scripts/training/unbundled_helper.py",
        "--json",
    )
    assert relative.returncode != 0
    assert json.loads(relative.stdout)["errors"][0]["code"] == (
        "script_bundle.create_unmanaged_source_argv_forbidden"
    )

    absolute = run_cli(
        "script-bundle",
        "create",
        "absolute_unmanaged_bundle",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--argv",
        "python",
        "--argv",
        str(launcher),
        "--json",
    )
    assert absolute.returncode != 0
    assert json.loads(absolute.stdout)["errors"][0]["code"] == (
        "script_bundle.create_unmanaged_source_argv_forbidden"
    )


def test_script_bundle_create_rejects_shell_wrapper_entrypoint(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    launcher = source_root / "scripts" / "training" / "launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("print('train')\n")

    result = run_cli(
        "script-bundle",
        "create",
        "shell_bundle",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--argv",
        "bash",
        "--argv=-lc",
        "--argv",
        "python scripts/training/launch.py",
        "--json",
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.shell_wrapper_forbidden"


def test_manual_script_bundle_manifest_with_user_sha256_is_rejected(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    write_minimal_registry(project_root)
    bundle_id = "manual_bundle"
    script = (
        project_root
        / "script_bundles"
        / bundle_id
        / "root"
        / "scripts"
        / "training"
        / "launch.py"
    )
    script.parent.mkdir(parents=True)
    script.write_text("print('manual')\n")
    manifest = {
        "schema_version": 1,
        "kind": "kikai_script_bundle",
        "bundle_id": bundle_id,
        "immutable": True,
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
                "sha256": sha256_file(script),
            }
        ],
    }
    (script.parents[3] / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n")

    validate = run_cli("validate", "--project-root", str(project_root), "--json")

    assert validate.returncode != 0
    payload = json.loads(validate.stdout)
    assert payload["errors"][0]["code"] == "operation.script_bundle_generator_missing"


def test_created_script_bundle_validates_and_executes_with_fake_docker(tmp_path):
    project_root = tmp_path / "registry"
    source_root = tmp_path / "source"
    write_container(project_root)
    write_minimal_registry(project_root)
    launcher = source_root / "scripts" / "training" / "launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("print('train')\n")

    create = run_cli(
        "script-bundle",
        "create",
        "example_run_train",
        "--project-root",
        str(project_root),
        "--source-root",
        str(source_root),
        "--entrypoint",
        "train",
        "--file",
        "scripts/training/launch.py",
        "--argv",
        "python",
        "--argv",
        "scripts/training/launch.py",
        "--json",
    )
    assert create.returncode == 0

    validate = run_cli("validate", "--project-root", str(project_root), "--json")
    assert validate.returncode == 0
    assert json.loads(validate.stdout)["ok"] is True

    op = tmp_path / "script_bundle_exec.json"
    write_operation(op, project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)

    exec_result = run_cli("exec", str(op), env={"KIKAI_DOCKER_BIN": str(fake_docker)})

    assert exec_result.returncode == 0
    assert json.loads(docker_argv_path.read_text()) == [
        "exec",
        "example-run1-training",
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--dry-run",
    ]
