"""Intermediate representation: the single source of truth for a book.

All coordinates are normalized to the page (0..1, origin top-left) so they are
independent of render DPI and comparable across pages.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def intersection(self, other: BBox) -> float:
        w = min(self.x1, other.x1) - max(self.x0, other.x0)
        h = min(self.y1, other.y1) - max(self.y0, other.y0)
        return w * h if w > 0 and h > 0 else 0.0

    def overlap_ratio(self, other: BBox) -> float:
        """Fraction of *self* covered by other."""
        return self.intersection(other) / self.area if self.area else 0.0

    def union(self, other: BBox) -> BBox:
        return BBox(
            x0=min(self.x0, other.x0),
            y0=min(self.y0, other.y0),
            x1=max(self.x1, other.x1),
            y1=max(self.y1, other.y1),
        )


class BlockType(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    FIGURE = "figure"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    LIST = "list"
    TABLE = "table"
    QUOTE = "quote"
    FORMULA = "formula"


class Role(StrEnum):
    CONTENT = "content"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    DECORATION = "decoration"
    BACKGROUND = "background"


class Source(StrEnum):
    PDF = "pdf"
    OCR = "ocr"
    MERGED = "merged"
    LLM = "llm"
    MANUAL = "manual"


class FigureLayout(StrEnum):
    INLINE = "inline"
    FLOAT_LEFT = "float-left"
    FLOAT_RIGHT = "float-right"
    FULL = "full"
    SPREAD = "spread"


class Span(BaseModel):
    text: str
    lang: str | None = None
    dir: str | None = None  # "rtl" | "ltr" | None (inherit)
    bold: bool = False
    italic: bool = False
    sup: bool = False
    note_ref: str | None = None  # footnote block id this marker points to
    glyph: str | None = None  # glyph-cache key when span stands for an inline special glyph


class ImageRef(BaseModel):
    path: str  # relative to the book work dir
    width: int
    height: int


class Word(BaseModel):
    """A word from the PDF text layer (kept as a hint for the text stage)."""

    text: str
    bbox: BBox
    font: str = ""
    size: float = 0.0
    line: int = 0  # line index within the page text layer
    invisible: bool = False


class Block(BaseModel):
    id: str
    type: BlockType
    role: Role = Role.CONTENT
    bbox: BBox
    spans: list[Span] = Field(default_factory=list)
    source: Source = Source.PDF
    confidence: float = 1.0
    level: int | None = None  # heading level
    image: ImageRef | None = None
    layout: FigureLayout | None = None
    alt: str | None = None
    caption_of: str | None = None  # caption -> figure block id
    continues: bool = False  # paragraph continues on the next page
    continued: bool = False  # paragraph continues from the previous page
    order: int = 0  # reading order within the page
    flags: list[str] = Field(default_factory=list)
    lines: list[Word] = Field(default_factory=list)  # source text lines + boxes (locating marks on the scan)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    def set_text(self, text: str, **span_kwargs) -> None:
        self.spans = [Span(text=text, **span_kwargs)]


class PageKind(StrEnum):
    SCANNED = "scanned"
    VECTOR = "vector"
    IMAGE = "image"  # input was a plain image file


class Page(BaseModel):
    index: int  # 0-based position in the source
    label: str | None = None  # printed page number, if detected
    side: str | None = None  # "left" | "right" | None when unknown
    kind: PageKind = PageKind.VECTOR
    width_pt: float
    height_pt: float
    image: ImageRef | None = None
    dpi: float = 0.0
    skew_deg: float = 0.0
    words: list[Word] = Field(default_factory=list)
    edge_lines: list[Word] = Field(default_factory=list)  # OCR lines touching left/right page edge (spread evidence)
    glyph_hits: list[GlyphHit] = Field(default_factory=list)
    text_layer_quality: float | None = None
    blocks: list[Block] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)

    @property
    def content_blocks(self) -> list[Block]:
        return sorted((b for b in self.blocks if b.role == Role.CONTENT), key=lambda b: b.order)

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def new_block_id(self) -> str:
        used = {b.id for b in self.blocks}
        n = len(self.blocks)
        while (bid := f"p{self.index + 1:04d}-b{n:03d}") in used:
            n += 1
        return bid


class SpreadStatus(StrEnum):
    CANDIDATE = "candidate"
    MERGED = "merged"
    SIDE_BY_SIDE = "side_by_side"  # alignment QA failed: halves shown next to each other
    REJECTED = "rejected"


class Spread(BaseModel):
    left_page: int
    right_page: int
    left_block: str
    right_block: str
    status: SpreadStatus = SpreadStatus.CANDIDATE
    background: bool = False
    score: float = 0.0
    qa: dict[str, float] = Field(default_factory=dict)
    image: ImageRef | None = None
    block_id: str | None = None  # id of the merged figure block (lives on the left page)


PLACEHOLDER = "\uf8ff"  # stands in the text for a mark the LLM inserted, until it is verified on the scan


class GlyphHit(BaseModel):
    """One occurrence of a special inline mark (OCR misread it, or the LLM added one OCR missed)."""

    block_id: str
    symbol: str  # what is in the text now: OCR's misreading (e.g. "&") or PLACEHOLDER
    ordinal: int  # n-th occurrence of `symbol` in the block text
    bbox: BBox  # page coords of the mark itself (tight crop)
    key: str = ""  # glyph cluster
    proposed: str | None = None  # text the LLM put there (only a hint; verified like any other mark)


class Glyph(BaseModel):
    """A distinct printed mark (cluster of visually identical occurrences)."""

    key: str
    image: ImageRef | None = None  # prototype crop
    text: str | None = None  # verified Unicode text ("" = noise, drop it)
    lang: str | None = None
    kind: str | None = None
    confidence: float = 0.0
    occurrences: int = 0
    resolved: bool = False  # True once decided (text verified, noise, or keep-as-image)
    symbol: str = ""  # OCR's reading of the first occurrence (PLACEHOLDER for marks OCR dropped)
    sample_page: int = 0
    sample_bbox: BBox | None = None
    render: str = "image"  # "text" only when verified; otherwise the printed mark is kept as an image


class Document(BaseModel):
    id: str
    source: str
    title: str = ""
    author: str = ""
    language: str = "en"
    pages: list[Page] = Field(default_factory=list)
    spreads: list[Spread] = Field(default_factory=list)
    glyphs: dict[str, Glyph] = Field(default_factory=dict)
    meta: dict[str, str] = Field(default_factory=dict)

    def find_block(self, block_id: str) -> tuple[Page, Block] | None:
        for page in self.pages:
            if (b := page.block(block_id)) is not None:
                return page, b
        return None
