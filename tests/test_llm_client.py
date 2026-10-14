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
    s = LLMSettings(_env_file=None)
    assert s.key_value() == "not-needed"
    s = LLMSettings(_env_file=None, api_key="sk-secret", extra_headers='{"X-Title": "t"}')
    assert s.key_value() == "sk-secret"
    assert "sk-secret" not in s.describe() and "sk-secret" not in repr(s)
    assert s.extra_headers == {"X-Title": "t"}
