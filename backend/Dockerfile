# Backend: FastAPI + agent + data adapters. Build context is the repository root:
#   docker build -f backend/Dockerfile -t clinic-assistant-backend .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend

# Dependencies first (own layer, rebuilt only when pyproject.toml changes).
COPY backend/pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/static ./static
COPY config /app/config
COPY demo /app/demo
COPY templates /app/templates

# Run as an unprivileged user. uid 1000 matches the first user on most Linux hosts, so the bind-mounted
# .env and config/ stay editable from the admin UI.
RUN useradd --uid 1000 --create-home clinic \
    && mkdir -p /app/data \
    && chown -R clinic:clinic /app/data /app/config
USER clinic

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# One worker: chat sessions, pending confirmations and the background workers live in this process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
