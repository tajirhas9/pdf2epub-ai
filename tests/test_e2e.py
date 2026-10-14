"""End to end on a synthetic vector PDF, with a fake layout engine (no Paddle needed):
pipeline -> Vue pages -> `vite build --mode epub` -> epubcheck."""

import json
import shutil
import zipfile
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image

from pdf2epub import app as core
from pdf2epub.config import Settings
from pdf2epub.engines import Region
from pdf2epub.ingest import open_document
from pdf2epub.ir import Role
from pdf2epub.paths import REPO_ROOT
from pdf2epub.pipeline import Pipeline
from pdf2epub.stages.figures import Figures
from pdf2epub.stages.layout import Layout
from pdf2epub.stages.order import ReadingOrder
from pdf2epub.stages.rasterize import Rasterize
from pdf2epub.stages.running import RunningElements
from pdf2epub.stages.spreads import Spreads
from pdf2epub.stages.text import Text
from tests.test_spreads import synthetic_picture

W, H = 450, 660
ARABIC_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
BODY = (
    "Thanks to its geographical position, the peninsula has always had great strategic importance. "
    "Considering its internal features, it comprises mostly deserts and arid areas, which made it "
    "inaccessible to foreigners and invaders."
)


def make_pdf(path: Path, tmp: Path) -> dict:
    """4 pages: header/footer/page numbers everywhere; p1 title+text+footnote; p2-p3 a spread
    picture across the gutter; p4 text with an Arabic line."""
    pic = synthetic_picture(h=500, w=800, seed=7)
    left_png, right_png = tmp / "l.png", tmp / "r.png"
    Image.fromarray(pic[:, :400]).save(left_png)
    Image.fromarray(np.ascontiguousarray(pic[:, 400:])).save(right_png)
    doc = pymupdf.open()
    boxes: dict[int, list[Region]] = {}
    for i in range(4):
        page = doc.new_page(width=W, height=H)
        num = 31 + i
        regions = []

        def text(rect, s, label, size=10, fontfile=None, fontname="helv"):
            r = pymupdf.Rect(*rect)
            kw = {"fontsize": size, "fontname": fontname}
            if fontfile:
                kw["fontfile"] = str(fontfile)
            rc = page.insert_textbox(r, s, **kw)
            assert rc >= 0, f"text overflow on page {i}: {s[:30]}"
            regions.append(Region(label, 0.95, tuple(v * 200 / 72 for v in rect)))

        text((60, 20, 400, 34), "Location and Nature of Arab Tribes", "header", 8)
        nx = 400 if num % 2 else 30
        text((nx, 630, nx + 25, 645), str(num), "page_number", 8)
        if i == 0:
            text((60, 60, 400, 100), "Location of the Arabs", "title", 20)
            text((60, 110, 400, 250), BODY + " It is well known to everybody.1", "text")
            text((60, 590, 400, 615), "1 See Sahih Al-Bukhari 1/474.", "footnote", 8)
        elif i in (1, 2):
            rect = pymupdf.Rect(60, 120, W, 420) if i == 1 else pymupdf.Rect(0, 120, W - 60, 420)
            page.insert_image(rect, filename=str(left_png if i == 1 else right_png), keep_proportion=False)
            regions.append(Region("figure", 0.95, tuple(v * 200 / 72 for v in rect)))
            text((60, 440, 390, 520), BODY, "text")
        else:
            text((60, 60, 400, 200), BODY, "text")
            text((60, 220, 400, 250), "فلما أسلما وتله للجبين", "text", 12, ARABIC_FONT, "dejavu")
        boxes[i] = regions
    doc.save(path)
    return boxes


class FakeLayout:
    """Returns the regions we drew (in page-image pixels at 200 dpi)."""

    def __init__(self, regions: dict[int, list[Region]]):
        self.regions = regions
        self.calls = 0

    def detect(self, images):
        out = [self.regions[self.calls + k] for k in range(len(images))]
        self.calls += len(images)
        return out


