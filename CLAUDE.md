# pdf2epub-ai

Setup, checks, project rules, commit style: see CONTRIBUTING.md (read it first).

## Layout
- `src/pdf2epub/`: Python pipeline (ingest → `stages/` → IR in `work/<book>/ir/` → `llm/` review → `codegen/vue_page.py` writes Vue pages).
- `web/plugins/epub/`: EPUB assembly (Vite plugin): `chapters.ts` groups pages into chapters (new chapter at h1 or every 40 pages), `xhtml.ts` turns SSR HTML into XHTML, `package.ts` builds the OPF, nav and zip.
- `web/src/book/components/`: Vue components that render the book (NoteRef, Footnote, …). `web/src/ui/`: web app.

## Debugging a conversion
- Status: `work/<book>/run.json`. Output and epubcheck result: `out/<name>.epub` and `out/<name>.report.json`.
- Rebuild the EPUB from an existing work dir without OCR or LLM (~5 s):
  `cd web && PDF2EPUB_BOOK_DIR=../work/<book> PDF2EPUB_EPUB_OUT=/tmp/x.epub npx vite build --mode epub`
- Validate: `java -jar .cache/epubcheck/epubcheck-*/epubcheck.jar /tmp/x.epub`

## Gotchas
- Chapters are built from pages, so an in-book `#id` link can point into another chapter file. `chapters.ts` rewrites these to `chNNN.xhtml#id`; don't emit links that assume the target is in the same file.

## Tests
- Python: `uv run pytest`. Web: `cd web && npm test` (vitest, `web/tests/*.test.ts`) and `npm run typecheck`.
