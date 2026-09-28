# Governance log: unattended runs on hugo-dgx1

The operator's side of the experiments: what is in flight, and what the next session does. Update it at every hand-over.

## How to follow the DGX without polling

- Feed: `ssh hugo-dgx1 '~/.local/bin/dgx-autonomy notify --follow'` (all milestones: `plan.waiting`, `plan.dryrun`, `run.launched`, `progress`, `claim`, `evaluation`, `handoff`, `rollover`, `recovery`, `run.ended`, `qualify.done`).
- Laptop helper: `/tmp/dgx-notify.sh` (resumes after the number in `/tmp/dgx-notify.since`; run it under Monitor and re-arm every 30 min). The formatter is `/tmp/dgx-notify-fmt.py`.
- SSH without the key agent: `ssh -o IdentityAgent=none hugo-dgx1`. Privileged steps: `/tmp/dgxsudo.sh 'cmd'` (password from `/tmp/.dgx-sudo`, 0600, supplied by the user on 2026-09-22 for operator steps).
- Check-in: run `/tmp/dgx-watch.sh` under Monitor (30-min timeout; re-arm on expiry). It prints a `HEARTBEAT` line at start and every 30 minutes (from `/tmp/dgx-heartbeat.py` on the DGX), then every feed milestone. A line ending in `!!` needs the operator: a planner waiting, or no agent event for 45+ minutes. A new session's first action is to re-arm it. Cloud `/schedule` routines cannot do this job: the DGX is reachable only over Tailscale, with the SSH key on this Mac.
- **A `plan.waiting` notice needs an answer.** The planner does nothing until the operator replies (`/tmp/plansend.py PLAN_ID FILE`, or `dgx-autonomy plan --attach`). On 2026-09-24 a session ended right after one, and the planner waited over an hour while claude-qwen stayed displaced.

## State (2026-09-24 14:15 UTC)

- Model: Qwen3.8-Flash-Next, qualified at 256K context (record `20260924T123724.json`): 206K-token prompt at 387 tok/s prefill, 8.4 tok/s decode. Thinking budget 32K, output 64K per response.
- Reservation: **held** since 11:57 UTC; claude-qwen displaced (busy notice on :8090).
- Plan in flight: `20260924-125324-f96a94`, the weather app rebuilt with a UI/UX bar (tmux session `weather2` on the DGX). The planner agreed 7 proposals; answered at 14:14 UTC. Previous checks are copied into its workspace at `/workspace/previous/`.
- Queued next: the personal budget tracker. Its request is ready at `/tmp/budget-request2.txt` on the DGX; it is seeded with the earlier draft brief, `~/budget-brief-v0.md`.

### 16:52 UTC

- Still planning, 4 hours in. The planner never wrote `brief.md`. Its memory was condensed twice (16:23, 16:38) by OpenHands' 240-event default, and it began suspecting its files had been replaced. At 16:50 it was told exactly what is on disk and the order to finish in.
- Fixed for every new conversation (commit bbb2d1f): only the context limit condenses. The running planner keeps its old condenser.
- The checks on disk: 7 carried-over specs (errors extended), `fixtures.ts`, `visual.ts`, and 8 new specs (contrast, background coverage, overflow, tap targets, theme persisted, theme follows system, loading skeleton, extended data). Reference app: `reference/index.html` (24 KB).

### 18:04 UTC: weather rebuild launched

- Run **`20260924-180353-eabd48`**, 40 h budget, deadline 2026-09-26 10:03 UTC. 15 automated checks (57 tests; `loading-skeleton` not required), 1 human judgment.
- Before launch: the planner's own negative control failed 12 checks on the white-on-white copy (contrast ratio 1 vs 4.5). The controller's dry run passed: every check failed on nothing and passed on the reference.
- `dgx-autonomy-release-after@20260924-180353-eabd48` (systemd user unit) returns claude-qwen when the run ends. The budget tracker then needs `dgx-autonomy reserve` again.

### 00:10 UTC (2026-09-25)

