# dgx-autonomy

An environment for unattended OpenHands coding runs on the DGX Spark (`hugo-dgx1`).
It is a separate Python package with its own toolchain. The beach app's Next.js
build, lint, typecheck and Vitest scopes all skip `autonomy/`.

```text
dgx-autonomy CLI (jim, over SSH)
   │  unix socket, mode 0600, owned by jim
   ▼
controller container ── docker.sock ──► Docker daemon
   │ REST, private network                │
   ▼                                      ▼
agent container (Agent Server, uid 10001, no caps, no socket)
   │ private network
   ▼
llama-server container (GPU, model mounted read-only, not published)
```

- The **controller** is the only container that mounts the Docker socket. It keeps
  lifecycle state in SQLite, with a deadline that is fixed at launch. It creates the
  other containers and watches the OpenHands conversation. Compose restarts it
  (`restart: unless-stopped`).
- The **agent container** runs the pinned OpenHands Agent Server (1.49.4) and adds
  Node 22 and pnpm. It runs as the unprivileged `openhands` user (10001) with
  `--cap-drop ALL` and `no-new-privileges`. Passwordless sudo is removed. It sees its
  own workspace and a read-only copy of the brief, and nothing else from the host.
- The **inference container** is llama.cpp `f95b0d9`, built for GB10 (`sm_121a`). It
  serves the model in `config/models.yaml` on the internal network only.

Phase 1 uses `qwen3.6-35b-a3b` (Q3, about 17 GB). It fits next to `claude-qwen`, so
this phase stops and modifies no existing host service.

## Operator setup (once, privileged)

`jim` is not in the `docker` group, so building images and starting the controller
need an operator with sudo. Everything after this runs as `jim` without Docker access.

1. **Deploy the code** from the laptop (rsync plus `uv sync`, no sudo):

   ```bash
   autonomy/scripts/deploy.sh            # DGX_HOST=hugo-dgx1 DGX_DEST=dgx-autonomy by default
   ```

   The script writes `~/dgx-autonomy/containers/.env` on the DGX with jim's uid/gid
   and the host paths. Compose reads it.

2. **Download the model** as `jim`. This is unprivileged and about 17 GB:

   ```bash
   ~/.local/bin/uvx --from huggingface_hub hf download unsloth/Qwen3.6-35B-A3B-GGUF \
     Qwen3.6-35B-A3B-UD-Q3_K_XL.gguf \
     --revision a483e9e6cbd595906af30beda3187c2663a1118c \
     --local-dir ~/models/qwen3.6-35b-a3b
   ```

   llama-server runs as uid 65534. The model directory and files must be
   world-readable (`chmod -R o+rX ~/models/qwen3.6-35b-a3b`).

3. **Build the images and start the controller** as the operator:

   ```bash
   sudo install -d -m 0755 /var/lib/dgx-autonomy
   cd ~jim/dgx-autonomy/containers
   sudo docker compose --profile images build     # controller, inference (sm_121a check), agent
   sudo docker compose up -d controller
   sudo docker compose logs -f controller          # expect "control socket at ..."
   ```

   The inference build fails on purpose if the compiled CUDA code does not contain
   `sm_121a`.

4. **Check isolation** after the first run creates an agent container:

   ```bash
   sudo docker inspect dgx-autonomy-agent-<run-id> --format \
     'user={{.Config.User}} caps={{.HostConfig.CapDrop}} sec={{.HostConfig.SecurityOpt}}
   mounts={{range .Mounts}}{{.Source}}->{{.Destination}}({{if .RW}}rw{{else}}ro{{end}}) {{end}}
   ports={{.HostConfig.PortBindings}}'
   ```

   Expected result: `user=10001:10001`, `caps=[ALL]`, `sec=[no-new-privileges]`,
   exactly two mounts (the workspace, rw, and `brief.md`, ro), no `docker.sock`, and
   no port bindings.

## CLI (as jim, over SSH)

Put the CLI on the PATH once: `ln -sf ~/dgx-autonomy/.venv/bin/dgx-autonomy ~/.local/bin/`.

```bash
dgx-autonomy launch --brief brief.md [--budget-hours 40] [--model qwen3.6-35b-a3b]
dgx-autonomy status [RUN_ID]          # phase, deadline, operations, last event
dgx-autonomy logs RUN_ID [--follow]   # summarized OpenHands events
dgx-autonomy runs
dgx-autonomy inference [status|up]
```

The CLI sends the contents of the brief file, because the controller cannot read
jim's files. The controller stores its own copy at
`/var/lib/dgx-autonomy/runs/<id>/brief.md`, and the agent sees it read-only at
`/brief/brief.md`. The project the agent builds is at
`/var/lib/dgx-autonomy/runs/<id>/agent/project`.

`--budget-hours` must be at most 40. The deadline is written once at launch. Phase 1
records the deadline but does not enforce it yet; Phase 2 adds enforcement.

## Layout on the DGX

```text
/var/lib/dgx-autonomy/            controller-owned (root), mounted at the same path in the controller
  control/control.sock            0600, owned by jim
  state/controller.sqlite3        runs + operations (never mounted into agent/inference)
  runs/<id>/brief.md              frozen copy, mounted read-only into the agent
  runs/<id>/secrets/              Agent Server session key and secret key, 0700 root
  runs/<id>/agent/                uid 10001; the agent's /workspace
    project/                      what the agent builds
    conversations/                OpenHands SDK persistence (SDK-owned format)
/home/jim/models/                 GGUFs, mounted read-only into inference at /models
```

## Development

```bash
cd autonomy
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run pytest -m "not dgx"          # no Docker, SSH or model needed
```

Target-host smoke tests (on the DGX, controller running, model downloaded). `uv` is
not on the non-interactive SSH PATH, so call it by its full path:

```bash
ssh hugo-dgx1 'cd ~/dgx-autonomy && ~/.local/bin/uv run pytest -m dgx tests/dgx/test_tool_calls.py tests/dgx/test_trivial_run.py'
```
