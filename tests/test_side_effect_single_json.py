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


def write_operation(path, *, operation="noop", argv=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": operation,
                    "project_root": "${PROJECT_ROOT}",
                    "target_id": "example_run_qc",
                    "adapter": "noop",
                    "argv": ["true"] if argv is None else argv,
                },
            },
            indent=2,
        )
    )


def load_json(path):
    return json.loads(path.read_text())


def write_container(
    root,
    container_id="run1_training",
    docker_name="example-run1-training",
    *,
    extra_yaml="",
):
    containers = root / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    (containers / f"{container_id}.yaml").write_text(
        f"""schema_version: 1
kind: docker_container
container_id: {container_id}
docker:
  name: {docker_name}
  image: env:EXAMPLE_TRAINING_IMAGE
{extra_yaml}"""
    )


def write_docker_operation(path, project_root, *, request_extra=None):
    request = {
        "operation": "render_qc",
        "project_root": str(project_root),
        "target_id": "example_run_qc",
        "adapter": "docker_exec",
        "container_id": "run1_training",
        "argv": ["python", "-c", "print('ok')"],
    }
    if request_extra:
        request.update(request_extra)
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


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_script_bundle(
    project_root, bundle_id="example_run_train", *, launcher_text=None, bundle_extra=None
):
    launcher_text = "print('bundle launcher')\n" if launcher_text is None else launcher_text
    bundle_root = project_root / "script_bundles" / bundle_id
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
                "sha256": sha256_text(launcher_text),
            }
        ],
    }
    if bundle_extra:
        bundle.update(bundle_extra)
    (bundle_root / "bundle.json").write_text(json.dumps(bundle, indent=2))
    return bundle_root


def write_script_bundle_operation(path, project_root, *, request_extra=None):
    request = {
        "operation": "example_run_train",
        "project_root": str(project_root),
        "target_id": "example_run_train",
        "adapter": "script_bundle_exec",
        "bundle_id": "example_run_train",
        "entrypoint": "train",
        "container_id": "run1_training",
        "args": ["--dry-run"],
    }
    if request_extra:
        request.update(request_extra)
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


def write_fake_docker(tmp_path, *, returncode=0):
    output_path = tmp_path / "docker_argv.json"
    fake = tmp_path / "fake_docker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(output_path)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('fake docker stdout')\n"
        "print('fake docker stderr', file=sys.stderr)\n"
        f"raise SystemExit({returncode})\n"
    )
    fake.chmod(0o755)
    return fake, output_path


def write_fake_docker_recording_env(tmp_path, env_name):
    output_path = tmp_path / "docker_env_argv.json"
    fake = tmp_path / "fake_docker_env.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"payload = {{'argv': sys.argv[1:], 'env': os.environ.get({env_name!r})}}\n"
        f"pathlib.Path({str(output_path)!r}).write_text(json.dumps(payload))\n"
        "raise SystemExit(0)\n"
    )
    fake.chmod(0o755)
    return fake, output_path


def test_target_dry_run_accepts_exactly_one_positional_operation_json_and_writes_receipt(tmp_path):
    op = tmp_path / "ops" / "example_run_qc.json"
    write_operation(op)

    result = run_cli("target", "dry-run", str(op))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["operation_file"] == str(op)
    saved = load_json(op)
    assert saved["guard_receipt"]["status"] == "passed"
    assert saved["guard_receipt"]["request_sha256"]


def test_exec_accepts_operation_json_after_dry_run_receipt(tmp_path):
    op = tmp_path / "ops" / "example_run_qc.json"
    write_operation(op)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "validated_noop"


