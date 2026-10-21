"""Server profiles: request fields that differ between OpenAI-compatible servers.

The client speaks plain OpenAI chat completions to every server. A profile only adds the optional,
server-specific fields that make the same request cheaper or faster:
- prompt-cache hints: every page review starts with the same system prompt; servers that don't keep it
  cached between requests re-read it each time (on a slow local server that is most of a call)
- the switch for the model's "thinking" (llm.reasoning)

The profile is detected once per run (llm.server="auto") or set explicitly. Unknown servers get the
generic profile, which adds nothing a plain OpenAI-compatible endpoint wouldn't accept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

SERVERS = ("openai", "deepseek", "llamacpp", "strata", "local")


@dataclass(frozen=True)
class ServerProfile:
    name: str
    # Fields that keep the shared start of the prompt (the system message) cached between requests.
    cache_fields: dict
    # How "thinking" is switched: "effort" (reasoning_effort), "deepseek" (thinking object),
    # "template" (chat_template_kwargs.enable_thinking, llama.cpp-style chat templates).
    reasoning_style: str

    def reasoning_fields(self, mode: str) -> dict:
        """Fields for llm.reasoning = "off" / "low" ("default": none)."""
        if mode not in ("off", "low"):
            return {}
        if self.reasoning_style == "deepseek":
            if mode == "off":
                return {"thinking": {"type": "disabled"}}
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
        if self.reasoning_style == "template":  # on or off only
            return {"chat_template_kwargs": {"enable_thinking": mode != "off"}}
        return {"reasoning_effort": "minimal" if mode == "off" else "low"}


PROFILES = {
    # OpenAI, Gemini, OpenRouter, …: prefix caching (where offered) is automatic.
    "openai": ServerProfile("openai", {}, "effort"),
    # DeepSeek: automatic disk cache of repeated prefixes; its own thinking switch.
    "deepseek": ServerProfile("deepseek", {}, "deepseek"),
    # llama.cpp server: reuse the cached prompt prefix (older builds default to off).
    "llamacpp": ServerProfile("llamacpp", {"cache_prompt": True}, "template"),
    # Strata: system prompts under 2,048 tokens are only kept when the request pins them.
    "strata": ServerProfile("strata", {"strata_prefix": {"messages": 1}}, "template"),
    # Other local servers (Ollama, LM Studio, vLLM, …): nothing server-specific known.
    "local": ServerProfile("local", {}, "template"),
}


def guess(base_url: str, has_key: bool, setting: str = "auto") -> ServerProfile:
    """The profile without asking the server: llm.server when set, else from the host and the API key."""
    if setting in PROFILES:
        return PROFILES[setting]
    if urlparse(base_url).hostname == "api.deepseek.com":
        return PROFILES["deepseek"]
    return PROFILES["openai" if has_key else "local"]


async def detect(base_url: str, has_key: bool, setting: str = "auto") -> ServerProfile:
    """The profile for this endpoint: llm.server when set, else from the host and the server's /props."""
    profile = guess(base_url, has_key, setting)
    if setting in PROFILES or profile.name == "deepseek":
        return profile
    props = await _props(base_url)
    if props is not None:
        build = str(props.get("build_info", ""))
        if build.startswith("Strata"):
            return PROFILES["strata"]
        if "default_generation_settings" in props:  # llama.cpp server's /props
            return PROFILES["llamacpp"]
    return profile


async def _props(base_url: str) -> dict | None:
    """llama.cpp-family servers describe themselves at /props (next to /v1). None when absent."""
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            resp = await http.get(f"{root}/props")
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None
