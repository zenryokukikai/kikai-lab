import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime

import yaml

VALID_SHA256 = "a" * 64


def run_cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
        cwd=cwd,
    )


def write_registry(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "demo",
                "current_experiment_id": "exp1",
                "current_run_name": "run1",
                "current_checkpoint": (
                    "${CONTAINER_TRAINING_RUNS_ROOT}/run1/checkpoints/checkpoint.pt"
                ),
                "current_model_arch": "arch1",
                "must_read_external_ref_ids": ["EXAMPLE-REF-001"],
                "verified_by": "test-agent",
                "last_verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "staleness_warn_after_hours": 72,
                "staleness_block_after_hours": 168,
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
    write_run_record(root)


def write_run_record(root, *, data_source_refs=None, fresh_no_resume=None):
    run = {
        "run_name": "run1",
        "experiment_id": "exp1",
        "status": "completed",
        "model_arch": "arch1",
        "checkpoint": {"latest": "${CONTAINER_TRAINING_RUNS_ROOT}/run1/checkpoints/checkpoint.pt"},
    }
    if data_source_refs is not None:
        run["data_source_refs"] = data_source_refs
    if fresh_no_resume is not None:
        run["fresh_no_resume"] = fresh_no_resume
    (root / "runs" / "run1.yaml").write_text(yaml.safe_dump(run, sort_keys=False))


def valid_data_source(data_source_id="example_pose_manifest_v1"):
    return {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": data_source_id,
        "status": "active",
        "summary": "Pose training manifest for an example fixture.",
        "source_type": "dataset_manifest",
        "immutability": {"mode": "immutable", "verified_at": "2026-06-28T00:00:00Z"},
        "storage": {
            "storage_kind": "host_path",
            "host_ref": "example_training_host",
            "path": "env:EXAMPLE_POSE_MANIFEST_PATH",
            "container_mount_path": "env:CONTAINER_EXAMPLE_POSE_MANIFEST_PATH",
        },
        "integrity": {
            "strategy": "not_available",
            "reason": "registry shape fixture is not a launch-time file input",
        },
        "contract": {
            "role_compatibility": ["train_manifest", "eval_manifest"],
            "media_type": "application/x-yaml",
        },
        "provenance": {
            "created_by": "kikai data-source register",
            "upstream_data_source_ids": [],
            "upstream_source_snapshot_ids": ["example_project_fixture"],
        },
        "notes": [],
    }


def write_data_source(root, data_source_id="example_pose_manifest_v1", *, patch=None):
    data = valid_data_source(data_source_id)
    if patch:
        data = deepcopy(data)
        patch(data)
    data_source_dir = root / "data_sources"
    data_source_dir.mkdir(exist_ok=True)
    (data_source_dir / f"{data_source_id}.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return data


def validate_codes(root):
    result = run_cli("validate", "--project-root", str(root), "--json")
    payload = json.loads(result.stdout)
    return result, [item["code"] for item in payload["errors"]], payload


def test_validate_accepts_valid_data_source_record(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path)

    result, codes, payload = validate_codes(tmp_path)

    assert result.returncode == 0
    assert payload["ok"] is True
    assert codes == []


def test_validate_blocks_symlinked_data_source_record_without_traceback(tmp_path):
    write_registry(tmp_path)
    data_source_dir = tmp_path / "data_sources"
    data_source_dir.mkdir(exist_ok=True)
    outside_record = tmp_path / "outside.yaml"
    outside_record.write_text(yaml.safe_dump(valid_data_source("evil_manifest"), sort_keys=False))
    (data_source_dir / "evil_manifest.yaml").symlink_to(outside_record)

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")
    payload = json.loads(result.stdout)

    assert result.returncode != 0
    assert result.stderr == ""
    assert payload["ok"] is False
    assert [item["code"] for item in payload["errors"]] == ["data_source.path_invalid"]


def test_validate_blocks_data_source_id_mismatch(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path, patch=lambda data: data.update({"data_source_id": "other_manifest"})
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.id_mismatch" in codes


def test_validate_blocks_invalid_data_source_kind(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path, patch=lambda data: data.update({"kind": "other_kind"}))

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.kind_invalid" in codes


def test_validate_blocks_invalid_data_source_status(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path, patch=lambda data: data.update({"status": "maybe"}))

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.status_invalid" in codes


def test_validate_blocks_invalid_source_type(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path, patch=lambda data: data.update({"source_type": "pose_manifest"}))

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.source_type_invalid" in codes


def test_validate_blocks_invalid_storage_shape(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        patch=lambda data: data.update(
            {"storage": {"storage_kind": "host_path", "path": "env:ONLY_PATH"}}
        ),
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.storage_invalid" in codes


def test_validate_blocks_invalid_immutability_mode(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path, patch=lambda data: data.update({"immutability": {"mode": "rewriteable"}})
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.immutability_invalid" in codes


def test_validate_blocks_invalid_file_sha256(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        patch=lambda data: data.update(
            {
                "integrity": {
                    "strategy": "file_sha256",
                    "sha256": "A" * 64,
                    "calculated_by": "kikai_lab.data-source.create-file",
                }
            }
        ),
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.integrity_invalid" in codes


def test_validate_blocks_externally_supplied_file_sha256(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        patch=lambda data: data.update(
            {"integrity": {"strategy": "file_sha256", "sha256": VALID_SHA256}}
        ),
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.integrity_invalid" in codes


def test_validate_blocks_spoofed_kikai_file_sha256_marker(tmp_path):
    write_registry(tmp_path)
    source_file = tmp_path / "manifest.yaml"
    source_file.write_text("rows: []\n")
    actual_sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()
    spoofed_sha256 = "b" * 64
    assert spoofed_sha256 != actual_sha256

    def spoof_marker(data):
        data["storage"]["path"] = str(source_file)
        data["integrity"] = {
            "strategy": "file_sha256",
            "sha256": spoofed_sha256,
            "calculated_by": "kikai_lab.data-source.create-file",
            "calculated_at": "2026-06-28T00:00:00Z",
        }

    write_data_source(tmp_path, patch=spoof_marker)

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.integrity_unverified" in codes


def test_validate_relative_file_sha256_path_ignores_cwd_shadow(tmp_path):
    project_root = tmp_path / "registry"
    write_registry(project_root)
    project_file = project_root / "manifest.yaml"
    project_file.write_text("rows: []\n")
    shadow_cwd = tmp_path / "shadow_cwd"
    shadow_cwd.mkdir()
    shadow_file = shadow_cwd / "manifest.yaml"
    shadow_file.write_text("rows:\n  - outside-cwd\n")

    def use_shadow_hash_with_relative_path(data):
        data["storage"]["path"] = "manifest.yaml"
        data["integrity"] = {
            "strategy": "file_sha256",
            "sha256": hashlib.sha256(shadow_file.read_bytes()).hexdigest(),
            "calculated_by": "kikai_lab.data-source.create-file",
            "calculated_at": "2026-06-28T00:00:00Z",
        }

    write_data_source(project_root, patch=use_shadow_hash_with_relative_path)

    result = run_cli("validate", "--project-root", str(project_root), "--json", cwd=shadow_cwd)
    payload = json.loads(result.stdout)
    codes = [item["code"] for item in payload["errors"]]

    assert result.returncode != 0
    assert "data_source.integrity_unverified" in codes


def test_validate_blocks_file_sha256_without_calculated_at(tmp_path):
    write_registry(tmp_path)
    source_file = tmp_path / "manifest.yaml"
    source_file.write_text("rows: []\n")

    def omit_calculated_at(data):
        data["storage"]["path"] = str(source_file)
        data["integrity"] = {
            "strategy": "file_sha256",
            "sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
            "calculated_by": "kikai_lab.data-source.create-file",
        }

    write_data_source(tmp_path, patch=omit_calculated_at)

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.integrity_invalid" in codes


def test_validate_blocks_directory_manifest_sha256_without_calculated_at(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        patch=lambda data: data.update(
            {
                "integrity": {
                    "strategy": "directory_manifest_sha256",
                    "sha256": VALID_SHA256,
                    "calculated_by": "kikai_lab.data-source.create-directory-manifest",
                }
            }
        ),
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.integrity_invalid" in codes


def test_data_source_create_file_calculates_sha256(tmp_path):
    write_registry(tmp_path)
    source_file = tmp_path / "manifest.yaml"
    source_text = "rows: []\n"
    source_file.write_text(source_text)

    result = run_cli(
        "data-source",
        "create-file",
        "example_pose_manifest_v1",
        "--project-root",
        str(tmp_path),
        "--source-type",
        "dataset_manifest",
        "--path",
        str(source_file),
        "--host-ref",
        "example_training_host",
        "--role",
        "train_manifest",
        "--summary",
        "Example pose manifest.",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    integrity = payload["data"]["data_source"]["integrity"]
    assert integrity["sha256"] == hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    assert integrity["calculated_by"] == "kikai_lab.data-source.create-file"


def test_data_source_create_file_hashes_project_relative_path_not_cwd_shadow(tmp_path):
    project_root = tmp_path / "registry"
    write_registry(project_root)
    project_file = project_root / "manifest.yaml"
    project_text = "rows: []\n"
    project_file.write_text(project_text)
    shadow_cwd = tmp_path / "shadow_cwd"
    shadow_cwd.mkdir()
    shadow_file = shadow_cwd / "manifest.yaml"
    shadow_file.write_text("rows:\n  - outside-cwd\n")

    result = run_cli(
        "data-source",
        "create-file",
        "project_relative_manifest_v1",
        "--project-root",
        str(project_root),
        "--source-type",
        "dataset_manifest",
        "--path",
        "manifest.yaml",
        "--host-ref",
        "example_training_host",
        "--role",
        "train_manifest",
        "--summary",
        "Project relative manifest.",
        "--json",
        cwd=shadow_cwd,
    )

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    integrity = payload["data"]["data_source"]["integrity"]
    assert integrity["sha256"] == hashlib.sha256(project_text.encode("utf-8")).hexdigest()
    assert payload["data"]["resolved_path"] == str(project_file.resolve(strict=False))


def test_data_source_create_file_has_no_user_supplied_sha256_argument(tmp_path):
    write_registry(tmp_path)
    source_file = tmp_path / "manifest.yaml"
    source_file.write_text("rows: []\n")

    result = run_cli(
        "data-source",
        "create-file",
        "example_pose_manifest_v1",
        "--project-root",
        str(tmp_path),
        "--source-type",
        "dataset_manifest",
        "--path",
        str(source_file),
        "--host-ref",
        "example_training_host",
        "--role",
        "train_manifest",
        "--summary",
        "Example pose manifest.",
        "--sha256",
        VALID_SHA256,
    )

    assert result.returncode == 2
    assert "--sha256" in result.stderr


def test_data_source_create_directory_calculates_manifest_sha256(tmp_path):
    write_registry(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "b.txt").write_text("bee\n")
    (cache_root / "nested").mkdir()
    (cache_root / "nested" / "a.txt").write_text("aye\n")

    result = run_cli(
        "data-source",
        "create-directory",
        "example_face_cache_v1",
        "--project-root",
        str(tmp_path),
        "--source-type",
        "cache_directory",
        "--path",
        str(cache_root),
        "--host-ref",
        "example_training_host",
        "--role",
        "face_cache",
        "--summary",
        "Example generated cache directory.",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    integrity = payload["data"]["data_source"]["integrity"]
    assert integrity["strategy"] == "directory_manifest_sha256"
    assert integrity["calculated_by"] == "kikai_lab.data-source.create-directory-manifest"
    assert integrity["file_count"] == 2
    assert len(integrity["sha256"]) == 64


def test_data_source_create_directory_rejects_symlink(tmp_path):
    write_registry(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "target.txt").write_text("ok\n")
    (cache_root / "link.txt").symlink_to(cache_root / "target.txt")

    result = run_cli(
        "data-source",
        "create-directory",
        "example_face_cache_v1",
        "--project-root",
        str(tmp_path),
        "--source-type",
        "cache_directory",
        "--path",
        str(cache_root),
        "--host-ref",
        "example_training_host",
        "--role",
        "face_cache",
        "--summary",
        "Example generated cache directory.",
        "--json",
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "data_source.directory_manifest_unverified"


def test_data_source_show_rejects_path_traversal_id(tmp_path):
    write_registry(tmp_path)
    outside = tmp_path / "secret.yaml"
    outside.write_text("secret: do-not-read\n")

    result = run_cli(
        "data-source",
        "show",
        "../secret",
        "--project-root",
        str(tmp_path),
        "--json",
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "data_source.id_invalid"
    assert "do-not-read" not in result.stdout


def test_data_source_create_file_rejects_path_traversal_id(tmp_path):
    write_registry(tmp_path)
    source_file = tmp_path / "manifest.yaml"
    source_file.write_text("rows: []\n")

    result = run_cli(
        "data-source",
        "create-file",
        "../escaped",
        "--project-root",
        str(tmp_path),
        "--source-type",
        "dataset_manifest",
        "--path",
        str(source_file),
        "--host-ref",
        "example_training_host",
        "--role",
        "train_manifest",
        "--summary",
        "Example pose manifest.",
        "--json",
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "data_source.id_invalid"
    assert not (tmp_path / "escaped.yaml").exists()


def test_data_source_create_file_rejects_invalid_upstream_data_source_id(tmp_path):
    write_registry(tmp_path)
    source_file = tmp_path / "manifest.yaml"
    source_file.write_text("rows: []\n")

    result = run_cli(
        "data-source",
        "create-file",
        "example_pose_manifest_v1",
        "--project-root",
        str(tmp_path),
        "--source-type",
        "dataset_manifest",
        "--path",
        str(source_file),
        "--host-ref",
        "example_training_host",
        "--role",
        "train_manifest",
        "--summary",
        "Example pose manifest.",
        "--upstream-data-source-id",
        "../evil",
        "--json",
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "data_source.id_invalid"
    assert not (tmp_path / "data_sources" / "example_pose_manifest_v1.yaml").exists()


def test_validate_blocks_unknown_contract_role(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        patch=lambda data: data["contract"].update({"role_compatibility": ["training_manifest"]}),
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.role_unknown" in codes


def test_validate_rejects_project_local_role_extension_placeholders(tmp_path):
    write_registry(tmp_path)

    def add_reserved_role_extensions(data):
        data["role_namespace"] = "example"
        data["custom_roles"] = ["example:training_manifest"]

    write_data_source(tmp_path, patch=add_reserved_role_extensions)

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.invalid" in codes


def test_validate_blocks_blocked_data_source_status(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path, patch=lambda data: data.update({"status": "blocked"}))

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.status_invalid" in codes


def test_validate_blocks_direct_data_source_lineage_cycle(tmp_path):
    write_registry(tmp_path)

    def add_self_reference(data):
        data["provenance"]["upstream_data_source_ids"] = [data["data_source_id"]]

    write_data_source(tmp_path, patch=add_self_reference)
    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.lineage_cycle" in codes


def test_validate_blocks_invalid_upstream_data_source_id(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        patch=lambda data: data["provenance"].update(
            {"upstream_data_source_ids": ["../evil"]}
        ),
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.id_invalid" in codes



def test_validate_resolves_run_data_source_refs(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path)
    write_run_record(
        tmp_path,
        data_source_refs=[
            {
                "role": "train_manifest",
                "data_source_id": "example_pose_manifest_v1",
                "required": True,
            }
        ],
    )

    result, codes, payload = validate_codes(tmp_path)

    assert result.returncode == 0
    assert payload["ok"] is True
    assert codes == []


def test_validate_blocks_required_run_data_source_ref_without_id(tmp_path):
    write_registry(tmp_path)
    write_run_record(
        tmp_path,
        data_source_refs=[{"role": "train_manifest", "data_source_id": None, "required": True}],
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.required_missing" in codes


def test_validate_blocks_run_data_source_ref_missing_role(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path)
    write_run_record(
        tmp_path,
        data_source_refs=[{"data_source_id": "example_pose_manifest_v1", "required": True}],
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.role_missing" in codes


def test_validate_blocks_run_data_source_ref_unknown_role(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path)
    write_run_record(
        tmp_path,
        data_source_refs=[
            {
                "role": "training_manifest",
                "data_source_id": "example_pose_manifest_v1",
                "required": True,
            }
        ],
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.role_unknown" in codes


def test_validate_blocks_run_data_source_ref_incompatible_role(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path)
    write_run_record(
        tmp_path,
        data_source_refs=[
            {"role": "source_audio", "data_source_id": "example_pose_manifest_v1", "required": True}
        ],
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "data_source.role_incompatible" in codes


def test_validate_blocks_fresh_no_resume_with_required_initial_checkpoint(tmp_path):
    write_registry(tmp_path)
    write_data_source(
        tmp_path,
        "example_initial_checkpoint_v1",
        patch=lambda data: (
            data.update({"source_type": "checkpoint_file"}),
            data["contract"].update({"role_compatibility": ["initial_checkpoint"]}),
        ),
    )
    write_run_record(
        tmp_path,
        fresh_no_resume=True,
        data_source_refs=[
            {
                "role": "initial_checkpoint",
                "data_source_id": "example_initial_checkpoint_v1",
                "required": True,
            }
        ],
    )

    result, codes, _ = validate_codes(tmp_path)

    assert result.returncode != 0
    assert "run.data_source_ref_invalid" in codes


def test_data_source_show_returns_record(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path)

    result = run_cli(
        "data-source",
        "show",
        "example_pose_manifest_v1",
        "--project-root",
        str(tmp_path),
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["data_source"]["data_source_id"] == "example_pose_manifest_v1"


def test_data_source_validate_returns_errors_for_invalid_record(tmp_path):
    write_registry(tmp_path)
    write_data_source(tmp_path, patch=lambda data: data.update({"status": "blocked"}))

    result = run_cli(
        "data-source",
        "validate",
        "example_pose_manifest_v1",
        "--project-root",
        str(tmp_path),
        "--json",
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "data_source.status_invalid"
