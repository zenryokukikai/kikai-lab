#!/usr/bin/env python3
"""Synchronize a remote Kikai Lab git checkout with origin.

This is the only intended direct SSH path: remote repository update/sync.
All experiment/container side effects must go through Kikai operation JSON adapters.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

try:
    from scripts.check_public_hygiene import Violation, format_violations, scan_paths
except ModuleNotFoundError:  # pragma: no cover - supports direct `python scripts/...` execution
    from check_public_hygiene import Violation, format_violations, scan_paths

SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z0-9_./~+-]+$")
SAFE_BRANCH = re.compile(r"^[A-Za-z0-9_./+-]+$")


def require_safe_remote_path(value: str) -> str:
    if not value or not SAFE_REMOTE_PATH.fullmatch(value):
        raise SystemExit(f"unsafe remote path: {value!r}")
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise SystemExit(f"remote path must not contain '..': {value!r}")
    return value


def require_safe_branch(value: str) -> str:
    if not value or not SAFE_BRANCH.fullmatch(value):
        raise SystemExit(f"unsafe branch/ref: {value!r}")
    if value.startswith("-") or ".." in value:
        raise SystemExit(f"unsafe branch/ref: {value!r}")
    return value


def run_public_hygiene_guard(repo_root: Path) -> None:
    violations: list[Violation] = scan_paths(root=repo_root)
    if violations:
        raise SystemExit(
            "public hygiene guard failed before remote sync:\n"
            f"{format_violations(violations)}\n\n"
            "Keep private run-specific material in repo-external Kikai runtime state."
        )


def run_ssh(host: str, remote_args: list[str], *, dry_run: bool) -> None:
    command = ["ssh", host, *remote_args]
    print("+", " ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


def create_clean_worktree(
    host: str,
    source_root: str,
    worktree_root: str,
    remote: str,
    branch: str,
    *,
    dry_run: bool,
) -> None:
    clean_root = require_safe_remote_path(worktree_root)
    run_ssh(
        host,
        ["git", "-C", source_root, "worktree", "add", "--detach", clean_root, f"{remote}/{branch}"],
        dry_run=dry_run,
    )
    run_ssh(host, ["git", "-C", clean_root, "status", "--short", "--branch"], dry_run=dry_run)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("KIKAI_REMOTE_SSH_HOST"),
        help="SSH host alias",
    )
    parser.add_argument(
        "--remote-root",
        default=os.environ.get("REMOTE_KIKAI_LAB_ROOT"),
        help="Remote Kikai Lab checkout path",
    )
    parser.add_argument("--branch", default=os.environ.get("KIKAI_SYNC_BRANCH", "main"))
    parser.add_argument("--remote", default=os.environ.get("KIKAI_SYNC_REMOTE", "origin"))
    parser.add_argument(
        "--clean-worktree-root",
        default=os.environ.get("REMOTE_KIKAI_CLEAN_WORKTREE_ROOT"),
        help=(
            "Create and verify this clean remote git worktree instead of pulling "
            "the primary checkout"
        ),
    )
    parser.add_argument(
        "--fallback-worktree-root",
        default=None,
        help=(
            "Explicitly requested fallback only: if primary ff-only sync fails, "
            "create and verify this clean remote git worktree"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    repo_root = Path(__file__).resolve().parents[1]
    run_public_hygiene_guard(repo_root)
    if not args.host:
        raise SystemExit("--host or KIKAI_REMOTE_SSH_HOST is required")
    if not args.remote_root:
        raise SystemExit("--remote-root or REMOTE_KIKAI_LAB_ROOT is required")
    remote_root = require_safe_remote_path(args.remote_root)
    branch = require_safe_branch(args.branch)
    remote = require_safe_branch(args.remote)

    run_ssh(args.host, ["git", "-C", remote_root, "fetch", remote, branch], dry_run=args.dry_run)

    if args.clean_worktree_root:
        create_clean_worktree(
            args.host,
            remote_root,
            args.clean_worktree_root,
            remote,
            branch,
            dry_run=args.dry_run,
        )
        return 0

    run_ssh(args.host, ["git", "-C", remote_root, "checkout", branch], dry_run=args.dry_run)
    try:
        run_ssh(
            args.host,
            ["git", "-C", remote_root, "pull", "--ff-only", remote, branch],
            dry_run=args.dry_run,
        )
    except subprocess.CalledProcessError:
        if not args.fallback_worktree_root:
            raise
        print("ff-only sync failed; creating clean worktree", file=sys.stderr)
        create_clean_worktree(
            args.host,
            remote_root,
            args.fallback_worktree_root,
            remote,
            branch,
            dry_run=args.dry_run,
        )
        return 0
    run_ssh(
        args.host,
        ["git", "-C", remote_root, "status", "--short", "--branch"],
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
