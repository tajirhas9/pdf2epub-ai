import pytest

from pdf2epub.config import LLMSettings
from pdf2epub.llm.client import extract_json


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('<think>hmm {"no"}</think>\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Sure! Here it is: {"ops": [], "verdict": "ok"} hope that helps') == {
        "ops": [],
        "verdict": "ok",
    }
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_api_key_optional_and_secret(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # ignore the developer's own config file and keyring
    monkeypatch.setattr("pdf2epub.config._toml_sources", lambda *a, **k: ())
    monkeypatch.setenv("PDF2EPUB_NO_KEYRING", "1")
    s = LLMSettings(_env_file=None)
    assert s.key_value() == "not-needed"
    s = LLMSettings(_env_file=None, api_key="sk-secret", extra_headers='{"X-Title": "t"}')
    assert s.key_value() == "sk-secret"
    assert "sk-secret" not in s.describe() and "sk-secret" not in repr(s)
    assert s.extra_headers == {"X-Title": "t"}


def _reply(content: str, finish_reason: str = "stop"):
    from types import SimpleNamespace

    msg = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)])


def _client_with_replies(replies: list, **settings):
    from pdf2epub.llm.client import OpenAICompatClient

    client = OpenAICompatClient(LLMSettings(_env_file=None, model="m", **settings))
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return replies.pop(0)

    client._client.chat.completions.create = create
    return client, calls


def test_empty_reply_retried_once():
    import asyncio

    client, calls = _client_with_replies([_reply(""), _reply('{"a": 1}')])
    assert asyncio.run(client.complete_json("sys", "json please", [], {"type": "object"}, "t")) == {"a": 1}
    assert len(calls) == 2


def test_empty_reply_twice_fails():
    import asyncio

    from pdf2epub.llm.client import LLMError

    client, calls = _client_with_replies([_reply(""), _reply("")])
    with pytest.raises(LLMError, match="empty reply"):
        asyncio.run(client.complete_json("sys", "json please", [], {"type": "object"}, "t"))
    assert len(calls) == 2


def test_deepseek_starts_with_json_object():
    from pdf2epub.llm.client import OpenAICompatClient

    ds = OpenAICompatClient(LLMSettings(_env_file=None, base_url="https://api.deepseek.com", api_key="k"))
    assert ds._json_mode == "json_object"
    other = OpenAICompatClient(LLMSettings(_env_file=None, base_url="https://api.openai.com/v1", api_key="k"))
    assert other._json_mode == "json_schema"
    pinned = OpenAICompatClient(
        LLMSettings(_env_file=None, base_url="https://api.deepseek.com", api_key="k", json_mode="prompt")
    )
    assert pinned._json_mode == "prompt"


def test_deepseek_provider_preset():
    from pdf2epub.setup_wizard import PROVIDERS

    p = PROVIDERS["deepseek"]
    assert p.base_url == "https://api.deepseek.com" and p.needs_key
    assert p.default_model == "deepseek-flash"


def test_json_object_mode_sends_schema_hint():
    import asyncio

    client, calls = _client_with_replies([_reply('{"color": "red"}')], json_mode="json_object")
    schema = {"type": "object", "properties": {"color": {"type": "string"}}}
    asyncio.run(client.complete_json("sys", "json please", [], schema, "t"))
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert '"color"' in calls[0]["messages"][0]["content"]


def _status_error(cls, status: int, message: str, headers: dict | None = None):
    import httpx

    req = httpx.Request("POST", "http://x/v1/chat/completions")
    resp = httpx.Response(status, request=req, headers=headers or {}, json={"error": {"message": message}})
    return cls(message, response=resp, body={"error": {"message": message}})


def _client_with(outcomes: list, **settings):
    """Like _client_with_replies, but an outcome that is an exception is raised."""
    from pdf2epub.llm.client import OpenAICompatClient

    client = OpenAICompatClient(LLMSettings(_env_file=None, model="m", **settings))
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    client._client.chat.completions.create = create
    return client, calls


def _ask(client, images=()):
    import asyncio

    return asyncio.run(client.complete_json("sys", "json please", list(images), {"type": "object"}, "t"))


def test_rate_limit_is_retried_after_a_wait():
    import openai

    limited = _status_error(openai.RateLimitError, 429, "slow down", {"retry-after": "0"})
    client, calls = _client_with([limited, limited, _reply('{"a": 1}')])
    assert _ask(client) == {"a": 1}
    assert len(calls) == 3


def test_rate_limit_that_lasts_pauses():
    import openai

    from pdf2epub.llm.client import LLMUnavailable

    limited = _status_error(openai.RateLimitError, 429, "slow down")
    client, calls = _client_with([limited] * 4)
    with pytest.raises(LLMUnavailable, match="rate limit"):
        _ask(client)
    assert len(calls) == 4  # first try + TRANSIENT_WAITS


def test_cut_off_reply_is_retried_with_a_bigger_budget():
    client, calls = _client_with([_reply('{"ops": [{"te', "length"), _reply('{"a": 1}')], max_output_tokens=4096)
    assert _ask(client) == {"a": 1}
    assert [c["max_tokens"] for c in calls] == [4096, 8192]
    assert client._max_out == 8192  # kept for the next call


def test_grown_budget_over_the_model_limit_settles_lower():
    import openai

    too_big = _status_error(openai.BadRequestError, 400, "Invalid max_tokens value, the valid range is [1, 8192]")
    client, calls = _client_with([_reply("{", "length"), too_big, _reply('{"a": 1}')], max_output_tokens=4096)
    assert _ask(client) == {"a": 1}
    assert [c["max_tokens"] for c in calls] == [4096, 8192, 6144]


def test_too_large_request_retries_with_smaller_images():
    import openai
    from PIL import Image

    from pdf2epub.llm.client import ImagePart

    imgs = [ImagePart.from_pil(Image.new("RGB", (1000, 800)), f"i{n}") for n in range(3)]
    too_large = _status_error(openai.BadRequestError, 400, "This model's maximum context length is 8192 tokens")
    client, calls = _client_with([too_large, _reply('{"a": 1}')])
    assert _ask(client, imgs) == {"a": 1}
    sent = [p for p in calls[1]["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(sent) == 2


def test_model_without_images_pauses():
    import openai
    from PIL import Image

    from pdf2epub.llm.client import ImagePart, LLMUnavailable

    err = _status_error(openai.BadRequestError, 400, "unknown variant `image_url`, expected `text`")
    client, _ = _client_with([err])
    with pytest.raises(LLMUnavailable, match="doesn't accept images"):
        _ask(client, [ImagePart.from_pil(Image.new("RGB", (10, 10)))])


def test_no_credit_pauses_and_odd_status_fails_the_page():
    import openai

    from pdf2epub.llm.client import LLMError, LLMUnavailable

    client, _ = _client_with([_status_error(openai.APIStatusError, 402, "Insufficient Balance")])
    with pytest.raises(LLMUnavailable, match="payment"):
        _ask(client)
    client, _ = _client_with([_status_error(openai.APIStatusError, 418, "teapot")])
    with pytest.raises(LLMError, match="418") as e:
        _ask(client)
    assert not isinstance(e.value, LLMUnavailable)
