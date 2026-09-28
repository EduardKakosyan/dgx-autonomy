# syntax=docker/dockerfile:1.7
#
# The agent sandbox: the pinned OpenHands Agent Server plus a Node 22 / pnpm
# toolchain for building web apps.
#
# The controller runs this image with --user 10001:10001, --cap-drop ALL,
# --security-opt no-new-privileges, resource limits, no Docker socket, and one
# published port: the demo's, on host loopback only (runtime.py). This file also
# removes the image's passwordless sudo, so the sandbox does not depend on
# no-new-privileges alone.
#
# PID 1 is sandbox/dgx_sandbox.py, not the base image's tini. It starts the Agent
# Server in a session of its own and stays up when the controller ends agent
# execution, so the demo (another session) survives. With /dgx-control/mode set to
# `demo-only` it never starts the Agent Server, including after a restart.
#
# Build context: autonomy/ (compose sets it to ..), for src/dgx_autonomy/demo_tool.py.

# 1.49.4-python, pinned by the multi-arch index digest (linux/arm64 included).
ARG AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.49.4-python@sha256:9b215fb9ad536bd6bc07964b046a45f378cd05c0d0a856bd1be3adaf6c46c69c
FROM ${AGENT_SERVER_IMAGE}

ARG TARGETARCH
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256_ARM64=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
ARG NODE_SHA256_AMD64=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
ARG PNPM_VERSION=10.12.1

USER root

# Node 22 in /opt/node22, first on PATH. The image's Node 24 stays for the Agent Server.
RUN set -eux; \
    if ! command -v curl >/dev/null || ! command -v xz >/dev/null; then \
      apt-get update; \
      apt-get install -y --no-install-recommends ca-certificates curl xz-utils; \
      rm -rf /var/lib/apt/lists/*; \
    fi; \
    case "${TARGETARCH:-arm64}" in \
      arm64) arch=arm64; sha="${NODE_SHA256_ARM64}" ;; \
      amd64) arch=x64;   sha="${NODE_SHA256_AMD64}" ;; \
      *) echo "unsupported arch ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    tarball="node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    curl -fsSLo "/tmp/${tarball}" "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"; \
    echo "${sha}  /tmp/${tarball}" | sha256sum -c -; \
    mkdir -p /opt/node22; \
    tar -xJf "/tmp/${tarball}" -C /opt/node22 --strip-components=1; \
    rm "/tmp/${tarball}"; \
    # The base image sets an npm prefix of its own; pin this install to /opt/node22.
    /opt/node22/bin/npm install -g --prefix /opt/node22 "pnpm@${PNPM_VERSION}"; \
    /opt/node22/bin/node --version; \
    /opt/node22/bin/pnpm --version

# No privilege escalation inside the sandbox.
RUN set -eux; \
    sed -i '/^openhands ALL=(ALL) NOPASSWD:ALL$/d' /etc/sudoers; \
    rm -f /etc/sudoers.d/openhands; \
    (gpasswd -d openhands sudo || true); \
    ! grep -Rqs '^[^#]*NOPASSWD' /etc/sudoers /etc/sudoers.d

# The supervisor/helper and the builder's tools (start_demo, write_handoff,
# declare_blocked, and terminal_grouping, which lets the terminal tool run a
# heredoc and the line after it as one command), root-owned so the agent (uid 10001) cannot change what the
# controller runs inside the sandbox. The Agent Server loads the tools with
# --import-modules; they import only the SDK, pydantic and the standard library.
COPY --chown=root:root --chmod=0644 containers/sandbox/dgx_sandbox.py /opt/dgx-autonomy/dgx_sandbox.py
COPY --chown=root:root --chmod=0644 src/dgx_autonomy/__init__.py src/dgx_autonomy/demo_tool.py \
     src/dgx_autonomy/handoff_tool.py src/dgx_autonomy/terminal_grouping.py \
     /opt/dgx-autonomy/tools/dgx_autonomy/
RUN set -eux; \
    chmod 0755 /opt/dgx-autonomy /opt/dgx-autonomy/tools /opt/dgx-autonomy/tools/dgx_autonomy; \
    /usr/local/bin/python3 -I -m py_compile /opt/dgx-autonomy/dgx_sandbox.py; \
    /usr/local/bin/python3 -I /opt/dgx-autonomy/dgx_sandbox.py ps > /dev/null; \
    rm -rf /opt/dgx-autonomy/__pycache__

# Global installs go to the agent's own workspace, the only place it can write.
ENV PATH=/opt/node22/bin:/workspace/.npm-global/bin:/workspace/.pnpm-home:${PATH} \
    PNPM_HOME=/workspace/.pnpm-home \
    npm_config_prefix=/workspace/.npm-global

USER 10001:10001

ENTRYPOINT ["/usr/local/bin/python3", "-I", "/opt/dgx-autonomy/dgx_sandbox.py", "supervise", "--", \
            "/usr/local/bin/openhands-agent-server", \
            "--extra-python-path", "/opt/dgx-autonomy/tools", \
            "--import-modules", "dgx_autonomy.demo_tool,dgx_autonomy.handoff_tool,dgx_autonomy.terminal_grouping"]
