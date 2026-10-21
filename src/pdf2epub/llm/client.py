"""OpenAI-compatible chat client with vision + JSON output that degrades gracefully across providers.

Works with OpenAI, Gemini's OpenAI endpoint, OpenRouter, DeepSeek, llama.cpp, LM Studio, Ollama, vLLM…
Differences handled here:
- api key optional (local) or required (cloud) — `LLMSettings.api_key`
- structured output: json_schema -> json_object -> prompt-only, learned on first 400
- parameters some models reject (temperature) dropped on 400
- reasoning output (<think>…</think>) and markdown fences stripped before JSON parsing
- one repair round-trip when the reply is not valid JSON
- one retry when the reply is empty (DeepSeek's JSON mode occasionally returns nothing)
- rate limits, timeouts and 5xx: the SDK's quick retries, then a few patient ones (TRANSIENT_WAITS)
- reply cut off at max_output_tokens: retried with a bigger budget (kept for later calls)
- request too large for the model's context / the server: retried with fewer, smaller images
- errors every later call would hit too (no image support, no credit, unknown model, bad key): LLMUnavailable,
  so a run pauses instead of failing page after page
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import openai
from PIL import Image

from pdf2epub.config import LLMSettings
from pdf2epub.llm import servers

log = logging.getLogger(__name__)

JSON_MODES = ("json_schema", "json_object", "prompt")
# Providers documented to accept only json_object structured output.
NO_JSON_SCHEMA_HOSTS = frozenset({"api.deepseek.com"})
# Seconds to wait before each extra try once the SDK's own quick retries gave up on a transient error
# (rate limit, timeout, overloaded server). A Retry-After header from the server wins when longer.
TRANSIENT_WAITS = (15.0, 30.0, 60.0)
MAX_RETRY_AFTER = 300.0
TRANSIENT_ERRORS = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
MAX_OUTPUT_CEILING = 32768  # a truncated reply doubles the output budget up to this
SHRINK_ROUNDS = 2  # a too-large request is retried this often with fewer, smaller images
_TOO_LARGE = (
    "context length", "context_length", "context window", "maximum context", "too many tokens",
    "prompt is too long", "input is too long", "reduce the length", "request too large", "payload too large",
    "too many images", "image too large",
)  # fmt: skip
_NO_IMAGES = ("image_url", "image input", "does not support image", "not support vision", "vision is not", "multimodal")
_REASONING_PARAMS = ("reasoning_effort", "thinking", "enable_thinking")
_OUTPUT_LIMIT = ("max_tokens", "max_completion_tokens", "max_output_tokens", "output tokens")


@dataclass(frozen=True)
class ImagePart:
    data: bytes  # encoded image (PNG/JPEG)
    label: str = ""

    @classmethod
    def from_pil(cls, img: Image.Image, label: str = "", max_side: int = 1600) -> ImagePart:
        img = img.convert("RGB")
        scale = max_side / max(img.size)
        if scale < 1:
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return cls(buf.getvalue(), label)

    @classmethod
    def from_path(cls, path: Path, label: str = "", max_side: int = 1600) -> ImagePart:
        with Image.open(path) as im:
            return cls.from_pil(im, label, max_side)


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """The server is down or not answering (after the SDK's retries): pause, don't fail the page."""


class _TooLarge(Exception):
    """The request doesn't fit the model or the server: send fewer / smaller images."""


class LLMClient(Protocol):
    async def complete_json(
        self, system: str, text: str, images: list[ImagePart], schema: dict, name: str, reasoning: str | None = None
    ) -> dict: ...


def _server_message(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(e)[:300]


def _shrink(images: list[ImagePart]) -> list[ImagePart]:
    """Fewer, smaller images: keep the first two (the page scan comes first), at 70% of the size."""
    out = []
    for img in images[:2]:
        with Image.open(io.BytesIO(img.data)) as im:
            out.append(ImagePart.from_pil(im, img.label, max_side=max(256, round(max(im.size) * 0.7))))
    return out


def _retry_after(e: Exception) -> float | None:
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        return min(float(headers.get("retry-after")), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return None


def extract_json(raw: str) -> dict:
    """Parse the first JSON object in a model reply, tolerating think-blocks and fences."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.M).strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned, m.start())
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object found in model reply")


USAGE_FIELDS = (
    "requests", "cached_replies", "retries", "prompt_tokens", "completion_tokens", "reasoning_tokens",
    "cached_prompt_tokens", "prompt_ms", "predicted_ms", "seconds",
)  # fmt: skip


def _usage_record(resp, seconds: float) -> dict:
    """Token counts and server timings of one reply. Fields a provider doesn't report are left out."""
    rec: dict = {"seconds": round(seconds, 2)}
    usage = getattr(resp, "usage", None)
    if usage is not None:
        rec["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
        rec["completion_tokens"] = getattr(usage, "completion_tokens", None)
        details = getattr(usage, "completion_tokens_details", None)
        rec["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        extra = getattr(usage, "model_extra", None) or {}
        rec["cached_prompt_tokens"] = extra.get("prompt_cache_hit_tokens", cached)  # DeepSeek names it differently
    timings = (getattr(resp, "model_extra", None) or {}).get("timings")  # llama.cpp: where the time went
    if isinstance(timings, dict):
        rec["prompt_ms"] = timings.get("prompt_ms")
        rec["predicted_ms"] = timings.get("predicted_ms")
        if timings.get("cache_n") is not None:
            rec["cached_prompt_tokens"] = timings["cache_n"]
    return {k: v for k, v in rec.items() if isinstance(v, (int, float))}


class OpenAICompatClient:
    def __init__(self, settings: LLMSettings, cache_dir: Path | None = None, usage_log: Path | None = None):
        self.s = settings
        self.cache_dir = cache_dir
        self.usage_log = usage_log  # one JSON line per request (pdf2epub.llm.client._usage_record)
        self.usage: dict[str, dict[str, float]] = {}  # call name -> totals of USAGE_FIELDS
        self._cache_miss_warned = False
        self._client = openai.AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.key_value(),
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
            default_headers=settings.extra_headers or None,
        )
        self._json_mode = settings.json_mode if settings.json_mode in JSON_MODES else JSON_MODES[0]
        self._auto = settings.json_mode == "auto"
        if self._auto and urlparse(settings.base_url).hostname in NO_JSON_SCHEMA_HOSTS:
            self._json_mode = "json_object"  # skip the json_schema request that is known to be rejected
        self._model = settings.model
        self._send_reasoning = True  # dropped when the provider rejects the fields
        self._send_cache_hints = settings.prompt_cache != "off"  # dropped when the provider rejects them
        self._server: servers.ServerProfile | None = None  # detected on the first request (servers.detect)
        self._send_temperature = settings.temperature is not None
        self._max_tokens_field = "max_tokens"  # some providers require max_completion_tokens
        self._max_out = settings.max_output_tokens  # grows when replies get cut off (_grow_budget)
        self._out_ceiling = max(settings.max_output_tokens, MAX_OUTPUT_CEILING)

    async def close(self) -> None:
        await self._client.close()

    # ---- public -------------------------------------------------------------------------

    async def complete_json(
        self, system: str, text: str, images: list[ImagePart], schema: dict, name: str, reasoning: str | None = None
    ) -> dict:
        mode = reasoning or self.s.reasoning
        if len(images) > self.s.max_images_per_call:
            log.warning("%s: %d images > max %d, truncating", name, len(images), self.s.max_images_per_call)
            images = images[: self.s.max_images_per_call]
        key = self._cache_key(system, text, images, schema, mode)
        if (cached := self._cache_get(key)) is not None:
            self._totals(name)["cached_replies"] += 1
            return cached

        for shrink in range(SHRINK_ROUNDS + 1):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": self._user_content(text, images)},
            ]
            try:
                raw = await self._create(messages, schema, name, mode)
                break
            except _TooLarge as e:
                if shrink == SHRINK_ROUNDS or not images:
                    raise LLMError(f"{name}: request too large for the model even with smaller images: {e}") from e
                images = _shrink(images)
                log.warning("%s: request too large (%s); retrying with %d smaller image(s)", name, e, len(images))
        try:
            result = extract_json(raw)
        except ValueError:
            # One repair round-trip: show the model its reply and ask for JSON only.
            messages += [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content": "That was not a valid JSON object. Reply with only the JSON object."},
            ]
            try:
                raw = await self._create(messages, schema, name, mode)
            except _TooLarge as e:
                raise LLMError(f"{name}: model did not return JSON (repair request too large: {e})") from e
            try:
                result = extract_json(raw)
            except ValueError as e:
                raise LLMError(f"{name}: model did not return JSON") from e
        self._cache_put(key, result)
        return result

    # ---- request building ---------------------------------------------------------------

    def _user_content(self, text: str, images: list[ImagePart]) -> list[dict]:
        parts: list[dict] = []
        for img in images:
            if img.label:
                parts.append({"type": "text", "text": f"[image: {img.label}]"})
            b64 = base64.b64encode(img.data).decode()
            mime = "image/png" if img.data[:4] == b"\x89PNG" else "image/jpeg"
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        parts.append({"type": "text", "text": text})
        return parts

    def _response_format(self, schema: dict, name: str) -> dict | None:
        if self._json_mode == "json_schema":
            return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
        if self._json_mode == "json_object":
            return {"type": "json_object"}
        return None

    async def ping(self) -> None:
        """Fail fast (once) when the endpoint is unreachable or rejects the key."""
        try:
            await self._client.with_options(timeout=5, max_retries=0).models.list()
        except openai.AuthenticationError as e:
            raise LLMError(
                f"{self.s.base_url} rejected the API key — set it with `pdf2epub config set llm.api_key`"
            ) from e
        except openai.APIConnectionError as e:
            raise LLMUnavailable(f"cannot reach the LLM at {self.s.base_url}") from e
        except openai.PermissionDeniedError as e:
            raise LLMError(f"{self.s.base_url} refused the request (403): {_server_message(e)}") from e
        except openai.APIStatusError:
            pass  # some servers don't implement /models; real requests will tell

    async def _resolve_model(self) -> str:
        """Blank model setting: use the server's model when it offers exactly one (typical local server)."""
        if self._model:
            return self._model
        try:
            ids = [m.id async for m in self._client.models.list()]
        except Exception as e:
            raise LLMError(f"no model configured and {self.s.base_url} can't list models: {e}") from e
        if len(ids) != 1:
            raise LLMError(
                f"no model configured; {self.s.base_url} offers {len(ids)} models — "
                "pick one with `pdf2epub config set llm.model <id>`"
            )
        self._model = ids[0]
        return self._model

    async def _create(self, messages: list[dict], schema: dict, name: str, reasoning: str = "default") -> str:
        model = await self._resolve_model()
        await self._detect_server()
        empty_retry = True
        waits = list(TRANSIENT_WAITS)
        attempt = 0
        while True:
            attempt += 1
            budget = self._max_out
            kwargs: dict = {"model": model, "messages": messages}
            if (fmt := self._response_format(schema, name)) is not None:
                kwargs["response_format"] = fmt
            if self._json_mode != "json_schema":  # json_object only forces JSON; the model still needs the shape
                kwargs["messages"] = self._with_schema_hint(messages, schema)
            if self._send_temperature:
                kwargs["temperature"] = self.s.temperature
            if budget:
                kwargs[self._max_tokens_field] = budget
            if body := {**self._cache_hints(), **self._reasoning_body(reasoning), **self.s.extra_body}:
                kwargs["extra_body"] = body
            try:
                t0 = time.perf_counter()
                resp = await self._client.chat.completions.create(**kwargs)
                self._record(name, _usage_record(resp, time.perf_counter() - t0), retry=attempt > 1)
            except TRANSIENT_ERRORS as e:
                if waits:
                    wait = max(waits.pop(0), _retry_after(e) or 0)
                    log.warning("%s: %s; trying again in %.0fs", name, _server_message(e), wait)
                    await asyncio.sleep(wait)
                    continue
                if isinstance(e, openai.RateLimitError):  # still limited: pause, resume later
                    raise LLMUnavailable(f"{name}: {self.s.base_url} rate limit: {_server_message(e)}") from e
                if isinstance(e, openai.APIConnectionError):  # incl. timeouts
                    raise LLMUnavailable(f"{name}: cannot reach {self.s.base_url}: {e}") from e
                raise LLMUnavailable(f"{name}: {self.s.base_url} is not answering: {_server_message(e)}") from e
            except openai.BadRequestError as e:
                if self._adjust(_server_message(e), budget, name, reasoning):
                    continue
                raise LLMError(f"{name}: request rejected by provider: {_server_message(e)}") from e
            except openai.AuthenticationError as e:
                raise LLMUnavailable(
                    f"{name}: authentication failed for {self.s.base_url} — check the API key in Settings"
                ) from e
            except openai.APIStatusError as e:
                msg = _server_message(e)
                if e.status_code == 413:
                    raise _TooLarge(msg) from e
                if e.status_code == 402:
                    raise LLMUnavailable(f"{name}: {self.s.base_url} needs payment / credit: {msg}") from e
                if e.status_code == 403:
                    raise LLMUnavailable(f"{name}: {self.s.base_url} refused the request (403): {msg}") from e
                if e.status_code == 404:
                    raise LLMUnavailable(f"{name}: model {model!r} not found at {self.s.base_url}: {msg}") from e
                if e.status_code == 422 and self._adjust(msg, budget, name, reasoning):
                    continue
                raise LLMError(f"{name}: provider error {e.status_code}: {msg}") from e
            choice = resp.choices[0]
            content = choice.message.content or ""
            if choice.finish_reason == "length" and self._grow_budget(budget, name):
                continue  # cut off (often mid-JSON): ask again with room for the whole reply
            if not content.strip():
                if empty_retry and choice.finish_reason != "length":
                    empty_retry = False
                    log.info("%s: empty reply (finish_reason=%s); retrying once", name, choice.finish_reason)
                    continue
                raise LLMError(f"{name}: empty reply (finish_reason={choice.finish_reason})")
            if choice.finish_reason == "length":
                log.warning("%s: reply hit max_output_tokens (%d)", name, budget)
            return content

    # ---- usage -------------------------------------------------------------------------

    def _totals(self, name: str) -> dict[str, float]:
        return self.usage.setdefault(name, dict.fromkeys(USAGE_FIELDS, 0))

    def _record(self, name: str, rec: dict, retry: bool) -> None:
        totals = self._totals(name)
        totals["requests"] += 1
        totals["retries"] += retry
        for k, v in rec.items():
            totals[k] += v
        if self.usage_log is not None:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_log.open("a") as f:
                f.write(json.dumps({"call": name, "retry": retry, **rec}) + "\n")
        # A local server recomputing the same system prompt every request: by far the slowest part of a call.
        if totals["requests"] == 3 and not self._cache_miss_warned and "prompt_ms" in rec:
            if not totals["cached_prompt_tokens"]:
                self._cache_miss_warned = True
                log.warning(
                    "%s reuses no prompt cache between requests: every call re-reads the whole prompt, "
                    "including the system prompt that is the same for every page. Check the server's "
                    "prompt-cache options (server-specific request fields go in llm.extra_body)",
                    self.s.base_url,
                )

    def usage_summary(self) -> str:
        """One line: requests, tokens and time per call name (empty when nothing was sent)."""
        parts = []
        for name, t in self.usage.items():
            if not t["requests"]:
                continue
            bits = [f"{t['requests']:.0f} requests", f"{t['prompt_tokens']:.0f} in"]
            bits.append(f"{t['completion_tokens']:.0f} out")
            if t["reasoning_tokens"]:
                bits.append(f"{t['reasoning_tokens']:.0f} reasoning")
            if t["cached_prompt_tokens"]:
                bits.append(f"{t['cached_prompt_tokens']:.0f} cached")
            if t["retries"]:
                bits.append(f"{t['retries']:.0f} retries")
            bits.append(f"{t['seconds'] / t['requests']:.1f}s/request")
            parts.append(f"{name}: " + ", ".join(bits))
        return "; ".join(parts)

    def _grow_budget(self, budget: int, name: str) -> bool:
        """A reply hit the output cap: double it (up to the ceiling) for this and later calls."""
        if not budget or budget >= self._out_ceiling:
            return False
        self._max_out = min(budget * 2, self._out_ceiling)
        log.warning("%s: reply cut off at %d tokens; retrying with %d", name, budget, self._max_out)
        return True

    def _adjust(self, error: str, budget: int, name: str, reasoning: str = "default") -> bool:
        """Change the request after a 400/422. True: retry. Raises when the request must change shape."""
        low = error.lower()
        if any(k in low for k in _TOO_LARGE):
            raise _TooLarge(error)
        if any(k in low for k in _NO_IMAGES):
            raise LLMUnavailable(
                f"{name}: model {self._model!r} doesn't accept images — pick a vision model in Settings ({error})"
            )
        if any(k in low for k in _OUTPUT_LIMIT) and budget > self.s.max_output_tokens:
            # our grown budget is over the model's limit: settle between the two
            self._out_ceiling = self._max_out = max(self.s.max_output_tokens, (budget * 3) // 4)
            log.info("%s: output budget %d too high for the model; using %d", name, budget, self._max_out)
            return True
        return self._degrade(error, reasoning)

    async def _detect_server(self) -> servers.ServerProfile:
        if self._server is None:
            self._server = await servers.detect(self.s.base_url, bool(self.s.api_key), self.s.server)
            log.info("%s: server profile %s", self.s.base_url, self._server.name)
        return self._server

    def _profile(self) -> servers.ServerProfile:
        return self._server or servers.guess(self.s.base_url, bool(self.s.api_key), self.s.server)

    def _cache_hints(self) -> dict:
        """The server's prompt-cache fields (llm.prompt_cache; none for plain OpenAI-compatible servers)."""
        return self._profile().cache_fields if self._send_cache_hints else {}

    def _reasoning_body(self, mode: str) -> dict:
        """The server's fields for llm.reasoning / llm.glyph_reasoning ("default": none)."""
        return self._profile().reasoning_fields(mode) if self._send_reasoning else {}

    def _degrade(self, error: str, reasoning: str = "default") -> bool:
        """Drop an unsupported feature after a 400. Returns True when a retry makes sense."""
        low = error.lower()
        if self._send_reasoning and self._reasoning_body(reasoning) and any(k in low for k in _REASONING_PARAMS):
            self._send_reasoning = False
            log.info("provider rejected the reasoning setting; retrying without it")
            return True
        if self._send_cache_hints and any(k.lower() in low for k in self._cache_hints()):
            self._send_cache_hints = False
            log.info("provider rejected the prompt-cache fields; retrying without them")
            return True
        if self._max_tokens_field == "max_tokens" and "max_tokens" in low and "max_completion_tokens" in low:
            self._max_tokens_field = "max_completion_tokens"
            return True
        if self._send_temperature and "temperature" in low:
            self._send_temperature = False
            log.info("provider rejected temperature; retrying without it")
            return True
        if self._auto and self._json_mode != "prompt":
            nxt = JSON_MODES[JSON_MODES.index(self._json_mode) + 1]
            log.info("provider rejected %s; falling back to %s", self._json_mode, nxt)
            self._json_mode = nxt
            return True
        return False

    @staticmethod
    def _with_schema_hint(messages: list[dict], schema: dict) -> list[dict]:
        hint = (
            "\n\nRespond with a single JSON object (no prose, no markdown) matching this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        out = [dict(m) for m in messages]
        out[0]["content"] = out[0]["content"] + hint
        return out

    # ---- cache --------------------------------------------------------------------------

    def _cache_key(
        self, system: str, text: str, images: list[ImagePart], schema: dict, reasoning: str = "default"
    ) -> str:
        h = hashlib.sha256()
        for part in (self.s.base_url, self._model or "", system, text, json.dumps(schema, sort_keys=True)):
            h.update(part.encode())
            h.update(b"\0")
        if reasoning != "default":  # keys of the default requests stay as they were
            h.update(f"reasoning={reasoning}".encode())
        for img in images:
            h.update(hashlib.sha256(img.data).digest())
        return h.hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _cache_put(self, key: str, value: dict) -> None:
        if not self.cache_dir:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False))
