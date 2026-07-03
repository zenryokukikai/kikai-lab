import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def write_registry(
    root,
    *,
    must_read=None,
    external_refs=None,
    checkpoint=None,
    run_checkpoint=None,
    model_arch="arch1",
    run_model_arch="arch1",
    required_container_ids=None,
):
    must_read = ["EXAMPLE-REF-001"] if must_read is None else must_read
    external_refs = ["EXAMPLE-REF-001", "X-BBB222"] if external_refs is None else external_refs
    checkpoint = checkpoint or "${CONTAINER_TRAINING_RUNS_ROOT}/run1/checkpoints/checkpoint.pt"
    run_checkpoint = run_checkpoint or checkpoint
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    current_doc = {
        "schema_version": 1,
        "project_id": "demo",
        "current_experiment_id": "exp1",
        "current_run_name": "run1",
        "current_checkpoint": checkpoint,
        "current_model_arch": model_arch,
        "must_read_external_ref_ids": must_read,
        "verified_by": "test-agent",
        "last_verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "staleness_warn_after_hours": 72,
        "staleness_block_after_hours": 168,
        "established_by_decision_id": "decision-old",
        "next_decision_id": "decision-next",
        "next_decision_required": True,
    }
    if required_container_ids is not None:
        current_doc["required_container_ids"] = required_container_ids
    (root / "current.json").write_text(json.dumps(current_doc))
    refs_yaml = "\n".join(
        "  - provider: example_design_registry\n"
        f"    id: {ref_id}\n"
        "    kind: design\n"
        "    required: true"
        for ref_id in external_refs
    )
    (root / "experiments" / "exp1.yaml").write_text(
        f"""experiment_id: exp1
status: active
external_refs:
{refs_yaml}
"""
    )
    (root / "runs" / "run1.yaml").write_text(
        f"""run_name: run1
experiment_id: exp1
status: completed
model_arch: {run_model_arch}
checkpoint:
  latest: {run_checkpoint}
"""
    )


def write_container(root, container_id="run1_training", *, record_container_id=None, docker=None):
    (root / "containers").mkdir(exist_ok=True)
    record_container_id = container_id if record_container_id is None else record_container_id
    if docker is None:
        docker = {
            "name": "example-run1-training",
            "image": "env:EXAMPLE_TRAINING_IMAGE",
        }
    docker_yaml = "\n".join(f"  {key}: {value}" for key, value in docker.items())
    (root / "containers" / f"{container_id}.yaml").write_text(
        f"""schema_version: 1
kind: docker_container
container_id: {record_container_id}
role: training
status: desired_running
docker:
{docker_yaml}
"""
    )


def write_script_bundle(root, bundle_id="run1_train", *, launcher_text=None):
    launcher_text = "print('bundle launcher')\n" if launcher_text is None else launcher_text
    bundle_root = root / "script_bundles" / bundle_id
    launcher_path = bundle_root / "root" / "scripts" / "training" / "launch.py"
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(launcher_text)
    bundle = {
        "schema_version": 1,
        "kind": "kikai_script_bundle",
        "bundle_id": bundle_id,
        "immutable": True,
        "generated_by": {
            "tool": "kikai script-bundle create",
            "schema_version": 1,
        },
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
                "sha256": hashlib.sha256(launcher_text.encode("utf-8")).hexdigest(),
            }
        ],
    }
    (bundle_root / "bundle.json").write_text(json.dumps(bundle, indent=2))
    return launcher_path


def test_validate_accepts_matching_current_links(tmp_path):
    write_registry(tmp_path)

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_blocks_missing_required_container_definition(tmp_path):
    write_registry(tmp_path, required_container_ids=["run1_training"])

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "current.container_missing"
    assert payload["errors"][0]["details"]["container_id"] == "run1_training"


def test_validate_blocks_container_id_mismatch(tmp_path):
    write_registry(tmp_path, required_container_ids=["run1_training"])
    write_container(tmp_path, "run1_training", record_container_id="other_container")

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "container.id_mismatch"
    assert payload["errors"][0]["details"]["expected_container_id"] == "run1_training"


def test_validate_blocks_container_missing_docker_identity(tmp_path):
    write_registry(tmp_path, required_container_ids=["run1_training"])
    write_container(tmp_path, "run1_training", docker={"name": "example-run1-training"})

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "container.docker_identity_missing"
    assert payload["errors"][0]["details"]["missing_fields"] == ["docker.image"]


def test_validate_accepts_required_container_definition(tmp_path):
    write_registry(tmp_path, required_container_ids=["run1_training"])
    write_container(tmp_path, "run1_training")

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_blocks_must_read_not_in_external_refs(tmp_path):
    write_registry(tmp_path, must_read=["X-MISSING"], external_refs=["EXAMPLE-REF-001"])

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "current.must_read_not_in_external_refs"
    assert payload["errors"][0]["details"] == {"missing_ids": ["X-MISSING"]}


def test_validate_blocks_checkpoint_mismatch(tmp_path):
    write_registry(
        tmp_path,
        run_checkpoint="${CONTAINER_TRAINING_RUNS_ROOT}/run1/checkpoints/other.pt",
    )

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "current.checkpoint_mismatch"


def test_validate_checks_script_bundle_integrity(tmp_path):
    write_registry(tmp_path)
    write_script_bundle(tmp_path)

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_blocks_script_bundle_hash_mismatch(tmp_path):
    write_registry(tmp_path)
    launcher_path = write_script_bundle(tmp_path)
    launcher_path.write_text("print('mutated')\n")

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "operation.script_bundle_hash_mismatch"


def test_validate_blocks_model_arch_mismatch(tmp_path):
    write_registry(tmp_path, model_arch="arch1", run_model_arch="arch2")

    result = run_cli("validate", "--project-root", str(tmp_path), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "current.model_arch_mismatch"
