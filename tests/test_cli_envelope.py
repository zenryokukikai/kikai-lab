import json
import subprocess
import sys


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_validate_missing_project_root_returns_json_failure(tmp_path):
    missing = tmp_path / "missing"

    result = run_cli("validate", "--project-root", str(missing), "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == 1
    assert payload["data"] == {}
    assert payload["warnings"] == []
    assert payload["errors"][0]["code"] == "registry.project_root_missing"
    assert payload["errors"][0]["blocking"] is True
    assert payload["next_actions"][0]["id"] == "create_registry_root"


def test_unknown_command_returns_json_failure():
    result = run_cli("unknown", "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "cli.unknown_command"


def test_top_level_help_lists_kikai_commands():
    result = run_cli("--help")

    assert result.returncode == 0
    assert "usage: kikai" in result.stdout
    assert "target" in result.stdout
    assert "exec" in result.stdout
    assert "show" in result.stdout


def test_target_dry_run_help_is_discoverable():
    result = run_cli("target", "dry-run", "--help")

    assert result.returncode == 0
    assert "usage: kikai target dry-run" in result.stdout
    assert "operation_json" in result.stdout
