"""Different printed marks must never be merged; the same mark must be recognized again."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from pdf2epub.ir import BBox, Word
from pdf2epub.stages.glyphs import crop, descriptor, locate_after_word, similarity

FONT_LATIN = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")
FONT_AR = next(iter(sorted(Path(__file__).resolve().parents[1].glob(".cache/fonts/Amiri-Regular.ttf"))), None)
pytestmark = pytest.mark.skipif(FONT_AR is None or not FONT_LATIN.exists(), reason="needs Amiri + DejaVu fonts")


def render_line(words_before: str, mark: str, words_after: str, size=28):
    """A scanned-looking text line with an inline mark; returns (image, line Word without the mark)."""
    lat = ImageFont.truetype(str(FONT_LATIN), size)
    ar = ImageFont.truetype(str(FONT_AR), int(size * 1.1))
    img = Image.new("RGB", (1400, 80), "white")
    d = ImageDraw.Draw(img)
    x = 20
    d.text((x, 20), words_before, font=lat, fill="black")
    x += d.textlength(words_before, font=lat) + 16
    d.text((x, 14), mark, font=ar, fill="black")
    x += d.textlength(mark, font=ar) + 16
    d.text((x, 20), words_after, font=lat, fill="black")
    x += d.textlength(words_after, font=lat)
    W, H = img.size
    line = Word(text=f"{words_before} {words_after}", bbox=BBox(x0=10 / W, y0=8 / H, x1=(x + 10) / W, y1=72 / H))
    return img, line


def mark_desc(before, mark, after):
    img, line = render_line(before, mark, after)
    k = len(before.split()) - 1  # the mark follows the last word before it
    box = locate_after_word(img, line, k)
    assert box is not None
    return descriptor(crop(img, box))


def test_same_mark_matches_different_marks_do_not():
    saw_1 = mark_desc("the Prophet Muhammad", "ﷺ", "said to them")
    saw_2 = mark_desc("Messenger of Allah", "ﷺ", "spoke")
    as_1 = mark_desc("and Ibrahim", "عليه السلام", "went to Makkah")
    as_2 = mark_desc("when Ismael", "عليه السلام", "grew up")
    assert similarity(saw_1, saw_2) >= 0.8
    assert similarity(as_1, as_2) >= 0.8
    assert similarity(saw_1, as_1) < 0.8 and similarity(saw_2, as_2) < 0.8


def test_locate_picks_the_mark_not_a_word():
    img, line = render_line("the Prophet Muhammad", "ﷺ", "said")
    box = locate_after_word(img, line, 2)
    W = img.width
    # the mark sits after 'Muhammad' and before 'said'
    lat = ImageFont.truetype(str(FONT_LATIN), 28)
    start = 20 + ImageDraw.Draw(img).textlength("the Prophet Muhammad", font=lat)
    assert box.x0 * W >= start - 4 and box.x1 * W <= start + 120


def test_implausible_crops_are_rejected():
    from pdf2epub.stages.glyphs import plausible_mark

    img, line = render_line("the Prophet Muhammad", "ﷺ", "said")
    W, H = img.size
    lh = line.bbox.height
    wide = BBox(x0=0.05, y0=line.bbox.y0, x1=0.05 + 6 * lh * H / W, y1=line.bbox.y1)  # a run of words
    assert plausible_mark(wide, line, img) is None
    short = Word(text=line.text, bbox=BBox(x0=line.bbox.x0, y0=0.4, x1=line.bbox.x1, y1=0.6))
    tall = BBox(x0=0.3, y0=0.05, x1=0.32, y1=0.95)  # reaches into the lines above and below
    assert plausible_mark(tall, short, img) is None
    ok = locate_after_word(img, line, 2)
    assert plausible_mark(ok, line, img) is not None


def test_harmonize_majority_and_split(tmp_path):
    from pdf2epub.ir import Document, Glyph, ImageRef
    from pdf2epub.stages.glyphs import harmonize

    def save(key, before, mark, after):
        img, line = render_line(before, mark, after)
        box = locate_after_word(img, line, len(before.split()) - 1)
        c = crop(img, box)
        (tmp_path / "img").mkdir(exist_ok=True)
        c.save(tmp_path / "img" / f"{key}.png")
        return ImageRef(path=f"img/{key}.png", width=c.width, height=c.height)

    def g(key, text, n, before, mark, after):
        return Glyph(
            key=key,
            image=save(key, before, mark, after),
            text=text,
            lang="ar",
            render="text",
            resolved=True,
            occurrences=n,
        )

    AS, SAW = "عليه السلام", "ﷺ"
    glyphs = {
        "a1": g("a1", AS, 5, "and Ibrahim", AS, "went"),
        "a2": g("a2", AS, 3, "when Ismael", AS, "grew"),
        "a3": g("a3", SAW, 1, "son of Ismael", AS, "had"),  # model misread one occurrence
        "s1": g("s1", SAW, 2, "Prophet Muhammad", SAW, "said"),  # a genuinely different mark
    }
    doc = Document(id="t", source="x", glyphs=glyphs)
    assert harmonize(doc, tmp_path) >= 1
    assert doc.glyphs["a3"].text == AS and doc.glyphs["a3"].render == "text"  # 8 vs 1: majority wins
    assert doc.glyphs["s1"].text == SAW  # different family untouched

    # an even split inside one family -> nobody is trusted: printed images
    doc.glyphs["a3"].text, doc.glyphs["a3"].occurrences = SAW, 8
    doc.glyphs["a3"].render = "text"
    harmonize(doc, tmp_path)
    assert {doc.glyphs[k].render for k in ("a1", "a2", "a3")} == {"image"}
    assert doc.glyphs["s1"].render == "text"


def test_punctuation_after_mark_is_not_part_of_it():
    img, line = render_line("O Ibrahim", "ﷺ", "! You have fulfilled")
    line = Word(text="O Ibrahim! You have fulfilled", bbox=line.bbox)  # OCR glued '!' to the name
    box = locate_after_word(img, line, 1)
    alone = locate_after_word(*render_line("O Ibrahim", "ﷺ", "You have fulfilled"), 1)
    assert box is not None and alone is not None
    assert abs(box.width - alone.width) * img.width < 6  # same mark width: '!' excluded
