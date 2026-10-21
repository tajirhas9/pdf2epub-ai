"""Edit operations the LLM may propose, with validation guards and an applier.

The LLM never writes markup. It returns ops against block ids; every op is validated against
the current IR and rejected when it would invent or destroy content.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from pdf2epub.ir import PLACEHOLDER, Block, BlockType, FigureLayout, Page, Role, Source, Span
from pdf2epub.textutil import dominant_script, normalize

OPS = ["fix_text", "set_role", "set_type", "set_lang", "set_layout", "alt_text", "reorder", "merge", "flag"]

OPS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "fixed", "needs_human"]},
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": OPS},
                    "block": {"type": "string"},
                    "text": {"type": "string"},
                    "role": {"type": "string", "enum": [r.value for r in Role]},
                    "type": {"type": "string", "enum": [t.value for t in BlockType]},
                    "level": {"type": "integer"},
                    "lang": {"type": "string"},
                    "dir": {"type": "string", "enum": ["ltr", "rtl"]},
                    "layout": {"type": "string", "enum": [layout.value for layout in FigureLayout]},
                    "blocks": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["op"],
            },
        },
    },
    "required": ["verdict", "ops"],
}


def ops_schema(edit_op: bool = False) -> dict:
    """OPS_SCHEMA, optionally offering the find/replace "edit" op (small text fixes without re-sending the block)."""
    if not edit_op:
        return OPS_SCHEMA
    item = OPS_SCHEMA["properties"]["ops"]["items"]
    props = dict(item["properties"], op={"type": "string", "enum": OPS + ["edit"]})
    props |= {"find": {"type": "string"}, "replace": {"type": "string"}}
    ops = {"type": "array", "items": {**item, "properties": props}}
    return {**OPS_SCHEMA, "properties": {**OPS_SCHEMA["properties"], "ops": ops}}


MIN_SIMILARITY = 0.55  # fix_text must stay this close to the current text (unless text is untrustworthy)


@dataclass
class OpResult:
    op: dict
    status: str  # applied | rejected | skipped (the op would change nothing)
    reason: str = ""


def similarity(a: str, b: str) -> float:
    a, b = normalize(a).lower(), normalize(b).lower()
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


class OpApplier:
    def __init__(self, page: Page):
        self.page = page
        self.proposed_marks: list[tuple[str, str]] = []  # (block id, text the model inserted)

    def apply_all(self, ops: list[dict]) -> list[OpResult]:
        results = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            if self._unchanged(op):
                results.append(OpResult(op, "skipped"))
                continue
            try:
                reason = self.apply(op)
                results.append(OpResult(op, "rejected" if reason else "applied", reason or ""))
            except Exception as e:  # malformed op from the model
                results.append(OpResult(op, "rejected", f"error: {e}"))
        return results

    def apply(self, op: dict) -> str | None:
        """Apply one op. Returns a rejection reason, or None when applied."""
        kind = op.get("op")
        handler = getattr(self, f"_op_{kind}", None)
        if handler is None:
            return f"unknown op {kind!r}"
        return handler(op)

    # ---- helpers ------------------------------------------------------------------------

    def _unchanged(self, op: dict) -> bool:
        """The op restates the current value. (An unchanged fix_text is not one: it confirms the text and
        clears the low-confidence flags.)"""
        kind = op.get("op")
        if kind == "reorder":
            content = sorted((b for b in self.page.blocks if b.role == Role.CONTENT), key=lambda b: b.order)
            return op.get("blocks") == [b.id for b in content] and [b.order for b in content] == list(
                range(len(content))
            )
        b = self.page.block(op["block"]) if isinstance(op.get("block"), str) else None
        if b is None:
            return False
        if kind == "set_role":
            return op.get("role") == b.role.value
        if kind == "set_type":
            same_level = b.type != BlockType.HEADING or (bool(b.level) and op.get("level") in (None, b.level))
            return op.get("type") == b.type.value and same_level
        if kind == "set_layout":
            return b.layout is not None and op.get("layout") == b.layout.value
        if kind == "alt_text":
            return bool(b.alt) and (op.get("text") or "").strip() == b.alt
        if kind == "edit":
            return isinstance(op.get("find"), str) and op.get("find") == op.get("replace")
        return False

    def _block(self, op: dict) -> Block | str:
        bid = op.get("block")
        b = self.page.block(bid) if bid else None
        return b if b is not None else f"unknown block {bid!r}"

    # ---- ops ----------------------------------------------------------------------------

    def _op_fix_text(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        new = normalize(op.get("text") or "").strip()
        if not new:
            return "empty text"
        old = payload_text(b).strip()
        # Verified special marks are locked tokens: they must survive unchanged and in order.
        old_keys = GLYPH_TOKEN.findall(old)
        if GLYPH_TOKEN.findall(new) != old_keys:
            return "verified marks ⟦g:…⟧ must be kept exactly (same tokens, same order)"
        untrusted = b.confidence < 0.6 or "no-text" in b.flags or "low-confidence" in b.flags or not old.strip()
        plain_old, plain_new = GLYPH_TOKEN.sub(" ", old), GLYPH_TOKEN.sub(" ", new)
        sim = similarity(plain_old, plain_new)
        if not untrusted and sim < MIN_SIMILARITY:
            return f"too different from current text (similarity {sim:.2f})"
        if plain_old.strip() and not 0.4 <= len(plain_new) / max(1, len(plain_old)) <= 2.5 and not untrusted:
            return "length changed too much"
        # Special marks the model *added* (absent from the scan's OCR) are not trusted as text:
        # they become placeholders, located on the scan and verified like any other mark.
        new, proposed = mark_insertions(old, new)
        self.proposed_marks += [(b.id, t) for t in proposed]
        script = dominant_script(GLYPH_TOKEN.sub(" ", new).replace(PLACEHOLDER, " "))
        lang = op.get("lang") or next((s.lang for s in b.spans if not s.glyph and s.lang), None)
        d = op.get("dir") or ("rtl" if script in ("arabic", "hebrew") else None)
        old_plain = b.text
        notes = [(s, _note_pos(b, s, old_plain)) for s in b.spans if s.note_ref]
        glyphs = {s.glyph: s for s in b.spans if s.glyph}
        spans: list[Span] = []
        for i, piece in enumerate(GLYPH_TOKEN.split(new)):
            if i % 2:  # odd positions are the captured glyph keys
                spans.append(glyphs[piece])
            elif piece:
                spans.append(Span(text=piece, lang=lang, dir=d))
        b.spans = spans
        for n, pos in notes:  # keep footnote markers (never lose a footnote link)
            _reattach_note(b, n, pos)
        b.source = Source.LLM
        b.flags = [f for f in b.flags if f not in ("low-confidence", "no-text")]
        return None

    def _op_edit(self, op: dict) -> str | None:
        """Small fix: replace one exact, unique piece of the block text. The result goes through every
        fix_text check (similarity, length, locked marks, inserted marks, footnote markers)."""
        b = self._block(op)
        if isinstance(b, str):
            return b
        find, replace = op.get("find"), op.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            return "edit needs non-empty find and a replace string"
        if "⟦" in find + replace or "⟧" in find + replace:
            return "verified marks ⟦g:…⟧ cannot be edited"
        old = payload_text(b)
        count = old.count(find)
        if count != 1:
            return f"find text occurs {count} times in the block (must be exactly once)"
        fix = {"op": "fix_text", "block": b.id, "text": old.replace(find, replace, 1)}
        return self._op_fix_text(fix | {k: op[k] for k in ("lang", "dir") if op.get(k)})

    def _op_set_role(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        try:
            b.role = Role(op.get("role"))
        except ValueError:
            return f"bad role {op.get('role')!r}"
        return None

    def _op_set_type(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        try:
            new = BlockType(op.get("type"))
        except ValueError:
            return f"bad type {op.get('type')!r}"
        visual = {BlockType.FIGURE, BlockType.TABLE, BlockType.FORMULA}
        if (new in visual) != (b.type in visual):
            return "cannot convert between text and visual blocks"
        b.type = new
        if new == BlockType.HEADING:
            b.level = max(1, min(6, int(op.get("level") or b.level or 2)))
        return None

    def _op_set_lang(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        lang = op.get("lang")
        if not lang or not re.fullmatch(r"[a-zA-Z]{2,3}(-[A-Za-z0-9]{2,8})*", lang):
            return f"bad lang {lang!r}"
        for s in b.spans:
            s.lang = lang
            s.dir = op.get("dir") or s.dir
        return None

    def _op_set_layout(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        if b.type not in (BlockType.FIGURE, BlockType.TABLE, BlockType.FORMULA):
            return "layout only applies to figures"
        try:
            b.layout = FigureLayout(op.get("layout"))
        except ValueError:
            return f"bad layout {op.get('layout')!r}"
        return None

    def _op_alt_text(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        if b.type != BlockType.FIGURE:
            return "alt text only for figures"
        text = (op.get("text") or "").strip()
        if not text or len(text) > 400:
            return "alt text empty or too long"
        b.alt = text
        return None

    def _op_reorder(self, op: dict) -> str | None:
        ids = op.get("blocks") or []
        content = {b.id for b in self.page.blocks if b.role == Role.CONTENT}
        if set(ids) != content or len(ids) != len(content):
            return "reorder must list every content block exactly once"
        for i, bid in enumerate(ids):
            self.page.block(bid).order = i
        return None

    def _op_merge(self, op: dict) -> str | None:
        ids = op.get("blocks") or []
        blocks = [self.page.block(i) for i in ids]
        if len(ids) < 2 or any(b is None for b in blocks):
            return "merge needs >= 2 existing blocks"
        if len({b.type for b in blocks}) != 1 or blocks[0].type in (BlockType.FIGURE, BlockType.TABLE):
            return "merge only same-type text blocks"
        first = blocks[0]
        for other in blocks[1:]:
            for b in self.page.blocks:  # markers pointing at a merged-away note follow it
                for sp in b.spans:
                    if sp.note_ref == other.id:
                        sp.note_ref = first.id
            first.spans += [Span(text=" ")] + other.spans
            first.lines += other.lines
            first.bbox = first.bbox.union(other.bbox)
            self.page.blocks.remove(other)
        return None

    def _op_flag(self, op: dict) -> str | None:
        b = self._block(op)
        if isinstance(b, str):
            return b
        b.flags.append("llm:" + (op.get("reason") or "check")[:80])
        return None


GLYPH_TOKEN = re.compile(r"⟦g:(\w+)⟧")
_RTL = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")


def payload_text(block: Block) -> str:
    """Block text as shown to the LLM: verified/printed marks as locked ⟦g:KEY⟧ tokens."""
    return "".join(f"⟦g:{s.glyph}⟧" if s.glyph else s.text for s in block.spans)


def mark_insertions(old: str, new: str) -> tuple[str, list[str]]:
    """In mostly-Latin text, RTL-script runs that the model inserted (or put where OCR read only
    symbols) are replaced by PLACEHOLDER. Returns (new text, proposed texts in order)."""
    if dominant_script(GLYPH_TOKEN.sub(" ", old)) != "latin" or len(old.split()) < 3:
        return new, []  # RTL / empty block: other-script text is the content itself
    old_toks, new_toks = old.split(), new.split()
    out: list[str] = []
    proposed: list[str] = []
    sm = difflib.SequenceMatcher(None, old_toks, new_toks, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        seg = new_toks[j1:j2]
        if tag in ("insert", "replace"):
            old_rtl = any(_RTL.search(t) for t in old_toks[i1:i2])
            run: list[str] = []
            for t in seg + [""]:
                if t and _RTL.search(t) and not old_rtl and not GLYPH_TOKEN.search(t):
                    run.append(t)
                    continue
                if run:
                    proposed.append(" ".join(run))
                    out.append(PLACEHOLDER)
                    run = []
                if t:
                    out.append(t)
        else:
            out += seg
    return " ".join(out), proposed


def _note_pos(block: Block, note: Span, text: str) -> float:
    """Relative position (0..1) of a footnote marker in the block text."""
    offset = 0
    for s in block.spans:
        if s is note:
            return offset / max(1, len(text))
        offset += len(s.text)
    return 1.0


def _reattach_note(block: Block, note: Span, pos: float = 1.0) -> None:
    """Put a footnote marker back after a text fix: where its number is glued to a word, else at the
    same relative position (next word boundary). A marker is never dropped."""
    for i, s in enumerate(block.spans):
        if s.note_ref or s.glyph:
            continue
        m = re.search(rf"(?<=[^\W\d_]|[.,;:!?\"”’)]){re.escape(note.text)}(?=\D|$)", s.text)
        if m:
            block.spans[i : i + 1] = [
                s.model_copy(update={"text": s.text[: m.start()]}),
                note,
                s.model_copy(update={"text": s.text[m.end() :]}),
            ]
            return
    total = sum(len(s.text) for s in block.spans) or 1
    target = pos * total
    offset = 0
    for i, s in enumerate(block.spans):
        if s.note_ref or s.glyph or offset + len(s.text) < target:
            offset += len(s.text)
            continue
        local = max(0, int(target - offset))
        m = re.search(r"(?=\s)|$", s.text[local:])
        cut = local + (m.start() if m else 0)
        block.spans[i : i + 1] = [
            s.model_copy(update={"text": s.text[:cut]}),
            note,
            s.model_copy(update={"text": s.text[cut:]}),
        ]
        return
    block.spans.append(note)