def test_docker_exec_adapter_invokes_docker_with_structured_argv(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    op = tmp_path / "ops" / "docker_exec.json"
    write_docker_operation(op, project_root)
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op), env={"KIKAI_DOCKER_BIN": str(fake_docker)})

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "docker_exec_completed"
    assert payload["data"]["container_id"] == "run1_training"
    assert payload["data"]["container_name"] == "example-run1-training"
    assert payload["data"]["stdout"] == "fake docker stdout\n"
    assert payload["data"]["stderr"] == "fake docker stderr\n"
    assert json.loads(docker_argv_path.read_text()) == [
        "exec",
        "example-run1-training",
        "python",
        "-c",
        "print('ok')",
    ]


def test_docker_exec_adapter_rejects_command_strings_and_shell_wrappers(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    cases = [
        ({"command_string": "python render.py --many args"}, "operation.command_shape_forbidden"),
        ({"heredoc": "EOF\npython render.py\nEOF"}, "operation.command_shape_forbidden"),
        ({"argv": ["bash", "-lc", "python render.py"]}, "operation.shell_wrapper_forbidden"),
    ]
    for request_extra, expected_code in cases:
        op = tmp_path / "ops" / f"docker_exec_{expected_code}_{len(request_extra)}.json"
        write_docker_operation(op, project_root, request_extra=request_extra)
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli("exec", str(op))

        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["errors"][0]["code"] == expected_code


def test_docker_exec_adapter_passes_workdir_and_env_as_structured_args(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    op = tmp_path / "ops" / "docker_exec_env.json"
    write_docker_operation(
        op,
        project_root,
        request_extra={
            "workdir": "/workspace/example_engine",
            "env": {"PYTHONUNBUFFERED": "1", "RUN_NAME": "example_run"},
        },
    )
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op), env={"KIKAI_DOCKER_BIN": str(fake_docker)})

    assert result.returncode == 0
    assert json.loads(docker_argv_path.read_text()) == [
        "exec",
        "--workdir",
        "/workspace/example_engine",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "RUN_NAME=example_run",
        "example-run1-training",
        "python",
        "-c",
        "print('ok')",
    ]


def test_docker_exec_adapter_resolves_env_refs_in_workdir_and_env(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    op = tmp_path / "ops" / "docker_exec_env_refs.json"
    write_docker_operation(
        op,
        project_root,
        request_extra={
            "workdir": "env:KIKAI_TEST_WORKDIR",
            "env": {"RUN_NAME": "env:KIKAI_TEST_RUN_NAME"},
        },
    )
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "KIKAI_TEST_WORKDIR": "/workspace/example_engine",
            "KIKAI_TEST_RUN_NAME": "example_run",
        },
    )

    assert result.returncode == 0
    assert json.loads(docker_argv_path.read_text()) == [
        "exec",
        "--workdir",
        "/workspace/example_engine",
        "-e",
        "RUN_NAME=example_run",
        "example-run1-training",
        "python",
        "-c",
        "print('ok')",
    ]


def test_docker_exec_adapter_passes_docker_host_to_docker_cli_environment(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    op = tmp_path / "ops" / "docker_exec_docker_host.json"
    write_docker_operation(
        op,
        project_root,
        request_extra={"docker_host": "env:KIKAI_TEST_DOCKER_HOST"},
    )
    fake_docker, docker_payload_path = write_fake_docker_recording_env(tmp_path, "DOCKER_HOST")
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "KIKAI_TEST_DOCKER_HOST": "ssh://training-host.example",
        },
    )

    assert result.returncode == 0
    payload = json.loads(docker_payload_path.read_text())
    assert payload["env"] == "ssh://training-host.example"
    assert payload["argv"] == [
        "exec",
        "example-run1-training",
        "python",
        "-c",
        "print('ok')",
    ]


def test_script_bundle_exec_expands_immutable_bundle_entrypoint_to_fake_docker(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_exec.json"
    write_script_bundle_operation(op, project_root)
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op), env={"KIKAI_DOCKER_BIN": str(fake_docker)})

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "script_bundle_exec_completed"
    assert payload["data"]["bundle_id"] == "example_run_train"
    assert payload["data"]["entrypoint"] == "train"
    assert payload["data"]["expanded_argv"] == [
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--dry-run",
    ]
    assert json.loads(docker_argv_path.read_text()) == [
        "exec",
        "example-run1-training",
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--dry-run",
    ]


