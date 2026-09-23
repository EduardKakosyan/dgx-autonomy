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
agent container (uid 10001, no caps, no socket)
   │  PID 1 supervisor ─┬─ agent session: Agent Server + everything its tools start
   │                    └─ demo session:  the command start_demo asked for
   │                                      published on 127.0.0.1:<host port> only
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
  own workspace, a read-only copy of the brief and a read-only control directory,
  and nothing else from the host. PID 1 is a small supervisor
  (`containers/sandbox/dgx_sandbox.py`) that keeps agent execution and the demo in
  separate sessions, so ending the agent does not end the demo.
- The **inference container** is llama.cpp `f95b0d9`, built for GB10 (`sm_121a`). It
  serves the model in `config/models.yaml` on the internal network only.

The default model is `qwen3.6-35b-a3b` (Q3, about 17 GB). It fits next to
`claude-qwen`, so runs work without displacing it. `dgx-autonomy reserve` displaces
`claude-qwen` explicitly (see [the reservation](#the-inference-reservation)). That and
the agent's egress policy are the only host changes the environment makes. Both are
installed by the operator from `host/`.

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
   exactly three mounts (the workspace, rw; `brief.md`, ro; the run's `control/`
   directory at `/dgx-control`, ro), no `docker.sock`, and one port binding:
   `3000/tcp` on `127.0.0.1:<host port>` (43000 and up, one per run).

5. **Install the host pieces** (egress policy, reservation helper, busy notice,
   sudoers entry). Review every file in `host/` first. Then:

   ```bash
   sudo ~jim/dgx-autonomy/host/install.sh
   ```

   It checks the sudoers entry with `visudo -c` and the rules with `nft -c`,
   installs root-owned copies, enables `dgx-autonomy-egress.service` (loaded before
   Docker on every boot) and `dgx-autonomy-busy-notice.service` (runs only while a
   reservation is held), and loads the rules now. It does not touch `claude-qwen`.

6. **Recreate the networks with their fixed bridge names** (`dgx-egress`,
   `dgx-internal`) if they were created before Phase 3. The rules match those
   names, and the controller refuses to create an agent container while they
   differ. Agent containers from earlier runs are still attached to the old networks,
   so remove them first. This takes their retained demos down too:

   ```bash
   cd ~jim/dgx-autonomy/containers
   sudo docker ps -a --filter label=dgx-autonomy.role --format '{{.Names}}'   # review
   sudo docker rm -f $(sudo docker ps -aq --filter label=dgx-autonomy.role)
   sudo docker compose down
   sudo docker compose --profile images build controller
   sudo docker compose up -d controller
   ```

   Then, as jim, `dgx-autonomy egress` should print `egress policy in place`.

## CLI (as jim, over SSH)

Put the CLI on the PATH once: `ln -sf ~/dgx-autonomy/.venv/bin/dgx-autonomy ~/.local/bin/`.

```bash
dgx-autonomy launch --brief brief.md [--budget-hours 40] [--model qwen3.6-35b-a3b]
dgx-autonomy status [RUN_ID]          # phase, outcome, deadline, demo, stop evidence
dgx-autonomy logs RUN_ID [--follow]   # summarized OpenHands events
dgx-autonomy stop RUN_ID              # end agent execution now; the demo stays up
dgx-autonomy tunnel [RUN_ID]          # prints: ssh -N -L <p>:127.0.0.1:<p> hugo-dgx1
dgx-autonomy ps [RUN_ID]              # the sandbox's processes, by role
dgx-autonomy runs
dgx-autonomy inference [status|up]
dgx-autonomy reserve [--model M]      # displace claude-qwen, start the owned llama-server
dgx-autonomy release                  # remove the owned llama-server, restore claude-qwen
dgx-autonomy reservation              # held or not, since when, memory available
dgx-autonomy egress [HOST:PORT ...]   # is the egress policy in place / probe through it
```

The CLI sends the contents of the brief file, because the controller cannot read
jim's files. The controller stores its own copy at
`/var/lib/dgx-autonomy/runs/<id>/brief.md`, and the agent sees it read-only at
`/brief/brief.md`. The project the agent builds is at
`/var/lib/dgx-autonomy/runs/<id>/agent/project`.

`--budget-hours` must be at most 40. The deadline is written once at launch and
never moves.

### Deadline, stop and the demo

A watchdog thread in the controller checks the clock every second, independently
of the reconcile loop, so a hung model request cannot delay it. At the deadline
(outcome `expired`) or on `dgx-autonomy stop` (outcome `stopped`) the controller:

1. persists the stop (phase `stopping`) and switches the sandbox to `demo-only`,
   so a restarted sandbox never starts the Agent Server again;
2. calls the Agent Server's `/interrupt` (cancels the in-flight LLM call) and
   `/pause`, then waits up to 30 s for the conversation to go quiet;
3. ends every `agent` process in the sandbox (SIGTERM, 5 s, then SIGSTOP+SIGKILL),
   sparing the demo session;
4. checks that no `agent` process is left. If some are, it restarts the sandbox
   (it comes back `demo-only`) and relaunches the recorded demo command. If it still
   cannot show that the agent has stopped, the run stays `stopping`, the evidence
   says `failed`, and the controller keeps trying;
5. records the evidence on the run (`status` shows it). The phase is then `stopped`.

"Agent process" means everything in the sandbox except PID 1, processes the
controller created with `docker exec`, and the demo session. That includes what
daemonized away from the Agent Server, such as the terminal tool's tmux server.

**`pause()` alone does not end the tools' processes (Agent Server 1.49.4, measured
on hugo-dgx1).** After `/interrupt` and `/pause` the conversation reported `paused`
right away. The foreground tool call was cancelled ("Tool call interrupted before
completion"), but seven agent processes were still alive: the Agent Server (2),
its openvscode-server (2), the tmux server, its bash, and a `nohup sleep 100000 &`
the agent had started. The kill in step 3 is what ends agent execution; the pause
only stops the agent from starting new steps.

The agent serves its app with the `start_demo(command, port)` tool. The tool writes
a request into the workspace. The controller validates it (port 3000, a bounded
command), records it, and runs the command in its own session with `PORT=3000` and
`HOST=0.0.0.0`. Output goes to `/workspace/.dgx/demo.log`. Processes the agent starts
itself (`&`, `nohup`) end when agent execution ends.

To open a demo from the laptop, run the command `dgx-autonomy tunnel RUN_ID` prints
on the DGX, keep it running, and browse to `http://127.0.0.1:<p>/`. The demo is bound
to DGX loopback only.

### The agent's network boundary

The agent container is on two Docker networks: `dgx-autonomy-internal` (the owned
llama-server; Docker `internal`, no route out) and `dgx-autonomy-egress` (the
internet). A Docker bridge is not a policy, so the host adds one:
`host/nftables-autonomy.nft`, its own `inet dgx_autonomy` table. It does not touch
Docker's, Tailscale's or anyone else's rules, and Docker does not flush it. From the
two bridges:

- **allowed:** the internet, other containers on the same bridge (the model), and
  replies to connections the host opened (the controller, the demo's loopback port);
- **rejected at once:** every address of the DGX itself (Docker gateways,
  `192.168.50.216`, `100.75.80.123`: sshd, open-webui, anything on `0.0.0.0`), the
  LAN, the tailnet (`100.64.0.0/10`, `fd7a:…`), other Docker networks, link-local
  and multicast.

The host's DNS upstream is the LAN router, which is on the other side of that line,
so the agent resolves names through public resolvers (`--dns 1.1.1.1 --dns 9.9.9.9`).

The controller cannot read the host's firewall. Before it creates or restarts an agent
sandbox, it checks two things. First, the marker that `dgx-autonomy-egress` writes
when it loads the rules must name the current boot. Second, both networks must use
the bridge names the rules match. If either check fails, the run fails with the
reason, and no container is created. `dgx-autonomy egress HOST:PORT ...` starts a
throwaway container with the agent's image, networks, DNS and hardening, and reports
what it can reach.

The rules were checked with nftables 1.0.9 (the DGX's version) in a privileged
container with network namespaces standing in for the agent, the LAN, the tailnet
and the internet. Before loading, everything was reachable. After loading, only the
internet was, and host-to-demo still worked.

### The inference reservation

`claude-qwen` (`/etc/systemd/system/claude-qwen.service`, as jim, `Restart=always`,
enabled) serves Qwen3.8-27B Q8 on `127.0.0.1:8090` and uses about 37 GB. Larger
models for the environment only fit after it is displaced. That is
operator-authorized, and only for this service.

`dgx-autonomy reserve` runs `sudo -n /usr/local/sbin/dgx-autonomy-reservation
reserve`, which:

1. records the unit, its drop-ins, `systemctl cat`, whether it is enabled and active,
   and its client address in `/var/lib/dgx-autonomy/reservation/`;
2. adds one drop-in, `claude-qwen.service.d/50-dgx-autonomy-reservation.conf`, with
   `ConditionPathExists=!/var/lib/dgx-autonomy/reservation/record.json`. The unit file
   is not edited. While the record exists, `systemctl start`, `Restart=always` and a
   reboot all skip the service. Masking is not used: a unit in `/etc/systemd/system`
   cannot be masked in place;
3. stops it, and measures the memory that frees (at least the size of its weights);
4. starts `dgx-autonomy-busy-notice.service` on the same address. It answers every
   request with 503 and "DGX inference is in use by the autonomous coding
   environment", as an Anthropic error for `/v1/messages` and an OpenAI error for
   everything else.

Then the CLI starts the owned llama-server. Finishing, failing or expiring a run
does not release anything.

`dgx-autonomy release` removes the owned llama-server. The controller refuses while
a run is active. The CLI then runs the helper's `release`, which **refuses, and
changes nothing, unless the memory `claude-qwen` used plus 8 GiB headroom is
available**. Retained demos hold memory. Otherwise it stops the notice, removes the
drop-in, compares the unit, its drop-ins and `systemctl cat` with the record,
restores the prior enabled state, starts the service if it was running, and waits
for `/health`. Differences are reported, not overwritten: someone else made them. The
record is archived under `reservation/released/`.

The sudoers entry (`host/sudoers-autonomy`) lets jim run exactly `reserve`,
`release` and `status`. The helper is a root-owned, standard-library-only copy of
`src/dgx_autonomy/reservation.py`, run with `python3 -I`, and it takes no options.

## Layout on the DGX

```text
/var/lib/dgx-autonomy/            controller-owned (root), mounted at the same path in the controller
  control/control.sock            0600, owned by jim
  state/controller.sqlite3        runs, operations, demos (never mounted into agent/inference)
  policy/egress.json              written by dgx-autonomy-egress: boot id, rules sha256
  reservation/record.json         present while claude-qwen is displaced (host helper)
  reservation/prior/              the unit text and `systemctl cat` before reserve
  runs/<id>/brief.md              frozen copy, mounted read-only into the agent
  runs/<id>/control/              mounted read-only at /dgx-control: mode, demo.json
  runs/<id>/secrets/              Agent Server session key and secret key, 0700 root
  runs/<id>/agent/                uid 10001; the agent's /workspace
    project/                      what the agent builds
    conversations/                OpenHands SDK persistence (SDK-owned format); read
                                  by `logs`/`status` once the Agent Server is gone
    .dgx/                         start_demo request and the demo's log
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
ssh hugo-dgx1 'cd ~/dgx-autonomy && ~/.local/bin/uv run pytest -m dgx tests/dgx/test_stop_retains_demo.py -s'
ssh hugo-dgx1 'cd ~/dgx-autonomy && ~/.local/bin/uv run pytest -m dgx tests/dgx/test_egress.py'
# stops claude-qwen for a few minutes; opt in explicitly:
ssh hugo-dgx1 'cd ~/dgx-autonomy && DGX_AUTONOMY_RESERVATION_TEST=1 ~/.local/bin/uv run pytest -m dgx tests/dgx/test_reserve_release.py -s'
```

`test_stop_retains_demo.py` takes about 6 minutes (a 0.1 h budget). With `-s` it
prints the stop evidence, including which processes were alive after the pause.
`test_egress.py` tries every address of the DGX from the agent's network position
and expects each to be rejected at once, not to time out.
