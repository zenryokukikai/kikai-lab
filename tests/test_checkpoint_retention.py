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


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def make_run(tmp_path, checkpoints, *, metrics_rows=None, extra=None):
    """Create a run dir with the given checkpoint filenames (empty .pt files)."""
    run_dir = tmp_path / "runs" / "example_run"
    ckpt_dir = run_dir / "checkpoints"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    for name in checkpoints:
        touch(ckpt_dir / name)
    for name in extra or []:
        touch(ckpt_dir / name)
    if metrics_rows is not None:
        (ckpt_dir / "metrics.jsonl").write_text(
            "\n".join(json.dumps(row) for row in metrics_rows) + "\n"
        )
    return run_dir, ckpt_dir


def write_retention_operation(path, project_root, run_dir, *, request_extra=None):
    request = {
        "operation": "checkpoint_retention",
        "project_root": str(project_root),
        "adapter": "checkpoint_retention",
        "run_dir": str(run_dir),
    }
    if request_extra:
        request.update(request_extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "kind": "kikai_operation", "request": request},
            indent=2,
        )
    )


def run_retention(tmp_path, project_root, run_dir, *, request_extra=None):
    op = tmp_path / "ops" / "retention.json"
    write_retention_operation(op, project_root, run_dir, request_extra=request_extra)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stderr
    result = run_cli("exec", str(op))
    return result


def test_keep_latest_only(tmp_path):
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_001000_loss30p0.pt",
            "checkpoint_step_002000_loss20p0.pt",
            "checkpoint_step_003000_loss25p0.pt",
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 2, "keep_best": 0},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert set(data["kept_latest"]) == {
        "checkpoint_step_003000_loss25p0.pt",
        "checkpoint_step_002000_loss20p0.pt",
    }
    assert data["kept_best"] == []
    assert data["deleted"] == ["checkpoint_step_001000_loss30p0.pt"]
    assert not (ckpt_dir / "checkpoint_step_001000_loss30p0.pt").exists()
    assert (ckpt_dir / "checkpoint_step_003000_loss25p0.pt").exists()
    assert data["config"]["source"] == "request"


def test_keep_best_only(tmp_path):
    # best window = newest keep_best of the best_step_* family (each is a strictly-better
    # snapshot, so newest-by-step == best-by-metric). checkpoint_step_* are NOT best-eligible.
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "best_step_001000_loss30p0.pt",
            "best_step_002000_loss20p0.pt",
            "best_step_003000_loss10p0.pt",  # newest best
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 0, "keep_best": 1, "metric_mode": "min"},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert data["kept_best"] == ["best_step_003000_loss10p0.pt"]
    assert set(data["deleted"]) == {
        "best_step_001000_loss30p0.pt",
        "best_step_002000_loss20p0.pt",
    }
    assert (ckpt_dir / "best_step_003000_loss10p0.pt").exists()


def test_best_window_is_best_step_family_only(tmp_path):
    # A low-loss periodic checkpoint_step_* must NOT be pulled into the best window; only
    # best_step_* count as best. Guards the curated-best-archive-never-evicted invariant.
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_005000_loss1p0.pt",  # lowest loss, but NOT a best_step
            "best_step_002000_loss20p0.pt",
            "best_step_003000_loss15p0.pt",
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 0, "keep_best": 2, "metric_mode": "min"},
    )
    data = json.loads(result.stdout)["data"]
    assert set(data["kept_best"]) == {
        "best_step_003000_loss15p0.pt",
        "best_step_002000_loss20p0.pt",
    }
    # the low-loss periodic checkpoint is NOT protected by the best window (keep_latest=0)
    assert "checkpoint_step_005000_loss1p0.pt" in data["deleted"]


def test_keep_latest_and_best_combined(tmp_path):
    # Two independent windows: latest keeps newest checkpoint_step_*, best keeps newest
    # best_step_*. A file present in BOTH families' newest windows is protected once.
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_001000_loss10p0.pt",  # oldest periodic
            "checkpoint_step_002000_loss50p0.pt",
            "checkpoint_step_003000_loss40p0.pt",  # newest periodic
            "best_step_001000_loss10p0.pt",  # only best
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 1, "keep_best": 1},
    )
    data = json.loads(result.stdout)["data"]
    assert data["kept_latest"] == ["checkpoint_step_003000_loss40p0.pt"]
    assert data["kept_best"] == ["best_step_001000_loss10p0.pt"]
    assert set(data["deleted"]) == {
        "checkpoint_step_001000_loss10p0.pt",
        "checkpoint_step_002000_loss50p0.pt",
    }
    assert (ckpt_dir / "checkpoint_step_003000_loss40p0.pt").exists()
    assert (ckpt_dir / "best_step_001000_loss10p0.pt").exists()


