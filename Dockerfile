FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim

COPY ./cairn/pyproject.toml /cairn/pyproject.toml
COPY ./cairn/uv.lock /cairn/uv.lock
WORKDIR /cairn

# Runtime dependencies only (dev group excluded to keep the image small),
# installed before the project code for better layer caching.
RUN uv sync --frozen --no-dev --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./cairn /cairn
RUN uv sync --frozen --no-dev -i https://mirrors.aliyun.com/pypi/simple/

# The image is fully synced at build time; stop `uv run` from re-syncing at
# startup (which would re-install dev dependencies and need network access).
ENV UV_NO_SYNC=1

ENV TZ=Asia/Shanghai