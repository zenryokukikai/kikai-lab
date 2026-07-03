"""Operation templates: named, parameterised recipes that render to a normal operation.

A *generation template* captures a known-good, reusable command recipe (which script, which
container, which arguments) as a first-class, reviewable artifact so it does not have to be
re-derived by hand -- and mis-assembled -- every time. It renders to an ordinary operation
object that then flows through the SAME guard path (`kikai target dry-run` / `run`).

Template file (JSON / YAML / TOML, chosen by extension), e.g. `templates/<name>.yaml`::

    kind: kikai_operation_template
    schema_version: 1
    name: render-preview
    description: Render a short preview from a finished run
    parameters:
      - {name: source_run_dir, required: true, description: "Run dir to render from"}
      - {name: max_frames, default: "250"}
    request:
      adapter: script_bundle_run
      operation: "{{operation_id}}"
      args: ["--source-run-dir", "{{source_run_dir}}", "--max-frames", "{{max_frames}}"]

`{{name}}` placeholders are substituted from declared parameter defaults + `--set k=v`
overrides. Missing required params, or any placeholder left unresolved, is a hard error --
so a rendered operation is always complete.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kikai_lab.operation import _loads_operation, _operation_format

TEMPLATE_KIND = "kikai_operation_template"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class TemplateError(Exception):
    """Raised for malformed templates or incomplete parameter sets."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def load_template(path: Path) -> dict[str, Any]:
    """Load + structurally validate a template file (format by extension)."""
    if not path.exists():
        raise TemplateError("template.file_missing", f"template file does not exist: {path}", {"path": str(path)})
    fmt = _operation_format(path)
    try:
        data = _loads_operation(path.read_text(encoding="utf-8"), fmt)
    except Exception as exc:  # noqa: BLE001 -- surface any parser error uniformly
        raise TemplateError(
            "template.invalid", f"template could not be parsed as {fmt}: {exc}",
            {"path": str(path), "format": fmt}) from exc
    if not isinstance(data, dict):
        raise TemplateError("template.invalid", "template must be a mapping/object")
    if data.get("kind") != TEMPLATE_KIND:
        raise TemplateError(
            "template.kind_invalid", f"template kind must be '{TEMPLATE_KIND}'",
            {"kind": data.get("kind")})
    if not isinstance(data.get("request"), dict):
        raise TemplateError("template.request_missing", "template must contain a request object")
    params = data.get("parameters", [])
    if not isinstance(params, list):
        raise TemplateError("template.parameters_invalid", "parameters must be a list")
    for p in params:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"]:
            raise TemplateError("template.parameter_invalid", "each parameter needs a string name", {"parameter": p})
    return data


def parse_set_overrides(pairs: list[str]) -> dict[str, str]:
    """Parse `--set key=value` pairs into a dict (value may itself contain '=')."""
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise TemplateError("template.set_invalid", f"--set expects key=value, got: {pair!r}", {"value": pair})
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise TemplateError("template.set_invalid", f"--set key is empty: {pair!r}", {"value": pair})
        out[key] = value
    return out


def _resolve_params(template: dict[str, Any], overrides: dict[str, str]) -> dict[str, str]:
    declared = {p["name"]: p for p in template.get("parameters", [])}
    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise TemplateError(
            "template.unknown_parameter", f"unknown parameters passed via --set: {', '.join(unknown)}",
            {"unknown": unknown, "declared": sorted(declared)})
    resolved: dict[str, str] = {}
    for name, spec in declared.items():
        if spec.get("default") is not None:
            resolved[name] = str(spec["default"])
    resolved.update(overrides)
    missing = sorted(n for n, spec in declared.items() if spec.get("required") and n not in resolved)
    if missing:
        raise TemplateError(
            "template.missing_required", f"missing required parameters: {', '.join(missing)}",
            {"missing": missing})
    return resolved


def _substitute(value: Any, params: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, str):
        def _repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in params:
                raise TemplateError(
                    "template.unresolved_placeholder",
                    f"no value for placeholder {{{{{name}}}}} (declare it in parameters or pass --set {name}=...)",
                    {"placeholder": name})
            return str(params[name])
        return _PLACEHOLDER.sub(_repl, value)
    return value


def render_template(template: dict[str, Any], overrides: dict[str, str]) -> dict[str, Any]:
    """Render a loaded template + overrides into a complete operation object."""
    params = _resolve_params(template, overrides)
    request = _substitute(template["request"], params)
    return {
        "kind": "kikai_operation",
        "schema_version": int(template.get("schema_version", 1)),
        "request": request,
    }


def list_templates(project_root: Path) -> list[dict[str, Any]]:
    """List templates under <project_root>/templates/ with name + description + params."""
    templates_dir = Path(project_root) / "templates"
    if not templates_dir.is_dir():
        return []
    exts = {".json", ".yaml", ".yml", ".toml"}
    out: list[dict[str, Any]] = []
    for path in sorted(templates_dir.iterdir()):
        if path.suffix.lower() not in exts:
            continue
        try:
            data = load_template(path)
        except TemplateError:
            continue
        out.append({
            "path": str(path),
            "name": data.get("name", path.stem),
            "description": data.get("description", ""),
            "parameters": [
                {"name": p["name"], "required": bool(p.get("required")), "default": p.get("default")}
                for p in data.get("parameters", [])
            ],
        })
    return out
