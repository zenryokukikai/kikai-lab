import os

import pytest

from kikai_lab.operation import OperationError, local_project_payload_files


def test_payload_accepts_in_root_file(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "a.json").write_text("{}", encoding="utf-8")
    files = local_project_payload_files(tmp_path, ["ops/a.json"])
    assert files == [{"path": "ops/a.json", "text": "{}"}]


def test_payload_rejects_dotdot(tmp_path):
    with pytest.raises(OperationError) as exc:
        local_project_payload_files(tmp_path, ["../escape.txt"])
    assert exc.value.code == "operation.remote_project_payload_invalid"


def test_payload_rejects_absolute(tmp_path):
    with pytest.raises(OperationError) as exc:
        local_project_payload_files(tmp_path, ["/etc/passwd"])
    assert exc.value.code == "operation.remote_project_payload_invalid"


def test_payload_rejects_symlink_escaping_root(tmp_path):
    # A secret file living OUTSIDE the project root.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    root = tmp_path / "project"
    root.mkdir()
    # A relative, non-'..' payload entry that is a symlink pointing outside root.
    link = root / "leak.txt"
    os.symlink(secret, link)

    with pytest.raises(OperationError) as exc:
        local_project_payload_files(root, ["leak.txt"])
    assert exc.value.code == "operation.remote_payload_path_escapes_root"


def test_payload_accepts_symlink_within_root(tmp_path):
    root = tmp_path / "project"
    (root / "real").mkdir(parents=True)
    target = root / "real" / "data.json"
    target.write_text("{\"ok\": true}", encoding="utf-8")
    link = root / "link.json"
    os.symlink(target, link)

    files = local_project_payload_files(root, ["link.json"])
    assert files == [{"path": "link.json", "text": "{\"ok\": true}"}]
