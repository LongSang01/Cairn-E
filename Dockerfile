# Cairn application image (API server + dispatcher).
#
# Built directly on astral's official uv image so the Python interpreter (3.13)
# is pinned and tested against the uv version. It no longer shares a base image
# with cairn-worker-container: the worker needs Debian's system Python for its
# tools, while the app needs a pinned interpreter — the two are independent.
#
# To pin the uv version as well, use a tag like:
#   ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim
#
#   docker build -t cairn-app .
FROM --platform=amd64 ghcr.io/astral-sh/uv:python3.13-trixie-slim

ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple

COPY ./cairn/pyproject.toml /cairn/pyproject.toml
COPY ./cairn/uv.lock /cairn/uv.lock
WORKDIR /cairn

# Runtime dependencies only (dev group excluded to keep the image small),
# installed before the project code for better layer caching.
RUN uv sync --frozen --no-dev --no-install-project -i "${PIP_INDEX}"

COPY ./cairn /cairn
RUN uv sync --frozen --no-dev -i "${PIP_INDEX}"

# The image is fully synced at build time; stop `uv run` from re-syncing at
# startup (which would re-install dev dependencies and need network access).
ENV UV_NO_SYNC=1

ENV TZ=Asia/Shanghai
