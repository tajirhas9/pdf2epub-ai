"""Settings and first-run setup for the web UI (same config file and keyring as `pdf2epub init`/`config`)."""

from __future__ import annotations

import os
import shutil
from functools import cache
from typing import Any

from pydantic import TypeAdapter, ValidationError

from pdf2epub import config_store
from pdf2epub.config import LLMSettings, Settings, keyring_delete, keyring_get, keyring_set
from pdf2epub.paths import locations

# Settings the UI edits (dotted keys as in `pdf2epub config set`).
EDITABLE = (
    "llm.enabled",
    "llm.base_url",
    "llm.model",
    "llm.review_policy",
    "llm.concurrency",
    "llm.max_output_tokens",
    "llm.timeout_s",
    "llm.extra_body",
    "llm.extra_headers",
    "langs",
    "out_dir",
    "keep_work",
    "use_gpu",
    "ocr_det_model",
    "vector_dpi",
)

LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "tr": "Turkish", "id": "Indonesian", "ms": "Malay", "pl": "Polish", "sw": "Swahili",
    "ar": "Arabic", "fa": "Persian", "ur": "Urdu", "ps": "Pashto", "ug": "Uyghur", "sd": "Sindhi",
    "he": "Hebrew", "yi": "Yiddish", "ru": "Russian", "uk": "Ukrainian", "bg": "Bulgarian", "sr": "Serbian",
    "el": "Greek", "hi": "Hindi", "mr": "Marathi", "ne": "Nepali", "th": "Thai", "ta": "Tamil",
    "te": "Telugu", "ko": "Korean", "zh": "Chinese", "ja": "Japanese",
}  # fmt: skip


def _field(key: str):
    fields = config_store.valid_keys()
    if key not in fields:
        raise KeyError(f"unknown setting {key!r}")
    return fields[key]


def _dotenv_keys() -> set[str]:
    keys: set[str] = set()
    for f in locations().dotenv_files:
        if f.exists():
            for line in f.read_text().splitlines():
                name, sep, _ = line.strip().partition("=")
                if sep and not name.startswith("#"):
                    keys.add(name.strip().upper())
    return keys


def env_overrides() -> list[str]:
    """Editable settings currently pinned by environment variables / .env (they beat the config file)."""
    present = {k.upper() for k in os.environ} | _dotenv_keys()
    out = []
    for key in (*EDITABLE, "llm.api_key"):
        section, _, name = key.rpartition(".")
        env = f"LLM_{name}" if section == "llm" else f"PDF2EPUB_{name}"
        if env.upper() in present:
            out.append(key)
    return out


def _keyring_available() -> bool:
    from pdf2epub.config import _keyring

    return _keyring() is not None


def state() -> dict:
    from pdf2epub.setup_wizard import PROVIDERS
    from pdf2epub.textutil import LANG_SCRIPT

    s = Settings()
    values: dict[str, Any] = {}
    for key in EDITABLE:
        obj, name = (s.llm, key[4:]) if key.startswith("llm.") else (s, key)
        v = getattr(obj, name)
        values[key] = str(v) if hasattr(v, "is_absolute") else v
    codes = list(dict.fromkeys([*LANGUAGE_NAMES, *LANG_SCRIPT]))
    loc = locations()
    return {
        "configured": config_store.config_path().exists(),
        "config_path": str(config_store.config_path()),
        "layout": "checkout" if loc.checkout else "installed",
        "values": values,
        "api_key_set": s.llm.api_key is not None,
        "keyring": _keyring_available(),
        "env_overrides": env_overrides(),
        "providers": [
            {"id": k, "label": p.label, "base_url": p.base_url, "needs_key": p.needs_key, "hint": p.hint}
            for k, p in PROVIDERS.items()
        ],
        "languages": [{"code": c, "name": LANGUAGE_NAMES.get(c, c)} for c in codes],
    }


