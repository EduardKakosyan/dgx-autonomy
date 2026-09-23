# syntax=docker/dockerfile:1.7
#
# The acceptance-check evaluator: Playwright Test (Chromium) and pytest + httpx.
# The controller starts one disposable container per automated criterion
# (evaluation.py) with --user 10002:10002, --cap-drop ALL, no-new-privileges and
# resource limits. It mounts the frozen checks read-only at /checks and that
# criterion's own output directory at /out. It is on the egress network only: it
# reaches the demo by the sandbox's name, and the host's egress rules keep it off
# the DGX, the LAN and the tailnet. No Docker socket, no model, no controller state.
#
# Build context: autonomy/ (compose sets it to ..).

# v1.63.0-noble, pinned by the multi-arch index digest (linux/arm64 included). It
# matches the beach app's @playwright/test and ships the browsers for 1.63.0.
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
FROM ${PLAYWRIGHT_IMAGE}

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 python3-venv; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -g 10002 evaluator; \
    useradd -u 10002 -g 10002 -M -d /tmp -s /usr/sbin/nologin evaluator

WORKDIR /opt/evaluator

# Exact versions, verified against the lockfile's integrity hashes (npm) and the
# requirements' sha256 hashes (pip).
COPY containers/evaluator/package.json containers/evaluator/package-lock.json ./
COPY containers/evaluator/requirements.txt ./
RUN set -eux; \
    npm ci --no-audit --no-fund; \
    python3 -m venv venv; \
    venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt; \
    # Checks import '@playwright/test' from /checks: resolve it to this install.
    ln -s /opt/evaluator/node_modules /node_modules

COPY --chown=root:root --chmod=0644 containers/evaluator/playwright.config.mjs ./

ENV NODE_PATH=/opt/evaluator/node_modules \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp \
    CI=1

# The image works before it is used: the pinned runner, the pinned browser, pytest.
RUN set -eux; \
    test "$(node_modules/.bin/playwright --version)" = "Version 1.63.0"; \
    node -e "require('@playwright/test').chromium.launch().then(b => b.newPage().then(p => p.setContent('<p>ok</p>').then(() => p.textContent('p'))).then(t => { if (t !== 'ok') process.exit(1); return b.close() }))"; \
    venv/bin/python -m pytest --version; \
    venv/bin/python -c "import httpx"

USER 10002:10002
WORKDIR /checks
