import subprocess

import pytest

import scripts.sync_remote_kikai_repo as sync_remote_kikai_repo


def test_sync_remote_runs_public_hygiene_before_ssh(monkeypatch):
    guard_calls = []

    def fake_guard(repo_root):
        guard_calls.append(repo_root)
        raise SystemExit("public hygiene guard failed before remote sync")

    def fake_run_ssh(host, remote_args, *, dry_run):
        raise AssertionError("ssh must not run after public hygiene guard failure")

    monkeypatch.setattr(sync_remote_kikai_repo, "run_public_hygiene_guard", fake_guard)
    monkeypatch.setattr(sync_remote_kikai_repo, "run_ssh", fake_run_ssh)

    with pytest.raises(SystemExit, match="public hygiene guard failed"):
        sync_remote_kikai_repo.main(
            [
                "--host",
                "training-host.example",
                "--remote-root",
                "/srv/kikai-lab-example",
            ]
        )

    assert guard_calls


def test_sync_remote_can_create_clean_worktree_without_pulling_primary_checkout(monkeypatch):
    calls = []

    def fake_run_ssh(host, remote_args, *, dry_run):
        calls.append((host, remote_args, dry_run))

    monkeypatch.setattr(sync_remote_kikai_repo, "run_ssh", fake_run_ssh)

    result = sync_remote_kikai_repo.main(
        [
            "--host",
            "training-host.example",
            "--remote-root",
            "/srv/kikai-lab-example",
            "--clean-worktree-root",
            "/srv/kikai-lab-example-clean-main",
            "--branch",
            "main",
        ]
    )

    assert result == 0
    assert calls == [
        (
            "training-host.example",
            ["git", "-C", "/srv/kikai-lab-example", "fetch", "origin", "main"],
            False,
        ),
        (
            "training-host.example",
            [
                "git",
                "-C",
                "/srv/kikai-lab-example",
                "worktree",
                "add",
                "--detach",
                "/srv/kikai-lab-example-clean-main",
                "origin/main",
            ],
            False,
        ),
        (
            "training-host.example",
            ["git", "-C", "/srv/kikai-lab-example-clean-main", "status", "--short", "--branch"],
            False,
        ),
    ]
    assert all("pull" not in args for _, args, _ in calls)


def test_sync_remote_requires_explicit_fallback_flag_after_ff_only_pull_failure(monkeypatch):
    calls = []

    def fake_run_ssh(host, remote_args, *, dry_run):
        calls.append((host, remote_args, dry_run))
        if remote_args[:4] == ["git", "-C", "/srv/kikai-lab-example", "pull"]:
            raise subprocess.CalledProcessError(128, ["ssh", host, *remote_args])

    monkeypatch.setenv("REMOTE_KIKAI_FALLBACK_WORKTREE_ROOT", "/srv/implicit-fallback")
    monkeypatch.setattr(sync_remote_kikai_repo, "run_ssh", fake_run_ssh)

    with pytest.raises(subprocess.CalledProcessError):
        sync_remote_kikai_repo.main(
            [
                "--host",
                "training-host.example",
                "--remote-root",
                "/srv/kikai-lab-example",
                "--branch",
                "main",
            ]
        )

    assert all("worktree" not in args for _, args, _ in calls)


def test_sync_remote_falls_back_to_clean_worktree_after_explicit_flag(monkeypatch, capsys):
    calls = []

    def fake_run_ssh(host, remote_args, *, dry_run):
        calls.append((host, remote_args, dry_run))
        if remote_args[:4] == ["git", "-C", "/srv/kikai-lab-example", "pull"]:
            raise subprocess.CalledProcessError(128, ["ssh", host, *remote_args])

    monkeypatch.setattr(sync_remote_kikai_repo, "run_ssh", fake_run_ssh)

    result = sync_remote_kikai_repo.main(
        [
            "--host",
            "training-host.example",
            "--remote-root",
            "/srv/kikai-lab-example",
            "--fallback-worktree-root",
            "/srv/kikai-lab-example-clean-main",
            "--branch",
            "main",
        ]
    )

    assert result == 0
    assert calls[-2:] == [
        (
            "training-host.example",
            [
                "git",
                "-C",
                "/srv/kikai-lab-example",
                "worktree",
                "add",
                "--detach",
                "/srv/kikai-lab-example-clean-main",
                "origin/main",
            ],
            False,
        ),
        (
            "training-host.example",
            ["git", "-C", "/srv/kikai-lab-example-clean-main", "status", "--short", "--branch"],
            False,
        ),
    ]
    assert "ff-only sync failed; creating clean worktree" in capsys.readouterr().err
