FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Node + the official Claude Code CLI (the SDK shells out to it).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py auth.py setup_flow.py ./
COPY static ./static
COPY templates ./templates

# UID/GID match the most common LinuxServer.io / docker-compose convention.
# Override at runtime if your mounted dirs need different ownership.
RUN useradd -u 1000 -m claude
USER claude

ENV CLAUDE_HOME=/home/claude/.claude \
    CLAUDE_WEB_STATE_DIR=/home/claude/.claude-web \
    CLAUDE_PROJECT_DIR=/workspace

EXPOSE 3001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3001"]
