# LM Studio on the 4090

How this machine loads and debugs the local model. Read this when changing load settings, chasing speed, or budgeting VRAM. Working numbers measured 2026-09-22.

## Load settings

Hermes' floor is 64k context, and the base prompt of a fresh session eats into it before any message does. It is not one number: the system prompt is much the same everywhere, but the tool schemas are not, and `cli` carries several times the schema bulk of `api_server` — terminal, file, browser, code execution and delegation are the heavy ones, and the unattended path has none of them (`docs/security.md`). A tool that gates itself on a key or an environment is absent from the schema even when its toolset is enabled, so the schema is smaller than what the toolsets resolve to.

Both counts, now:

```fish
tools/facts.sh tools
```

When loading `qwen/qwen3.8-27b` in LM Studio:

- **Context length 131072.** Hermes refuses to start below 64,000, and 64k runs out once a session accumulates tool results. 131072 is the largest that fits with the vision projector off; 118k tokens of it has been filled end to end.
- **KV cache quantization Q8_0** for both K *and* V. Requires flash attention on.
- **Parallel / concurrent sequences = 1.** This is a multiplier, not a divider. The log prints `n_slots = N, n_ctx_slot = 131072` — every slot gets its own full context, so `parallel=4` allocates 4 × 131072 = 524k tokens of cache (17 GiB instead of 4.2 GiB). This is a single-user agent; 1 is correct.
- **Evaluation batch size 512**, not 2048. The vocab is 248k tokens, so the batch-sized output buffer is expensive at large batches. Costs a little prefill speed, nothing else.
- **Draft MTP speculative decoding on** (`speculative_draft_mtp`). It costs 1.23 GiB and pays for it: 76 tok/s with, 45 without, at ~0.45 draft acceptance.
- `context_checkpoints` 1. Flash attention on. A checkpoint is the hybrid's SSM state — 150 MiB fixed plus ~4 KiB per token — so keeping more than one is not free.

Verified end to end at 131072 on a freshly booted machine running only LM Studio, Task Manager, WSL and Tailscale: **23492 MiB of the card's 24564**, leaving **1072 MiB**. Generation **76-84 tok/s**, prompt eval 2010 tok/s at 63k tokens and 1700 tok/s at 118k.

Of that total the server is ~22.2 GiB and the Windows desktop ~1.3 GiB. **Measure the desktop's share from a fresh boot, with the model never loaded.** Reading it by unloading the model instead gives a number several hundred MiB too low, and budgeting from that number invents headroom that is not there.

The KV cache is allocated at load, so a full context costs no more VRAM than an empty one: a 118k-token prompt moved the total by 7 MiB. Headroom is decided at load time and nowhere else.

What each knob is worth, all measured at 100096 on this card:

| change | VRAM | what it costs |
| --- | --- | --- |
| vision projector off | 1.11 GiB | `vision_analyze` stops working |
| draft MTP off | 1.23 GiB | 76 → 45 tok/s |
| KV V-cache Q8_0 → Q4_0 | ~0.8 GiB | long-context recall, which is the reason for a large window |
| `context_checkpoints` 1 → 0 | ~0.2 GiB | re-prefill on every context rewind |

Only the projector is free of a quality or speed cost, and it is already off.

## Why the 27B KV is smaller than a 27B looks

`qwen3.8-27b` is a hybrid, not a plain transformer. GGUF metadata says `block_count = 65` but `full_attention_interval = 4`, plus a set of `ssm.*` keys — so only ~16 layers carry a real KV cache, and the other ~49 are SSM layers whose state is fixed-size and does not grow with context.

KV cache per token, from `head_count_kv = 4`, `key_length = value_length = 256`, ~16 attention layers:

| precision | per token | @ 131072 ctx |
| --- | --- | --- |
| fp16 | 64 KiB | 8.00 GiB |
| **Q8_0** | 34 KiB | **4.25 GiB** |
| Q4_0 | 17 KiB | 2.13 GiB |

The measured slope matches: 34 KiB per token, or 33 MiB per thousand. Budget any context change from that number.

Weights on disk: `qwen/qwen3.8-27b` Q4_K_M 15.66 GiB.

## The 4090 silently oversubscribes

LM Studio pins all layers to GPU (`n_gpu_layers=999999`), so when the total exceeds 24 GiB the Windows NVIDIA driver pages to system RAM instead of failing. The model loads, answers correctly, and is eight times slower: 150016 context with the projector on measures **10.6 tok/s** against 76 for a load that fits. Nothing in the LM Studio log says why.

NVIDIA Control Panel → CUDA - Sysmem Fallback Policy is set to "Prefer No Sysmem Fallback", which turns that into a refusal: an over-budget load stops with `Error: Failed to load model` / `failed to initialize the context: failed to allocate compute pp buffers`. Trust that error — it is the VRAM budget talking, not a corrupt file.

