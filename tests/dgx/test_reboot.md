# DGX restart checklist (manual)

A real reboot of hugo-dgx1 cannot be automated from the operator account, so this is a script to follow. It checks two things. A run resumes after the DGX restarts, with nobody connected. A deadline that passes while the DGX is off ends the run as `expired`, and the demo is kept.

Coordinate the reboot first: it also restarts `claude-qwen` and everything else on the DGX. If the inference reservation is held (`dgx-autonomy reservation`), `claude-qwen` stays down after the reboot, as intended.

## Part 1: the run resumes

1. Launch a run that works for a while and serves a demo:

   ```bash
   cat > /tmp/reboot-brief.md <<'EOF'
   Do these steps in order. Each is required.
   1. Create index.html in the project directory whose body is exactly: reboot demo ok
   2. Call start_demo with command `python3 -m http.server 3000 --bind 0.0.0.0` and port 3000.
   3. Run `sleep 300` in the terminal with the timeout set to 360 seconds, and wait for it.
   4. Create after-reboot.txt containing: done
   5. Run `sleep 120` the same way, then finish.
   EOF
   dgx-autonomy launch --brief /tmp/reboot-brief.md --budget-hours 1
   ```

2. Wait until `dgx-autonomy status RUN` shows the demo `running` and `dgx-autonomy logs RUN` shows the `sleep 300` action. Note the `conversation_id`, the `deadline_at` and the agent container id under `container`.
3. Disconnect the laptop. On the DGX, as the operator: `sudo reboot`.
4. Reconnect after the DGX is back (do not run anything else first), then wait 5 minutes.
5. `dgx-autonomy status RUN`. Expect:
   - [ ] `phase` is `running` or `finished`, with the same `conversation_id` and `deadline_at`
   - [ ] one `recovery #1 done`, caused by `llama-server is exited …; the agent sandbox is exited …`, with `conversation resumed from error`
   - [ ] one agent container, with the same id as before
   - [ ] the demo `running` again (`relaunched from the recorded spec…` in its history), and `curl -s 127.0.0.1:<host_port>/index.html` returns `reboot demo ok`
6. `dgx-autonomy logs RUN`. Expect exactly two user messages: the brief, then the recovery notice ("Your sandbox was restarted…").
7. Wait for `finished`, then check that `after-reboot.txt` exists in the `workspace_dir`.
8. `sudo docker compose -f ~jim/dgx-autonomy/containers/compose.yaml logs controller | grep -E "writer lock|startup reconciliation|recover"`. It shows `the DGX restarted since`, and then the recovery.

## Part 2: the deadline passes while the DGX is off

1. Launch the same brief with `--budget-hours 0.1` (6 minutes). Wait for the demo to be `running`.
2. `sudo poweroff`, and leave the DGX off until at least 10 minutes after the launch. Then power it on. Without physical access, use `sudo systemctl reboot` about 30 s before the `deadline_at` instead: the DGX takes about a minute to come back, so the deadline passes while it is down.
3. Reconnect and run `dgx-autonomy status RUN`. Expect:
   - [ ] `phase` `stopped`, `outcome` `expired`, and the same `deadline_at`
   - [ ] no recoveries, and exactly one user message in `logs`
   - [ ] stop evidence `verified`, `container_running: false`, demo `relaunched: true`
   - [ ] `dgx-autonomy ps RUN` shows only the supervisor and the demo, with no Agent Server
   - [ ] the demo answers on `127.0.0.1:<host_port>`

Record the results (times, the recovery's cause and steps, anything unexpected) in the Phase 4 section of the structure outline.
