from pdf2epub.config import Settings
from pdf2epub.ir import BlockType, Document, Role
from pdf2epub.pipeline import Context
from pdf2epub.stages.order import link_footnotes, mark_continuations, xycut
from pdf2epub.stages.running import RunningElements
from tests.helpers import block, page


def test_running_elements_labels_and_sides(tmp_path):
    pages = []
    for i in range(10):
        num = 30 + i
        x = (0.9, 0.95) if num % 2 else (0.05, 0.1)
        pages.append(
            page(
                i,
                [
                    block(f"h{i}", "Location and Nature of Arab Tribes", bbox=(0.5, 0.05, 0.9, 0.08)),
                    block(f"s{i}", "The Seald Nectar", bbox=(0.0, 0.1, 0.04, 0.4)),
                    block(f"n{i}", str(num), bbox=(x[0], 0.95, x[1], 0.97)),
                    block(f"b{i}", f"Body text {i} unique content here", bbox=(0.1, 0.2, 0.9, 0.8)),
                ],
            )
        )
    doc = Document(id="t", source="x", pages=pages)
    ctx = Context(settings=Settings(), book_dir=tmp_path, source=tmp_path)
    RunningElements().run(doc, ctx)
    p = doc.pages[3]
    assert p.block("h3").role == Role.HEADER
    assert p.block("s3").role == Role.DECORATION
    assert p.block("n3").role == Role.PAGE_NUMBER
    assert p.block("b3").role == Role.CONTENT
    assert p.label == "33" and p.side == "right"
    assert doc.pages[4].side == "left"


def test_xycut_two_columns_and_wrapped_figure():
    left_text = block("t1", "a", bbox=(0.1, 0.30, 0.45, 0.6))
    fig = block("f", t=BlockType.FIGURE, bbox=(0.5, 0.30, 0.9, 0.6))
    top = block("t0", "b", bbox=(0.1, 0.1, 0.9, 0.25))
    below = block("t2", "c", bbox=(0.1, 0.65, 0.9, 0.9))
    order = [b.id for b in xycut([below, fig, left_text, top])]
    assert order == ["t0", "t1", "f", "t2"]
    rtl = [b.id for b in xycut([fig, left_text], rtl=True)]
    assert rtl == ["f", "t1"]


def test_continuation_detection():
    p1 = page(0, [block("a", "the story of the Zamzam spring and", order=0)])
    p2 = page(1, [block("b", "everybody knows it.", order=0)])
    p3 = page(2, [block("c", "A new paragraph.", order=0)])
    mark_continuations([p1, p2, p3])
    assert p1.block("a").continues and p2.block("b").continued
    assert not p2.block("b").continues and not p3.block("c").continued


def test_footnote_marker_linking():
    p = page(
        0,
        [
            block("a", "is well known to everybody.1 Another Yemeni tribe", order=0),
            block("n", "1 See Sahih Al-Bukhari", t=BlockType.FOOTNOTE, order=1),
        ],
    )
    link_footnotes(p)
    spans = p.block("a").spans
    assert [s.note_ref for s in spans] == [None, "n", None]
    assert spans[0].text.endswith("everybody.") and spans[1].text == "1"


def test_superscript_footnote_marker():
    p = page(
        0,
        [
            block("a", "Sarah gave Hagar to Ibrahim as a wife³and Ibrahim returned", order=0),
            block("n", "3 See Rahmatul-lil-Alamin 2/34.", t=BlockType.FOOTNOTE, order=1),
        ],
    )
    link_footnotes(p)
    spans = p.block("a").spans
    assert [s.note_ref for s in spans] == [None, "n", None]
    assert spans[1].text == "³"


def test_merge_split_paragraph_around_figure():
    from pdf2epub.stages.order import merge_split_paragraphs

    p = page(
        0,
        [
            block("a", "which is an extension of the Indian Ocean, and in the north by", order=0),
            block("f", t=BlockType.FIGURE, order=1),
            block("b", "Shaam (ancient Syria) and parts of Iraq.", order=2),
            block("c", "Thanks to its position, the peninsula", order=3),
            block("d", "1. Perished Arabs: The ancient Arabs", order=4),
        ],
    )
    merge_split_paragraphs(p)
    texts = [b.text for b in p.content_blocks if b.type == BlockType.PARAGRAPH]
    assert (
        texts[0]
        == "which is an extension of the Indian Ocean, and in the north by Shaam (ancient Syria) and parts of Iraq."
    )
    assert texts[-1].startswith("1. Perished")  # list items are not glued on


def test_missing_footnote_number_recovered():
    p = page(
        0,
        [
            block("a", "is well known to everybody.1 Another tribe came.2 And more", order=0),
            block("n1", "See Sahih Al-Bukhari 1/474.", t=BlockType.FOOTNOTE, bbox=(0.1, 0.85, 0.9, 0.87), order=1),
            block("n2", "2 See Sahih Al-Bukhari 1/475.", t=BlockType.FOOTNOTE, bbox=(0.1, 0.88, 0.9, 0.90), order=2),
        ],
    )
    link_footnotes(p)
    assert p.block("n1").text.startswith("1 ")
    assert sorted(s.note_ref for s in p.block("a").spans if s.note_ref) == ["n1", "n2"]


def test_lone_unnumbered_note_takes_the_only_marker():
    p = page(
        0,
        [
            block("a", "the Virtue of the Lineage.1 Then the text goes on", order=0),
            block("n", "Recorded by Muslim from Wathilah.", t=BlockType.FOOTNOTE, bbox=(0.1, 0.85, 0.9, 0.87), order=1),
        ],
    )
    link_footnotes(p)
    assert [s.note_ref for s in p.block("a").spans if s.note_ref] == ["n"]
