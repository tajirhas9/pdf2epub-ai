#!/usr/bin/env bash
# One-time setup. Everything is installed inside this repo (.venv, .cache, web/node_modules),
# so `pdf2epub uninstall` (or deleting those folders) removes it all.
set -euo pipefail
cd "$(dirname "$0")/.."

export UV_CACHE_DIR="$PWD/.cache/uv"
command -v uv >/dev/null || { echo "install uv first: https://docs.astral.sh/uv/"; exit 1; }
command -v node >/dev/null || { echo "install Node.js >= 22.12"; exit 1; }

echo "== Python deps (.venv)"
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then EXTRA=gpu; else EXTRA=cpu; fi
echo "   Paddle: $EXTRA"
uv sync --extra "$EXTRA"

echo "== Web deps (web/node_modules)"
(cd web && npm ci)

echo "== epubcheck (.cache/epubcheck, needs Java to run)"
EPUBCHECK_VERSION=5.4.0
if [ ! -f ".cache/epubcheck/epubcheck-${EPUBCHECK_VERSION}/epubcheck.jar" ]; then
  mkdir -p .cache/epubcheck
  curl -sSL -o .cache/epubcheck/ec.zip \
    "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip"
  (cd .cache/epubcheck && unzip -qo ec.zip && rm ec.zip)
fi
command -v java >/dev/null || echo "warning: java not found — EPUBs are built but not validated"

echo "done. Configure the LLM: uv run pdf2epub init   (or edit .env.local / .pdf2epub.toml)"
