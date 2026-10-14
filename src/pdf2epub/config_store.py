"""Read/write the user config file (TOML) — used by `pdf2epub init` and `pdf2epub config`."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from pdf2epub.config import LLMSettings, Settings
from pdf2epub.paths import locations

SECRET_KEYS = {"llm.api_key"}


def config_path() -> Path:
    return locations().config_file


def read(path: Path | None = None) -> dict:
    path = path or config_path()
    return tomllib.loads(path.read_text()) if path.exists() else {}


def write(data: dict, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(data))
    os.chmod(tmp, 0o600)  # may hold an API key when no keyring is available
    tmp.replace(path)
    return path


def valid_keys() -> dict[str, Any]:
    """'section.key' -> field info, for validation and help."""
    keys = {name: f for name, f in Settings.model_fields.items() if name != "llm"}
    keys.update({f"llm.{name}": f for name, f in LLMSettings.model_fields.items()})
    return keys


def parse_value(key: str, raw: str) -> Any:
    """CLI string -> typed TOML value, using the field's annotation."""
    field = valid_keys().get(key)
    if field is None:
        raise KeyError(f"unknown setting {key!r} (see `pdf2epub config show --all`)")
    ann = str(field.annotation)
    low = raw.strip().lower()
    if "bool" in ann:
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} expects true/false")
    if "list" in ann:
        return [x.strip() for x in raw.split(",") if x.strip()]
    if "dict" in ann:
        import json

        return json.loads(raw)
    if "int" in ann and "float" not in ann:
        return int(raw)
    if "float" in ann:
        return float(raw)
    return raw


def set_value(key: str, raw: str, path: Path | None = None) -> Path:
    value = parse_value(key, raw)
    data = read(path)
    section, _, name = key.rpartition(".")
    target = data.setdefault(section, {}) if section else data
    target[name] = value
    return write(data, path)


def unset_value(key: str, path: Path | None = None) -> Path:
    data = read(path)
    section, _, name = key.rpartition(".")
    target = data.get(section, {}) if section else data
    target.pop(name, None)
    if section and not target:
        data.pop(section, None)
    return write(data, path)


def redacted(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = {kk: ("***" if f"{k}.{kk}" in SECRET_KEYS and vv else vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out
