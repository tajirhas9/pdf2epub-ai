# pdf2epub as a ready-to-run tool: Python + Paddle (GPU) + Node (Vite build) + Java (epubcheck).
#   docker build -t pdf2epub .
#   docker run --rm -it --gpus all --user "$(id -u):$(id -g)" -v "$PWD:/data" -v pdf2epub-cache:/cache \
#       -e LLM_BASE_URL -e LLM_API_KEY -e LLM_MODEL pdf2epub run book.pdf -o /data
# Web app: add -p 8765:8765 -v pdf2epub-config:/config and run `pdf2epub ui --host 0.0.0.0 --no-open`
# CPU only: docker build --build-arg PADDLE=cpu -t pdf2epub:cpu .   (run without --gpus)
FROM python:3.11-slim-bookworm

ARG PADDLE=gpu
ARG NODE_MAJOR=22
ENV DEBIAN_FRONTEND=noninteractive \
    PDF2EPUB_INSTALLED_LAYOUT=1 \
    PDF2EPUB_NO_KEYRING=1 \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    HOME=/home/app \
    XDG_CACHE_HOME=/cache \
    XDG_CONFIG_HOME=/config

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg openjdk-17-jre-headless libgl1 libglib2.0-0 libgomp1 \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.27 /uv /usr/local/bin/uv

WORKDIR /src
COPY . .
RUN PDF2EPUB_REQUIRE_UI_BUILD=1 uv build --wheel --out-dir /tmp/dist \
    && if [ "$PADDLE" = "gpu" ]; then \
         uv tool install "$(ls /tmp/dist/*.whl)[gpu]" --index paddle=https://www.paddlepaddle.org.cn/packages/stable/cu129/ --index-strategy unsafe-best-match; \
       else \
         uv tool install "$(ls /tmp/dist/*.whl)[cpu]"; \
       fi \
    && pdf2epub --help >/dev/null \
    && rm -rf /tmp/uv-cache /tmp/dist   # same layer: the download cache must not stay in the image
# Browser for review renders (Playwright inside the tool environment)
RUN /opt/uv-tools/pdf2epub/bin/python -m playwright install --with-deps chromium-headless-shell \
    && rm -rf /src /var/lib/apt/lists/*

# Runs as any user (`--user $(id -u):$(id -g)` keeps output files yours): writable home, cache, config.
RUN mkdir -p /home/app /cache /config /data && chmod 1777 /home/app /cache /config /data

WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["pdf2epub"]
CMD ["--help"]
