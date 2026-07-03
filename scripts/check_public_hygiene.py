#!/usr/bin/env python3
"""Public hygiene guard for the Kikai Lab framework repository.

The framework repo must stay publishable and generic. Private experiment names,
personal mount paths, and adopter repository names belong in repo-external Kikai
runtime state, not in tracked framework code/docs/fixtures.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    description: str


@dataclass(frozen=True)
class Violation:
    path: Path
    line_number: int
    rule: str
    matched_text: str
    line: str


def _joined(*parts: str) -> str:
    return "".join(parts)


_SLASH = "/"

RULES: tuple[Rule, ...] = (
    Rule(
        name="concrete-run-name",
        # A trailing "_" is a word char, so plain \b misses run<NNN>_suffix
        # forms; the digit-only lookahead catches them without matching runXY.
        pattern=re.compile(r"\brun\d{3,}(?![0-9])", re.IGNORECASE),
        description="Concrete private experiment run names must not be committed.",
    ),
    Rule(
        name="private-path-or-identity",
        pattern=re.compile(
            rf"(?:"
            rf"{re.escape(_SLASH + 'mnt' + _SLASH)}[^\s\"'<>]+|"
            rf"{re.escape(_SLASH + 'lab_data' + _SLASH)}[^\s\"'<>]+|"
            rf"{re.escape(_SLASH + 'Users' + _SLASH)}[^\s\"'<>]+|"
            rf"\b{re.escape(_joined('koji', 'ra'))}\b|"
            rf"\b{re.escape(_joined('a', '100', 'n'))}\b"
            rf")",
            re.IGNORECASE,
        ),
        description="Private hostnames, users, and mount paths must stay out of the public repo.",
    ),
    Rule(
        name="adopter-repo-name",
        pattern=re.compile(
            rf"\b(?:"
            rf"{re.escape(_joined('z', '-', 'lipsync'))}|"
            rf"{re.escape(_joined('lipsync', '-', 'engine'))}"
            rf")\b",
            re.IGNORECASE,
        ),
        description="Adopter/private repository names must not appear in framework fixtures.",
    ),
    Rule(
        name="private-method-vocabulary",
        # Substring match (no \b): these distinctive tokens can hide after an
        # underscore inside a longer identifier, where a word boundary fails.
        pattern=re.compile(
            rf"(?:"
            rf"{re.escape('flash' + 'lips')}|"
            rf"{re.escape('teacher_' + 'pose')}|"
            rf"{re.escape('z_' + 'lips')}|"
            rf"{re.escape('mouth_' + 'highpass')}|"
            rf"{re.escape('pose_' + 'renderer')}|"
            rf"{re.escape('stage' + '_b')}|"
            rf"{re.escape('stage' + '-b')}|"
            rf"{re.escape('static' + 'cache')}|"
            rf"{re.escape('wav' + '2lip')}"
            rf")",
            re.IGNORECASE,
        ),
        description=(
            "Private research method/architecture names must not leak "
            "into the public framework."
        ),
    ),
    Rule(
        name="secret-like-value",
        pattern=re.compile(
            r"(?:https://(?:discord(?:app)?\.com/api/webhooks|hooks\.slack\.com/services)/[^\s\"'<>]+|"
            r"\b(?:ghp|github_pat|xox[baprs])-[-_A-Za-z0-9]{20,})",
            re.IGNORECASE,
        ),
        description="Webhook URLs and token-like values must not be committed.",
    ),
)

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".csv",
    ".gitignore",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".schema",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _git_public_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
    )
    return sorted(root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def _is_text_candidate(path: Path) -> bool:
    if path.name in {"uv.lock"}:
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def _scan_file(path: Path, *, root: Path) -> list[Violation]:
    if not _is_text_candidate(path):
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    violations: list[Violation] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                violations.append(
                    Violation(
                        path=path.relative_to(root),
                        line_number=line_number,
                        rule=rule.name,
                        matched_text=match.group(0),
                        line=line.strip(),
                    )
                )
    return violations


def scan_paths(paths: Iterable[Path] | None = None, *, root: Path | None = None) -> list[Violation]:
    root = (root or Path.cwd()).resolve()
    candidates = list(paths) if paths is not None else _git_public_paths(root)

    violations: list[Violation] = []
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        if path.is_dir():
            continue
        violations.extend(_scan_file(path, root=root))
    return violations


def format_violations(violations: Iterable[Violation]) -> str:
    return "\n".join(
        f"{violation.path}:{violation.line_number}: {violation.rule}: "
        f"{violation.matched_text!r} :: {violation.line}"
        for violation in violations
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to scan. Defaults to the current working directory.",
    )
    args = parser.parse_args(argv)

    violations = scan_paths(root=args.root)
    if violations:
        print("Public hygiene violations found:", file=sys.stderr)
        print(format_violations(violations), file=sys.stderr)
        print(
            "\nPrivate run-specific material must stay in repo-external Kikai runtime state.",
            file=sys.stderr,
        )
        return 1

    print("Public hygiene guard passed: no private/run-specific tokens in tracked/untracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