def test_script_bundle_run_uses_container_definition_mounts_and_fresh_docker_run(tmp_path):
    project_root = tmp_path / "registry"
    host_training_runs = tmp_path / "training_runs"
    host_example_project = tmp_path / "example-project"
    write_container(
        project_root,
        extra_yaml="""network_mode: host
gpus: all
shm_size: 8g
workdir: /workspace/kikai_project
mounts:
  - source: env:HOST_TRAINING_RUNS
    target: /workspace/training_runs
    mode: rw
  - source: env:HOST_EXAMPLE_PROJECT
    target: /workspace/example_project
    mode: ro
""",
    )
    write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_run.json"
    write_script_bundle_operation(
        op,
        project_root,
        request_extra={
            "adapter": "script_bundle_run",
            "env": {"PYTHONUNBUFFERED": "${EXAMPLE_RUN_TEST_PYTHONUNBUFFERED}"},
            "args": ["--real-qc", "${EXAMPLE_RUN_TEST_SUFFIX}"],
        },
    )
    fake_docker, docker_argv_path = write_fake_docker(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
            "HOST_TRAINING_RUNS": str(host_training_runs),
            "HOST_EXAMPLE_PROJECT": str(host_example_project),
            "EXAMPLE_RUN_TEST_PYTHONUNBUFFERED": "1",
            "EXAMPLE_RUN_TEST_SUFFIX": "suffix1",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["execution_status"] == "script_bundle_run_completed"
    assert payload["data"]["expanded_argv"] == [
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--real-qc",
        "suffix1",
    ]
    assert json.loads(docker_argv_path.read_text()) == [
        "run",
        "--rm",
        "--name",
        "example-run1-training",
        "--gpus",
        "all",
        "--network",
        "host",
        "--shm-size",
        "8g",
        "--workdir",
        "/workspace/kikai_project",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "KIKAI_RUN_ID=run1_training",
        "-e",
        "KIKAI_CONTAINER_ID=run1_training",
        "-e",
        "KIKAI_OPERATION=example_run_train",
        "-v",
        f"{project_root.resolve()}:/workspace/kikai_project:ro",
        "-v",
        f"{host_training_runs}:/workspace/training_runs:rw",
        "-v",
        f"{host_example_project}:/workspace/example_project:ro",
        "example-engine:dev",
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--real-qc",
        "suffix1",
    ]


def write_fake_docker_detached(tmp_path, *, inspect_found=False):
    """Fake docker that records the `run` argv and answers `inspect` according to
    inspect_found. `inspect <name>` exits non-zero (no such container) when
    inspect_found is False, mirroring a free container name; `run -d` prints a fake
    container id and exits 0."""
    run_argv_path = tmp_path / "docker_run_argv.json"
    fake = tmp_path / "fake_docker_detached.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == 'inspect':\n"
        f"    found = {inspect_found!r}\n"
        "    if found:\n"
        "        print(json.dumps([{'Name': '/' + argv[-1]}]))\n"
        "        raise SystemExit(0)\n"
        "    print('Error: No such object', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        f"pathlib.Path({str(run_argv_path)!r}).write_text(json.dumps(argv))\n"
        "print('deadbeefcafe0001')\n"
        "raise SystemExit(0)\n"
    )
    fake.chmod(0o755)
    return fake, run_argv_path


