# DGX restart checklist (manual)

This is a manual checklist, not a test file: pytest does not collect it, and `dgx-autonomy readiness` lists it as manual without running it. A real reboot of the DGX cannot be automated from the operator account, so this is a script to follow. It checks two things. A run resumes after the DGX restarts, with nobody connected. A deadline that passes while the DGX is off ends the run as `expired`, and the demo is kept.

Coordinate the reboot first: it also restarts `claude-qwen` and everything else on the DGX. If the inference reservation is held (`dgx-autonomy reservation`), `claude-qwen` stays down after the reboot, as intended.

Global options go before the command: `dgx-autonomy --json status RUN` prints every field. Plain `status` prints a summary, which leaves some of the fields below out.

## Part 1: the run resumes

1. Launch a run that works for a while and serves a demo. `--model` must name the model the DGX can serve: with the reservation held, the one it serves (for example `qwen3.8-flash-next-sglang`); without it, `qwen3.6-35b-a3b`. The default in `config/models.yaml` needs the reservation.

   ```bash
   cat > /tmp/reboot-brief.md <<'EOF'
   Do these steps in order. Each is required.
   1. Create index.html in the project directory whose body is exactly: reboot demo ok
   2. Call start_demo with command `python3 -m http.server 3000 --bind 0.0.0.0` and port 3000.
   3. Run `sleep 300` in the terminal with the timeout set to 360 seconds, and wait for it.
   4. Create after-reboot.txt containing: done
   5. Run `sleep 120` the same way, then finish.
   EOF
   dgx-autonomy launch --brief /tmp/reboot-brief.md --budget-hours 1 --model MODEL
   ```

2. Wait until `dgx-autonomy status RUN` shows the demo `running` and `dgx-autonomy logs RUN` shows the `sleep 300` action. Note the `conversation_id` and `deadline_at` (plain `status` shows both), and the agent container's `id` under `containers` (only in `dgx-autonomy --json status RUN`).
3. Disconnect the laptop. On the DGX, as the operator: `sudo reboot`.
4. Reconnect after the DGX is back (do not run anything else first). Wait until the model server is ready again and the recovery has finished: about 5 minutes with llama.cpp. SGLang takes longer, because it rewrites its N-gram table at every start; its load took about 12 minutes (715 s) in qualification, so allow 20 minutes. `dgx-autonomy inference` shows the model server's `health`.
5. `dgx-autonomy status RUN`. Expect:
   - [ ] `phase` is `running` or `finished`, with the same `conversation_id` and `deadline_at`
   - [ ] one `recovery #1 done`, caused by `llama-server is exited …; the agent sandbox is exited …`, with `conversation resumed from error` (the cause names the model server `llama-server` with either backend)
   - [ ] one agent container, with the same `id` as before (`dgx-autonomy --json status RUN`, under `containers`)
   - [ ] the demo `running` again, and `curl -s 127.0.0.1:<host_port>/index.html` returns `reboot demo ok`. In `dgx-autonomy --json status RUN`, the recovery's `steps` include `demo_relaunched`, and the demo's `message` reads `listening on port 3000` (it reads `relaunched from the recorded spec…` only until the demo listens again)
6. `dgx-autonomy logs RUN`. Expect exactly two user messages: the brief, then the recovery notice ("Your sandbox was restarted…").
7. Wait for `finished`, then check that `after-reboot.txt` exists in the `workspace_dir`.
8. `sudo docker compose -f ~<user>/dgx-autonomy/containers/compose.yaml logs controller | grep -E "writer lock|startup reconciliation|recover"`. It shows `the DGX restarted since`, and then the recovery.

## Part 2: the deadline passes while the DGX is off

1. Launch the same brief with `--budget-hours 0.1` (6 minutes) and the same `--model`. Wait for the demo to be `running`.
2. `sudo poweroff`, and leave the DGX off until at least 10 minutes after the launch. Then power it on. Without physical access, use `sudo systemctl reboot` about 30 s before the `deadline_at` instead: the DGX takes about a minute to come back, so the deadline passes while it is down.
3. Reconnect and run `dgx-autonomy status RUN`. Expect:
   - [ ] `phase` `stopped`, `outcome` `expired`, and the same `deadline_at`
   - [ ] no recoveries, and exactly one user message in `logs`
   - [ ] the `stop` line says agent execution ended `verified`; in `dgx-autonomy --json status RUN`, `stop_evidence` has `container_running: false` and `demo.relaunched: true`
   - [ ] `dgx-autonomy ps RUN` shows only the supervisor and the demo, with no Agent Server
   - [ ] the demo answers on `127.0.0.1:<host_port>`

Record the results (times, the recovery's cause and steps, anything unexpected) with the run, for example in the operator log. The first results are in the Phase 4 section of [the structure outline](../../docs/design/05-structure-outline.md).
