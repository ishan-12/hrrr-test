FROM python:3.12-slim

# uv installs exactly what uv.lock pins; ecCodes comes as a manylinux wheel (x86_64 and aarch64).
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
# Mount your folder here: points.txt is read from it, data.db and .cache/ are written to it.
WORKDIR /data
ENTRYPOINT ["hrrr-ingest"]
