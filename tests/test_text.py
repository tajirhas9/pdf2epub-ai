from pdf2epub.ir import BBox, BlockType, Word
from pdf2epub.stages.text import Line, _split_footnote, _too_few_letters, split_glued_words
from pdf2epub.textutil import is_known, join_lines, text_layer_quality
from tests.helpers import block, page


def test_split_glued_words():
    assert is_known("the", "en") is True
    assert (
        split_glued_words("the peninsula has always hadgreat strategic", "en")
        == "the peninsula has always had great strategic"
    )
    assert split_glued_words("east bythe Red Sea", "en") == "east by the Red Sea"
    assert split_glued_words("the people ofQahtan lived", "en") == "the people of Qahtan lived"
    # real words and names untouched
    assert split_glued_words("Khuzaimah strategically", "en") == "Khuzaimah strategically"


def test_join_lines_dehyphenates():
    assert join_lines(["the penin-", "sula is large", "Well-", "Known"]) == "the peninsula is large Well- Known"


def test_text_layer_quality_detects_split_words():
    good = [
        Word(text=w, bbox=BBox(x0=0, y0=0, x1=0.1, y1=0.1))
        for w in "the biography of the prophet represents a complete picture of the message".split()
    ]
    bad = [
        Word(text=w, bbox=BBox(x0=0, y0=0, x1=0.1, y1=0.1))
        for w in "M uham m ad represents a com plete picture of the m agnificent D ivine".split()
    ]
    assert text_layer_quality(good) > 0.85
    assert text_layer_quality(bad) < 0.6


def test_footnote_region_split_into_notes():
    p = page(0, [block("n", t=BlockType.FOOTNOTE, bbox=(0.1, 0.8, 0.9, 0.95))])
    mk = lambda t, y: Line(text=t, score=0.99, bbox=BBox(x0=0.1, y0=y, x1=0.9, y1=y + 0.02), script="latin")  # noqa: E731
    lines = [
        mk("1 Tarikh Ibn Khaldun.", 0.80),
        mk("2 It is popularly believed that Hagar", 0.83),
        mk("was a slave girl.", 0.86),
    ]
    _split_footnote(p, p.blocks[0], lines, ["en"])
    notes = [b for b in p.blocks if b.type == BlockType.FOOTNOTE]
    assert [b.text for b in notes] == [
        "1 Tarikh Ibn Khaldun.",
        "2 It is popularly believed that Hagar was a slave girl.",
    ]


def test_too_few_letters():
    assert _too_few_letters("#", (0, 0, 600, 30))
    assert not _too_few_letters("of Allah, and he had laid him prostrate on his forehead", (0, 0, 600, 30))


def test_fix_punct_spacing():
    from pdf2epub.textutil import fix_punct_spacing

    assert fix_punct_spacing("dawn of history,this term") == "dawn of history, this term"
    assert fix_punct_spacing("without water.Ever since") == "without water. Ever since"
    assert fix_punct_spacing("1,000 men and 3.5 km, e.g. here") == "1,000 men and 3.5 km, e.g. here"


def test_glyph_hits_and_apply():
    from pdf2epub.ir import Document, Glyph
    from pdf2epub.stages.glyphs import apply_glyphs
    from pdf2epub.stages.text import glyph_hits_for

    lines = [
        Line(
            text="Sarah forced Ibrahim&to send Hagar",
            score=0.99,
            bbox=BBox(x0=0.1, y0=0.5, x1=0.9, y1=0.52),
            script="latin",
        ),
        Line(
            text="away, and Ismael & , grew up", score=0.99, bbox=BBox(x0=0.1, y0=0.53, x1=0.9, y1=0.55), script="latin"
        ),
    ]
    hits = glyph_hits_for("b", lines)
    assert [(h.symbol, h.ordinal) for h in hits] == [("&", 0), ("&", 1)]
    p = page(0, [block("b", "Sarah forced Ibrahim&to send Hagar away, and Ismael & , grew up")])
    for h in hits:
        h.key = "g000"
    p.glyph_hits = hits
    verified = Glyph(key="g000", text="عليه السلام", lang="ar", resolved=True, render="text")
    doc = Document(id="t", source="x", pages=[p], glyphs={"g000": verified})
    assert apply_glyphs(doc) == 2
    text = p.block("b").text
    assert "&" not in text and text.count("عليه السلام") == 2
    assert "Ibrahim عليه السلام to send" in text
    rtl = [s for s in p.block("b").spans if s.glyph]
    assert all(s.dir == "rtl" and s.lang == "ar" for s in rtl)


def test_unverified_glyph_stays_printed_image():
    from pdf2epub.ir import Document, Glyph, GlyphHit, ImageRef
    from pdf2epub.stages.glyphs import apply_glyphs, refresh_glyph_spans

    p = page(0, [block("b", "the Prophet Muhammad & said:")])
    p.glyph_hits = [GlyphHit(block_id="b", symbol="&", ordinal=0, bbox=BBox(x0=0, y0=0, x1=0.1, y1=0.1), key="g1")]
    g = Glyph(key="g1", image=ImageRef(path="img/glyphs/g1.png", width=40, height=30))  # not verified
    doc = Document(id="t", source="x", pages=[p], glyphs={"g1": g})
    apply_glyphs(doc)
    spans = p.block("b").spans
    assert [s.glyph for s in spans] == [None, "g1", None] and spans[1].text == ""  # image, no guessed text
    assert p.block("b").text == "the Prophet Muhammad  said:"
    # verified later -> every occurrence becomes text
    g.render, g.text, g.lang, g.resolved = "text", "ﷺ", "ar", True
    assert refresh_glyph_spans(doc) == 1
    assert p.block("b").text == "the Prophet Muhammad ﷺ said:"