def test_script_bundle_run_detach_starts_detached_named_container(tmp_path):
    project_root = tmp_path / "registry"
    host_training_runs = tmp_path / "training_runs"
    write_container(
        project_root,
        extra_yaml="""network_mode: host
gpus: all
shm_size: 8g
workdir: /workspace/kikai_project
mounts:
  - source: env:HOST_TRAINING_RUNS
    target: /workspace/training_runs
    mode: rw
""",
    )
    write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_run_detached.json"
    write_script_bundle_operation(
        op,
        project_root,
        request_extra={
            "adapter": "script_bundle_run",
            "detach": True,
            "env": {"PYTHONUNBUFFERED": "1"},
            "args": ["--real-qc"],
        },
    )
    fake_docker, docker_run_argv_path = write_fake_docker_detached(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
            "HOST_TRAINING_RUNS": str(host_training_runs),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["execution_status"] == "script_bundle_run_detached_started"
    assert payload["data"]["container_name"] == "example-run1-training"
    assert payload["data"]["started_container_id"] == "deadbeefcafe0001"
    assert payload["data"]["image"] == "example-engine:dev"
    run_argv = json.loads(docker_run_argv_path.read_text())
    assert run_argv == [
        "run",
        "-d",
        "--name",
        "example-run1-training",
        "--gpus",
        "all",
        "--network",
        "host",
        "--shm-size",
        "8g",
        "--workdir",
        "/workspace/kikai_project",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "KIKAI_RUN_ID=run1_training",
        "-e",
        "KIKAI_CONTAINER_ID=run1_training",
        "-e",
        "KIKAI_OPERATION=example_run_train",
        "-v",
        f"{project_root.resolve()}:/workspace/kikai_project:ro",
        "-v",
        f"{host_training_runs}:/workspace/training_runs:rw",
        "example-engine:dev",
        "python",
        "script_bundles/example_run_train/root/scripts/training/launch.py",
        "--real-qc",
    ]


def test_script_bundle_run_detach_fails_when_name_in_use(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_run_detached_inuse.json"
    write_script_bundle_operation(
        op,
        project_root,
        request_extra={"adapter": "script_bundle_run", "detach": True},
    )
    fake_docker, _ = write_fake_docker_detached(tmp_path, inspect_found=True)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
        },
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "operation.script_bundle_run_name_in_use"


def test_script_bundle_run_detach_requires_docker_name(tmp_path):
    project_root = tmp_path / "registry"
    # Container with NO docker.name -> detach must fail with a clear error.
    containers = project_root / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    (containers / "run1_training.yaml").write_text(
        """schema_version: 1
kind: docker_container
container_id: run1_training
docker:
  image: env:EXAMPLE_TRAINING_IMAGE
"""
    )
    write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_run_detached_noname.json"
    write_script_bundle_operation(
        op,
        project_root,
        request_extra={"adapter": "script_bundle_run", "detach": True},
    )
    fake_docker, _ = write_fake_docker_detached(tmp_path)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
        },
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "operation.script_bundle_run_detach_requires_name"