- 6 h in. The builder committed once (21:02, "57 acceptance checks green") and has not claimed completion. It is polishing the UI and has since rewritten `styles.css` from scratch (23:25). That left 2 `background-coverage` failures (results state, light and dark). The cause: its decorative data bars (hourly bars, day range bars) are opaque paint that the sampler treats as a background. It is moving them to inline SVG, which the checker's background walk ignores. Look at this when it claims. Data bars in SVG are reasonable, but the check must not have been dodged.
- Friction seen repeatedly: the terminal tool rejects heredocs ("Cannot execute multiple commands at once"). The builder then writes a helper file instead. It costs a turn each time, but the builder has not got stuck on it.
- `dgx-autonomy logs RUN` shows only the first 200 events. For recent ones, use `--since N` (the run was at about 560 events).

### 00:40 UTC: two environment changes (uncommitted, deployed)

- **Heredoc fix** (`autonomy/src/dgx_autonomy/terminal_grouping.py`, loaded by the Agent Server with `--import-modules`). The cause was not heredocs as such. All 21 refusals in this run were a heredoc followed by a second line (write a file, then run it). OpenHands 1.49.4 refuses any input with more than one top-level statement on separate lines, and `&&` cannot follow a heredoc terminator. The module wraps such input in `{ ...\n}`, which runs in the same shell, line by line. Verified inside the rebuilt agent image's own bundle (tmux backend). **Only new sandboxes get it.** The weather run keeps its old image, including across its rollover.
- **Operator review** (schema v7, controller restarted 00:33, DB backed up to `state/backup-pre-v7-*`). `dgx-autonomy hold RUN`: a claim that passes every check waits (`run.review` notice, marked `!!` in the feed) instead of finishing. The reservation stays. `dgx-autonomy feedback RUN FILE`: sends product direction (`OPERATOR FEEDBACK #n`); the builder works, then claims again and is re-evaluated. Kept in `runs/<id>/feedback.jsonl` and carried into rollover context. `dgx-autonomy accept RUN`: finishes as VERIFIED. **The weather run is held.**
- At the restart the builder was at 223K of 262K context. The rollover to conversation #2 had begun (handoff requested) and resumed normally after the restart.

### 10:30 UTC (2026-09-25): first product review, feedback #1 sent

