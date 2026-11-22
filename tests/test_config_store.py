"""Config file writes keep the user's comments and layout."""

import tomllib

from pdf2epub import config_store

ORIGINAL = """\
# Local settings (gitignored).

[llm]
base_url = "http://localhost:8080/v1"
concurrency = 1  # the local server has one slot

[llm.extra_body.chat_template_kwargs]
enable_thinking = false  # reasoning output is slow
"""


def test_write_keeps_comments_and_applies_changes(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    data = config_store.read(path)
    data["keep_work"] = True
    data["llm"]["concurrency"] = 2
    del data["llm"]["base_url"]
    config_store.write(data, path)

    text = path.read_text()
    assert "# Local settings (gitignored)." in text
    assert "concurrency = 2  # the local server has one slot" in text
    assert "enable_thinking = false  # reasoning output is slow" in text
    assert "base_url" not in text
    assert tomllib.loads(text) == data


def test_write_unchanged_is_byte_identical(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    config_store.write(config_store.read(path), path)
    assert path.read_text() == ORIGINAL


def test_write_drops_emptied_sections_and_creates_new_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    config_store.write({"langs": ["en"]}, path)
    assert tomllib.loads(path.read_text()) == {"langs": ["en"]}
    fresh = tmp_path / "new" / "config.toml"
    config_store.write({"llm": {"model": "m"}, "vector_dpi": 150}, fresh)
    assert tomllib.loads(fresh.read_text()) == {"llm": {"model": "m"}, "vector_dpi": 150}
