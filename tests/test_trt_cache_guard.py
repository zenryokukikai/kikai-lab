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


def write_current(project_root):
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "example",
                "current_run_name": "example_run",
                "current_checkpoint": "/runs/example_run/checkpoints/checkpoint_step_003000.pt",
                "current_model_arch": "example_arch_v1",
                "verified_by": "test",
                "last_verified_at": "2026-06-25T00:00:00Z",
                "staleness_warn_after_hours": 999999,
                "staleness_block_after_hours": 999999,
            },
            indent=2,
        )
    )


def write_guard_operation(
    path,
    project_root,
    *,
    guard_id="trt_guard1",
    model_arch="example_arch_v1",
    trt_cache_dir="/workspace/trt_cache/example_run",
    compile_mode="reuse_cache",
    require_compile_cache=True,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "trt_cache_guard",
                    "project_root": str(project_root),
                    "adapter": "trt_cache_guard",
                    "guard_id": guard_id,
                    "model_arch": model_arch,
                    "trt_cache_dir": trt_cache_dir,
                    "compile_mode": compile_mode,
                    "require_compile_cache": require_compile_cache,
                },
            },
            indent=2,
        )
    )


def test_trt_cache_guard_passes_when_cache_is_required_and_model_matches_current(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "trt_guard.json"
    write_guard_operation(op, project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "trt_cache_guard_passed"
    record_path = project_root / "guard_records" / "trt_guard1.json"
    record = json.loads(record_path.read_text())
    assert record["status"] == "passed"
    assert record["model_arch"] == "example_arch_v1"
    assert record["trt_cache_dir"] == "/workspace/trt_cache/example_run"
    assert record["compile_mode"] == "reuse_cache"
    assert record["require_compile_cache"] is True


def test_trt_cache_guard_fails_when_cache_is_not_required(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "trt_guard.json"
    write_guard_operation(op, project_root, require_compile_cache=False)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.trt_cache_required"
    assert not (project_root / "guard_records" / "trt_guard1.json").exists()


def test_trt_cache_guard_fails_when_model_arch_mismatches_current(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "trt_guard.json"
    write_guard_operation(op, project_root, model_arch="wrong_arch")
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.trt_cache_model_arch_mismatch"
    assert not (project_root / "guard_records" / "trt_guard1.json").exists()


def test_trt_cache_guard_fails_when_compile_mode_disables_cache(tmp_path):
    project_root = tmp_path / "registry"
    write_current(project_root)
    op = tmp_path / "ops" / "trt_guard.json"
    write_guard_operation(op, project_root, compile_mode="disabled")
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.trt_cache_compile_mode_forbidden"
    assert not (project_root / "guard_records" / "trt_guard1.json").exists()
