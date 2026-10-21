"""Settings.

Precedence (highest first):
  1. CLI flags (applied by the CLI after loading)
  2. environment variables  LLM_*  /  PDF2EPUB_*
  3. .env / .env.local      (git checkout only)
  4. ./pdf2epub.toml        (per-folder overrides, current directory)
  5. user config file       (`pdf2epub config path`; written by `pdf2epub init`)
  6. defaults
The LLM API key is kept in the OS keyring (per endpoint) when available, so it never has to sit
in a config file; LLM_API_KEY or an explicit config value still take precedence.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pdf2epub.paths import locations, project_config_file

log = logging.getLogger(__name__)
LOC = locations()
PROJECT_ROOT = LOC.config_dir if LOC.checkout else LOC.cache_dir  # base of local files
KEYRING_SERVICE = "pdf2epub"

# Third-party caches (Paddle models, HF, Playwright browsers) -> our cache dir, never scattered in $HOME.
_CACHE_ENV = {
    "PADDLE_PDX_CACHE_HOME": "paddlex",
    "PLAYWRIGHT_BROWSERS_PATH": "ms-playwright",
    "HF_HOME": "hf",
}


# Third-party chatter off by default (pdf2epub -v turns it back on).
_QUIET_ENV = {
    "GLOG_minloglevel": "2",  # Paddle C++ info logs
    "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",  # model download bars (we show our own step)
    "TQDM_DISABLE": "1",
    "VITE_CONFIG_NATIVE_IGNORE_WARNING": "true",
}


def export_cache_env() -> None:
    """Must run before Paddle/HF/Playwright are imported (they read these from os.environ)."""
    for key, value in _QUIET_ENV.items():
        os.environ.setdefault(key, value)
    env = dict(_CACHE_ENV)
    if LOC.checkout:
        env["UV_CACHE_DIR"] = "uv"  # only inside the repo; never touch an installed user's uv cache
    for key, sub in env.items():
        os.environ.setdefault(key, str(LOC.cache_dir / sub))


def _dotenv() -> tuple[Path, ...] | None:
    return LOC.dotenv_files or None


class _TableSource(TomlConfigSettingsSource):
    """One [table] of a TOML file as a settings source (for nested sections like [llm])."""

    def __init__(self, settings_cls, toml_file: Path, table: str):
        self._table = table
        super().__init__(settings_cls, toml_file=toml_file)

    def _read_file(self, file_path):  # type: ignore[override]
        data = super()._read_file(file_path)
        return data.get(self._table, {}) if isinstance(data, dict) else {}


def _toml_sources(settings_cls, table: str | None = None) -> tuple[PydanticBaseSettingsSource, ...]:
    files = (project_config_file(), LOC.config_file)  # first = higher priority
    if table is None:
        return tuple(TomlConfigSettingsSource(settings_cls, toml_file=f) for f in files)
    return tuple(_TableSource(settings_cls, f, table) for f in files)


class LLMSettings(BaseSettings):
    """Any OpenAI-compatible endpoint: OpenAI, Gemini, OpenRouter, DeepSeek, Ollama, LM Studio, llama.cpp…"""

    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=_dotenv(), extra="ignore")

    base_url: str = "http://localhost:8080/v1"
    model: str = ""  # empty: the server's only/first model (local servers); cloud providers need a name
    # Optional: local servers ignore it, cloud providers require it. Never logged.
    api_key: SecretStr | None = None
    # Extra HTTP headers some gateways need, as JSON, e.g. '{"HTTP-Referer": "..."}'.
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # Provider-specific request fields as JSON, e.g. '{"reasoning_effort": "low"}' (OpenAI/Gemini)
    # or '{"chat_template_kwargs": {"enable_thinking": false}}' (llama.cpp/vLLM with Qwen).
    extra_body: dict = Field(default_factory=dict)
    timeout_s: float = 300.0
    max_output_tokens: int = 4096  # hard cap per reply: stops runaway generation on weak/local models
    max_retries: int = 4  # SDK retries 408/409/429/5xx with backoff honoring Retry-After
    concurrency: int = 2
    max_images_per_call: int = 4
    image_max_side: int = 1600
    temperature: float | None = 0.0
    # "auto" probes json_schema -> json_object -> prompt-only on first failure.
    json_mode: str = "auto"
    review_policy: str = "flagged"  # "flagged" | "all" | "none"
    # Leaner page-review requests (fewer tokens = faster on local servers, cheaper on cloud ones).
    # Defaults keep the original requests until the A/B quality check (eval/) shows no loss.
    review_payload: Literal["json", "lines"] = "json"  # page blocks as JSON objects or one line each
    review_edit_op: bool = False  # offer the find/replace "edit" op for small text fixes
    review_renders: Literal["all", "when-useful"] = "all"  # phone render on every page or only where layout matters
    review_crops: Literal["zoom", "lean"] = "zoom"  # block crops: always zoomed to 900 px, or only small ones, ≤2×
    # Model "thinking" for page review / glyph checks: "default" sends nothing (provider default); "off" / "low"
    # are mapped per provider (DeepSeek thinking switch, reasoning_effort, llama.cpp enable_thinking).
    # Thinking tokens are billed as output and are most of a thinking model's time. llm.extra_body still wins.
    reasoning: Literal["default", "off", "low"] = "default"
    glyph_reasoning: Literal["default", "off", "low"] = "default"
    # Which kind of OpenAI-compatible server this is (pdf2epub.llm.servers): decides the optional
    # server-specific fields sent. "auto" detects it from the host and the server's /props.
    server: Literal["auto", "openai", "deepseek", "llamacpp", "strata", "local"] = "auto"
    # Ask the server to keep the shared system prompt cached between requests, when it has a way
    # to be asked (llama.cpp, Strata). "off" sends no such fields.
    prompt_cache: Literal["auto", "off"] = "auto"
    enabled: bool = True

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings, env_settings, dotenv_settings, *_toml_sources(settings_cls, table="llm"))

    @field_validator("extra_headers", "extra_body", mode="before")
    @classmethod
    def _parse_json(cls, v):
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, v):
        return None if v in ("", None) else v

    @model_validator(mode="after")
    def _key_from_keyring(self):
        if self.api_key is None and (key := keyring_get(self.base_url)):
            self.api_key = SecretStr(key)
        return self

    def key_value(self) -> str:
        # The openai SDK requires a non-empty key even when the server ignores it.
        return self.api_key.get_secret_value() if self.api_key else "not-needed"

    def describe(self) -> str:
        model = self.model or "(server default)"
        return f"{model} @ {self.base_url} (api key: {'set' if self.api_key else 'none'})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PDF2EPUB_", env_file=_dotenv(), extra="ignore")

    work_dir: Path = LOC.work_dir
    out_dir: Path = LOC.out_dir
    web_dir: Path = LOC.web_dir
    cache_dir: Path = LOC.cache_dir

    vector_dpi: int = 200  # render DPI for vector pages (layout + preview)
    ocr_min_ppi: int = 220  # upscale OCR crops when the scan is below this
    workers: int = 0  # 0 = one per CPU core, capped by RAM (hardware.auto_workers)
    # Language hints, e.g. ["en", "ar"]. Empty = auto: every fallback script may be tried.
    # With hints, only the hinted scripts are tried (faster: fewer OCR models to load).
    langs: list[str] = Field(default_factory=list)
    # Scripts tried (lazily, only on low-confidence OCR lines) after the hinted ones.
    ocr_fallback_scripts: list[str] = Field(default_factory=lambda: ["arabic", "cyrillic", "devanagari", "cjk"])
    use_gpu: bool = True
    # Text-line detector: "auto" = server model on GPU, mobile model on CPU (same lines, ~16x faster);
    # or a PaddleOCR model name, e.g. "PP-OCRv5_server_det".
    ocr_det_model: str = "auto"
    # Text recognition: "auto" = Apple Vision on macOS (Neural Engine / GPU; Paddle is CPU-only there),
    # PaddleOCR elsewhere; or "apple" / "paddle". Page layout always uses Paddle.
    ocr_engine: Literal["auto", "apple", "paddle"] = "auto"

    # Text-layer quality (0..1) below which OCR is run for the page.
    text_layer_min_quality: float = 0.85
    # Running elements: fraction of pages an element must repeat on.
    running_min_repeat: float = 0.3
    # Spreads: figure must touch the gutter within this fraction of page width.
    spread_gutter_margin: float = 0.03
    spread_min_ncc: float = 0.55
    spread_max_seam_px: float = 2.0

    keep_work: bool = False
    llm: LLMSettings = Field(default_factory=LLMSettings)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings, env_settings, dotenv_settings, *_toml_sources(settings_cls))

    @field_validator("llm", mode="before")
    @classmethod
    def _llm_section(cls, v):
        # [llm] is loaded by LLMSettings itself (so LLM_* env vars beat TOML values); ignore it here.
        return LLMSettings() if isinstance(v, dict) else v

    @field_validator("work_dir", "out_dir", "cache_dir", "web_dir", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    def book_dir(self, book_id: str) -> Path:
        return self.work_dir / book_id

    def ocr_scripts(self) -> list[str]:
        from pdf2epub.textutil import LANG_SCRIPT

        if self.langs:
            return list(dict.fromkeys(LANG_SCRIPT.get(lang, "latin") for lang in self.langs))
        return list(dict.fromkeys(["latin"] + self.ocr_fallback_scripts))


# ---- API keys in the OS keyring ---------------------------------------------------------------


def _keyring():
    if os.environ.get("PDF2EPUB_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        return None if isinstance(keyring.get_keyring(), FailKeyring) else keyring
    except Exception:
        return None


def keyring_get(base_url: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, base_url)
    except Exception:
        return None


def keyring_set(base_url: str, key: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYRING_SERVICE, base_url, key)
        return True
    except Exception as e:
        log.warning("keyring unavailable: %s", e)
        return False


def keyring_delete(base_url: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, base_url)
        except Exception:
            pass


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
