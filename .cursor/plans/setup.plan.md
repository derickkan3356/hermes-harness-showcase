status: done

# Setup — Hermes on the local model at usable speed

Gate passed 2026-09-01: `qwen/qwen3.8-27b` Q4_K_M does real multi-step search and fits the 4090 at usable speed when loaded at 65024 / parallel=1 / KV Q8_0. Evidence and the VRAM table: `docs/lm-studio.md`.

`config/config.yaml` in this repo is the canonical config; `~/.hermes/config.yaml` is a symlink to it.

## Rejected

- OpenClaw as the harness — see CLAUDE.md.
- Trusting LM Studio **My Models** to persist `parallel`.

## Later (not this file)

- Scraping: Crawl4AI
- Windows Task Scheduler to start WSL at boot

## History

Go/no-go task was SearXNG version + official Docker method. Quality: hit the Docker Hub API, no hallucination. Speed was 9m06s from VRAM paging (`parallel=4` is a multiplier). After parallel=1 + Q8_0: 83–98 tok/s, 22.1 GiB. Method that paid off: change one thing at a time, believe the LM Studio log, not the GUI.
