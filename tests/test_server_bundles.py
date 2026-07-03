from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from tests.test_server_projects import make_client
from tests.test_server_resources import put_project


def make_tar(files: dict[str, bytes], *, gzip: bool = False) -> bytes:
    buffer = io.BytesIO()
    mode = "w:gz" if gzip else "w"
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def bundle_files(argv=("python", "scripts/train.py")) -> dict[str, bytes]:
    manifest = {"entrypoints": {"train": {"argv": list(argv)}}}
    return {
        "kikai_bundle.json": json.dumps(manifest).encode(),
        "scripts/train.py": b"print('train')\n",
        "scripts/util.py": b"X = 1\n",
    }


def upload(client, bundle_id: str, tar_bytes: bytes):
    return client.put(
        f"/v1/projects/example_new/bundles/{bundle_id}",
        content=tar_bytes,
        headers={"content-type": "application/x-tar"},
    )


def test_bundle_upload_creates_immutable_bundle(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")

    response = upload(client, "example_trainer_v1", make_tar(bundle_files()))
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["created"] is True
    assert data["entrypoints"] == ["train"]
    assert data["file_count"] == 2  # manifest itself is not part of the bundle

    manifest = json.loads(
        (
            tmp_path
            / "example_new"
            / "script_bundles"
            / "example_trainer_v1"
            / "bundle.json"
        ).read_text()
    )
    assert manifest["entrypoints"]["train"]["argv"] == [
        "python",
        "script_bundles/example_trainer_v1/root/scripts/train.py",
    ]
    assert manifest["immutable"] is True


def test_bundle_upload_idempotent_then_409(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    upload(client, "example_trainer_v1", make_tar(bundle_files()))

    again = upload(client, "example_trainer_v1", make_tar(bundle_files()))
    assert again.status_code == 200
    assert again.json()["data"]["already_exists"] is True

    changed_files = bundle_files()
    changed_files["scripts/util.py"] = b"X = 2\n"
    conflict = upload(client, "example_trainer_v1", make_tar(changed_files))
    assert conflict.status_code == 409
    assert conflict.json()["errors"][0]["code"] == "script_bundle.create_bundle_exists"


def test_bundle_upload_gzip_supported(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = upload(client, "example_trainer_v1", make_tar(bundle_files(), gzip=True))
    assert response.status_code == 201


def test_bundle_upload_rejects_traversal_members(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    evil = make_tar({**bundle_files(), "../escape.py": b"pwn\n"})
    response = upload(client, "example_trainer_v1", evil)
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "script_bundle.upload_member_invalid"
    assert not (tmp_path / "escape.py").exists()
    assert not (tmp_path / "example_new" / "script_bundles" / "example_trainer_v1").exists()


def test_bundle_upload_rejects_symlink_members(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in bundle_files().items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("scripts/evil_link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    response = upload(client, "example_trainer_v1", buffer.getvalue())
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "script_bundle.upload_member_invalid"


def test_bundle_upload_requires_manifest(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    files = bundle_files()
    files.pop("kikai_bundle.json")
    response = upload(client, "example_trainer_v1", make_tar(files))
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "script_bundle.upload_manifest_invalid"


def test_bundle_upload_rejects_non_tar_and_empty(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    garbage = upload(client, "example_trainer_v1", b"this is not a tar")
    assert garbage.status_code == 422
    assert garbage.json()["errors"][0]["code"] == "script_bundle.upload_invalid"

    empty = upload(client, "example_trainer_v1", b"")
    assert empty.status_code == 422


def test_bundle_list_and_detail(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    upload(client, "example_trainer_v1", make_tar(bundle_files()))

    listing = client.get("/v1/projects/example_new/bundles").json()["data"]
    assert listing["total"] == 1
    assert listing["bundles"][0] == {
        "bundle_id": "example_trainer_v1",
        "entrypoints": ["train"],
        "file_count": 2,
    }

    detail = client.get("/v1/projects/example_new/bundles/example_trainer_v1").json()
    assert detail["data"]["bundle"]["kind"] == "kikai_script_bundle"

    missing = client.get("/v1/projects/example_new/bundles/example_absent")
    assert missing.status_code == 404


def test_bundle_upload_blocked_on_archived_project(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    client.post("/v1/projects/example_new/archive")
    response = upload(client, "example_trainer_v1", make_tar(bundle_files()))
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "project.archived"


# ------------------------------------------------------------------- data sources
def test_data_source_put_computes_integrity_and_verifies(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    payload = tmp_path / "inputs" / "manifest.jsonl"
    payload.parent.mkdir(parents=True)
    payload.write_text('{"frame": 1}\n', encoding="utf-8")

    created = client.put(
        "/v1/projects/example_new/data-sources/example_manifest",
        json={
            "kind": "file",
            "source_type": "dataset_manifest",
            "path": str(payload),
            "host_ref": "local",
            "roles": ["train_manifest"],
            "summary": "example manifest",
        },
    )
    assert created.status_code == 201, created.text
    record = client.get(
        "/v1/projects/example_new/data-sources/example_manifest"
    ).json()["data"]["data_source"]
    assert record["integrity"]["strategy"] == "file_sha256"
    assert len(record["integrity"]["sha256"]) == 64

    verify = client.post("/v1/projects/example_new/data-sources/example_manifest/verify")
    assert verify.status_code == 200
    assert verify.json()["data"]["verified"] is True

    payload.write_text('{"frame": 2}\n', encoding="utf-8")  # tamper
    tampered = client.post(
        "/v1/projects/example_new/data-sources/example_manifest/verify"
    )
    assert tampered.status_code == 422
    assert tampered.json()["data"] == {
        "data_source_id": "example_manifest",
        "verified": False,
    }


def test_data_source_put_idempotent_and_immutable(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    payload = tmp_path / "inputs" / "manifest.jsonl"
    payload.parent.mkdir(parents=True)
    payload.write_text("{}\n", encoding="utf-8")
    body = {
        "kind": "file",
        "source_type": "dataset_manifest",
        "path": str(payload),
        "host_ref": "local",
        "roles": ["train_manifest"],
        "summary": "example manifest",
    }
    client.put("/v1/projects/example_new/data-sources/example_manifest", json=body)
    again = client.put(
        "/v1/projects/example_new/data-sources/example_manifest", json=body
    )
    assert again.status_code == 200
    assert again.json()["data"]["already_exists"] is True

    other = {**body, "path": str(payload.parent / "other.jsonl")}
    conflict = client.put(
        "/v1/projects/example_new/data-sources/example_manifest", json=other
    )
    assert conflict.status_code == 409
    assert conflict.json()["errors"][0]["code"] == "data_source.exists"
    assert "path" in conflict.json()["errors"][0]["details"]["diff_keys"]


def test_data_source_put_requires_fields(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = client.put(
        "/v1/projects/example_new/data-sources/example_manifest",
        json={"kind": "file", "path": "/tmp/x"},
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "data_source.record_invalid"


def test_bundle_reupload_with_pyc_stays_idempotent(tmp_path: Path) -> None:
    """create_script_bundle silently drops .pyc/__pycache__; the idempotency signature
    must agree, so a byte-identical re-upload is already_exists, never 409 (H3)."""
    client = make_client(tmp_path)
    put_project(client, "example_new")
    files = bundle_files()
    files["scripts/util.pyc"] = b"\x00compiled"
    files["scripts/__pycache__/cached.cpython-312.pyc"] = b"\x00cache"
    first = upload(client, "example_trainer_v1", make_tar(files))
    assert first.status_code == 201
    assert first.json()["data"]["file_count"] == 2  # excluded files never land

    again = upload(client, "example_trainer_v1", make_tar(files))
    assert again.status_code == 200
    assert again.json()["data"]["already_exists"] is True


def test_bundle_reupload_with_changed_entrypoints_is_409(tmp_path: Path) -> None:
    """Identical files but different argv must NOT silently keep the old entrypoints
    (H4) — the caller would believe their manifest registered."""
    client = make_client(tmp_path)
    put_project(client, "example_new")
    upload(client, "example_trainer_v1", make_tar(bundle_files()))

    changed = upload(
        client,
        "example_trainer_v1",
        make_tar(bundle_files(argv=("python", "scripts/train.py", "--fast"))),
    )
    assert changed.status_code == 409
    details = changed.json()["errors"][0]["details"]
    assert details["diff"] == ["entrypoints"]

    stored = client.get("/v1/projects/example_new/bundles/example_trainer_v1").json()
    argv = stored["data"]["bundle"]["entrypoints"]["train"]["argv"]
    assert "--fast" not in argv  # old bundle untouched


def test_bundle_upload_rejects_traversal_directory_member(tmp_path: Path) -> None:
    import io as _io
    import tarfile as _tarfile

    client = make_client(tmp_path)
    put_project(client, "example_new")
    buffer = _io.BytesIO()
    with _tarfile.open(fileobj=buffer, mode="w") as archive:
        escape_dir = _tarfile.TarInfo("../evil_dir")
        escape_dir.type = _tarfile.DIRTYPE
        archive.addfile(escape_dir)
        for name, payload in bundle_files().items():
            info = _tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, _io.BytesIO(payload))
    response = upload(client, "example_trainer_v1", buffer.getvalue())
    assert response.status_code == 422
    assert not (tmp_path.parent / "evil_dir").exists()


def test_bundle_upload_content_length_precheck(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = client.put(
        "/v1/projects/example_new/bundles/example_big",
        content=b"tiny",
        headers={
            "content-type": "application/x-tar",
            "content-length": "4",
        },
    )
    # sanity: normal small upload still evaluated (fails as non-tar, not size)
    assert response.json()["errors"][0]["code"] == "script_bundle.upload_invalid"


def test_bundle_manifest_only_tar_is_422_not_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    files = {"kikai_bundle.json": bundle_files()["kikai_bundle.json"]}
    response = upload(client, "example_trainer_v1", make_tar(files))
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "script_bundle.upload_invalid"


def test_data_source_verify_errors_are_sanitized(tmp_path: Path) -> None:
    import json as _json

    client = make_client(tmp_path)
    put_project(client, "example_new")
    payload = tmp_path / "inputs" / "manifest.jsonl"
    payload.parent.mkdir(parents=True)
    payload.write_text("{}\n", encoding="utf-8")
    client.put(
        "/v1/projects/example_new/data-sources/example_manifest",
        json={
            "kind": "file",
            "source_type": "dataset_manifest",
            "path": str(payload),
            "host_ref": "local",
            "roles": ["train_manifest"],
            "summary": "example",
        },
    )
    payload.write_text("tampered\n", encoding="utf-8")
    response = client.post(
        "/v1/projects/example_new/data-sources/example_manifest/verify"
    )
    assert response.status_code == 422
    assert str(tmp_path) not in _json.dumps(response.json())  # no host paths on the wire


def test_data_source_put_kind_change_is_409(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    payload = tmp_path / "inputs" / "manifest.jsonl"
    payload.parent.mkdir(parents=True)
    payload.write_text("{}\n", encoding="utf-8")
    body = {
        "kind": "file",
        "source_type": "dataset_manifest",
        "path": str(payload),
        "host_ref": "local",
        "roles": ["train_manifest"],
        "summary": "example",
    }
    client.put("/v1/projects/example_new/data-sources/example_manifest", json=body)
    as_directory = client.put(
        "/v1/projects/example_new/data-sources/example_manifest",
        json={**body, "kind": "directory"},
    )
    assert as_directory.status_code == 409


def test_data_source_put_string_roles_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    put_project(client, "example_new")
    response = client.put(
        "/v1/projects/example_new/data-sources/example_manifest",
        json={
            "kind": "file",
            "source_type": "dataset_manifest",
            "path": "/tmp/x",
            "host_ref": "local",
            "roles": "train_manifest",
            "summary": "s",
        },
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "data_source.record_invalid"