def test_script_bundle_exec_rejects_missing_and_modified_bundle_files(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    bundle_root = write_script_bundle(project_root)
    op = tmp_path / "ops" / "script_bundle_exec.json"
    write_script_bundle_operation(op, project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    launcher = bundle_root / "root" / "scripts" / "training" / "launch.py"
    launcher.unlink()
    missing = run_cli("exec", str(op))
    assert missing.returncode != 0
    missing_payload = json.loads(missing.stdout)
    assert missing_payload["errors"][0]["code"] == "operation.script_bundle_file_missing"

    write_script_bundle(project_root)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0
    launcher.write_text("print('mutated')\n")
    modified = run_cli("exec", str(op))
    assert modified.returncode != 0
    modified_payload = json.loads(modified.stdout)
    assert modified_payload["errors"][0]["code"] == "operation.script_bundle_hash_mismatch"


def test_script_bundle_exec_rejects_unsafe_or_mutable_shapes(tmp_path):
    project_root = tmp_path / "registry"
    write_container(project_root)
    cases = [
        ({"immutable": False}, {}, "operation.script_bundle_not_immutable"),
        ({}, {"argv": ["python", "live_script.py"]}, "operation.script_bundle_raw_argv_forbidden"),
        ({}, {"args": "--dry-run"}, "operation.script_bundle_args_invalid"),
        (
            {"entrypoints": {"train": {"argv": ["bash", "-lc", "python live_script.py"]}}},
            {},
            "operation.shell_wrapper_forbidden",
        ),
    ]
    for index, (bundle_extra, request_extra, expected_code) in enumerate(cases):
        bundle_id = f"example_run_train_{index}"
        write_script_bundle(project_root, bundle_id=bundle_id, bundle_extra=bundle_extra)
        op = tmp_path / "ops" / f"script_bundle_exec_{index}.json"
        write_script_bundle_operation(
            op,
            project_root,
            request_extra={"bundle_id": bundle_id, **request_extra},
        )
        dry_run = run_cli("target", "dry-run", str(op))
        assert dry_run.returncode == 0

        result = run_cli("exec", str(op))

        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["errors"][0]["code"] == expected_code


def test_exec_rejects_operation_json_without_guard_receipt(tmp_path):
    op = tmp_path / "ops" / "example_run_qc.json"
    write_operation(op)

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "operation.guard_receipt_missing"


def test_exec_rejects_if_request_changes_after_dry_run(tmp_path):
    op = tmp_path / "ops" / "example_run_qc.json"
    write_operation(op)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0
    doc = load_json(op)
    doc["request"]["argv"] = ["false"]
    op.write_text(json.dumps(doc, indent=2))

    result = run_cli("exec", str(op))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "operation.guard_receipt_mismatch"


def test_side_effect_commands_reject_multi_file_or_flag_shapes(tmp_path):
    op = tmp_path / "ops" / "example_run_qc.json"
    token = tmp_path / "ops" / "token.json"
    write_operation(op)
    token.write_text("{}")

    cases = [
        ("exec", "--file", str(op)),
        ("exec", "--args-file", str(op)),
        ("exec", str(op), "--approval-token", str(token)),
        ("exec", str(op), "--json"),
        ("exec", str(op), "--checkpoint", "checkpoint.pt"),
        ("target", "run", "--file", str(op)),
        ("target", "run", str(op), "extra"),
    ]
    for args in cases:
        result = run_cli(*args)
        assert result.returncode != 0, args
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["errors"][0]["code"] == "operation.single_json_argument_required"


def test_example_noop_operation_fixture_round_trips_after_copy(tmp_path):
    fixture = "examples/ops/noop_render_qc.json"
    source = tmp_path.cwd() / fixture
    op = tmp_path / "noop_render_qc.json"
    op.write_text(source.read_text())

    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli("exec", str(op))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["execution_status"] == "validated_noop"
    assert payload["data"]["operation"] == "render_qc"
    assert payload["data"]["target_id"] == "example_run_qc"


def test_tensorboard_service_ensure_running_starts_named_container_when_absent(tmp_path):
    project_root = tmp_path / "registry"
    write_container(
        project_root,
        container_id="example_run_tensorboard",
        docker_name="example-example_run-tensorboard",
        extra_yaml="""gpus: all
network_mode: host
workdir: /workspace
mounts:
  - source: env:HOST_TRAINING_RUNS_ROOT
    target: /workspace/training_runs
    mode: ro
""",
    )
    op = tmp_path / "ops" / "tensorboard_service.json"
    write_docker_operation(
        op,
        project_root,
        request_extra={
            "operation": "tensorboard_service",
            "target_id": "example_run_tensorboard",
            "adapter": "tensorboard_service",
            "container_id": "example_run_tensorboard",
            "action": "ensure-running",
            "logdir": "${CONTAINER_TRAINING_RUNS_ROOT}/example_run/tensorboard",
            "port": 6018,
        },
    )
    fake_docker = tmp_path / "fake_docker.py"
    docker_log = tmp_path / "fake_docker_argv.jsonl"
    fake_docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(docker_log)!r})\n"
        "argv = sys.argv[1:]\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log.open('a') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
        "if argv[:2] == ['inspect', 'example-example_run-tensorboard']:\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(0)\n"
    )
    fake_docker.chmod(0o755)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "EXAMPLE_TRAINING_IMAGE": "example-engine:dev",
            "HOST_TRAINING_RUNS_ROOT": str(tmp_path / "training_runs"),
            "CONTAINER_TRAINING_RUNS_ROOT": "/workspace/training_runs",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["execution_status"] == "tensorboard_service_running"
    calls = [json.loads(line) for line in docker_log.read_text().splitlines()]
    assert calls[0] == ["inspect", "example-example_run-tensorboard"]
    assert calls[1] == ["rm", "-f", "example-example_run-tensorboard"]
    assert calls[2] == [
        "run",
        "-d",
        "--name",
        "example-example_run-tensorboard",
        "--gpus",
        "all",
        "--network",
        "host",
        "--workdir",
        "/workspace",
        "-e",
        "KIKAI_RUN_ID=example_run_tensorboard",
        "-e",
        "KIKAI_CONTAINER_ID=example_run_tensorboard",
        "-e",
        "KIKAI_OPERATION=tensorboard_service",
        "-v",
        f"{project_root.resolve()}:/workspace/kikai_project:ro",
        "-v",
        f"{tmp_path / 'training_runs'}:/workspace/training_runs:ro",
        "example-engine:dev",
        "sh",
        "-lc",
        "python -m pip show tb-nightly >/dev/null 2>&1 && "
        "python -m pip uninstall -y tensorboard "
        ">/tmp/kikai_tensorboard_pip_uninstall.log 2>&1 || true; "
        "exec python -m tensorboard.main \"$@\"",
        "kikai-tensorboard",
        "--host",
        "0.0.0.0",
        "--port",
        "6018",
        "--logdir",
        "/workspace/training_runs/example_run/tensorboard",
    ]


