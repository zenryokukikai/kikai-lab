import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy

import yaml

VALID_SHA256 = "a" * 64


def run_cli(*args, env=None, cwd=None):
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
        env=run_env,
        cwd=cwd,
    )


def valid_data_source(data_source_id="example_pose_manifest_v1"):
    return {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": data_source_id,
        "status": "active",
        "summary": "Example data source.",
        "source_type": "dataset_manifest",
        "immutability": {"mode": "immutable", "verified_at": "2026-06-28T00:00:00Z"},
        "storage": {
            "storage_kind": "host_path",
            "host_ref": "example_training_host",
            "path": "env:EXAMPLE_DATA_SOURCE_PATH",
            "container_mount_path": "env:CONTAINER_EXAMPLE_DATA_SOURCE_PATH",
        },
        "integrity": {
            "strategy": "not_available",
            "reason": "operation test fixture is not a launch-time file input",
        },
        "contract": {"role_compatibility": ["train_manifest", "metrics_log"]},
        "provenance": {"upstream_data_source_ids": [], "upstream_source_snapshot_ids": []},
    }


def write_data_source(root, data_source_id="example_pose_manifest_v1", *, patch=None):
    data = valid_data_source(data_source_id)
    if patch:
        data = deepcopy(data)
        patch(data)
    path = root / "data_sources" / f"{data_source_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return data


def write_operation(path, project_root, *, data_source_refs=None):
    request = {
        "operation": "example_train",
        "project_root": str(project_root),
        "target_id": "example_train",
        "adapter": "script_bundle_run",
        "bundle_id": "example_train_bundle",
        "entrypoint": "train",
    }
    if data_source_refs is not None:
        request["data_source_refs"] = data_source_refs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": request,
            },
            indent=2,
        )
    )


def create_file_data_source(root, data_file, data_source_id="example_pose_manifest_v1"):
    root.mkdir(parents=True, exist_ok=True)
    result = run_cli(
        "data-source",
        "create-file",
        data_source_id,
        "--project-root",
        str(root),
        "--source-type",
        "dataset_manifest",
        "--path",
        str(data_file),
        "--host-ref",
        "example_training_host",
        "--role",
        "train_manifest",
        "--summary",
        "Example data source.",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["data"]["data_source"]


def write_sequence_operation(path, project_root, *, step_request):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "example_sequence",
                    "project_root": str(project_root),
                    "adapter": "operation_sequence",
                    "pipeline_run_id": "example_sequence_001",
                    "steps": [{"step_id": "train", "request": step_request}],
                },
            },
            indent=2,
        )
    )


def dry_run_payload(op_path, *, env=None, cwd=None):
    result = run_cli("target", "dry-run", str(op_path), env=env, cwd=cwd)
    return result, json.loads(result.stdout)


def test_operation_dry_run_blocks_missing_data_source_ref(tmp_path):
    project_root = tmp_path / "registry"
    project_root.mkdir()
    op = tmp_path / "ops" / "missing_data_source.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "missing_manifest"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.missing"


def test_operation_dry_run_rejects_traversal_data_source_ref(tmp_path):
    project_root = tmp_path / "registry"
    project_root.mkdir()
    op = tmp_path / "ops" / "traversal_data_source.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "../secret"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.id_invalid"