class NoOcr:
    def detect_lines(self, image):
        raise AssertionError("vector PDF with a good text layer must not be OCR'd")

    def recognize(self, crops, script):
        raise AssertionError("no OCR expected")


@pytest.fixture
def converted(tmp_path):
    pdf = tmp_path / "synthetic.pdf"
    regions = make_pdf(pdf, tmp_path)
    settings = Settings(work_dir=tmp_path / "work", out_dir=tmp_path / "out", workers=2, running_min_repeat=0.5)
    doc = open_document(pdf)
    ctx = core.make_context(settings, pdf, doc.id)
    pipe = Pipeline(
        [
            Rasterize(),
            Layout(FakeLayout(regions)),
            Text(NoOcr(), ["latin"]),
            RunningElements(),
            Figures(),
            Spreads(),
            ReadingOrder(),
        ]
    )
    doc = pipe.run(doc, ctx)
    core.write_pages(doc, ctx)
    return settings, doc, ctx


def test_pipeline_ir(converted):
    _, doc, _ = converted
    p1, p2, p3, p4 = doc.pages
    assert [p.label for p in doc.pages] == ["31", "32", "33", "34"]
    assert all(p.kind.value == "vector" for p in doc.pages)
    roles = {b.text.strip(): b.role for b in p1.blocks if b.text.strip()}
    assert roles["Location and Nature of Arab Tribes"] == Role.HEADER
    assert roles["31"] == Role.PAGE_NUMBER
    body = next(b for b in p1.content_blocks if "geographical" in b.text)
    assert any(s.note_ref for s in body.spans)
    # spread: one merged figure on the left page, halves hidden
    assert len(doc.spreads) == 1 and doc.spreads[0].status.value == "merged", doc.spreads
    merged = p2.block(doc.spreads[0].block_id)
    assert merged.image.width > 1.4 * merged.image.height  # both halves (800x500 picture)
    assert doc.spreads[0].qa["ncc"] > 0.7  # PDF render resampling lowers it vs. raw pixels
    assert not [b for b in p3.content_blocks if b.type.value == "figure"]
    arabic = next(b for b in p4.content_blocks if any(s.dir == "rtl" for s in b.spans))
    assert arabic.spans[0].lang == "ar"


def test_vue_pages_written(converted):
    _, _, ctx = converted
    v1 = (ctx.book_dir / "pages" / "p0001.vue").read_text()
    assert "<NoteRef" in v1 and "Location and Nature" not in v1
    v2 = (ctx.book_dir / "pages" / "p0002.vue").read_text()
    assert 'layout="spread"' in v2
    book = json.loads((ctx.book_dir / "book.json").read_text())
    assert len([a for a in book["assets"] if a.startswith("img/")]) == 1
    assert "fonts/Amiri-Regular.ttf" in book["assets"]  # Arabic text -> embedded font


@pytest.mark.skipif(
    not (REPO_ROOT / "web" / "node_modules").exists()
    or not shutil.which("java")
    or core.epubcheck_jar(Settings()) is None,
    reason="needs web/node_modules, java and epubcheck",
)
def test_build_epub_valid(converted):
    settings, _, ctx = converted
    settings.cache_dir = Settings().cache_dir  # epubcheck lives in the project cache
    res = core.build(settings, ctx.book_dir, settings.out_dir / "synthetic.epub")
    assert res.valid, res.epubcheck_output
    with zipfile.ZipFile(res.epub) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        text = "".join(z.read(n).decode() for n in names if n.startswith("OEBPS/text/"))
    assert 'epub:type="noteref"' in text and 'epub:type="pagebreak"' in text
    assert 'dir="rtl"' in text and "geographical" in text
    assert "Location and Nature of Arab Tribes" not in text  # running header removed
    assert text.count("<img") == 1  # spread shown once
    report = json.loads(res.report.read_text())
    assert report["epubcheck"]["valid"] is True
