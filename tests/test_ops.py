from pdf2epub.ir import BlockType, Role, Span
from pdf2epub.llm.ops import OpApplier
from tests.helpers import block, page


def make():
    return page(
        0,
        [
            block("b0", "Linguistically, the word Arab means deserts", confidence=0.95),
            block("b1", "M uham m ad represents a com plete picture", confidence=0.95),
            block("b2", "", confidence=0.3, flags=["low-confidence", "rtl"]),
            block("b3", "The Seald Nectar", bbox=(0.0, 0.1, 0.05, 0.4)),
            block("f1", t=BlockType.FIGURE, bbox=(0.5, 0.4, 0.9, 0.7)),
        ],
    )


def test_fix_text_accepts_ocr_correction():
    p = make()
    [r] = OpApplier(p).apply_all([{"op": "fix_text", "block": "b1", "text": "Muhammad represents a complete picture"}])
    assert r.status == "applied"
    assert p.block("b1").text == "Muhammad represents a complete picture"


def test_fix_text_rejects_rewrite_of_trusted_text():
    p = make()
    [r] = OpApplier(p).apply_all(
        [{"op": "fix_text", "block": "b0", "text": "Totally different invented sentence here"}]
    )
    assert r.status == "rejected"
    assert p.block("b0").text.startswith("Linguistically")


def test_fix_text_allows_untrusted_block_and_sets_rtl():
    p = make()
    [r] = OpApplier(p).apply_all([{"op": "fix_text", "block": "b2", "text": "فَلَمَّا أَسْلَمَا", "lang": "ar"}])
    assert r.status == "applied"
    s = p.block("b2").spans[0]
    assert s.dir == "rtl" and s.lang == "ar"
    assert "low-confidence" not in p.block("b2").flags


def test_unknown_block_and_op_rejected():
    p = make()
    res = OpApplier(p).apply_all([{"op": "fix_text", "block": "nope", "text": "x"}, {"op": "explode"}, "garbage"])
    assert [r.status for r in res] == ["rejected", "rejected"]


def test_set_role_and_reorder_validation():
    p = make()
    ap = OpApplier(p)
    assert ap.apply({"op": "set_role", "block": "b3", "role": "decoration"}) is None
    assert p.block("b3").role == Role.DECORATION
    assert ap.apply({"op": "reorder", "blocks": ["b1", "b0"]}) is not None  # missing ids
    assert ap.apply({"op": "reorder", "blocks": ["f1", "b2", "b1", "b0"]}) is None
    assert p.block("f1").order == 0


def test_cannot_turn_text_into_figure():
    p = make()
    assert OpApplier(p).apply({"op": "set_type", "block": "b0", "type": "figure"}) is not None


def test_merge_and_alt_text():
    p = make()
    ap = OpApplier(p)
    assert ap.apply({"op": "merge", "blocks": ["b0", "b1"]}) is None
    assert p.block("b1") is None and "Muhammad" not in p.block("b0").text and "uham" in p.block("b0").text
    assert ap.apply({"op": "alt_text", "block": "f1", "text": "Desert rock formations"}) is None
    assert ap.apply({"op": "alt_text", "block": "b0", "text": "x"}) is not None


def test_glyph_tokens_are_locked():
    from pdf2epub.ir import Span

    p = make()
    b = p.block("b0")
    b.spans = [
        Span(text="forced Ibrahim "),
        Span(text="عليه السلام", lang="ar", dir="rtl", glyph="g000"),
        Span(text=" to send Hagar and her baby awy"),
    ]
    ap = OpApplier(p)
    # dropping / swapping the verified mark is rejected
    assert ap.apply({"op": "fix_text", "block": "b0", "text": "forced Ibrahim ﷺ to send Hagar and her baby away"})
    # keeping the token: fix applied, glyph span preserved as is
    assert (
        ap.apply({"op": "fix_text", "block": "b0", "text": "forced Ibrahim ⟦g:g000⟧ to send Hagar and her baby away"})
        is None
    )
    assert b.text == "forced Ibrahim عليه السلام to send Hagar and her baby away"
    assert [s.glyph for s in b.spans] == [None, "g000", None]


def test_inserted_mark_becomes_placeholder():
    from pdf2epub.ir import PLACEHOLDER

    p = make()
    ap = OpApplier(p)
    op = {"op": "fix_text", "block": "b1", "text": "Muhammad ﷺ represents a complete picture"}
    assert ap.apply(op) is None
    assert p.block("b1").text == f"Muhammad {PLACEHOLDER} represents a complete picture"
    assert ap.proposed_marks == [("b1", "ﷺ")]