def test_operation_sequence_dry_run_blocks_child_data_source_ref_before_execution(tmp_path):
    project_root = tmp_path / "registry"
    project_root.mkdir()
    op = tmp_path / "ops" / "sequence_missing_data_source.json"
    write_sequence_operation(
        op,
        project_root,
        step_request={
            "operation": "example_train",
            "adapter": "script_bundle_run",
            "bundle_id": "example_train_bundle",
            "entrypoint": "train",
            "data_source_refs": [{"role": "train_manifest", "data_source_id": "missing_manifest"}],
        },
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.missing"


def test_operation_dry_run_blocks_mutable_live_launch_input(tmp_path):
    project_root = tmp_path / "registry"
    write_data_source(
        project_root,
        patch=lambda data: data.update({"immutability": {"mode": "mutable_live"}}),
    )
    op = tmp_path / "ops" / "mutable_live.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.mutable_live_forbidden"


def test_operation_dry_run_blocks_unverified_file_sha256(tmp_path):
    project_root = tmp_path / "registry"
    data_file = tmp_path / "manifest.yaml"
    data_file.write_text("rows: []\n")
    create_file_data_source(project_root, data_file)
    data_file.write_text("rows:\n  - changed\n")
    op = tmp_path / "ops" / "hash_mismatch.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )

    result, payload = dry_run_payload(op, env={"EXAMPLE_DATA_SOURCE_PATH": str(data_file)})

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.integrity_unverified"


def test_operation_dry_run_allows_append_only_metrics_log_without_rehash(tmp_path):
    project_root = tmp_path / "registry"
    write_data_source(
        project_root,
        "example_metrics_log_v1",
        patch=lambda data: (
            data.update({"source_type": "metrics_log", "immutability": {"mode": "append_only"}}),
            data["integrity"].update(
                {"strategy": "not_available", "reason": "append-only log grows during training"}
            ),
            data["contract"].update({"role_compatibility": ["metrics_log"]}),
        ),
    )
    op = tmp_path / "ops" / "metrics_log.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "metrics_log", "data_source_id": "example_metrics_log_v1"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode == 0
    assert payload["ok"] is True
    saved = json.loads(op.read_text())
    assert (
        saved["guard_receipt"]["data_source_preflight"][0]["integrity_status"]
        == "append_only_not_rehashed"
    )


def test_operation_dry_run_blocks_hand_authored_directory_manifest_hash(tmp_path):
    project_root = tmp_path / "registry"
    write_data_source(
        project_root,
        patch=lambda data: data.update(
            {
                "source_type": "dataset_directory",
                "integrity": {"strategy": "directory_manifest_sha256", "sha256": VALID_SHA256},
            }
        ),
    )
    op = tmp_path / "ops" / "directory_manifest.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.integrity_invalid"


def create_directory_data_source(root, directory, data_source_id="example_face_cache_v1"):
    root.mkdir(parents=True, exist_ok=True)
    result = run_cli(
        "data-source",
        "create-directory",
        data_source_id,
        "--project-root",
        str(root),
        "--source-type",
        "cache_directory",
        "--path",
        str(directory),
        "--host-ref",
        "example_training_host",
        "--role",
        "face_cache",
        "--summary",
        "Example directory data source.",
        "--json",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)["data"]["data_source"]


def test_operation_dry_run_records_verified_directory_manifest_sha256(tmp_path):
    project_root = tmp_path / "registry"
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "b.txt").write_text("bee\n")
    (directory / "nested").mkdir()
    (directory / "nested" / "a.txt").write_text("aye\n")
    create_directory_data_source(project_root, directory)
    op = tmp_path / "ops" / "verified_directory.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "face_cache", "data_source_id": "example_face_cache_v1"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode == 0, payload
    saved = json.loads(op.read_text())
    assert saved["guard_receipt"]["data_source_preflight"] == [
        {
            "data_source_id": "example_face_cache_v1",
            "role": "face_cache",
            "immutability_mode": "immutable",
            "integrity_status": "directory_manifest_sha256_verified",
            "path": str(directory),
            "file_count": 2,
        }
    ]


def test_operation_dry_run_rejects_changed_directory_manifest_sha256(tmp_path):
    project_root = tmp_path / "registry"
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "a.txt").write_text("aye\n")
    create_directory_data_source(project_root, directory)
    (directory / "a.txt").write_text("changed\n")
    op = tmp_path / "ops" / "changed_directory.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "face_cache", "data_source_id": "example_face_cache_v1"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.integrity_unverified"


def test_operation_dry_run_records_verified_file_sha256(tmp_path):
    project_root = tmp_path / "registry"
    data_file = tmp_path / "manifest.yaml"
    text = "rows: []\n"
    data_file.write_text(text)
    data_source = create_file_data_source(project_root, data_file)
    assert data_source["integrity"]["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    op = tmp_path / "ops" / "verified.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )

    result, payload = dry_run_payload(op, env={"EXAMPLE_DATA_SOURCE_PATH": str(data_file)})

    assert result.returncode == 0
    assert payload["ok"] is True
    saved = json.loads(op.read_text())
    assert saved["guard_receipt"]["data_source_preflight"] == [
        {
            "data_source_id": "example_pose_manifest_v1",
            "role": "train_manifest",
            "immutability_mode": "immutable",
            "integrity_status": "file_sha256_verified",
            "path": str(data_file),
        }
    ]


