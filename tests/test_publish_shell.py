"""生成した run.sh がシェルとして妥当か (issue #63)。

**実行はしない。** `bash -n` は構文だけを見る。shellcheck があれば併せて当てる。
"""
import json
import os
import shutil
import subprocess

import pytest

from kikai_lab.publish import build_plan
from kikai_lab.publish_package import Seg, render_word
from tests.test_publish import (
    docker_commands,
    op_doc,
    run_cli,
    write_bundle,
    write_container,
    write_op,
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bundle(root, "tools_v1", "run.sh")
    write_container(
        root,
        "gen_worker",
        mounts=[
            {"source": "/srv/example/faces", "target": "/workspace/data", "mode": "ro"},
            {"source": "env:OUT_DIR", "target": "/workspace/out", "mode": "rw"},
        ],
    )
    return root


def publish(project, tmp_path, name="pkg", **op_kwargs):
    op = write_op(tmp_path, "a.json", op_doc("gen", **op_kwargs))
    skip = write_op(
        tmp_path,
        "b.json",
        {
            "schema_version": 1,
            "kind": "kikai_operation",
            "request": {"operation": "notify", "project_root": "x", "adapter": "noop"},
        },
    )
    out = tmp_path / name
    result = run_cli(
        "publish", name,
        "--project-root", str(project),
        "--ops", str(op), str(skip),
        "--out", str(out),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def test_run_sh_parses_as_bash(project, tmp_path):
    out = publish(project, tmp_path)
    check = subprocess.run(
        ["bash", "-n", str(out / "run.sh")], check=False, text=True, capture_output=True
    )
    assert check.returncode == 0, check.stderr


def test_run_sh_passes_shellcheck_when_available(project, tmp_path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    out = publish(project, tmp_path)
    check = subprocess.run(
        ["shellcheck", "--severity=warning", str(out / "run.sh")],
        check=False,
        text=True,
        capture_output=True,
    )
    assert check.returncode == 0, check.stdout


def test_run_sh_is_strict_and_guards_unset_variables(project, tmp_path):
    out = publish(project, tmp_path)
    script = (out / "run.sh").read_text()
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    for name in ("SRC_WORKSPACE_DATA", "OUT_DIR", "GEN_IMAGE"):
        assert f': "${{{name}:?' in script, name
    assert (out / "run.sh").stat().st_mode & 0o111


def test_values_with_shell_metacharacters_are_quoted(project, tmp_path):
    """引数に空白やクォートが混ざっても、語が割れたり展開されたりしない。"""
    out = publish(
        project,
        tmp_path,
        name="meta",
        args=["--caption", "a b'c \"d\"", "--expr", "$(echo pwned)", "--tick", "`id`"],
    )
    script = (out / "run.sh").read_text()
    assert "$(echo pwned)" not in script.replace("'$(echo pwned)'", "")
    check = subprocess.run(
        ["bash", "-n", str(out / "run.sh")], check=False, text=True, capture_output=True
    )
    assert check.returncode == 0, check.stderr
    # bash 自身に語を数えさせる: docker を printf に替えて実行し、語の列を丸ごと比べる。
    # 期待値は shlex の読み (独立した第二のパーサ) に既知の変数値を入れたもの。
    joined = script.replace(" \\\n  ", " ")
    docker_lines = [line for line in joined.splitlines() if line.startswith("docker ")]
    assert len(docker_lines) == 1
    substitutions = {
        "PUBLISH_ROOT": "/pkg",
        "SRC_WORKSPACE_DATA": "/srv/data",
        "OUT_DIR": "/srv/out",
        "GEN_IMAGE": "example/img:1",
    }
    expected = []
    for token in docker_commands(script)[0][1:]:
        for name, value in substitutions.items():
            token = token.replace("${" + name + "}", value)
        expected.append(token)
    parsed = subprocess.run(
        ["bash", "-c", "printf '%s\\n' " + docker_lines[0].removeprefix("docker ")],
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, **substitutions},
    )
    assert parsed.returncode == 0, parsed.stderr
    assert parsed.stdout.splitlines() == expected
    # メタ文字入りの引数がそれぞれ 1 語のまま、展開もされずに残っている
    for word in ("a b'c \"d\"", "$(echo pwned)", "`id`"):
        assert word in expected


def test_render_word_quotes_literals_and_expands_vars():
    assert render_word((Seg("--rm"),)) == "--rm"
    assert render_word((Seg("a b"),)) == "'a b'"
    assert render_word((Seg("it's"),)) == "'it'\\''s'"
    assert render_word((Seg("X", is_var=True),)) == '"${X}"'
    assert render_word((Seg("X", is_var=True), Seg(":/t:ro"))) == '"${X}:/t:ro"'
    # 変数を含む語の literal 部分の $ ` " \\ は必ず殺す
    assert render_word((Seg("X", is_var=True), Seg('$(id)"`'))) == '"${X}\\$(id)\\"\\`"'


def test_shell_words_round_trip_through_bash(project, tmp_path, capsys):
    """bash 自身に語を数えさせる — 引用が正しいかを実物で確かめる。"""
    words = [
        (Seg("plain"),),
        (Seg("with space"),),
        (Seg("V", is_var=True), Seg(":/target:ro")),
        (Seg("dollar $HOME literal"),),
    ]
    rendered = " ".join(render_word(word) for word in words)
    check = subprocess.run(
        ["bash", "-c", f'V=/src; set -- {rendered}; printf "%s\\n" "$@"'],
        check=False,
        text=True,
        capture_output=True,
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.splitlines() == [
        "plain",
        "with space",
        "/src:/target:ro",
        "dollar $HOME literal",
    ]


def test_plan_is_pure_and_does_not_read_the_environment(project, monkeypatch):
    """翻訳は env を読まない。読んでいれば秘密が焼き込まれる余地が残る。"""
    monkeypatch.setenv("GEN_IMAGE", "should-not-appear")
    monkeypatch.setenv("OUT_DIR", "/should/not/appear")
    plan = build_plan(
        name="pure",
        project_root=project,
        operations=[op_doc("gen")],
    )
    rendered = json.dumps(
        [[render_word(word) for word in op.command] for op in plan.translated]
    )
    assert "should-not-appear" not in rendered
    assert "/should/not/appear" not in rendered
