# Cairn application image (API server + dispatcher).
#
# Built on top of the shared base image so it shares the Debian runtime, the
# system Python and the uv binaries with cairn-worker-container.
#
#   docker build -f Dockerfile.base -t cairn-base:latest .
#   docker build -t cairn-app .
# `make app` does both in order.
ARG CAIRN_BASE_IMAGE=cairn-base:latest
FROM --platform=amd64 ${CAIRN_BASE_IMAGE}

ARG PIP_INDEX=https://mirrors.ustc.edu.cn/pypi/simple

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
