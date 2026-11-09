# Cairn application image (API server + dispatcher).
#
# Two stages: uv resolves the locked dependency set into /cairn/.venv, then the venv and
# source are copied onto a plain Python image. The final image carries no uv binary, no
# build cache and no compiler toolchain.
#
# The `container` extra installs the docker SDK; the only execution mode the compose
# stack uses is container mode. A local-mode deployment can install the base package
# without it.
#
# To pin the uv version as well, use a tag like:
#   ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim
#
#   docker build -t cairn-app .
ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM --platform=amd64 ghcr.io/astral-sh/uv:python3.13-trixie-slim AS build

ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple

# Use the interpreter baked into the image, never a downloaded one: the venv is copied
# into the runtime stage, so its symlink target must exist there at the same path.
# Link mode "copy" keeps the venv self-contained (no references to uv's cache).
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

COPY ./cairn/pyproject.toml ./cairn/uv.lock /cairn/
WORKDIR /cairn

# Runtime dependencies only (dev group excluded to keep the image small), installed
# before the project code for better layer caching.
RUN uv sync --frozen --no-dev --extra container --no-install-project \
    --python /usr/local/bin/python3.13 -i "${PIP_INDEX}"

COPY ./cairn /cairn
RUN uv sync --frozen --no-dev --extra container \
    --python /usr/local/bin/python3.13 -i "${PIP_INDEX}"

FROM --platform=amd64 ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/cairn/.venv/bin:${PATH}" \
    TZ=Asia/Shanghai

WORKDIR /cairn
COPY --from=build /cairn /cairn

EXPOSE 8000

# No `uv run` here: the venv is already synced, and invoking uv at startup would need
# write access and the uv binary in the runtime image.
CMD ["cairn", "serve", "--host", "0.0.0.0", "--no-access-log"]
