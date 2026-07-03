import json
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "schemas"
REQUIRED_SCHEMAS = [
    "current.schema.json",
    "project.schema.json",
    "experiment.schema.json",
    "run.schema.json",
    "artifact.schema.json",
    "delivery_target.schema.json",
    "artifact_delivery.schema.json",
    "pipeline_target.schema.json",
    "pipeline_run.schema.json",
    "operation.schema.json",
    "recipe.schema.json",
    "metric.schema.json",
    "offline_probe.schema.json",
    "review.schema.json",
    "decision.schema.json",
    "next_action.schema.json",
    "implementation_change.schema.json",
    "environment.schema.json",
    "docker_container.schema.json",
    "managed_run.schema.json",
    "script_bundle.schema.json",
    "source_snapshot.schema.json",
    "data_source.schema.json",
    "tensorboard_service.schema.json",
    "external_ref.schema.json",
]


def test_required_schema_files_exist_and_are_json_objects():
    for filename in REQUIRED_SCHEMAS:
        path = SCHEMA_DIR / filename
        assert path.exists(), filename
        schema = json.loads(path.read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert "schema_version" in schema["properties"]


def test_operation_schema_allows_optional_null_data_source_refs():
    schema = json.loads((SCHEMA_DIR / "operation.schema.json").read_text())
    operation = {
        "schema_version": 1,
        "kind": "kikai_operation",
        "request": {
            "operation": "example",
            "project_root": "examples/example_project",
            "adapter": "script_bundle_run",
            "data_source_refs": [
                {"role": "initial_checkpoint", "data_source_id": None, "required": False}
            ],
        },
    }

    Draft202012Validator(schema).validate(operation)


def test_operation_schema_rejects_null_data_source_ref_unless_required_false():
    schema = json.loads((SCHEMA_DIR / "operation.schema.json").read_text())
    operation = {
        "schema_version": 1,
        "kind": "kikai_operation",
        "request": {
            "operation": "example",
            "project_root": "examples/example_project",
            "adapter": "script_bundle_run",
            "data_source_refs": [{"role": "initial_checkpoint", "data_source_id": None}],
        },
    }

    errors = list(Draft202012Validator(schema).iter_errors(operation))

    assert errors


def test_run_schema_rejects_null_data_source_ref_unless_required_false():
    schema = json.loads((SCHEMA_DIR / "run.schema.json").read_text())
    run = {
        "schema_version": 1,
        "run_name": "run1",
        "data_source_refs": [{"role": "initial_checkpoint", "data_source_id": None}],
    }

    errors = list(Draft202012Validator(schema).iter_errors(run))

    assert errors


def test_data_source_schema_rejects_file_sha256_without_kikai_calculated_by():
    schema = json.loads((SCHEMA_DIR / "data_source.schema.json").read_text())
    data_source = {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": "example_pose_manifest_v1",
        "status": "active",
        "source_type": "dataset_manifest",
        "storage": {
            "storage_kind": "host_path",
            "host_ref": "example_training_host",
            "path": "env:EXAMPLE_POSE_MANIFEST_PATH",
        },
        "immutability": {"mode": "immutable"},
        "integrity": {"strategy": "file_sha256", "sha256": "a" * 64},
    }

    errors = list(Draft202012Validator(schema).iter_errors(data_source))

    assert errors


def test_data_source_schema_rejects_host_path_without_host_ref_and_path():
    schema = json.loads((SCHEMA_DIR / "data_source.schema.json").read_text())
    data_source = {
        "schema_version": 1,
        "kind": "kikai_data_source",
        "data_source_id": "example_pose_manifest_v1",
        "status": "active",
        "source_type": "dataset_manifest",
        "storage": {"storage_kind": "host_path"},
        "immutability": {"mode": "immutable"},
        "integrity": {
            "strategy": "not_available",
            "reason": "schema fixture only",
        },
    }

    errors = list(Draft202012Validator(schema).iter_errors(data_source))

    assert errors
