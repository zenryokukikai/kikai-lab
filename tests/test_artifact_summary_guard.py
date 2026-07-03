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


def write_summary(tmp_path, *, optimize="trt", diagnostic_silent=False):
    preview = tmp_path / "preview.mp4"
    diagnostic = tmp_path / "diagnostic.mp4"
    audio = tmp_path / "audio.wav"
    trt_cache = tmp_path / "trt_cache"
    trt_cache.mkdir()
    timing_cache = trt_cache / "renderer_timing_bs8.bin"
    engine_cache = trt_cache / "renderer_engine_bs8"
    preview.write_bytes(b"preview mp4")
    diagnostic.write_bytes(b"diagnostic mp4")
    audio.write_bytes(b"audio wav")
    timing_cache.write_bytes(b"timing")
    engine_cache.mkdir()
    summary = {
        "event": "checkpoint_qc_preview_and_diagnostic_done",
        "optimize": optimize,
        "trt_cache_dir": str(trt_cache),
        "optimize_meta": {
            "renderer_trt_timing_cache": str(timing_cache),
            "renderer_trt_engine_cache_dir": str(engine_cache),
        },
        "preview_contract": "v18_fullframe_lab_feather_facesource",
        "preview": {
            "kind": "normal_preview",
            "path": str(preview),
            "silent": False,
            "expected_frames": 120,
            "written_frame_count": 120,
            "audio": {
                "path": str(audio),
                "duration_sec": 5.0,
                "source_duration_sec": 5.0,
                "start_sec": 0.0,
            },
            "audio_volume": {"max_volume_db": -12.0},
            "ffprobe": {"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]},
        },
        "diagnostic": {
            "kind": "diagnostic_video",
            "path": str(diagnostic),
            "silent": diagnostic_silent,
            "expected_frames": 120,
            "written_frame_count": 120,
            "audio": {
                "path": str(audio),
                "duration_sec": 5.0,
                "source_duration_sec": 5.0,
                "start_sec": 0.0,
            },
            "audio_volume": {"max_volume_db": -12.0},
            "ffprobe": {"streams": [{"codec_type": "video", "width": 1536, "height": 768}]},
        },
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary_path


def write_operation(
    path,
    project_root,
    summary_path,
    *,
    guard_id="summary_guard1",
    path_mappings=None,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "artifact_summary_guard",
                    "project_root": str(project_root),
                    "adapter": "artifact_summary_guard",
                    "guard_id": guard_id,
                    "summary_path": str(summary_path),
                    "require_optimize": "trt",
                    "require_preview_contract": "v18_fullframe_lab_feather_facesource",
                    "path_mappings": path_mappings or [],
                    "require_trt_cache_dir": True,
                    "require_trt_cache_files": True,
                    "artifacts": [
                        {
                            "artifact_id": "example_run_step003000_preview",
                            "summary_key": "preview",
                            "require_audio": True,
                            "min_audio_max_volume_db": -30,
                            "require_full_source_audio": True,
                            "audio_duration_tolerance_sec": 0.05,
                            "require_width": 1920,
                            "require_height": 1080,
                        },
                        {
                            "artifact_id": "example_run_step003000_diagnostic",
                            "summary_key": "diagnostic",
                            "require_audio": True,
                            "min_audio_max_volume_db": -30,
                            "require_width": 1536,
                            "require_height": 768,
                        },
                    ],
                },
            },
            indent=2,
        )
    )


def test_artifact_summary_guard_passes_trt_audio_preview_and_diagnostic(tmp_path):
    project_root = tmp_path / "registry"
    summary_path = write_summary(tmp_path)
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(op, project_root, summary_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(op))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "artifact_summary_guard_passed"
    record = json.loads((project_root / "guard_records" / "summary_guard1.json").read_text())
    assert record["status"] == "passed"
    assert record["summary_path"] == str(summary_path)
    assert record["optimize"] == "trt"
    assert record["preview_contract"] == "v18_fullframe_lab_feather_facesource"
    assert record["artifacts"][0]["video"] == {"width": 1920, "height": 1080}
    assert [artifact["artifact_id"] for artifact in record["artifacts"]] == [
        "example_run_step003000_preview",
        "example_run_step003000_diagnostic",
    ]


