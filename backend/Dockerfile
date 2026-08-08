# Texas Project Discovery — FastAPI backend
# Python 3.14 + uv. Deployed as a Render Web Service (Docker runtime).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# Install dependencies first for better layer caching.
# uv.lock is used when present (reproducible); falls back to pyproject.toml otherwise.
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev

# App source
COPY . .

# Render provides $PORT; default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000

# fastapi run == production uvicorn (no --reload).
CMD ["sh", "-c", "uv run fastapi run main.py --host 0.0.0.0 --port ${PORT}"]
