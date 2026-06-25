# Application image for both Railway services (web + worker).
#
# The worker runs the Claude engine, and the Claude Agent SDK (Python) drives the
# Claude Code CLI (a Node program) under the hood — so the image needs Node.js +
# the CLI in addition to Python and git. Both services use this one image; each
# Railway service sets its own start command (uvicorn for web, celery for worker).
#
#   docker build -t gnsis .
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e ".[service]"

EXPOSE 8000
# Default to the web service; the worker service overrides this with:
#   celery -A gnsis.service.tasks.celery_app worker --loglevel=info --concurrency=2
CMD ["sh", "-c", "uvicorn gnsis.service.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
