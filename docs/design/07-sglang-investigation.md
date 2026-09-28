# Would SGLang serve Qwen3.8-Flash-Next faster than llama.cpp on hugo-dgx1?

2026-09-25. Prompted by the SGLang cookbook page for Qwen3.8-Flash-Next (single-DGX-Spark cells).

## Short answer

Probably yes where it matters: at long context. The cookbook's "16 tok/s" is not measured at our context size. It is the single-Spark cell **without** MTP at 1,024 input tokens. With MTP the same page reports 27.5 tok/s. At that short context our llama.cpp build already does 25 tok/s. The gap is at long context: our decode falls to 8 tok/s at 225K, and the builder spends most of each conversation above 100K. SGLang has real sparse-attention kernels for this model's QSA layers, and our llama.cpp build does not (see below). Nobody has measured SGLang single-Spark decode at 200K+, so it has to be benchmarked here before we switch.

## What we get today (llama.cpp f95b0d9, UD-Q3_K_XL GGUF, 84 GiB)

Median decode by context size, from the weather run's llama-server log (last 24 h, responses of 50+ tokens):

| Context | Decode (tok/s) |
|---|---|
| 0–25K | 24.9 |
| 25–50K | 21.9 |
| 50–75K | 18.3 |
| 75–100K | 15.3 |
| 100–125K | 13.1 |
| 125–150K | 11.8 |
| 150–175K | 10.1 |
| 175–200K | 9.2 |
| 200–225K | 8.3 |
| 225–250K | 8.0 |

Qualification (2026-09-24): prefill 387 tok/s on a 206K prompt, 91 GiB resident, at least 16.3 GiB available with the workload running, load in 60 s.

## Why llama.cpp slows down: its QSA is dense attention with a mask

`src/models/qwen4exp.cpp` at our commit: `build_qsa_top_k` scores every block with the indexer, expands the scores to one per cached token, and runs `ggml_top_k` over all `n_kv` of them. `build_attn_qsa` then fills a KQ mask of size `n_kv` with -inf, unmasks the top-k cells, and calls the ordinary `build_attn_mha` over the **whole** K/V cache ("Dense GQA self-attention restricted to the cells that top_k names"). The result is numerically sparse attention, but compute and memory traffic still grow with the full context. Each decoded token also pays a top-k over about 225K scores in each of the 12 attention layers. So decode cost grows linearly with context, as the table shows.

SGLang implements QSA with dedicated kernels. Qwen reports up to 6.6x decode speedup for the QSA kernel at 1M tokens.

## What the cookbook says about a single DGX Spark

- Checkpoint: NVFP4, `RadixArk/Qwen3.8-Flash-Next-NVFP4` (126.0 GiB, 419 files), or the NVIDIA export. Both are 4-bit with calibration, likely better quality than our Q3_K_XL.
- It fits one Spark only if the 47.7 GiB FP8 N-gram table lives in a sparse file on NVMe (`--ple-offload-embedding --ple-offload-backend file`). 78.3 GiB of weights stay resident. `--mem-fraction-static 0.85`; "host memory never below 10 GiB".
- Measured at 1,024 in / 256 out: **27.5 tok/s single-stream with MTP** (TPOT 33.6 ms), **15.9 tok/s without MTP**. MTP accepts 3.5–3.7 of 4 draft tokens on normal output, about 2.5 on thinking output. Our agent output is mostly thinking, so the MTP gain for us is smaller.
- Boot rewrites the whole table file. On a fresh sparse file boot takes about 10 minutes; on a populated one about 55 minutes, so the old file must be deleted before each boot. Our llama.cpp loads in 60 s.
- The page warns that exhausting unified memory "can take the whole box down and needs a power cycle to recover".
- Image `lmsysorg/sglang:dev-qwen38-next-local` has an arm64 build (checked the manifest). hugo-dgx1 has 1.3 TB free on NVMe.

## What switching would take

1. **Measure first.** On the DGX, run the single-Spark low-latency cell with a single request and our workload next to it. Record decode at 1K, 50K, 100K, 200K and 250K; prefill at 200K; tool-call probes; minimum available memory. Compare with the table above. `dgx-autonomy qualify` measures most of this, but it starts llama.cpp, so it needs an SGLang backend first.
2. **Memory headroom.** We need about 16 GiB free for the sandbox, the evaluator and Chromium. We run one request at a time, so `--mem-fraction-static` can be lower than 0.85 and `--max-running-requests` 1–2.
3. **Controller support for a second inference backend:** a container spec, a health check, the PLE file deleted before each start, and a start timeout of at least 15 minutes. Flags: `--reasoning-parser qwen3 --tool-call-parser auto`.
4. **Behaviour we rely on in llama.cpp that SGLang has no flag for:**
   - `--reasoning-budget 32768`: stops runaway thinking. SGLang would need a custom logit processor, or a `max_tokens` cap on each request.
   - `--no-reasoning-preserve`: keeps past thinking out of the history. With SGLang this depends on what OpenHands sends back.
   Both need checking.
5. **Cost of trying:** a 126 GiB download (the 84 GiB GGUF took 29 min) and the image pull, both possible while a run is going. The benchmark itself needs the GPU, so it has to wait until the weather run ends.

## Recommendation

Worth a measured trial between runs, not a switch on the strength of the cookbook. The payoff is in the long-context regime where unattended runs spend most of their time. The case for switching: at 200K+ decode stays above about 15 tok/s, tool calls hold up, and at least 16 GiB stays free with the workload running.
