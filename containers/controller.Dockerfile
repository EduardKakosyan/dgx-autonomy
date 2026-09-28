# syntax=docker/dockerfile:1.7
#
# The trusted controller. This is the ONLY image that runs with the Docker socket
# mounted (see compose.yaml). It carries the docker CLI and the dgx_autonomy package.
# It runs no model and no agent tools.
#
# Build context: the repository root (compose sets it to ..).

FROM python:3.12-slim-bookworm

ARG TARGETARCH
# Match the host daemon (29.2.1 on hugo-dgx1).
ARG DOCKER_CLI_VERSION=29.2.1

RUN set -eux; \
    apt-get update; \
    # git: project snapshots for evaluations (snapshot.py), in a git directory the
    # controller owns; the agent's own repository is never used.
    apt-get install -y --no-install-recommends ca-certificates curl git tini; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH:-arm64}" in \
      arm64) arch=aarch64 ;; \
      amd64) arch=x86_64 ;; \
      *) echo "unsupported arch ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    docker --version; \
    git --version

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, for layer caching; then the package itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OPENHANDS_SUPPRESS_BANNER=1 \
    DGX_AUTONOMY_MODELS_FILE=/app/config/models.yaml

ENTRYPOINT ["tini", "--"]
CMD ["dgx-autonomy", "controller"]
