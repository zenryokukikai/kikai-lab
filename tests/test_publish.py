"""`kikai publish` の翻訳規則の検査 (issue #63)。

生成物は実行しない。文字列としての妥当性と、翻訳が何を落として何を残したかだけを見る。
"""
import hashlib
import json
import os
import shlex
import subprocess
import sys

import pytest

from kikai_lab.operation import (
    docker_detached_run_command,
    docker_run_command,
    load_container_record,
    load_script_bundle,
)
from kikai_lab.publish import build_plan
from kikai_lab.publish_package import render_word


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


def write_bundle(root, bundle_id="tools_v1", script="run.sh", body="#!/bin/sh\necho hi\n"):
    bundle_root = root / "script_bundles" / bundle_id
    (bundle_root / "root").mkdir(parents=True, exist_ok=True)
    script_path = bundle_root / "root" / script
    script_path.write_text(body)
    (bundle_root / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "kikai_script_bundle",
                "bundle_id": bundle_id,
                "immutable": True,
                "generated_by": {"tool": "kikai script-bundle create", "schema_version": 1},
                "entrypoints": {
                    "main": {"argv": ["bash", f"script_bundles/{bundle_id}/root/{script}"]}
                },
                "files": [
                    {
                        "path": f"root/{script}",
                        "sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )
    return bundle_root


def write_container(root, container_id="gen_worker", mounts=None, extra="", docker_extra=""):
    containers = root / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    mount_yaml = ""
    for mount in mounts or []:
        mount_yaml += f"  - source: {mount['source']}\n    target: {mount['target']}\n"
        if mount.get("mode"):
            mount_yaml += f"    mode: {mount['mode']}\n"
    body = f"""schema_version: 1
kind: docker_container
container_id: {container_id}
docker:
  name: kikai-{container_id}
  image: env:GEN_IMAGE
{docker_extra}gpus: all
shm_size: 16g
{extra}"""
    if mount_yaml:
        body += f"mounts:\n{mount_yaml}"
    (containers / f"{container_id}.yaml").write_text(body)


def op_doc(
    operation,
    bundle_id="tools_v1",
    container_id="gen_worker",
    project_root="/does/not/matter",
    adapter="script_bundle_run",
    **extra,
):
    request = {
        "operation": operation,
        "project_root": project_root,
        "target_id": operation,
        "adapter": adapter,
        "container_id": container_id,
        "bundle_id": bundle_id,
        "entrypoint": "main",
    }
    request.update(extra)
    return {"schema_version": 1, "kind": "kikai_operation", "request": request}


def write_op(tmp_path, filename, doc):
    path = tmp_path / filename
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bundle(root, "kimodo_v1", "kimodo.sh")
    write_bundle(root, "scail_v1", "scail.sh")
    write_container(
        root,
        "gen_worker",
        mounts=[
            {"source": "/srv/example/faces", "target": "/workspace/data", "mode": "ro"},
            {"source": "/srv/example/out", "target": "/workspace/out", "mode": "rw"},
        ],
    )
    return root


def rendered_command(command):
    return [render_word(word) for word in command]


def docker_commands(script):
    """run.sh の docker コマンドを語列として読み戻す (継続行を畳んでから shlex)。"""
    joined = script.replace(" \\\n  ", " ")
    return [
        shlex.split(line) for line in joined.splitlines() if line.startswith("docker ")
    ]


def adjacent(tokens, first, second):
    return any(
        tokens[i] == first and tokens[i + 1] == second for i in range(len(tokens) - 1)
    )


# --------------------------------------------------------------- 順序を保つ


def test_op_order_is_the_run_sh_order(project, tmp_path):
    """--ops の並び順がそのまま run.sh の並び順になる (依存順を触らない)。"""
    ops = [
        write_op(tmp_path, "a.json", op_doc("step_kimodo", bundle_id="kimodo_v1")),
        write_op(tmp_path, "b.json", op_doc("step_scail", bundle_id="scail_v1")),
        write_op(tmp_path, "c.json", op_doc("step_post", bundle_id="kimodo_v1")),
    ]
    out = tmp_path / "pkg"
    result = run_cli(
        "publish", "pattern_chain",
        "--project-root", str(project),
        "--ops", *[str(p) for p in ops],
        "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    script = (out / "run.sh").read_text()
    positions = [script.index(name) for name in ("step_kimodo", "step_scail", "step_post")]
    assert positions == sorted(positions), script
    docker_lines = [n for n, line in enumerate(script.splitlines()) if line.startswith("docker ")]
    assert len(docker_lines) == 3
    # 名前 → docker 行の対応も順序どおり (コメントだけ並んでいても駄目)
    bundles = [
        line for line in script.splitlines() if line.startswith("# [") and "bundle=" in line
    ]
    assert [b.split("bundle=")[1].split()[0] for b in bundles] == [
        "kimodo_v1",
        "scail_v1",
        "kimodo_v1",
    ]


def test_reversed_input_gives_reversed_output(project, tmp_path):
    """入力順を逆にすれば出力順も逆になる — 順序が入力由来だという対照。"""
    ops = [
        write_op(tmp_path, "a.json", op_doc("step_kimodo", bundle_id="kimodo_v1")),
        write_op(tmp_path, "b.json", op_doc("step_scail", bundle_id="scail_v1")),
    ]
    out = tmp_path / "pkg_rev"
    result = run_cli(
        "publish", "rev",
        "--project-root", str(project),
        "--ops", str(ops[1]), str(ops[0]),
        "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    script = (out / "run.sh").read_text()
    assert script.index("step_scail") < script.index("step_kimodo")


# ------------------------------------------------------------ mount の ro/rw


def test_mount_modes_are_preserved(project, tmp_path):
    """mounts の mode を落とさない。ro の口が rw で開くのは事故。"""
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    assert run_cli(
        "publish", "m", "--project-root", str(project), "--ops", str(op), "--out", str(out)
    ).returncode == 0
    script = (out / "run.sh").read_text()
    assert '"${SRC_WORKSPACE_DATA}:/workspace/data:ro"' in script
    assert '"${SRC_WORKSPACE_OUT}:/workspace/out:rw"' in script
    # bundle は必ず ro で被せる (published 版がレシピを書き換えられては困る)
    assert '"${PUBLISH_ROOT}/bundles:/workspace/kikai_project/script_bundles:ro"' in script


def test_readme_mount_table_carries_the_mode(project, tmp_path):
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    run_cli("publish", "m", "--project-root", str(project), "--ops", str(op), "--out", str(out))
    readme = (out / "README.md").read_text()
    assert "| `SRC_WORKSPACE_DATA` | `/workspace/data` | `ro` |" in readme
    assert "| `SRC_WORKSPACE_OUT` | `/workspace/out` | `rw` |" in readme


# ------------------------------------------------- 翻訳不能 adapter を黙って落とさない


def test_untranslatable_adapter_is_reported_three_ways(project, tmp_path):
    """除外は envelope・run.sh・README の 3 面すべてに出る。"""
    ops = [
        write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1")),
        write_op(
            tmp_path,
            "b.json",
            {
                "schema_version": 1,
                "kind": "kikai_operation",
                "request": {
                    "operation": "teardown_worker",
                    "project_root": str(project),
                    "adapter": "remote_docker_teardown",
                    "container_id": "gen_worker",
                },
            },
        ),
    ]
    out = tmp_path / "pkg"
    result = run_cli(
        "publish", "chain",
        "--project-root", str(project),
        "--ops", *[str(p) for p in ops],
        "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    codes = [w["code"] for w in envelope["warnings"]]
    assert "publish.operation_excluded" in codes
    assert envelope["data"]["operations_published"] == 1
    assert envelope["data"]["operations_skipped"][0]["operation"] == "teardown_worker"

    script = (out / "run.sh").read_text()
    assert "SKIPPED" in script and "teardown_worker" in script
    assert len(docker_commands(script)) == 1

    readme = (out / "README.md").read_text()
    assert "## 含まれない op" in readme
    assert "teardown_worker" in readme
    assert "remote_docker_teardown" in readme


def test_source_snapshot_mount_excludes_the_op(project, tmp_path):
    """snapshot マウントは書き出せないので、その op ごと除外して理由を残す。"""
    write_container(project, "snap_worker")
    (project / "containers" / "snap_worker.yaml").write_text(
        """schema_version: 1
kind: docker_container
container_id: snap_worker
docker:
  name: kikai-snap
  image: env:GEN_IMAGE
mounts:
  - source_kind: kikai_managed_source_snapshot
    source_snapshot_id: snap1
    target: /workspace/src
    mode: ro
"""
    )
    op = write_op(
        tmp_path, "a.json", op_doc("snap_gen", bundle_id="kimodo_v1", container_id="snap_worker")
    )
    out = tmp_path / "pkg"
    result = run_cli(
        "publish", "s", "--project-root", str(project), "--ops", str(op), "--out", str(out)
    )
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["data"]["operations_published"] == 0
    assert "source snapshot" in envelope["data"]["operations_skipped"][0]["reason"]
    assert "source snapshot" in (out / "README.md").read_text()


# ------------------------------------------------------ ホスト固有値の変数化


def test_no_host_paths_are_written_literally(project, tmp_path):
    """publish 元のホストのパスが run.sh に literal で現れない。"""
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    run_cli("publish", "v", "--project-root", str(project), "--ops", str(op), "--out", str(out))
    script = (out / "run.sh").read_text()
    assert "/srv/example/faces" not in script
    assert "/srv/example/out" not in script
    assert str(project) not in script
    # 元の値は README と env.example (受け手が埋める側) にだけ残る
    assert "/srv/example/faces" in (out / "env.example").read_text()


def test_env_example_lists_key_names_without_values(project, tmp_path):
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    run_cli("publish", "v", "--project-root", str(project), "--ops", str(op), "--out", str(out))
    env_example = (out / "env.example").read_text()
    assignments = [line for line in env_example.splitlines() if "=" in line and line[0] != "#"]
    assert assignments, env_example
    for line in assignments:
        assert line.endswith("="), line
    assert "GEN_IMAGE=" in assignments


def test_secret_refs_are_not_resolved(project, tmp_path):
    """env: 参照は解決しない。値が publish を通ってパッケージに出てはいけない。"""
    op = write_op(
        tmp_path,
        "a.json",
        op_doc("gen", bundle_id="kimodo_v1", env={"HF_TOKEN": "env:MY_SECRET_TOKEN"}),
    )
    out = tmp_path / "pkg"
    result = run_cli(
        "publish", "v",
        "--project-root", str(project),
        "--ops", str(op),
        "--out", str(out),
        env={"MY_SECRET_TOKEN": "s3cr3t-value", "GEN_IMAGE": "example/img:1"},
    )
    assert result.returncode == 0, result.stderr
    for name in ("run.sh", "env.example", "README.md"):
        text = (out / name).read_text()
        assert "s3cr3t-value" not in text, name
    assert '"HF_TOKEN=${MY_SECRET_TOKEN}"' in (out / "run.sh").read_text()
    assert "MY_SECRET_TOKEN=" in (out / "env.example").read_text()


def test_env_ref_name_must_be_shell_identifier(project, tmp_path):
    """`env:DATA-DIR` は `${DATA-DIR}` に落ちるとデフォルト展開として黙って誤動作するので、
    run.sh を出さずに publish 自体を拒否する。"""
    write_container(
        project,
        "dash_worker",
        mounts=[{"source": "env:DATA-DIR", "target": "/workspace/data", "mode": "ro"}],
    )
    op = write_op(
        tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1", container_id="dash_worker")
    )
    out = tmp_path / "pkg"
    result = run_cli(
        "publish", "e", "--project-root", str(project), "--ops", str(op), "--out", str(out)
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["errors"][0]["code"] == "publish.env_ref_invalid"
    assert not (out / "run.sh").exists()


def test_env_ref_name_may_contain_lowercase_and_digits(project, tmp_path):
    """シェル識別子の全域は通す — 検証が識別子より厳しくなったらここが赤になる。"""
    write_container(
        project,
        "lc_worker",
        mounts=[{"source": "env:data_dir2", "target": "/workspace/data", "mode": "ro"}],
    )
    op = write_op(
        tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1", container_id="lc_worker")
    )
    out = tmp_path / "pkg"
    result = run_cli(
        "publish", "lc", "--project-root", str(project), "--ops", str(op), "--out", str(out)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"${data_dir2}:/workspace/data:ro"' in (out / "run.sh").read_text()
    assert "data_dir2=" in (out / "env.example").read_text()


# ----------------------------------------------------------------- bundle 実体


def test_bundle_files_are_copied_with_matching_hashes(project, tmp_path):
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    run_cli("publish", "b", "--project-root", str(project), "--ops", str(op), "--out", str(out))
    copied = out / "bundles" / "kimodo_v1"
    manifest = json.loads((copied / "bundle.json").read_text())
    for item in manifest["files"]:
        path = copied / item["path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    # 使われていない bundle は入らない
    assert not (out / "bundles" / "scail_v1").exists()


def test_entrypoint_argv_resolves_under_the_bundle_mount(project, tmp_path):
    """argv は workdir 相対。bundle を被せる位置と workdir が食い違えば動かない。"""
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    run_cli("publish", "b", "--project-root", str(project), "--ops", str(op), "--out", str(out))
    script = (out / "run.sh").read_text()
    tokens = docker_commands(script)[0]
    assert adjacent(tokens, "--workdir", "/workspace/kikai_project")
    assert tokens[-2:] == ["bash", "script_bundles/kimodo_v1/root/kimodo.sh"]
    assert (out / "bundles" / "kimodo_v1" / "root" / "kimodo.sh").is_file()


# ---------------------------------------------------------------- detach と name


def test_detach_becomes_docker_run_d_with_name(project, tmp_path):
    op = write_op(tmp_path, "a.json", op_doc("train", bundle_id="kimodo_v1", detach=True))
    out = tmp_path / "pkg"
    assert run_cli(
        "publish", "d", "--project-root", str(project), "--ops", str(op), "--out", str(out)
    ).returncode == 0
    tokens = docker_commands((out / "run.sh").read_text())[0]
    assert tokens[:2] == ["docker", "run"]
    assert tokens[2] == "-d"
    assert adjacent(tokens, "--name", "kikai-gen_worker")
    assert "--rm" not in tokens


def test_detach_with_unsafe_literal_name_refuses_publish(project, tmp_path):
    """本体は unsafe な docker.name の detach を submit 時に拒否する。publish も対応を保つ。"""
    (project / "containers" / "bad_name.yaml").write_text(
        """schema_version: 1
kind: docker_container
container_id: bad_name
docker:
  name: "my worker"
  image: env:GEN_IMAGE
"""
    )
    op = write_op(
        tmp_path,
        "a.json",
        op_doc("gen", bundle_id="kimodo_v1", container_id="bad_name", detach=True),
    )
    result = run_cli(
        "publish", "d",
        "--project-root", str(project),
        "--ops", str(op),
        "--out", str(tmp_path / "pkg"),
    )
    assert result.returncode != 0
    codes = [e["code"] for e in json.loads(result.stdout)["errors"]]
    assert "operation.script_bundle_run_detach_requires_name" in codes
    assert not (tmp_path / "pkg" / "run.sh").exists()


def test_truncation_note_only_when_name_is_a_variable(project, tmp_path):
    """literal 名は publish 時に 63 字へ切り詰め済みなので「切っていない」の断りは出ない。
    名前が変数のときだけ README に断りが載る (対照)。"""
    write_container(project, "lit_worker", docker_extra="  ephemeral: true\n")
    (project / "containers" / "ref_worker.yaml").write_text(
        """schema_version: 1
kind: docker_container
container_id: ref_worker
docker:
  name: env:WORKER_NAME
  image: env:GEN_IMAGE
  ephemeral: true
"""
    )
    for container_id, pkg in (("lit_worker", "pkg_lit"), ("ref_worker", "pkg_ref")):
        op = write_op(
            tmp_path,
            f"{container_id}.json",
            op_doc(
                "gen",
                bundle_id="kimodo_v1",
                container_id=container_id,
                container_name_suffix="probe",
            ),
        )
        out = tmp_path / pkg
        result = run_cli(
            "publish", pkg, "--project-root", str(project), "--ops", str(op), "--out", str(out)
        )
        assert result.returncode == 0, result.stdout + result.stderr
    note = "63 字への切り詰めをしていない"
    assert note not in (tmp_path / "pkg_lit" / "README.md").read_text()
    assert note in (tmp_path / "pkg_ref" / "README.md").read_text()


# ------------------------------------------------------- 本体との乖離を捕まえる網

# 本体経路の要素を全部踏む fixture: network/ipc/request.env (ref 込み)/workdir 上書き/
# ephemeral+suffix。ここに無い要素は parity 網の外なので、翻訳へ足したら必ずここへも足す。
PARITY_SUBSTITUTIONS = {
    "SRC_WORKSPACE_DATA": "/srv/example/faces",
    "SRC_WORKSPACE_OUT": "/srv/example/out",
    "GEN_IMAGE": "example/img:1",
    "MY_TOKEN": "t0ken-value",
}
PARITY_COMPOSED_NAME = "kikai-par_worker__probe_run"


def parity_setup(project, monkeypatch, **op_extra):
    monkeypatch.delenv("KIKAI_DOCKER_BIN", raising=False)
    monkeypatch.setenv("GEN_IMAGE", PARITY_SUBSTITUTIONS["GEN_IMAGE"])
    monkeypatch.setenv("MY_TOKEN", PARITY_SUBSTITUTIONS["MY_TOKEN"])
    write_container(
        project,
        "par_worker",
        mounts=[
            {"source": "/srv/example/faces", "target": "/workspace/data", "mode": "ro"},
            {"source": "/srv/example/out", "target": "/workspace/out", "mode": "rw"},
        ],
        extra="network_mode: host\nipc_mode: host\n",
        docker_extra="  ephemeral: true\n",
    )
    return op_doc(
        "gen",
        bundle_id="kimodo_v1",
        container_id="par_worker",
        env={"MODE": "fast", "HF_TOKEN": "env:MY_TOKEN"},
        workdir="/workspace/other",
        container_name_suffix="probe_run",
        **op_extra,
    )


def resolve_published_command(command):
    """published の語列へ publish 元の値を戻し、文書化したマウント差分を正規化する。"""
    resolved = []
    for word in rendered_command(command):
        text = word.strip('"').replace("'", "")
        for name, value in PARITY_SUBSTITUTIONS.items():
            text = text.replace("${" + name + "}", value)
        resolved.append(text)
    return [
        "${PUBLISH_ROOT}/bundles:/workspace/kikai_project/script_bundles:ro"
        if item.endswith("/bundles:/workspace/kikai_project/script_bundles:ro")
        else item
        for item in resolved
    ]


def strip_documented_differences(expected):
    """kikai 帳簿ラベルと project_root マウント (設計 §4.1 の差分) を expected 側から除く。"""
    expected = [
        item for item in expected if item != "--label" and not item.startswith("kikai.")
    ]
    return [
        "${PUBLISH_ROOT}/bundles:/workspace/kikai_project/script_bundles:ro"
        if item.endswith(":/workspace/kikai_project:ro")
        else item
        for item in expected
    ]


def test_published_command_matches_docker_run_command(project, tmp_path, monkeypatch):
    """変数を publish 元の値へ戻せば、kikai 本体が組み立てる docker run と一致する。

    一致しなくてよいのは設計 §4.1 に書いた差分だけ: project_root マウント差し替え、
    kikai 帳簿ラベル、変数化。ここが崩れたら翻訳が本体から乖離している。
    """
    doc = parity_setup(project, monkeypatch)
    plan = build_plan(name="parity", project_root=project, operations=[doc])
    assert plan.ops[0].command is not None
    resolved = resolve_published_command(plan.ops[0].command)

    request = doc["request"]
    container = load_container_record(project, "par_worker")
    bundle, _ = load_script_bundle(project, "kimodo_v1")
    expected = strip_documented_differences(
        docker_run_command(
            request=request,
            container=container,
            container_id="par_worker",
            project_root=project,
            argv=bundle["entrypoints"]["main"]["argv"],
        )
    )
    assert PARITY_COMPOSED_NAME in expected  # fixture が name 合成まで踏んでいる対照
    assert resolved == expected


def test_published_detached_command_matches_docker_detached_run_command(
    project, tmp_path, monkeypatch
):
    """detach=true の翻訳は本体の docker_detached_run_command と一致する。"""
    doc = parity_setup(project, monkeypatch, detach=True)
    plan = build_plan(name="parity_d", project_root=project, operations=[doc])
    assert plan.ops[0].command is not None
    resolved = resolve_published_command(plan.ops[0].command)

    request = doc["request"]
    container = load_container_record(project, "par_worker")
    bundle, _ = load_script_bundle(project, "kimodo_v1")
    expected = strip_documented_differences(
        docker_detached_run_command(
            request=request,
            container=container,
            container_id="par_worker",
            project_root=project,
            container_name=PARITY_COMPOSED_NAME,
            argv=bundle["entrypoints"]["main"]["argv"],
        )
    )
    assert resolved[:4] == ["docker", "run", "-d", "--name"]
    assert resolved == expected


# ------------------------------------------------------------------ 入出力の扱い


def test_refuses_to_overwrite_a_non_empty_output_dir(project, tmp_path):
    op = write_op(tmp_path, "a.json", op_doc("gen", bundle_id="kimodo_v1"))
    out = tmp_path / "pkg"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    result = run_cli(
        "publish", "o", "--project-root", str(project), "--ops", str(op), "--out", str(out)
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["errors"][0]["code"] == "publish.output_exists"
    assert (out / "keep.txt").read_text() == "mine"


def test_missing_project_root_is_reported(tmp_path):
    op = write_op(tmp_path, "a.json", op_doc("gen"))
    result = run_cli(
        "publish", "x",
        "--project-root", str(tmp_path / "nope"),
        "--ops", str(op),
        "--out", str(tmp_path / "pkg"),
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["errors"][0]["code"] == "registry.project_root_missing"
