"""run_dir_chown adapter: one-shot docker repair for the root-write trap."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from kikai_lab.operation import OperationError, execute_operation


def make_project(tmp_path: Path) -> Path:
    (tmp_path / "containers").mkdir()
    (tmp_path / "containers" / "c1.yaml").write_text(
        "schema_version: 1\nkind: docker_container\ncontainer_id: c1\n"
        "docker:\n  name: c1-ctr\n  image: example-image:latest\n",
        encoding="utf-8",
    )
    return tmp_path


def chown_request(project: Path, run_dir: Path, **over) -> dict:
    request = {
        "adapter": "run_dir_chown",
        "operation": "r1_run_dir_chown",
        "project_root": str(project),
        "container_id": "c1",
        "run_dir": str(run_dir),
        "uid": 1004,
        "gid": 1004,
    }
    request.update(over)
    return {"kind": "kikai_operation", "schema_version": 1, "request": request}


def write_fake_docker(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    argv_log = tmp_path / "docker_argv.jsonl"
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake, argv_log


def test_run_dir_chown_happy_path(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    fake, argv_log = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    result = execute_operation(chown_request(project, run_dir))
    assert result["execution_status"] == "run_dir_chown_completed"
    argv = json.loads(argv_log.read_text().splitlines()[-1])
    assert argv[:2] == ["run", "--rm"]
    assert argv[argv.index("--user") + 1] == "0:0"  # chown must run as root in-container
    assert argv[argv.index("--entrypoint") + 1] == "chown"
    assert f"{run_dir}:/kikai_chown_target" in argv
    assert "example-image:latest" in argv
    assert argv[-3:] == ["-R", "1004:1004", "/kikai_chown_target"]


def test_run_dir_chown_nonzero_exit_is_operation_error(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    fake, _ = write_fake_docker(tmp_path, exit_code=125)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    with pytest.raises(OperationError) as exc:
        execute_operation(chown_request(project, run_dir))
    assert exc.value.code == "operation.run_dir_chown_failed"


def test_run_dir_chown_fail_closed_validation(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    fake, argv_log = write_fake_docker(tmp_path)
    monkeypatch.setenv("KIKAI_DOCKER_BIN", str(fake))

    # the charset/injection fixtures EXIST on disk, so only the regex can reject
    # them — a removed fullmatch clause fails these instead of hiding behind is_dir
    colon_dir = tmp_path / f"{run_dir.name}:ro"
    colon_dir.mkdir()
    space_dir = tmp_path / "a b" / "run"
    space_dir.mkdir(parents=True)
    link_to_root = tmp_path / "looks" / "safe"
    link_to_root.parent.mkdir()
    link_to_root.symlink_to("/")
    cases = [
        chown_request(project, run_dir, uid="1004"),  # stringly uid
        chown_request(project, run_dir, uid=-1),
        chown_request(project, run_dir, uid=True),  # bool sneaks through isinstance int
        chown_request(project, run_dir, container_id="../evil"),
        chown_request(project, tmp_path / "missing"),  # run_dir absent
        chown_request(project, Path("/")),  # whole-host chown
        chown_request(project, Path("/tmp")),  # one level deep = typo, even if it exists
        chown_request(project, Path("relative/dir")),  # docker named volume, not a path
        chown_request(project, colon_dir),  # -v mode injection (real directory)
        chown_request(project, space_dir),  # unsafe charset (real directory)
        chown_request(project, link_to_root),  # symlink at safe depth -> host root
    ]
    for op in cases:
        with pytest.raises(OperationError) as exc:
            execute_operation(op)
        assert exc.value.code in (
            "operation.run_dir_chown_invalid",
            "operation.container_record_missing",
        )
    assert not argv_log.exists()  # docker never ran for any invalid request
