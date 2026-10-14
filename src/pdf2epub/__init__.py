"""pdf2epub-ai: LLM-assisted PDF -> reflowable EPUB3."""

# Must run before Paddle/HF/Playwright are imported anywhere: keeps their caches inside the project.
from pdf2epub.config import export_cache_env as _export_cache_env

_export_cache_env()
