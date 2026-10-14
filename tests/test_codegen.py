import re

from pdf2epub.codegen.vue_page import VuePageWriter, esc, is_hand_edited
from pdf2epub.ir import BlockType, Document, FigureLayout, ImageRef, Role, Span
from tests.helpers import block, page


def doc_two_pages():
    p1 = page(
        0,
        [
            block("p1-h", "Location {{ of }} <Arabs>", t=BlockType.TITLE, order=0),
            block("p1-a", "of the Masjid in Makkah, where", order=1, continues=True),
            block("hdr", "Running header", role=Role.HEADER),
        ],
        label="32",
    )
    p1.blocks[1].spans = [Span(text="Text with note"), Span(text="1", note_ref="p1-n", sup=True), Span(text=" and ")]
    p1.blocks[1].spans.append(Span(text="عليه السلام", lang="ar", dir="rtl"))
    p1.blocks[1].spans.append(Span(text=" more text and still going"))
    p1.blocks.append(block("p1-n", "1 See Sahih Al-Bukhari 1/474.", t=BlockType.FOOTNOTE, order=3))
    fig = block("p1-f", t=BlockType.FIGURE, order=2, layout=FigureLayout.FLOAT_RIGHT)
    fig.image = ImageRef(path="img/figs/p1-f.jpg", width=400, height=300)
    p1.blocks.append(fig)
    p1.blocks.append(block("p1-c", "Clay rock near Riyadh", t=BlockType.CAPTION, caption_of="p1-f", order=4))
    p2 = page(
        1,
        [
            block("p2-a", "there were neither people nor water.", order=0, continued=True),
            block("p2-b", "Next para.", order=1),
        ],
        label="33",
    )
    return Document(id="t", source="x.pdf", title="T", pages=[p1, p2])


def test_escape_prevents_vue_interpolation():
    assert esc("{{ x }} <b>&") == "&#123;&#123; x &#125;&#125; &lt;b&gt;&amp;"


def test_render_page_structure(tmp_path):
    doc = doc_two_pages()
    w = VuePageWriter(doc, tmp_path)
    v1 = w.render(doc.pages[0])
    assert '<PageBreak label="32" />' in v1
    assert "Running header" not in v1  # non-content role excluded
    assert "&#123;&#123;" in v1 and "{{" not in v1
    assert '<NoteRef to="p1-n" label="1" />' in v1
    assert '<Lang lang="ar" dir="rtl">عليه السلام</Lang>' in v1
    assert 'layout="float-right"' in v1 and ">Clay rock near Riyadh</BookFigure>" in v1
    assert v1.count("Clay rock") == 1  # caption rendered once, inside the figure
    assert '<Footnote id="p1-n"' in v1 and ">See Sahih Al-Bukhari 1/474.</Footnote>" in v1
    # continuation: next page's first paragraph is pulled in after an inline page break
    assert re.search(r'still going<PageBreak label="33" /> there were neither', v1)
    v2 = w.render(doc.pages[1])
    assert "there were neither" not in v2 and '<PageBreak label="33"' not in v2
    assert "Next para." in v2


def test_hand_edit_locks_page(tmp_path):
    doc = doc_two_pages()
    w = VuePageWriter(doc, tmp_path)
    w.write_all()
    path = tmp_path / "pages" / "p0001.vue"
    assert not is_hand_edited(path)
    path.write_text(path.read_text().replace("Next", "Edited"))
    path.write_text(path.read_text() + "\n<!-- my fix -->\n")
    assert is_hand_edited(path)
    result = w.write_all()
    assert "p0001" in result["locked"]
    assert "my fix" in path.read_text()
    assert (tmp_path / "meta" / "p0001.json").exists()
    assert (tmp_path / "book.json").exists()


def test_footnote_backlink_only_when_marker_exists(tmp_path):
    doc = doc_two_pages()
    doc.pages[1].blocks.append(block("p2-n", "1 Unlinked note.", t=BlockType.FOOTNOTE, order=5))
    w = VuePageWriter(doc, tmp_path)
    assert ' label="1" back>' in w.render(doc.pages[0])  # p1-n is referenced by a NoteRef
    v2 = w.render(doc.pages[1])
    assert '<Footnote id="p2-n"' in v2 and " back>" not in v2


def test_split_output():
    from pathlib import Path

    from pdf2epub.cli import _split_output

    assert _split_output(Path("books/x.epub")) == (Path("books/x.epub"), None)
    assert _split_output(Path("books")) == (None, Path("books"))
    assert _split_output(None) == (None, None)
