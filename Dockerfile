FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /opt/cutdown
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY cutdown/ ./cutdown/
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/opt/cutdown/.venv/bin:$PATH" \
    UV_PYTHON_DOWNLOADS=never \
    HOME=/work \
    XDG_CACHE_HOME=/work/.cache \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
USER 1000:1000
ENTRYPOINT ["/opt/cutdown/.venv/bin/cutdown"]
CMD ["--help"]