def test_footnote_marker_never_lost():
    from pdf2epub.ir import Span

    p = make()
    b = p.block("b1")
    b.spans = [
        Span(text="the story is well known to everybody."),
        Span(text="1", note_ref="n1", sup=True),
        Span(text=" Another Yemeni tribe came"),
    ]
    # the model rewrote the sentence without the marker digit
    assert (
        OpApplier(p).apply(
            {"op": "fix_text", "block": "b1", "text": "The story is well known to everybody. Another Yemeni tribe came"}
        )
        is None
    )
    refs = [s for s in b.spans if s.note_ref == "n1"]
    assert len(refs) == 1
    before = "".join(s.text for s in b.spans[: b.spans.index(refs[0])])
    assert before.endswith("everybody.")


def test_merge_retargets_note_markers():
    p = page(
        0,
        [
            block("a", "text", order=0),
            block("n", "2 Historians differ", t=BlockType.FOOTNOTE, order=1),
            block("n-cont", "1/69). We gave", t=BlockType.FOOTNOTE, order=2),
        ],
    )
    p.block("a").spans.append(Span(text="2", note_ref="n-cont", sup=True))
    assert OpApplier(p).apply({"op": "merge", "blocks": ["n", "n-cont"]}) is None
    assert p.block("a").spans[-1].note_ref == "n"


def test_edit_fixes_one_unique_piece():
    p = make()
    [r] = OpApplier(p).apply_all([{"op": "edit", "block": "b1", "find": "M uham m ad", "replace": "Muhammad"}])
    assert r.status == "applied"
    assert p.block("b1").text == "Muhammad represents a com plete picture"


def test_edit_rejects_ambiguous_missing_and_mark_edits():
    from pdf2epub.ir import Span

    p = make()
    ap = OpApplier(p)
    assert "3 times" in ap.apply({"op": "edit", "block": "b1", "find": "m", "replace": "M"})
    assert "0 times" in ap.apply({"op": "edit", "block": "b1", "find": "nowhere", "replace": "x"})
    assert ap.apply({"op": "edit", "block": "b1", "find": "", "replace": "x"})
    b = p.block("b0")
    b.spans = [Span(text="forced Ibrahim "), Span(text="عليه السلام", glyph="g000"), Span(text=" to send awy")]
    assert "marks" in ap.apply({"op": "edit", "block": "b0", "find": "⟦g:g000⟧ to", "replace": "to"})
    assert ap.apply({"op": "edit", "block": "b0", "find": "awy", "replace": "away"}) is None
    assert [s.glyph for s in b.spans] == [None, "g000", None] and b.text.endswith("to send away")


def test_edit_keeps_fix_text_guards():
    p = make()  # b0 is trusted OCR: an edit that rewrites most of it is rejected like fix_text would be
    find = "Linguistically, the word Arab means deserts"
    op = {"op": "edit", "block": "b0", "find": find, "replace": "Nothing alike"}
    assert OpApplier(p).apply(op) is not None
    assert p.block("b0").text.startswith("Linguistically")


def test_ops_that_change_nothing_are_skipped():
    p = make()
    for i, b in enumerate(p.blocks):
        b.order = i
    res = OpApplier(p).apply_all(
        [
            {"op": "set_role", "block": "b0", "role": "content"},
            {"op": "set_type", "block": "b0", "type": "paragraph"},
            {"op": "reorder", "blocks": [b.id for b in p.blocks]},
            {"op": "edit", "block": "b0", "find": "Arab", "replace": "Arab"},
            {"op": "set_role", "block": "b3", "role": "decoration"},
        ]
    )
    assert [r.status for r in res] == ["skipped"] * 4 + ["applied"]


def test_unchanged_fix_text_still_confirms_the_text():
    p = make()
    p.block("b1").flags = ["low-confidence"]
    [r] = OpApplier(p).apply_all([{"op": "fix_text", "block": "b1", "text": p.block("b1").text}])
    assert r.status == "applied" and "low-confidence" not in p.block("b1").flags


def test_ops_schema_default_is_unchanged():
    from pdf2epub.llm.ops import OPS_SCHEMA, ops_schema

    assert ops_schema() is OPS_SCHEMA
    edit = ops_schema(edit_op=True)
    assert "edit" in edit["properties"]["ops"]["items"]["properties"]["op"]["enum"]
    assert "edit" not in OPS_SCHEMA["properties"]["ops"]["items"]["properties"]["op"]["enum"]
