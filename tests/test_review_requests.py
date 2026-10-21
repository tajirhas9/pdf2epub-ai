"""Page-review request building: defaults must stay byte-identical to the original requests."""

import json

from pdf2epub.config import LLMSettings
from pdf2epub.ir import BlockType, FigureLayout
from pdf2epub.review import SYSTEM, page_payload, page_text, review_system, wants_render
from tests.helpers import block, page


def _settings(**kw):
    return LLMSettings(_env_file=None, **kw)


def _page():
    return page(
        4,
        [
            block("p0005-b000", "Three Years of Secret Call", t=BlockType.HEADING, order=0, flags=["low-confidence"]),
            block("p0005-b001", "It is well-known that Makkah", order=1, bbox=(0.08, 0.3125, 0.92, 0.47)),
        ],
    )


def test_defaults_send_the_original_request():
    p = _page()
    s = _settings()
    assert review_system(s) == SYSTEM
    expected = "Current interpretation of this page:\n" + json.dumps(page_payload(p), ensure_ascii=False)
    assert page_text(p, s) == expected


def test_lean_variants():
    p = _page()
    s = _settings(review_payload="lines", review_edit_op=True)
    system = review_system(s)
    assert system != SYSTEM and '"op":"edit"' in system and "one per line" in system
    lines = page_text(p, s).splitlines()
    assert lines[0] == "Current blocks of page 5:"
    head = "p0005-b000 heading content #0 [0.1,0.2,0.9,0.3] flags=low-confidence"
    assert lines[1] == head + " | Three Years of Secret Call"
    assert lines[2] == "p0005-b001 paragraph content #1 [0.08,0.312,0.92,0.47] | It is well-known that Makkah"


def test_render_only_where_layout_matters():
    plain = _page()
    assert not wants_render(plain)
    fig = _page()
    fig.blocks.append(block("f", t=BlockType.FIGURE, layout=FigureLayout.FULL))
    assert wants_render(fig)
    missed = _page()
    missed.blocks[1].flags = ["layout-missed"]
    assert wants_render(missed)


def test_complete_all_keeps_order_and_limits_concurrency():
    import asyncio

    from pdf2epub.llm.client import LLMError, LLMUnavailable
    from pdf2epub.review import _complete_all

    class Client:
        running = peak = 0

        async def complete_json(self, system, text, images, schema, name, reasoning=None):
            Client.running += 1
            Client.peak = max(Client.peak, Client.running)
            await asyncio.sleep(0.01 * (5 - int(text)))  # later requests finish first
            Client.running -= 1
            if text == "2":
                raise LLMError("bad reply")
            if text == "9":
                raise LLMUnavailable("server gone")
            return {"n": int(text)}

    out = asyncio.run(_complete_all(Client(), [("s", str(i), [], {}, "t") for i in range(5)], limit=2))
    assert [o if isinstance(o, dict) else "error" for o in out] == [{"n": 0}, {"n": 1}, "error", {"n": 3}, {"n": 4}]
    assert Client.peak == 2
    import pytest

    with pytest.raises(LLMUnavailable):
        asyncio.run(_complete_all(Client(), [("s", str(i), [], {}, "t") for i in (1, 9, 3)], limit=3))
