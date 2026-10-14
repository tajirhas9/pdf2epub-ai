"""OpenAI-compatible chat client with vision + JSON output that degrades gracefully across providers.

Works with OpenAI, Gemini's OpenAI endpoint, OpenRouter, llama.cpp, LM Studio, Ollama, vLLM…
Differences handled here:
- api key optional (local) or required (cloud) — `LLMSettings.api_key`
- structured output: json_schema -> json_object -> prompt-only, learned on first 400
- parameters some models reject (temperature) dropped on 400
- reasoning output (<think>…</think>) and markdown fences stripped before JSON parsing
- one repair round-trip when the reply is not valid JSON
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import openai
from PIL import Image

from pdf2epub.config import LLMSettings

log = logging.getLogger(__name__)

JSON_MODES = ("json_schema", "json_object", "prompt")


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


class LLMClient(Protocol):
    async def complete_json(self, system: str, text: str, images: list[ImagePart], schema: dict, name: str) -> dict: ...


def _server_message(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(e)[:300]


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


class OpenAICompatClient:
    def __init__(self, settings: LLMSettings, cache_dir: Path | None = None):
        self.s = settings
        self.cache_dir = cache_dir
        self._client = openai.AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.key_value(),
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
            default_headers=settings.extra_headers or None,
        )
        self._json_mode = settings.json_mode if settings.json_mode in JSON_MODES else JSON_MODES[0]
        self._auto = settings.json_mode == "auto"
        self._model = settings.model
        self._send_temperature = settings.temperature is not None
        self._max_tokens_field = "max_tokens"  # some providers require max_completion_tokens

    async def close(self) -> None:
        await self._client.close()

    # ---- public -------------------------------------------------------------------------

    async def complete_json(self, system: str, text: str, images: list[ImagePart], schema: dict, name: str) -> dict:
        if len(images) > self.s.max_images_per_call:
            log.warning("%s: %d images > max %d, truncating", name, len(images), self.s.max_images_per_call)
            images = images[: self.s.max_images_per_call]
        key = self._cache_key(system, text, images, schema)
        if (cached := self._cache_get(key)) is not None:
            return cached

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_content(text, images)},
        ]
        raw = await self._create(messages, schema, name)
        try:
            result = extract_json(raw)
        except ValueError:
            # One repair round-trip: show the model its reply and ask for JSON only.
            messages += [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content": "That was not a valid JSON object. Reply with only the JSON object."},
            ]
            raw = await self._create(messages, schema, name)
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
            raise LLMError(f"cannot reach the LLM at {self.s.base_url}") from e
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

    async def _create(self, messages: list[dict], schema: dict, name: str) -> str:
        model = await self._resolve_model()
        while True:
            kwargs: dict = {"model": model, "messages": messages}
            if (fmt := self._response_format(schema, name)) is not None:
                kwargs["response_format"] = fmt
            if self._json_mode == "prompt":
                kwargs["messages"] = self._with_schema_hint(messages, schema)
            if self._send_temperature:
                kwargs["temperature"] = self.s.temperature
            if self.s.max_output_tokens:
                kwargs[self._max_tokens_field] = self.s.max_output_tokens
            if self.s.extra_body:
                kwargs["extra_body"] = self.s.extra_body
            try:
                resp = await self._client.chat.completions.create(**kwargs)
            except openai.BadRequestError as e:
                if self._degrade(str(e)):
                    continue
                raise LLMError(f"{name}: request rejected by provider: {e}") from e
            except openai.AuthenticationError as e:
                raise LLMError(
                    f"{name}: authentication failed for {self.s.base_url} — set LLM_API_KEY in .env.local"
                ) from e
            except openai.APIConnectionError as e:
                raise LLMError(f"{name}: cannot reach {self.s.base_url}: {e}") from e
            choice = resp.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                raise LLMError(f"{name}: empty reply (finish_reason={choice.finish_reason})")
            if choice.finish_reason == "length":
                log.warning("%s: reply hit max_output_tokens (%d)", name, self.s.max_output_tokens)
            return content

    def _degrade(self, error: str) -> bool:
        """Drop an unsupported feature after a 400. Returns True when a retry makes sense."""
        low = error.lower()
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

    def _cache_key(self, system: str, text: str, images: list[ImagePart], schema: dict) -> str:
        h = hashlib.sha256()
        for part in (self.s.base_url, self._model or "", system, text, json.dumps(schema, sort_keys=True)):
            h.update(part.encode())
            h.update(b"\0")
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
