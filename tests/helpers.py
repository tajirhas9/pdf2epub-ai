from pdf2epub.ir import BBox, Block, BlockType, Page, Role, Span


def block(bid, text="", t=BlockType.PARAGRAPH, bbox=(0.1, 0.2, 0.9, 0.3), role=Role.CONTENT, **kw):
    x0, y0, x1, y1 = bbox
    b = Block(id=bid, type=t, role=role, bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1), **kw)
    if text:
        b.spans = [Span(text=text)]
    return b


def page(index, blocks, **kw):
    return Page(index=index, width_pt=450, height_pt=660, blocks=blocks, **kw)