def test_operation_dry_run_verifies_project_relative_file_sha256_path(tmp_path):
    project_root = tmp_path / "registry"
    data_file = project_root / "manifest.yaml"
    text = "rows: []\n"
    data_file.parent.mkdir(parents=True)
    data_file.write_text(text)
    write_data_source(
        project_root,
        patch=lambda data: (
            data["storage"].update({"path": "manifest.yaml"}),
            data.update(
                {
                    "integrity": {
                        "strategy": "file_sha256",
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "calculated_by": "kikai_lab.data-source.create-file",
                        "calculated_at": "2026-06-28T00:00:00Z",
                    }
                }
            ),
        ),
    )
    op = tmp_path / "ops" / "relative_path.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode == 0
    assert payload["ok"] is True
    saved = json.loads(op.read_text())
    assert saved["guard_receipt"]["data_source_preflight"] == [
        {
            "data_source_id": "example_pose_manifest_v1",
            "role": "train_manifest",
            "immutability_mode": "immutable",
            "integrity_status": "file_sha256_verified",
            "path": str(data_file),
        }
    ]


def test_operation_dry_run_rejects_cwd_shadow_for_relative_file_sha256_path(tmp_path):
    project_root = tmp_path / "registry"
    project_file = project_root / "manifest.yaml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text("rows: []\n")
    shadow_cwd = tmp_path / "shadow_cwd"
    shadow_cwd.mkdir()
    shadow_file = shadow_cwd / "manifest.yaml"
    shadow_file.write_text("rows:\n  - outside-cwd\n")
    write_data_source(
        project_root,
        patch=lambda data: (
            data["storage"].update({"path": "manifest.yaml"}),
            data.update(
                {
                    "integrity": {
                        "strategy": "file_sha256",
                        "sha256": hashlib.sha256(shadow_file.read_bytes()).hexdigest(),
                        "calculated_by": "kikai_lab.data-source.create-file",
                        "calculated_at": "2026-06-28T00:00:00Z",
                    }
                }
            ),
        ),
    )
    op = tmp_path / "ops" / "cwd_shadow_relative_path.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )

    result, payload = dry_run_payload(op, cwd=shadow_cwd)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.integrity_unverified"


def test_operation_dry_run_rejects_empty_optional_data_source_id(tmp_path):
    project_root = tmp_path / "registry"
    project_root.mkdir()
    op = tmp_path / "ops" / "empty_optional_data_source.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "initial_checkpoint", "data_source_id": "", "required": False}],
    )

    result, payload = dry_run_payload(op)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "operation.data_source_ref_invalid"


def test_operation_run_rechecks_file_sha256_after_dry_run(tmp_path):
    project_root = tmp_path / "registry"
    data_file = tmp_path / "manifest.yaml"
    data_file.write_text("rows: []\n")
    create_file_data_source(project_root, data_file)
    op = tmp_path / "ops" / "stale_receipt.json"
    write_operation(
        op,
        project_root,
        data_source_refs=[{"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"}],
    )
    dry_run_result, dry_run = dry_run_payload(op)
    assert dry_run_result.returncode == 0, dry_run
    data_file.write_text("rows:\n  - changed\n")

    result = run_cli("target", "run", str(op))
    payload = json.loads(result.stdout)

    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "data_source.integrity_unverified"


def test_data_source_create_adapter_registers_directory_before_launch_ref(tmp_path):
    project_root = tmp_path / "registry"
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "a.txt").write_text("aye\n")
    create_op = tmp_path / "ops" / "create_face_cache.json"
    create_op.parent.mkdir(parents=True, exist_ok=True)
    create_op.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "create_face_cache",
                    "project_root": str(project_root),
                    "adapter": "data_source_create",
                    "data_source_kind": "directory",
                    "data_source_id": "example_face_cache_v1",
                    "source_type": "cache_directory",
                    "path": str(directory),
                    "host_ref": "example_training_host",
                    "roles": ["face_cache"],
                    "summary": "Example cache directory.",
                },
            },
            indent=2,
        )
    )
    dry_run_result = run_cli("target", "dry-run", str(create_op))
    assert dry_run_result.returncode == 0, dry_run_result.stdout

    create_result = run_cli("target", "run", str(create_op))
    assert create_result.returncode == 0, create_result.stdout
    created = json.loads(create_result.stdout)["data"]
    assert created["execution_status"] == "data_source_created"
    assert created["data_source_id"] == "example_face_cache_v1"

    launch_op = tmp_path / "ops" / "launch_with_ref.json"
    write_operation(
        launch_op,
        project_root,
        data_source_refs=[{"role": "face_cache", "data_source_id": "example_face_cache_v1"}],
    )
    launch_dry_run_result, payload = dry_run_payload(launch_op)
    assert launch_dry_run_result.returncode == 0, payload
    assert payload["ok"] is True
