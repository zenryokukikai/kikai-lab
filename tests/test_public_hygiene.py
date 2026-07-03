from pathlib import Path

from scripts.check_public_hygiene import scan_paths


def test_public_hygiene_rejects_private_run_specific_tokens(tmp_path):
    run_name = "run" + "145"
    private_path = "/" + "mnt" + "/ssd8001/" + "koji" + "ra"
    adopter_repo = "lipsync" + "-" + "engine"
    path = tmp_path / "private.md"
    path.write_text(
        f"{run_name} should never be committed to the public framework repo\n"
        f"{private_path}/{adopter_repo} is a private path\n",
        encoding="utf-8",
    )

    violations = scan_paths([path], root=tmp_path)

    assert [violation.rule for violation in violations] == [
        "concrete-run-name",
        "private-path-or-identity",
        "adopter-repo-name",
    ]


def test_public_hygiene_current_public_tree_has_no_private_tokens():
    root = Path(__file__).resolve().parents[1]

    violations = scan_paths(root=root)

    assert violations == []
