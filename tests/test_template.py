import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from kikai_lab.operation import load_operation
from kikai_lab.template import (
    TemplateError,
    list_templates,
    load_template,
    parse_set_overrides,
    render_template,
)

TEMPLATE = {
    "kind": "kikai_operation_template",
    "schema_version": 1,
    "name": "t",
    "parameters": [
        {"name": "input_dir", "required": True},
        {"name": "max_frames", "default": "250"},
    ],
    "request": {
        "adapter": "script_bundle_run",
        "operation": "op1",
        "args": ["--input-dir", "{{input_dir}}", "--max-frames", "{{max_frames}}"],
    },
}

# A minimal, GENERIC template authored on disk for the load/list/CLI tests. It exercises
# a required param, a defaulted param, and {{placeholder}} substitution in the request.
EXAMPLE_TEMPLATE = {
    "kind": "kikai_operation_template",
    "schema_version": 1,
    "name": "example-render",
    "description": "Generic example render recipe",
    "parameters": [
        {"name": "operation_id", "required": True, "description": "operation id"},
        {"name": "input_dir", "required": True, "description": "input directory"},
        {"name": "output_path", "required": True, "description": "output path"},
        {"name": "max_frames", "default": "250"},
    ],
    "request": {
        "adapter": "script_bundle_run",
        "operation": "{{operation_id}}",
        "args": [
            "--input-dir", "{{input_dir}}",
            "--output-path", "{{output_path}}",
            "--max-frames", "{{max_frames}}",
        ],
    },
}


def write_example_template(tmp_path: Path) -> Path:
    """Author the generic example template as a YAML file under tmp_path."""
    p = tmp_path / "example_render.yaml"
    p.write_text(yaml.safe_dump(EXAMPLE_TEMPLATE), encoding="utf-8")
    return p


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kikai_lab.cli", *args],
        check=False, text=True, capture_output=True, env=os.environ.copy(),
    )


def test_render_applies_defaults_and_overrides():
    op = render_template(TEMPLATE, {"input_dir": "/x/run"})
    assert op["kind"] == "kikai_operation"
    assert op["request"]["args"] == ["--input-dir", "/x/run", "--max-frames", "250"]
    op2 = render_template(TEMPLATE, {"input_dir": "/x/run", "max_frames": "12"})
    assert op2["request"]["args"][-1] == "12"


def test_missing_required_is_error():
    try:
        render_template(TEMPLATE, {})
        raise AssertionError("expected TemplateError")
    except TemplateError as exc:
        assert exc.code == "template.missing_required"
        assert "input_dir" in exc.details["missing"]


def test_unknown_override_is_error():
    try:
        render_template(TEMPLATE, {"input_dir": "/x", "nope": "1"})
        raise AssertionError("expected TemplateError")
    except TemplateError as exc:
        assert exc.code == "template.unknown_parameter"


def test_unresolved_placeholder_is_error():
    bad = dict(TEMPLATE)
    bad = json.loads(json.dumps(TEMPLATE))
    bad["request"]["args"].append("{{never_declared}}")
    try:
        render_template(bad, {"input_dir": "/x"})
        raise AssertionError("expected TemplateError")
    except TemplateError as exc:
        assert exc.code == "template.unresolved_placeholder"


def test_parse_set_overrides_handles_equals_in_value():
    out = parse_set_overrides(["a=b", "path=/x=y"])
    assert out == {"a": "b", "path": "/x=y"}


def test_load_template_rejects_wrong_kind(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump({"kind": "kikai_operation", "request": {}}))
    try:
        load_template(p)
        raise AssertionError("expected TemplateError")
    except TemplateError as exc:
        assert exc.code == "template.kind_invalid"


def test_example_template_loads_and_lists(tmp_path):
    example = write_example_template(tmp_path)
    assert example.exists()
    data = load_template(example)
    assert data["name"] == "example-render"
    names = {p["name"] for p in data["parameters"]}
    assert "input_dir" in names
    listed = list_templates(tmp_path)  # no templates/ subdir under tmp_path -> []
    assert isinstance(listed, list)
    assert listed == []


def test_cli_render_writes_operation_that_loads(tmp_path):
    example = write_example_template(tmp_path)
    out = tmp_path / "op.yaml"
    r = run_cli(
        "template", "render", str(example),
        "--set", "operation_id=render1",
        "--set", "input_dir=/tmp/input",
        "--set", "output_path=/tmp/output.mp4",
        "--set", "max_frames=8",
        "--out", str(out),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    op = load_operation(out)
    assert op["request"]["operation"] == "render1"
    assert "--input-dir" in op["request"]["args"]
    assert op["request"]["args"][op["request"]["args"].index("--max-frames") + 1] == "8"


def test_cli_render_missing_required_fails(tmp_path):
    example = write_example_template(tmp_path)
    r = run_cli("template", "render", str(example), "--set", "operation_id=x")
    assert r.returncode == 2
    assert "missing required" in (r.stdout + r.stderr)
