import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from kikai_lab.operation import (
    _operation_format,
    add_guard_receipt,
    load_operation,
    request_sha256,
    validate_guard_receipt,
)

OP = {
    "kind": "kikai_operation",
    "schema_version": 1,
    "request": {"adapter": "noop", "operation": "fmt_test", "note": "日本語 ok"},
}


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False, text=True, capture_output=True, env=os.environ.copy(),
    )


def test_format_detection_by_extension():
    assert _operation_format(Path("a.yaml")) == "yaml"
    assert _operation_format(Path("a.yml")) == "yaml"
    assert _operation_format(Path("a.toml")) == "toml"
    assert _operation_format(Path("a.json")) == "json"
    assert _operation_format(Path("a")) == "json"


def test_yaml_op_loads_equal_to_json_and_same_digest(tmp_path):
    (tmp_path / "op.json").write_text(json.dumps(OP, ensure_ascii=False))
    (tmp_path / "op.yaml").write_text(yaml.safe_dump(OP, allow_unicode=True))
    j = load_operation(tmp_path / "op.json")
    y = load_operation(tmp_path / "op.yaml")
    assert y == j
    # the guard digest is computed on the request dict -> format-agnostic
    assert request_sha256(y) == request_sha256(j)


def test_toml_op_loads(tmp_path):
    (tmp_path / "op.toml").write_text(
        'kind = "kikai_operation"\n'
        "schema_version = 1\n\n"
        "[request]\n"
        'adapter = "noop"\n'
        'operation = "fmt_test"\n'
    )
    d = load_operation(tmp_path / "op.toml")
    assert d["request"]["adapter"] == "noop"
    assert d["kind"] == "kikai_operation"


def test_yaml_receipt_writeback_roundtrip(tmp_path):
    p = tmp_path / "op.yaml"
    p.write_text(yaml.safe_dump(OP, allow_unicode=True))
    add_guard_receipt(p)
    # the file is STILL yaml (not silently rewritten as json) and carries a valid receipt
    assert _operation_format(p) == "yaml"
    yaml.safe_load(p.read_text())  # still parses as YAML
    reloaded = load_operation(p)
    assert reloaded["guard_receipt"]["status"] == "passed"
    validate_guard_receipt(reloaded)  # digest matches -> no raise


def test_invalid_yaml_op_reports_clear_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("this: is: not: valid: yaml: [")
    try:
        load_operation(p)
        raise AssertionError("expected an OperationError")
    except Exception as exc:  # noqa: BLE001
        assert "could not be parsed as yaml" in str(exc) or "operation" in str(exc)


def test_cli_target_dry_run_accepts_yaml_op(tmp_path):
    p = tmp_path / "op.yaml"
    p.write_text(yaml.safe_dump(OP, allow_unicode=True))
    r = run_cli("target", "dry-run", str(p))
    assert r.returncode == 0, r.stdout + r.stderr
    reloaded = load_operation(p)
    assert reloaded.get("guard_receipt", {}).get("status") == "passed"