def _validated(key: str, value: Any) -> Any:
    """JSON value from the browser -> TOML-ready value, checked against the setting's type."""
    field = _field(key)
    try:
        typed = TypeAdapter(field.annotation).validate_python(value)
    except ValidationError as e:
        raise ValueError(f"{key}: {e.errors()[0]['msg']}") from None
    return TypeAdapter(field.annotation).dump_python(typed, mode="json")


def save(changes: dict) -> dict:
    """{"set": {key: value}, "unset": [key], "api_key": str, "clear_api_key": bool} -> new state."""
    data = config_store.read()
    for key, value in (changes.get("set") or {}).items():
        if key not in EDITABLE:
            raise KeyError(f"{key!r} can't be changed here")
        section, _, name = key.rpartition(".")
        target = data.setdefault(section, {}) if section else data
        if value is None or value == "" or value == [] or value == {}:  # empty -> default
            target.pop(name, None)
        else:
            target[name] = _validated(key, value)
    for key in changes.get("unset") or []:
        _field(key)
        section, _, name = key.rpartition(".")
        (data.get(section, {}) if section else data).pop(name, None)
    data = {k: v for k, v in data.items() if v != {}}

    base_url = data.get("llm", {}).get("base_url") or LLMSettings.model_fields["base_url"].default
    if changes.get("clear_api_key"):
        keyring_delete(base_url)
        data.get("llm", {}).pop("api_key", None)
    key = (changes.get("api_key") or "").strip()
    if key:
        if keyring_set(base_url, key):
            data.get("llm", {}).pop("api_key", None)
        else:  # no OS keyring: file fallback (written with mode 600)
            data.setdefault("llm", {})["api_key"] = key
    config_store.write(data)
    return state()


def llm_settings(fields: dict) -> LLMSettings:
    """LLM settings for a test/listing: unsaved form values, falling back to the stored key."""
    allowed = {k: v for k, v in fields.items() if k in ("base_url", "model", "extra_body", "extra_headers")}
    if not allowed.get("model"):
        allowed.pop("model", None)
    key = (fields.get("api_key") or "").strip()
    if not key:
        current = Settings().llm
        base = allowed.get("base_url", current.base_url)
        if base == current.base_url and current.api_key:
            key = current.api_key.get_secret_value()
        else:
            key = keyring_get(base) or ""
    # a short timeout: this is an interactive check
    return LLMSettings(**allowed, api_key=key or None, timeout_s=60, max_retries=0)


def list_models(fields: dict) -> list[str]:
    from pdf2epub.llm.probe import list_models as _list

    return _list(llm_settings(fields))


def check_llm(fields: dict) -> dict:
    import asyncio

    from rich.text import Text

    from pdf2epub.llm.probe import probe

    try:
        out = asyncio.run(probe(llm_settings(fields)))
    except Exception as e:  # noqa: BLE001 - reported in the form
        return {"level": "error", "message": f"{type(e).__name__}: {e}"}
    text = Text.from_markup(out).plain
    level = "ok" if text.startswith("✓") else "warn" if text.startswith("!") else "error"
    return {"level": level, "message": text.lstrip("✓✗! ").strip()}


@cache
def _device() -> dict:
    try:
        import paddle  # noqa: F401
    except ImportError:
        return {"paddle": False, "gpu": False}
    from pdf2epub.engines import _device as device

    return {"paddle": True, "gpu": device(True).startswith("gpu")}


def system() -> dict:
    """What the conversion needs, for the setup checklist."""
    from pdf2epub.runtime import NODE_MIN, node_version

    if not _device().get("paddle"):
        _device.cache_clear()  # "Check again" after installing it
    s = Settings()
    node = node_version()
    models = (s.cache_dir / "paddlex" / "official_models" / "PP-DocLayout_plus-L").exists()
    return {
        "node": ".".join(map(str, node)) if node else None,
        "node_ok": bool(node and node >= NODE_MIN),
        "node_min": ".".join(map(str, NODE_MIN)),
        "java": shutil.which("java") is not None,
        "models": models,
        **_device(),
    }
