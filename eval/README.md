# Evaluation corpus

One JSON file per document in `corpus/`. Add PDFs of different classes (born-digital, multi-column,
RTL-majority, picture-heavy, tables, scans without text layer) — the sample alone is not enough.
PDFs are not committed; `pdf` is a path relative to the repo root.

Fields: `pdf`, `langs`, `expect_text` (phrases that must appear in the EPUB text),
`forbid_text` (running headers/decoration that must not), `spreads` ([[left, right] printed page
numbers]), `expect_rtl_blocks` (minimum RTL blocks).

`uv run pdf2epub eval [--llm]` converts each document, builds the EPUB and writes
`results/<timestamp>.json`; the summary compares with the previous run so regressions show up.