def test_best_and_latest_windows_are_independent(tmp_path):
    project_root = tmp_path / "registry"
    # best_step_* live in their own window and are protected independently of the newest
    # checkpoint_step_* window; neither window can evict the other's protected files.
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "best_step_001000_loss5p0.pt",  # a best, far behind the latest window
            "checkpoint_step_002000_loss40p0.pt",
            "checkpoint_step_003000_loss30p0.pt",
            "checkpoint_step_004000_loss20p0.pt",  # newest
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 2, "keep_best": 1},
    )
    data = json.loads(result.stdout)["data"]
    assert set(data["kept_latest"]) == {
        "checkpoint_step_004000_loss20p0.pt",
        "checkpoint_step_003000_loss30p0.pt",
    }
    assert data["kept_best"] == ["best_step_001000_loss5p0.pt"]
    # step 2000 periodic is neither in the newest-2 window nor a best_step -> deleted
    assert data["deleted"] == ["checkpoint_step_002000_loss40p0.pt"]
    assert (ckpt_dir / "best_step_001000_loss5p0.pt").exists()


def test_best_checkpoint_pointer_never_deleted(tmp_path):
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_001000_loss30p0.pt",
            "checkpoint_step_002000_loss20p0.pt",
        ],
        extra=["best_checkpoint.pt"],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 1, "keep_best": 0},
    )
    data = json.loads(result.stdout)["data"]
    assert data["best_checkpoint_pointer"] == "best_checkpoint.pt"
    assert "best_checkpoint.pt" not in data["deleted"]
    assert (ckpt_dir / "best_checkpoint.pt").exists()


def test_dry_run_makes_no_deletions(tmp_path):
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_001000_loss30p0.pt",
            "checkpoint_step_002000_loss20p0.pt",
            "checkpoint_step_003000_loss25p0.pt",
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 1, "keep_best": 0, "dry_run": True},
    )
    data = json.loads(result.stdout)["data"]
    assert data["dry_run"] is True
    assert data["execution_status"] == "checkpoint_retention_previewed"
    assert set(data["deleted"]) == {
        "checkpoint_step_001000_loss30p0.pt",
        "checkpoint_step_002000_loss20p0.pt",
    }
    # Nothing actually removed.
    for name in (
        "checkpoint_step_001000_loss30p0.pt",
        "checkpoint_step_002000_loss20p0.pt",
        "checkpoint_step_003000_loss25p0.pt",
    ):
        assert (ckpt_dir / name).exists()


def test_loss_decode_from_filename_tag():
    # Loss is no longer the basis for best selection (best window = newest best_step_*),
    # but the decode helper is still used for reporting + the convention warning. Unit-test
    # it directly: the p/m encoding round-trips, negatives decode, missing tag -> None.
    from pathlib import Path

    from kikai_lab.operation import checkpoint_loss_from_name

    assert checkpoint_loss_from_name(Path("checkpoint_step_021500_loss20p6986.pt")) == 20.6986
    assert checkpoint_loss_from_name(Path("best_step_001000_lossm0p5.pt")) == -0.5
    assert checkpoint_loss_from_name(Path("checkpoint_step_002000_loss12p25.pt")) == 12.25
    assert checkpoint_loss_from_name(Path("checkpoint_step_001000.pt")) is None


def test_loss_fallback_from_metrics_jsonl():
    # metrics.jsonl fallback for legacy (no _loss tag) filenames: exact early_stop_eval
    # match wins, else nearest train_metrics by step.
    from kikai_lab.operation import checkpoint_loss_from_metrics

    eval_rows = [(2000, 60.0)]
    train_rows = [(1000, 5.0), (2000, 50.0)]
    assert checkpoint_loss_from_metrics(1000, eval_rows=eval_rows, train_rows=train_rows) == 5.0
    # exact early_stop_eval step match takes precedence over train_metrics at the same step
    assert checkpoint_loss_from_metrics(2000, eval_rows=eval_rows, train_rows=train_rows) == 60.0
    assert checkpoint_loss_from_metrics(9999, eval_rows=[], train_rows=[]) is None


def test_legacy_unnamed_checkpoints_warn(tmp_path):
    # Legacy filenames lacking the _loss convention each emit a warning; latest window still
    # protects the newest by step.
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_001000.pt",
            "checkpoint_step_002000.pt",
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 1, "keep_best": 0},
    )
    data = json.loads(result.stdout)["data"]
    assert data["kept_latest"] == ["checkpoint_step_002000.pt"]
    warned = {w["details"]["name"] for w in data["warnings"]}
    assert warned == {"checkpoint_step_001000.pt", "checkpoint_step_002000.pt"}
    assert not (ckpt_dir / "checkpoint_step_001000.pt").exists()


def test_config_from_experiment_yaml(tmp_path):
    project_root = tmp_path / "registry"
    experiments = project_root / "experiments"
    experiments.mkdir(parents=True, exist_ok=True)
    (experiments / "exp1.yaml").write_text(
        "experiment_id: exp1\n"
        "checkpoint_retention:\n"
        "  keep_latest: 1\n"
        "  keep_best: 0\n"
        "  metric_key: mean_train_loss\n"
        "  metric_mode: min\n"
    )
    run_dir, _ = make_run(
        tmp_path,
        [
            "checkpoint_step_001000_loss30p0.pt",
            "checkpoint_step_002000_loss20p0.pt",
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"experiment_id": "exp1"},
    )
    data = json.loads(result.stdout)["data"]
    assert data["config"]["source"] == "experiment"
    assert data["config"]["keep_latest"] == 1
    assert data["kept_latest"] == ["checkpoint_step_002000_loss20p0.pt"]
    assert data["deleted"] == ["checkpoint_step_001000_loss30p0.pt"]