def test_tensorboard_service_status_reports_existing_matching_container(tmp_path):
    project_root = tmp_path / "registry"
    write_container(
        project_root,
        container_id="example_run_tensorboard",
        docker_name="example-example_run-tensorboard",
    )
    op = tmp_path / "ops" / "tensorboard_service_status.json"
    write_docker_operation(
        op,
        project_root,
        request_extra={
            "operation": "tensorboard_service",
            "target_id": "example_run_tensorboard",
            "adapter": "tensorboard_service",
            "container_id": "example_run_tensorboard",
            "action": "status",
            "logdir": "${CONTAINER_TRAINING_RUNS_ROOT}/example_run/tensorboard",
            "port": 6018,
        },
    )
    fake_docker = tmp_path / "fake_docker.py"
    docker_log = tmp_path / "fake_docker_argv.jsonl"
    inspect_json = [
        {
            "State": {"Running": True},
            "Args": [
                "-m",
                "tensorboard.main",
                "--host",
                "0.0.0.0",
                "--port",
                "6018",
                "--logdir",
                "/workspace/training_runs/example_run/tensorboard",
            ],
        }
    ]
    fake_docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(docker_log)!r})\n"
        "argv = sys.argv[1:]\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log.open('a') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
        f"print({json.dumps(json.dumps(inspect_json))})\n"
    )
    fake_docker.chmod(0o755)
    dry_run = run_cli("target", "dry-run", str(op))
    assert dry_run.returncode == 0

    result = run_cli(
        "exec",
        str(op),
        env={
            "KIKAI_DOCKER_BIN": str(fake_docker),
            "CONTAINER_TRAINING_RUNS_ROOT": "/workspace/training_runs",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["execution_status"] == "tensorboard_service_status"
    assert payload["data"]["running"] is True
    assert payload["data"]["port_matches"] is True
    assert payload["data"]["logdir_matches"] is True
    assert [json.loads(line) for line in docker_log.read_text().splitlines()] == [
        ["inspect", "example-example_run-tensorboard"]
    ]