If a load ever succeeds and then generates at a fraction of the expected rate, the policy has been reset (a driver update does it) and the card is paging again. `lms ps` reports an oversubscribed model as healthy, so the only proof is a timed generation.

This is also why the spare 1072 MiB is not spare. Games, a browser and the Windows compositor take VRAM from the same pool. Once the model is loaded its allocation is safe — the policy governs CUDA, and DirectX surfaces page on their own — but anything holding VRAM *before* LM Studio loads now makes the load fail outright. That is the intended trade.

## Debugging speed and memory

The obvious Hermes places are empty.

- **`hermes -z` one-shot writes no session record.** `~/.hermes/sessions/` stays empty and `~/.hermes/logs/` only has install-time entries. You cannot reconstruct a one-shot run from Hermes' own files.
- **LM Studio's server log is the source of truth** for anything speed- or memory-shaped. Path: `/mnt/c/Users/user/.lmstudio/server-logs/<YYYY-MM>/<date>.N.log`. It carries llama.cpp's per-request `print_timing` lines (prompt eval tok/s, generation tok/s, draft acceptance) and the `common_fit_params` VRAM warning. Parse it by timestamp window around the run you care about.
- **Only the first line of a multi-line event carries a `[timestamp]`.** Filtering by timestamp silently drops the `eval time` (generation tok/s), `graphs reused`, and `draft acceptance` lines — those are continuation lines of the `print_timing` block. Grep for `print_timing` instead, then filter.
- `hermes prompt-size` — system prompt and tool-schema bulk, per toolset.
- `hermes -z --usage-file PATH` — token accounting for one run. Use this instead of a stopwatch when comparing changes.
- **VRAM: `/mnt/c/Windows/System32/nvidia-smi.exe` runs from WSL** and is the only exact number available. The Linux `nvidia-smi` is not installed; the `.exe` is. `nvidia-smi.exe --query-gpu=memory.used,memory.total --format=csv,noheader` gives the card total. Per-process figures come back `[N/A]` — Windows WDDM does not expose them. Isolating the server's share means subtracting the desktop's, and the only trustworthy desktop reading is from a fresh boot before the model has ever loaded; a reading taken after unloading comes back far too low. When the question is whether a config fits, read the loaded total against 24564 and skip the subtraction. Output carries a `\r`; strip it before arithmetic. Read it a few seconds after the load settles, not the instant `lms load` returns.
- For *load config* use `lms`, which runs from WSL: `/mnt/c/Users/user/.lmstudio/bin/lms.exe ps` shows context and parallel in one line. `lms.exe load <key> -c 131072 --parallel 1 --gpu max -y` bypasses every GUI override. There is no KV-quant flag — `lms load` takes those from the saved per-model config.
- **`lms load --estimate-only` does not count the KV cache.** It reports 16.52 GiB for the 27B with `Confidence: LOW` regardless of context length or parallel. That number is the weights and nothing else — do not use it to budget.
- `hermes tools` **requires a TTY** and fails in an agent's shell. Ask the user to run it.

When it looks like the model is too weak, suspect the serving config first. `qwen3.8-27b` Q4_K_M ran a clean multi-step research loop, chose an authoritative API over a blog, and hallucinated nothing.

## Gotchas

- LM Studio must be bound beyond loopback ("Serve on Local Network") or WSL can't reach it.
- **My Models default config does not win.** Saving load settings there writes `.internal/user-concrete-model-default-config/<pub>/<model>.json`, and most keys do get applied — but `numParallelSessions` was silently overridden back to 4 on load, while `evalBatchSize`, `contextCheckpoints` and the cache quant types from the same file went through. Set parallel in the **Developer tab's** own load panel and reload there, or skip the GUI and use `lms.exe load`. Always verify against the models API on the Windows Tailscale IP (`tools/facts.sh net` prints `lmstudio_url`; use `/api/v1/models` for `loaded_instances[].config.context_length`) and the log's `n_slots` line. Live load: `tools/facts.sh lmstudio`.
- **The vision projector is off by rename, because there is no GUI switch.** LM Studio loads any `mmproj-*.gguf` sitting next to the model file automatically, and it costs 1.11 GiB. The official weights park it as `mmproj-Qwen3.8-27B-BF16.gguf.disabled`. The obliterated weights park it as `mmproj-model-bf16.gguf.disabled`. Rename either back to `.gguf` to restore vision, at the price of 34k tokens of context. Confirm which way it went by grepping the log for `loaded multimodal model` after a load — absent means off.
- LM Studio's *loaded* context is what counts, not the model's advertised max. The 27B advertises 262144 but can be loaded much smaller. Check `loaded_instances[].config.context_length` via `tools/facts.sh lmstudio` / the `/api/v1/models` URL from `tools/facts.sh net`, not `max_context_length`.
