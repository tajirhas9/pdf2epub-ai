# Contributing to pdf2epub-ai

Thanks for helping! Bug reports, test documents, docs fixes and code are all welcome.
By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

- [Ways to help](#ways-to-help)
- [Development setup](#development-setup)
- [Making a change](#making-a-change)
- [Project rules](#project-rules)
- [Pull requests](#pull-requests)
- [Licensing of contributions](#licensing-of-contributions)

## Ways to help

- **Report a bug** — open an issue with the *Bug report* form. Include `pdf2epub --version`, your OS,
  OCR engine, LLM provider/model, the exact command and the output of `pdf2epub -v …`. A page of the
  PDF (or a screenshot) that shows the problem helps most, if you are allowed to share it.
- **Share a test document** — PDFs that break the converter in a new way (multi-column layouts,
  tables, right-to-left scripts, picture-heavy magazines, scans without a text layer) are gold.
  Only share documents you have the right to share (public domain or your own).
- **Suggest a feature** — use the *Feature request* form; describe the document and the result you
  expected.
- **Improve the docs** — typos, unclear steps, missing troubleshooting entries.
- **Write code** — look for issues labelled `good first issue` or `help wanted`, or open an issue
  first for anything bigger than a small fix, so we can agree on the approach.

Security problems: please **don't** open a public issue — see [SECURITY.md](SECURITY.md).

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/), Node.js ≥ 22.12, optionally Java (epubcheck).

```bash
git clone https://github.com/tajirhas9/pdf2epub-ai && cd pdf2epub-ai
scripts/setup.sh              # .venv + best Paddle build for this machine + web/node_modules + epubcheck, all in the repo
uv run pdf2epub init          # writes .pdf2epub.toml (gitignored); or copy .env.example to .env.local
uv run pdf2epub llm-check
```

Everything heavy lives inside the project (`.venv`, `.cache`, `web/node_modules`, `work/`); prefix
commands with `uv run`. Paddle is not in the lockfile (the right build depends on the hardware), so
`uv sync` removes it; the next `uv run pdf2epub …` puts it back, or run `uv run pdf2epub install-paddle`.

**Web app**: in a checkout `pdf2epub ui` rebuilds the page whenever `web/src/ui` changes (about 2 s).
For hot reload run `pdf2epub ui` once to get the access cookie, then `cd web && npm run dev:ui`
(proxies `/api` to it; `PDF2EPUB_API` changes the target).

**Releases**: `uv build --wheel` builds the web app into the wheel (`hatch_build.py`, needs Node at
build time; `PDF2EPUB_REQUIRE_UI_BUILD=1` makes a missing Node an error instead of a warning).

## Making a change

1. Fork the repo and create a branch from `main` (`fix/footnote-labels`, `feat/table-support`).
2. Keep the change focused: one fix or feature per pull request.
3. Add or update tests in `tests/` for behaviour you change.
4. Run the checks:

   ```bash
   uv run pytest                 # unit + end-to-end (synthetic PDF → EPUB → epubcheck) + web backend
   uvx ruff check src tests      # lint (config in pyproject.toml)
   cd web && npm run typecheck   # web app and page templates
   uv run pdf2epub eval          # corpus evaluation: must show no regressions (eval/README.md)
   ```

5. Update `README.md` when you change commands, options or behaviour users see.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`feat: …`, `fix: …`, `docs: …`, `refactor: …`, `test: …`, `chore: …`. Every commit must be signed
off (see [below](#developer-certificate-of-origin)): `git commit -s`.

## Project rules

- **Stay generic.** Heuristics and prompts must handle a *class* of documents, never one book. If a
  fix only helps your PDF, describe the failure class instead and add the document to
  `eval/corpus/` (see `eval/README.md`).
- **The IR is the source of truth.** Vue pages are generated from it. The LLM never writes markup:
  it returns edit operations that are validated (no invented text, no unknown blocks, similarity
  guards) before they are applied.
- **Never invent content.** Text or picture parts lost in a scan stay lost; the converter may flag
  them, not make them up.
- **Keep `pdf2epub eval` free of regressions** when tuning thresholds or prompts.
- **No secrets in logs.** API keys must never be printed, logged or written to work files.

## Pull requests

- Fill in the pull request template and link the issue it closes.
- The checks above must pass; new code needs tests.
- Screenshots (or a before/after page from `pdf2epub dev`) help for layout and web-app changes.
- A maintainer reviews every PR; please respond to comments, and push follow-up commits rather than
  force-pushing during review.

## Licensing of contributions

pdf2epub-ai is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE). The
project owner also offers commercial licenses. So that your contribution can be part of both, the
following applies to every contribution (code, docs, test documents, anything submitted through a
pull request, issue or patch):

### Contribution license grant

By submitting a contribution, you:

1. license it to everyone under the PolyForm Noncommercial License 1.0.0, like the rest of the
   project; **and**
2. grant Syed Tajir Hasnain (the project owner) and their successors a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license to use, copy, modify, distribute, sublicense and
   relicense your contribution under any terms, **including commercial licenses**; **and**
3. confirm that the contribution is your own original work, or that you otherwise have the right to
   grant these licenses (for example, your employer agreed), and that it does not knowingly
   infringe anyone else's rights.

You keep the copyright to your contribution. If you can't agree to this, please don't submit — open
an issue describing the change instead.

### Developer Certificate of Origin

Every commit must carry a `Signed-off-by` line, certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit -s -m "fix: keep footnote labels on continuation pages"
# → Signed-off-by: Your Name <you@example.com>
```

Forgot? `git commit --amend -s` for the last commit, or `git rebase --signoff main` for the branch.
