import sys

import numpy as np
import pytest

from pdf2epub import engines
from pdf2epub.config import Settings
from pdf2epub.engines import AppleVisionOcr, PaddleOcr, make_ocr, resolve_ocr_engine, vision_box, vision_langs

MAC_LANGS = frozenset({"en-US", "fr-FR", "de-DE", "ru-RU", "uk-UA", "zh-Hans", "ja-JP", "ar-SA"})


@pytest.fixture
def vision(monkeypatch):
    """Pretend Apple Vision is (or isn't) available, independent of the machine running the tests."""

    def set_langs(langs=MAC_LANGS):
        monkeypatch.setattr(engines, "_vision_languages", lambda: frozenset(langs))

    return set_langs


def test_auto_picks_apple_vision_only_where_it_exists(vision):
    vision()
    assert resolve_ocr_engine("auto") == "apple"
    assert resolve_ocr_engine("apple") == "apple"
    assert resolve_ocr_engine("paddle") == "paddle"
    vision(())  # Windows, Linux, or a Mac without the Vision framework
    assert resolve_ocr_engine("auto") == "paddle"
    assert resolve_ocr_engine("apple") == "paddle"


def test_make_ocr_follows_the_setting(vision):
    vision()
    assert isinstance(make_ocr(Settings(ocr_engine="auto")), AppleVisionOcr)
    assert isinstance(make_ocr(Settings(ocr_engine="paddle")), PaddleOcr)
    vision(())
    assert isinstance(make_ocr(Settings(ocr_engine="auto")), PaddleOcr)


def test_vision_box_flips_to_top_left_pixels():
    # Vision: normalized, origin bottom-left. A box in the top-left quarter of a 200x100 image:
    assert vision_box(((0.0, 0.5), (0.5, 0.5)), 200, 100) == (0.0, 0.0, 100.0, 50.0)
    assert vision_box(((0.25, 0.0), (0.5, 0.25)), 200, 100) == (50.0, 75.0, 150.0, 100.0)


def test_vision_langs_filter_by_support_and_prefer_the_book_language(vision):
    vision()
    assert vision_langs("latin")[:3] == ["en-US", "fr-FR", "de-DE"]
    assert vision_langs("latin", ("de",))[0] == "de-DE"
    assert vision_langs("cyrillic") == ["ru-RU", "uk-UA"]
    assert vision_langs("devanagari") == []


def test_unsupported_scripts_go_to_paddle(vision, monkeypatch):
    vision()

    class FakePaddle:
        calls: list[str] = []

        def recognize(self, crops, script):
            self.calls.append(script)
            return [("नमस्ते", 0.9)] * len(crops)

    fake = FakePaddle()
    monkeypatch.setattr(engines, "_vision_read", lambda *a: pytest.fail("Vision can't read Devanagari"))
    ocr = AppleVisionOcr(fallback=lambda: fake)
    crop = np.full((20, 60, 3), 255, np.uint8)
    assert ocr.recognize([crop], "devanagari") == [("नमस्ते", 0.9)]
    assert fake.calls == ["devanagari"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS only")
def test_apple_vision_reads_a_rendered_line():
    from PIL import Image, ImageDraw, ImageFont

    if not engines.apple_vision_available():
        pytest.skip("Vision framework unavailable")
    img = Image.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 48)
    except OSError:
        font = ImageFont.load_default(48)
    ImageDraw.Draw(img).text((30, 50), "Hello World 123", fill="black", font=font)
    page = np.asarray(img)
    ocr = AppleVisionOcr(["en"])

    [(box, text, score)] = ocr.read_lines(page, "latin")
    assert text == "Hello World 123" and score > 0.3
    x0, y0, x1, y1 = box
    assert 20 < x0 < 60 and 40 < y0 < 80 and y1 < 130  # top-left pixel coordinates

    [(text, _)] = ocr.recognize([page[int(y0) : int(y1), int(x0) : int(x1)]], "latin")
    assert text == "Hello World 123"


def test_uncovered_finds_what_the_accurate_pass_skipped():
    from pdf2epub.engines import _uncovered

    # fast pass saw a whole TOC row; accurate pass only its title (the leaders + page number are missing)
    fast = [(90.0, 160.0, 900.0, 190.0), (90.0, 210.0, 380.0, 240.0)]
    accurate = [(89.0, 161.0, 430.0, 194.0), (89.0, 210.0, 360.0, 242.0)]
    assert _uncovered(fast, accurate) == [(430.0, 160.0, 900.0, 190.0)]  # second row: 20px gap < line height
    assert _uncovered(fast, []) == fast
