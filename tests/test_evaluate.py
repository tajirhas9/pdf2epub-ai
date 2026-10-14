from pdf2epub.evaluate import phrase_found, regressions


def test_phrase_found_tolerates_ocr_noise():
    text = "Beyond a shadow of doubt, the biography of Prophet Muhammad represents a com plete picture"
    assert phrase_found("the biography of Prophet Muhammad", text)
    assert phrase_found("represents a complete picture", text)
    assert not phrase_found("the Arabian Gulf in the east", text)


def test_regressions():
    prev = {"results": [{"doc": "a", "text_recall": 0.9, "epubcheck": True, "seconds_per_page": 0.5, "leaked": []}]}
    now = [{"doc": "a", "text_recall": 0.8, "epubcheck": True, "seconds_per_page": 0.52, "leaked": ["x"]}]
    regs = regressions(now, prev)
    assert any("text_recall" in r for r in regs) and any("leaked" in r for r in regs)
    assert not any("seconds_per_page" in r for r in regs)
