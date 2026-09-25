# Image for the bounded autonomous engineer (CLI) and its test suite.
#
#   docker build -t autonomous-engineer .
#   docker compose run --rm engineer --repo examples/sample-fastapi --task "..."
#
# This packages the engineer; it is not the Phase 2 per-command validation sandbox.
# Validation still runs target-repo code as the container user, so only mount trusted repos.

FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

# Match the host user so bind-mounted repos, worktrees and data/ stay owned by them
# (and git doesn't reject the repos as "dubious ownership").
ARG UID=1000
ARG GID=1000
ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node runtime, only needed for the `claude` CLI that the claude-code provider drives.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

RUN groupadd --gid "${GID}" engineer \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash engineer

WORKDIR /app

# Dependencies first (cached layer), then the source. Editable install because
# `engineer.examples` locates tests/fixtures relative to the package directory.
COPY pyproject.toml README.md ./
RUN mkdir engineer && touch engineer/__init__.py \
    && pip install --no-cache-dir --no-binary claude-agent-sdk -e '.[dev,claude-code,anthropic]'
COPY . .
RUN pip install --no-cache-dir --no-deps -e . \
    && mkdir -p data examples /home/engineer/.claude \
    && chown -R engineer:engineer /app /home/engineer

USER engineer

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLAUDE_CONFIG_DIR=/home/engineer/.claude \
    ENGINEER_DATA_DIR=/app/data

CMD ["python", "-m", "engineer.run", "--help"]