def test_missing_config_errors(tmp_path):
    project_root = tmp_path / "registry"
    run_dir, _ = make_run(tmp_path, ["checkpoint_step_001000_loss30p0.pt"])
    op = tmp_path / "ops" / "retention.json"
    # No keep_latest/keep_best and no experiment_id -> clear error.
    write_retention_operation(op, project_root, run_dir)
    assert run_cli("target", "dry-run", str(op)).returncode == 0
    result = run_cli("exec", str(op))
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.checkpoint_retention_config_missing"


def test_warning_on_non_convention_filenames(tmp_path):
    project_root = tmp_path / "registry"
    run_dir, ckpt_dir = make_run(
        tmp_path,
        [
            "checkpoint_step_001000_loss30p0.pt",
            "checkpoint_step_002000.pt",  # no _loss tag -> warned
        ],
        metrics_rows=[
            {"event": "train_metrics", "step": 2000, "loss": 15.0},
        ],
    )
    result = run_retention(
        tmp_path,
        project_root,
        run_dir,
        request_extra={"keep_latest": 2, "keep_best": 0},
    )
    data = json.loads(result.stdout)["data"]
    warned = {w["details"]["name"] for w in data["warnings"]}
    assert warned == {"checkpoint_step_002000.pt"}
    assert data["warnings"][0]["code"] == "operation.checkpoint_retention_filename_convention"
    # Both kept (keep_latest=2), so nothing deleted.
    assert data["deleted"] == []
    assert (ckpt_dir / "checkpoint_step_002000.pt").exists()


def test_keep_milestones_protects_trajectory_anchors(tmp_path):
    names = [f"checkpoint_step_{s:06d}_loss1p0.pt" for s in range(1000, 16001, 1000)]
    names += ["best_step_009000_loss0p5.pt"]
    run_dir, ckpt_dir = make_run(tmp_path, names)

    result = run_retention(
        tmp_path, tmp_path, run_dir,
        request_extra={
            "keep_latest": 2,
            "keep_best": 1,
            "keep_milestones": [{"every_steps": 2000, "until_step": 10000}],
        },
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    kept = sorted(p.name for p in ckpt_dir.glob("*.pt"))
    # rolling windows: newest 2 periodic + newest 1 best
    assert "checkpoint_step_016000_loss1p0.pt" in kept
    assert "checkpoint_step_015000_loss1p0.pt" in kept
    assert "best_step_009000_loss0p5.pt" in kept
    # milestone anchors every 2000 up to 10000 survive
    for s in (2000, 4000, 6000, 8000, 10000):
        assert f"checkpoint_step_{s:06d}_loss1p0.pt" in kept
    # non-anchor, non-window checkpoints are gone
    for s in (1000, 3000, 5000, 7000, 9000, 11000, 12000, 13000, 14000):
        assert f"checkpoint_step_{s:06d}_loss1p0.pt" not in kept
    assert sorted(data["kept_milestones"]) == [
        f"checkpoint_step_{s:06d}_loss1p0.pt" for s in (2000, 4000, 6000, 8000, 10000)
    ]
    # milestones never pull from the best family
    assert all(n.startswith("checkpoint_step_") for n in data["kept_milestones"])


def test_keep_milestones_fail_closed_validation(tmp_path):
    # TWO checkpoints, keep_latest=1: the older one is protected by NOTHING, so the
    # survive-assertions below are non-vacuous — a delete-then-validate regression
    # (mutation-tested by review) would remove it and fail this test.
    run_dir, _ = make_run(
        tmp_path,
        ["checkpoint_step_000500_loss2p0.pt", "checkpoint_step_001000_loss1p0.pt"],
    )
    bad_rules = [
        "not-a-list",
        [{"every_steps": 0}],
        [{"every_steps": True}],
        [{"every_steps": 1000, "from_step": -5}],
        [{"every_steps": 1000, "from_step": 200, "until_step": 100}],
        [42],
    ]
    for rules in bad_rules:
        op = tmp_path / "ops" / "retention_bad.json"
        write_retention_operation(
            op, tmp_path, run_dir,
            request_extra={"keep_latest": 1, "keep_best": 1,
                           "keep_milestones": rules},
        )
        dry = run_cli("target", "dry-run", str(op))
        assert dry.returncode == 0, dry.stderr  # receipt only; config loads at exec
        result = run_cli("exec", str(op))
        assert result.returncode != 0, f"rule {rules!r} was accepted"
        assert "checkpoint_retention_config_invalid" in (result.stdout + result.stderr)
    # nothing was deleted by any failed attempt — INCLUDING the checkpoint no
    # rolling window protects
    assert (run_dir / "checkpoints" / "checkpoint_step_001000_loss1p0.pt").exists()
    assert (run_dir / "checkpoints" / "checkpoint_step_000500_loss2p0.pt").exists()