- Rollovers at 00:50 (#2) and 06:59 (#3), both from the builder's own handoff. Checkpoint #2: 11 of 12 roadmap items done; the last was "serve the demo, then finish". The demo came up at 07:03, but the builder did not finish. It spent 10:00–10:30 writing Lab-space searches to maximise the perceptual distance between hero palettes. It has not claimed yet.
- Reviewed the live demo (`weather3-review1-*.png`). A big step up from v1: warm empty state, strong hero, good type scale. Five product issues went to the builder as feedback #1 (`weather3-feedback-1.txt`), in this order: star dots drawn over the text on the night hero; desktop forecast cards stretched tall with the hours below the fold; the sun reads brown; unexplained bars on the hour cards and a brown day-range bar; the daytime hero glares in dark mode. Also told it to stop optimising the palette. Sent while it was working, not after a claim, because it was not converging on finishing.

### 15:00 UTC: feedback #1 acted on; rollover #4 pending

- The builder switched to the feedback at 10:45 (its first action after the message was already in flight: one more palette script). It reports: #1 root cause (the sky SVG stretched with `preserveAspectRatio: none` over the whole card), decoration now confined to a clipped sky window; #3 sun/moon/bolt icons get their own colours; #5 dark-theme day skies become deep dusk skies with light text; #4 a temperature colour ramp on the range bar; #2 desktop recomposed (natural 87 px day rows; hours strip starts at y=737, inside the fold at 1280×800). It stripped palette "distinctiveness" out of its gate, as told. 57/57 at 13:36 and 14:06. Not yet reviewed by the operator.
- A handoff was requested at 14:41 (226K of 262K). The builder ignored it and kept debugging its new decoration harness (`audit/decor.js`). The controller waits `request_timeout_s + handoff_timeout_s` = 100 min before falling back (about 16:22), so a builder that ignores the request can keep filling its context for over an hour. **Environment improvement to consider:** re-send the handoff request every few minutes, and fall back sooner when the builder keeps acting without calling `write_handoff`.

### 15:25 UTC: weather run stopped for the SGLang experiment

- The user asked for a better handoff request, and for the next iteration to run on SGLang, continuing the existing weather codebase. They allowed stopping the current run.
- **Handoff reminders** (controller restarted 15:23, no schema change): after 3 non-`write_handoff` actions since a request, the builder is reminded with the same request id, up to 3 requests. Then the controller takes the fallback at once instead of waiting `request_timeout_s + 10 min`. The request text now says the next action must be the `write_handoff` call. First live use: the reminder at 15:24 got checkpoint #3 at 15:25, after 43 minutes of the builder ignoring the request.
- **SGLang backend** (`backend: sglang` in models.yaml; model `qwen3.8-flash-next-sglang`, NVFP4 RadixArk checkpoint). Runs offline as 65534, `--shm-size 8g`. The PLE table is a file in `/var/lib/dgx-autonomy/inference/ple`, deleted before each boot. Caches go in `.../inference/cache`. `--mem-fraction-static 0.80`. Qualification measures prefill and decode by timing two requests, since SGLang has no llama.cpp `timings`. No thinking budget.
- **`launch --from-run RUN --report FILE --hold`**: copies the ended run's project, with its git history, into the new workspace. The report goes to the builder before the brief and becomes feedback #1.
- Run `20260924-180353-eabd48` stopped at 15:25 (outcome `stopped`; demo retained on :43029). The release-after unit was stopped first, so the reservation is still **held**. The five feedback #1 fixes are in its project, uncommitted. Second review: `weather3-review2-*.png`. The report for the next run is `weather4-report.txt`.
- Downloads started 15:00: `~/models/qwen3.8-flash-next-nvfp4` (tmux `nvfp4dl`) and the image `lmsysorg/sglang:dev-qwen38-next-local` (root tmux `sglangpull`).

### 16:35 UTC: SGLang qualified; weather continuation launched on it

- The stopped weather run's final evaluation passed **15/15**, with the five feedback #1 fixes in place.
- First SGLang qualification (0.80) failed only on the long prompt: the KV pool held 131,712 tokens. At `mem_fraction 0.84` the pool holds 279,360. **Qualified** (record `20260925T161541.json`):
  - boot 715 s; 101 GiB resident; tool calls 5/5
  - 205K-token prompt: **1,940 tok/s prefill** (llama.cpp 387) and **13.6 tok/s decode** (llama.cpp 8.4); recalled the secret word
  - near an empty context: 19–34 tok/s, with MTP accepting 3.3–3.7 of 4 draft tokens
  - workload run VERIFIED in 4 minutes
  - **at least 9.3 GiB stayed available** (llama.cpp: 16.3); swap grew 0.6 GiB
- Fixed a claim bug that SGLang exposed: long thoughts cut the finish action out of the 300-character event summary, so a stats update was taken as the claim. `_claim` now uses the event's tool name.
- **Run `20260925-163449-24c587`**: qwen3.8-flash-next-sglang, from-run `20260924-180353-eabd48`, report `weather4-report.txt`, **held**, 12 h budget (deadline 2026-09-26 04:34 UTC). The frozen digest is identical to the original run's. `dgx-autonomy-release-after@20260925-163449-24c587` returns claude-qwen when it ends.
- Retired the two qualification sandboxes. About 10 GiB is free with the run going. The heartbeat now reports free memory and flags `!!` below 6 GiB (`/tmp/dgx-heartbeat.py`; backup at `.bak`).
- All the environment changes since 00:40 are deployed but **not committed**. That includes the heredoc grouping, review hold and feedback, handoff reminders, the SGLang backend, `--from-run` and the claim fix. 388 tests pass.

### 21:58 UTC: SGLang run's claim reviewed; feedback #2

- The SGLang builder committed the inherited fixes (`14a770a`) and finished all five report items in about 2.5 h (`96ae7e7`, 18:31). It claimed at 19:14, evaluation #1 passed 15/15, and the run has been **held** since 19:16. The review waited 2.5 h because the operator's watchers died with the previous session: re-arm `/tmp/dgx-watch.sh` first thing in a new session.
- Review (`weather4-review1-*.png`): the desktop layout, quiet night hours, removed labels and cool overcast daytime all work. **Item 3 is not done**: a grey smudge or streak sits above the hero icon in every state (`weather4-review1-hero-smudge.png`). Minor: in dark mode, overcast day and night look nearly the same.
- Feedback #2 sent 21:58 (`weather4-feedback-2.txt`). The builder resumed at once. Next claim: review the hero in clear, cloudy, rain and night, both themes. If the smudge is gone, `dgx-autonomy accept 20260925-163449-24c587`.

### 22:05 UTC: SGLang in production, measured

- SGLang's own log for run `20260925-163449-24c587`: 1,881 decode samples up to a 180K context. Median decode stays **21–25 tok/s at every context size** (for example 21.3 at 150–175K). llama.cpp on the previous run: 10.1 at 150–175K and 8.3 at 200–225K. MTP accepts about 2.5 of 4 draft tokens on this agent work (thinking-heavy), as the cookbook predicted. Weighted by where the builder spends its time, that is about **2× the generation speed** (about 24 against 12 tok/s). Prefill is 5× faster. The five report items took about 2.5 h in one conversation, with no rollover.
- The qualification's 13.6 tok/s "decode at 206K" understates it: the two-request timing includes re-reading the cached prompt, and the answer was short. The live figures are the ones to quote.
- Feature request from the user: tides, sea and fishing conditions. Written up in `weather-backlog.md` for the next iteration: a planned brief, the Open-Meteo Marine API, coastal locations only, fixtures for coastal and inland. It stays out of the current run (the frozen brief allows two hosts only).

### 01:35 UTC (2026-09-26): an SGLang-only failure, fixed

- 22:22–22:24: every request failed with a 400, "Requested token count exceeds the model's maximum context length of 262144 tokens … 198397 tokens from the input messages and 65536 tokens for the completion". **SGLang refuses a prompt + max_tokens above the context; llama.cpp never enforced that.** Requests failed from 196,608 prompt tokens, below the 85% rollover threshold (about 223K). Three nudges, then an `errors` rollover. The broken conversation could not answer the handoff request (the same error), yet the controller waited the full 100 minutes. Conversation #2 started at 00:04 from a `controller_fallback` checkpoint; it gets both pieces of operator feedback through the recovery context.
- Fixes (deployed 01:35, 390 tests):
  - Context rollover now also fires when there is no longer room for a full response: `ctx - max_output_tokens - 16384`, about 180K for this model.
  - An `errors` rollover whose conversation is still in error falls back after 2 minutes (`handoff_errored_timeout_s`).
- Conversation #2 is working on the smudge (feedback #2), again by first building a pixel detector for it (`audit/hero-marks.js`). Heredocs now work in its terminal: the grouping fix is live in this sandbox.
- **The operator's watchers die with each Claude session.** The review at 19:16 waited 2.5 h, and this failure went unseen until the user asked. A new session's first action must be to re-arm `/tmp/dgx-watch.sh`.

### 11:20 UTC (2026-09-26): overnight check; the SGLang run expired while held

- Conversation #2 committed its fix for feedback #2 at 02:08 (`40a37ab`, "no paint around the hero mark, day reads as day in dark"). It claimed at about 02:29. Evaluation #2 passed 15/15, and the run was **held** for review from then on.
- No operator session was watching, so nobody reviewed the claim. The run reached its deadline at 04:34 (outcome `expired`, 12 h budget). Final evaluation #3 passed 15/15 on the same snapshot (`e432a23749bb`). Human judgment is still open.
- The release-after unit ran: claude-qwen (Qwen3.8-27B Q8) restarted at 04:35. No reservation is held. 78 GiB is available. The demo is still up on :43031.
- Conversation #2 ran from 00:04 to the claim with no further rollover (the report lists only the one checkpoint).
- The builder's final message says it tried `write_handoff` unprompted and the tool refused it. That is expected, since no handoff was requested.
- The smudge fix (feedback #2) has not been reviewed yet. Review it on :43031 before starting the next iteration.

### 11:40 UTC: operator review of the expired run's last claim

- Looked at 7 live cities at 390×844 and 1280 px, in both themes: overcast day (Lisbon), clear day (Cairo, London, Mumbai), rain (Bergen), overcast night (Honolulu) and partly cloudy night (Tokyo). Screenshots: `weather4-review2-*.png`.
- **Feedback #2 item 1 is done.** Nothing sits above or around the hero icon in any of the 28 hero views.
- **Item 2 is done, if subtly.** In the dark theme an overcast day is grey-slate and a night is deep navy. They are easy to tell apart side by side. Clear days in dark have a warm glow at the bottom and don't glare.
- Remaining minor issues (product level, for the next iteration's report):
  - The night "partly cloudy" icon puts the crescent moon inside the cloud body, with a fragment poking out above it. It looks like a drawing error (`weather4-review2-night-partly-cloudy-icon.png`).
  - "Mainly clear" shows a sun-and-cloud icon, so it looks like partly cloudy.
  - In the light theme, the forecast bars for cool days (12–17°) are olive or brown. They read as muddy rather than cool.
- Verdict: acceptable. Human judgment "pleasant on phones and desktop" passes. Not recorded in the environment yet. The run has expired, and it hasn't been checked whether `accept` still applies.

### 11:42 UTC: Shoreline launched (40 h, SGLang)

- The user asked why the fishing and beach request never reached the builder. It had been parked in `weather-backlog.md`, because the old frozen brief allowed only two hosts. The user now wants the app **oriented to the beach and fishing, not just weather**, with a 40 h run and "pure determination to take this to the next level".
- The operator wrote the brief and checks by hand, not with the planner. `launch --plan` can't take `--from-run`, and jim can't write into a planner's workspace without root (docker needs sudo).
- **Brief** (`shoreline-brief.md`): the app becomes *Shoreline*. The weather contract is carried over word for word. New:
  - the Marine API (`marine-api.open-meteo.com/v1/marine`); a place is coastal when `current.wave_height` is a number;
  - Good/Fair/Poor verdicts for the beach and for fishing, with exact rules and reason keywords;
  - today's tides (hourly highs and lows, rising or falling, a chart, an "approximate" note), sea state, the moon phase, and unit conversions;
  - inland places show nothing about the sea; a marine failure shows a note;
  - on a phone, both verdicts sit on the first screen, above the hourly strip and forecast.
- **Checks:** 19 automated, 2 human.
  - Four new specs (`water-*.spec.ts`). `fixtures.ts` now intercepts the marine host. It answers only the variables requested, with nulls inland.
  - Coastal fixtures: Cascais is Good/Good, Newquay is Poor/Poor, Lisbon is Fair/Fair, and Sydney has sea data too.
  - The contrast, coverage, tap-target and overflow checks gained a `coastal` state.
- **Dry run on the Mac (Playwright 1.63).**
  - The reference is the current app plus a throwaway `water.js`. It passed all 78 tests, and 234/234 over three repeats.
  - The current app fails every new test and passes the old ones. One old test (`errors` "Try again") failed once under parallel load, then passed 3/3 alone.
- Reserved the DGX for `qwen3.8-flash-next-sglang` (claude-qwen displaced). It was ready at 11:42.
- **Run `20260926-114212-325c20`**: launched with `--from-run 20260925-163449-24c587`, report `weather5-report.txt` (the review's three small issues and the new direction), `--hold`, 40 h. Deadline 2026-09-28 03:42 UTC. Demo port 43032. `release-after` is armed.
- The brief bundle is at `~jim/briefs/shoreline` on the DGX. The local copy is `/tmp/wnext` (draft, reference and harness). Watch out: a macOS `tar` adds `._*` files, and `launch` rejects them as non-UTF-8.
- The watcher is `dgx-autonomy notify --follow --run 20260926-114212-325c20` under a Claude Monitor. It dies with the session, so re-arm it in each new session.

### 2026-09-27 20:34 UTC: Shoreline claim reviewed; feedback #2

- The builder did the whole brief in about 8 h: three context rollovers, all by handoff. It claimed at 20:01 on 09-26, and evaluation #1 passed 19/19. The claim then sat **held for 24 h unreviewed**: the session's Monitor had expired after 30 min and was never re-armed. **Re-arm the Monitor on every expiry.**
- Review on live data (Cascais, Newquay, Honolulu, Sydney, Cape Town, Halifax, Madrid; phone and desktop; both themes):
  - The verdicts sit on the first phone screen, with a compact hero.
  - The reasons are local-sounding.
  - The tide chart has dawn and dusk bands.
  - Madrid, inland, is untouched.
  - No console errors.
- **Found a flaw in the operator's own brief.** The strict neighbour rule for tide turns misses flat tops and bottoms, which are common in live hourly data (Honolulu 03/04 h at 0.93 m and 21/22 h at 0.26 m; Sydney 14/15 h at −0.63 m). The chart shows a peak that the list omits.
- Feedback #2 (`weather5-feedback-1.txt`):
  1. Amended rule: a flat run of two or more hours counts as one turn, timed at the first hour of the run. The fixtures have no flat runs, so the checks are unaffected.
  2. "Next turn" must be after now; after today's last turn, use tomorrow's data.
  3. Desktop: fix the empty gap under the hero and the squeezed hourly strip.
  - Optional: chart labels clear of the line, and a "Tomorrow looks …" line after sunset.
- The deadline is 03:42 UTC on 09-28, about 7 h after the feedback.

### 2026-09-28 12:30 UTC: Shoreline expired while held; the final state is accepted on review

- After feedback #2 the builder went through two more rollovers (conversations #5 and #6). It claimed at 01:17, and evaluation #2 passed 19/19. It was held; **nobody reviewed it** (the session's Monitor had died again). It expired at 03:42, and final evaluation #3 passed 19/19. claude-qwen is back and the reservation is released. The demo is still on :43032.
- Review on live data at 12:25 UTC:
  1. Flat turns: Sydney's −0.63 m at 14–15 h is now "Low 14:00 −0.6 m". Honolulu had no flat run today; its four turns match the curve.
  2. Next tide: Sydney after its last turn shows "Low tomorrow at 03:00".
  3. Desktop: the forecast fills the left column under the hero, and the hourly strip spans the full width. No gap.
  - Optional items done too: chart labels sit clear of the line, and a facts-only "Tomorrow …" line appears after sunset.
  - A nit left: on the phone, the "00" axis label touches the NOW pill when now is just after midnight.
- Verdict: accepted, and both human criteria pass. `dgx-autonomy accept` refuses on an expired run ("not waiting for review"), so this is recorded here only. Screenshots: `shoreline-review2-*.png`.
- **Pattern: held claims expire unreviewed** (three runs in a row). The Claude Monitor dies when a session ends and has to be re-armed every 30 min. The operator's attention needs something that outlives a session. Candidates:
  - a push notification from the DGX (a user systemd unit on `notify --follow`) to a phone;
  - the controller accepting on deadline when the last claim passed every check;
  - letting `accept` record a verdict on an expired run.

## Operator's role on the weather run (from the user, 2026-09-25)

Act as the product lead (L7). Judge the product, not the code: no code help, no architecture or code-quality notes. When `run.review` arrives:

1. Open the demo (`dgx-autonomy tunnel RUN`) and look at it as a user would: 390×844 and ~1280 px, light and dark, in every state (empty, results for a few real cities with different weather, picker, notice, error). Compare with the first weather app (`weather-app-demo.png`).
2. Check that the SVG data bars (see 00:10) read as intended and did not just dodge the background check.
3. If it falls short, write product feedback: what a user sees and should feel, and what matters most, not how to build it. Send it with `dgx-autonomy feedback RUN FILE`. Otherwise `dgx-autonomy accept RUN`.

## Next steps

1. **Weather run `20260925-163449-24c587` (SGLang).** On `run.review`: review against `weather4-report.txt` (five points), then accept or send feedback. Watch the heartbeat's free-memory figure. Compare how the builder behaves on SGLang with the llama.cpp runs: decode speed through the run, how far the context grows, and thinking length (no reasoning budget here).
   *(Done 16:35: the steps below.)* Wait for both downloads, then `chmod -R o+rX ~/models/qwen3.8-flash-next-nvfp4`. Check that the image's `python3 -m sglang.launch_server --help` knows `--ple-offload-backend`, `--ple-offload-dir`, `--tool-call-parser auto` and `--fp4-gemm-backend`. Run `dgx-autonomy qualify qwen3.8-flash-next-sglang`. If it qualifies: `dgx-autonomy launch --brief /var/lib/dgx-autonomy/runs/20260924-180353-eabd48/frozen --model qwen3.8-flash-next-sglang --from-run 20260924-180353-eabd48 --report weather4-report.txt --hold`, then `dgx-autonomy release --after NEW_RUN --detach`. If not: the same launch with the llama.cpp model.
2. Then the budget tracker: plan it (its request is `/tmp/budget-request2.txt` on the DGX), dry-run, `/launch 40`, `dgx-autonomy hold RUN`, and as jim `dgx-autonomy release --after RUN_ID --detach`. It is the first run with the heredoc fix.
3. Commit the two environment changes when the user agrees.
