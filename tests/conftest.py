import pytest


@pytest.fixture(autouse=True)
def _no_llm_waits(monkeypatch):
    """The LLM client's patient retries wait up to minutes; tests try again right away."""
    monkeypatch.setattr("pdf2epub.llm.client.TRANSIENT_WAITS", (0.0, 0.0, 0.0))
