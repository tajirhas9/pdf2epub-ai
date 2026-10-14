from pdf2epub.cleanup import cleanup_after_build


def test_cleanup_rules(tmp_path):
    d = tmp_path / "book"
    d.mkdir()
    (d / "x").write_text("x")
    assert not cleanup_after_build(d, success=False, keep_work=False) and d.exists()
    assert not cleanup_after_build(d, success=True, keep_work=True) and d.exists()
    assert cleanup_after_build(d, success=True, keep_work=False) and not d.exists()