def test_artifact_summary_guard_fails_closed_on_silent_diagnostic(tmp_path):
    project_root = tmp_path / "registry"
    summary_path = write_summary(tmp_path, diagnostic_silent=True)
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(op, project_root, summary_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.artifact_summary_audio_required"
    assert not (project_root / "guard_records" / "summary_guard1.json").exists()


def test_artifact_summary_guard_fails_closed_on_short_source_audio(tmp_path):
    project_root = tmp_path / "registry"
    summary_path = write_summary(tmp_path)
    summary = json.loads(summary_path.read_text())
    summary["preview"]["audio"]["duration_sec"] = 5.12
    summary["preview"]["audio"]["source_duration_sec"] = 5.713958
    summary_path.write_text(json.dumps(summary, indent=2))
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(op, project_root, summary_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.artifact_summary_audio_source_incomplete"
    assert not (project_root / "guard_records" / "summary_guard1.json").exists()


def test_artifact_summary_guard_fails_closed_without_trt_optimize(tmp_path):
    project_root = tmp_path / "registry"
    summary_path = write_summary(tmp_path, optimize="none")
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(op, project_root, summary_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.artifact_summary_optimize_mismatch"
    assert not (project_root / "guard_records" / "summary_guard1.json").exists()


def test_artifact_summary_guard_fails_closed_on_lower_tile_preview_contract(tmp_path):
    project_root = tmp_path / "registry"
    summary_path = write_summary(tmp_path)
    summary = json.loads(summary_path.read_text())
    summary["preview_contract"] = "lower_tile_static_cache_qc"
    summary_path.write_text(json.dumps(summary, indent=2))
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(op, project_root, summary_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.artifact_summary_preview_contract_mismatch"
    assert not (project_root / "guard_records" / "summary_guard1.json").exists()


def test_artifact_summary_guard_fails_closed_on_non_1080p_preview(tmp_path):
    project_root = tmp_path / "registry"
    summary_path = write_summary(tmp_path)
    summary = json.loads(summary_path.read_text())
    summary["preview"]["ffprobe"]["streams"][0]["width"] = 512
    summary["preview"]["ffprobe"]["streams"][0]["height"] = 256
    summary_path.write_text(json.dumps(summary, indent=2))
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(op, project_root, summary_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "operation.artifact_summary_video_resolution_mismatch"
    assert not (project_root / "guard_records" / "summary_guard1.json").exists()


def test_artifact_summary_guard_maps_container_paths_to_host_paths(tmp_path):
    project_root = tmp_path / "registry"
    host_root = tmp_path / "host_training_runs"
    container_root = "/workspace/training_runs"
    run_dir = host_root / "example_run" / "qc"
    run_dir.mkdir(parents=True)
    preview = run_dir / "preview.mp4"
    diagnostic = run_dir / "diagnostic.mp4"
    audio = host_root / "example_source" / "input" / "source_audio_16k.wav"
    trt_cache = host_root / "example_run" / "qc" / "trt_cache"
    timing_cache = trt_cache / "example_arch_v1_bs1_timing.bin"
    engine_cache = trt_cache / "example_arch_v1_bs1_engine"
    preview.write_bytes(b"preview")
    diagnostic.write_bytes(b"diagnostic")
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    trt_cache.mkdir(parents=True)
    timing_cache.write_bytes(b"timing")
    engine_cache.mkdir()

    container_trt_cache = f"{container_root}/example_run/qc/trt_cache"
    summary = {
        "optimize": "trt",
        "trt_cache_dir": container_trt_cache,
        "optimize_meta": {
            "renderer_trt_timing_cache": f"{container_trt_cache}/{timing_cache.name}",
            "renderer_trt_engine_cache_dir": f"{container_trt_cache}/{engine_cache.name}",
        },
        "preview_contract": "v18_fullframe_lab_feather_facesource",
        "preview": {
            "path": f"{container_root}/example_run/qc/preview.mp4",
            "silent": False,
            "expected_frames": 16,
            "written_frame_count": 16,
            "audio": {
                "path": f"{container_root}/example_source/input/source_audio_16k.wav",
                "duration_sec": 0.66,
                "source_duration_sec": 0.66,
                "start_sec": 0.0,
                "mux_gain_db": 12.0,
            },
            "audio_volume": {"max_volume_db": -22.6},
            "ffprobe": {"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]},
        },
        "diagnostic": {
            "path": f"{container_root}/example_run/qc/diagnostic.mp4",
            "silent": False,
            "expected_frames": 16,
            "written_frame_count": 16,
            "audio": {
                "path": f"{container_root}/example_source/input/source_audio_16k.wav",
                "duration_sec": 0.66,
                "source_duration_sec": 0.66,
                "start_sec": 0.0,
                "mux_gain_db": 12.0,
            },
            "audio_volume": {"max_volume_db": -22.6},
            "ffprobe": {"streams": [{"codec_type": "video", "width": 1536, "height": 768}]},
        },
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    op = tmp_path / "ops" / "summary_guard.json"
    write_operation(
        op,
        project_root,
        "env:EXAMPLE_RUN_REAL_SUMMARY_PATH",
        path_mappings=[{"from": container_root, "to": "env:HOST_TRAINING_RUNS_ROOT"}],
    )
    dry_run = run_cli(
        "target",
        "dry-run",
        str(op),
        env={
            "EXAMPLE_RUN_REAL_SUMMARY_PATH": str(summary_path),
            "HOST_TRAINING_RUNS_ROOT": str(host_root),
        },
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr

    result = run_cli(
        "exec",
        str(op),
        env={
            "EXAMPLE_RUN_REAL_SUMMARY_PATH": str(summary_path),
            "HOST_TRAINING_RUNS_ROOT": str(host_root),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((project_root / "guard_records" / "summary_guard1.json").read_text())
    assert record["status"] == "passed"
    assert record["path_mappings"] == [{"from": container_root, "to": str(host_root)}]
    assert record["trt_cache"]["trt_cache_paths"]["renderer_trt_timing_cache"] == str(timing_cache)
    assert record["artifacts"][0]["path"] == str(preview)
    assert record["artifacts"][0]["audio"]["path"] == str(audio)