def test_row_fragments_are_merged():
    from pdf2epub.stages.text import merge_row_fragments

    mk = lambda t, x0, x1, y: Line(text=t, score=0.95, bbox=BBox(x0=x0, y0=y, x1=x1, y1=y + 0.02), script="latin")  # noqa: E731
    lines = [
        mk("See Sahih Al-Bukhari 1/475.", 0.15, 0.6, 0.90),
        mk("2", 0.10, 0.12, 0.901),
        mk("Next line", 0.1, 0.5, 0.93),
    ]
    merged = merge_row_fragments(lines)
    assert [ln.text for ln in merged] == ["2 See Sahih Al-Bukhari 1/475.", "Next line"]


def test_row_merge_keeps_columns_apart():
    from pdf2epub.stages.text import merge_row_fragments

    mk = lambda t, x0, x1: Line(text=t, score=0.95, bbox=BBox(x0=x0, y0=0.5, x1=x1, y1=0.52), script="latin")  # noqa: E731
    merged = merge_row_fragments([mk("left column text", 0.05, 0.45), mk("right column text", 0.55, 0.95)])
    assert len(merged) == 2


def _ln(text, x0, y0, x1, y1, vertical=False):
    return Line(text=text, score=0.95, bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1), script="latin", vertical=vertical)


def test_vertical_side_label_is_not_merged_into_rows():
    """A rotated margin label overlaps every row beside it; it must not glue onto any of them."""
    from pdf2epub.stages.text import merge_row_fragments

    label = _ln("The Seald Nectar", 0.94, 0.43, 0.97, 0.58, vertical=True)
    rows = [
        _ln("Having fully perceived that Muhammad could never be prevented", 0.09, 0.432, 0.84, 0.448),
        _ln("from his Call, the Quraish, in a desperate attempt", 0.08, 0.454, 0.84, 0.470),
        _ln("wave of the Call, turned to other dishonorable means", 0.08, 0.476, 0.84, 0.492),
    ]
    merged = merge_row_fragments([label, *rows])
    assert sorted(ln.text for ln in merged) == sorted(ln.text for ln in [label, *rows])


def test_heading_row_keeps_side_label_out():
    from pdf2epub.stages.text import merge_row_fragments

    label = _ln("The Seald Nectar", 0.044, 0.12, 0.075, 0.266, vertical=True)
    heading = _ln("The Farewell", 0.178, 0.116, 0.445, 0.153)
    assert sorted(ln.text for ln in merge_row_fragments([heading, label])) == ["The Farewell", "The Seald Nectar"]


def test_drop_cap_joins_only_its_first_line():
    """A tall box (drop cap) spanning three rows must not chain them into one line."""
    from pdf2epub.stages.text import merge_row_fragments

    cap = _ln("A", 0.17, 0.30, 0.22, 0.36)
    rows = [
        _ln("fter the accomplishment of the call", 0.23, 0.30, 0.85, 0.316),
        _ln("proclamation of the Message, and the", 0.23, 0.321, 0.85, 0.337),
        _ln("establishment of a new society", 0.23, 0.342, 0.85, 0.358),
    ]
    merged = sorted(merge_row_fragments([cap, *rows]), key=lambda ln: ln.bbox.cy)
    assert [ln.text for ln in merged] == [
        "After the accomplishment of the call",
        "proclamation of the Message, and the",
        "establishment of a new society",
    ]


def test_ocr_vertical_box_detection():
    from pdf2epub.stages.text import _is_vertical_box

    assert _is_vertical_box(w=20, h=150, line_h=22)  # rotated side label
    assert not _is_vertical_box(w=40, h=70, line_h=22)  # drop cap
    assert not _is_vertical_box(w=5, h=22, line_h=22)  # a lone "1" or "l"


def test_text_layer_marks_rotated_words():
    sample_pdf_page = 607
    import pytest

    from pdf2epub.paths import REPO_ROOT

    pdf = REPO_ROOT / "sample" / "bug" / "The Sealed Nectar.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not present")
    import pymupdf

    from pdf2epub.stages.rasterize import _words
    from pdf2epub.stages.text import lines_from_words, merge_row_fragments

    with pymupdf.open(pdf) as doc:
        words = _words(doc[sample_pdf_page - 1])
    assert {w.text for w in words if w.vertical} == {"The", "Seald", "Nectar"}
    texts = [ln.text for ln in merge_row_fragments(lines_from_words(words))]
    assert "The Farewel" in texts and "The Seald Nectar" in texts


def test_raised_drop_cap_starts_first_line():
    """Initial rising above line 1 and hanging over line 2 (p607 'After')."""
    from pdf2epub.stages.text import merge_row_fragments

    cap = _ln("After", 0.165, 0.282, 0.437, 0.437)
    rows = [
        _ln("the accomplishment of the call, the", 0.396, 0.335, 0.857, 0.358),
        _ln("proclamation of the Message, and the", 0.402, 0.358, 0.858, 0.379),
        _ln("establishment of a new society on the basis of There", 0.296, 0.382, 0.854, 0.4),
        _ln("is no God but Allah, and on Muhammad's mission,", 0.296, 0.404, 0.854, 0.42),
    ]
    merged = sorted(merge_row_fragments([cap, *rows]), key=lambda ln: ln.bbox.cy)
    assert [ln.text for ln in merged][:2] == [
        "After the accomplishment of the call, the",
        "proclamation of the Message, and the",
    ]
