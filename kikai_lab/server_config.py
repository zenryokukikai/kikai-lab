from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".kikai"


def server_config_dir() -> Path:
    configured = os.environ.get("KIKAI_SERVER_CONFIG_HOME")
    if configured:
        return Path(configured)
    return DEFAULT_CONFIG_DIR


def _json_path(kind: str) -> Path:
    return server_config_dir() / f"{kind}.json"


def _load_json(kind: str) -> dict[str, str]:
    path = _json_path(kind)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data: Any = json.load(f)
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(key, str)}


def _write_json(kind: str, data: dict[str, str]) -> Path:
    directory = server_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _json_path(kind)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)
    return path


def set_server_value(kind: str, name: str, value: str) -> Path:
    data = _load_json(kind)
    data[name] = value
    return _write_json(kind, data)


def get_server_setting(name: str) -> str | None:
    value = _load_json("settings").get(name)
    if value == "":
        return None
    return value


def get_server_secret(name: str) -> str | None:
    value = _load_json("secrets").get(name)
    if value == "":
        return None
    return value


def resolve_registered_value(name: str) -> str | None:
    setting = get_server_setting(name)
    if setting is not None:
        return setting
    return get_server_secret(name)
