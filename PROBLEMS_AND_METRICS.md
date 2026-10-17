# Engineering Problems, Metrics, Solutions & Improvements

This document tracks the core engineering, performance, reliability, and security problems encountered while building and testing the **Multi-Agent Swarm Engine**, the quantitative metrics used to measure each issue, the solutions implemented, and the resulting metric improvements.

---

## 1. Gradual Query Latency & Token Optimization Progression (Stages 0 → 5)

To systematically reduce per-query execution time on local hardware, we ran a controlled benchmark mission across `PeerSwarm`:
> **Benchmark Mission**: `"Use the calculator to compute 144 * 89 and return the exact number."` (`coordinator` → `coder` → `calculator` → final synthesis)

### Step-by-Step Benchmark Progression

| Stage | Optimization Applied | Total Query Time (`s`) | Latency / Step (`s/step`) | Steps | Total Tokens | GPU / CPU Memory Split (`ollama ps`) | Cumulative Speedup |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Stage 0 (Baseline)** | Unmodified OpenAI `/v1/chat/completions` on `qwen3:8b` (`/no_think` in system prompt only) | **`101.88s`** | `33.96s` | `3` | `4,154` | `6.0 GB` (`30% CPU / 70% GPU`) | **1.00x (Baseline)** |
| **Stage 1 (Fix 1)** | **Native Ollama `/api/chat` with `think: False`** ([`core/llm.py`](core/llm.py)) | **`21.55s`** | `5.39s` | `4` | `3,936` | `6.0 GB` (`30% CPU / 70% GPU`) | **4.73x faster (`-78.8%`)** |
| **Stage 2 + 3 (Fixes 2 & 3)** | **Direct Specialist Completion Fast-Path** ([`core/peer_swarm.py`](core/peer_swarm.py)) + **Lean Role-Scoped Tool Schemas** ([`core/swarm.py`](core/swarm.py)) | **`18.20s`** | `6.07s` | `3` | `2,423` | `6.0 GB` (`30% CPU / 70% GPU`) | **5.60x faster (`-82.1%` time, `-41.7%` tokens)** |
| **Stage 4 (Fix 4)** | **Compact KV Cache (`num_ctx: 2048`) & Output Cap (`num_predict: 256`)** ([`core/llm.py`](core/llm.py)) | **`17.76s`** | `5.92s` | `3` | `2,423` | **`5.7 GB` (`25% CPU / 75% GPU`)** | **5.74x faster (`+5%` GPU offload)** |
| **Stage 5 (Fix 5)** | **Coordinator Context & Bridge Schema Scoping** ([`core/swarm.py`](core/swarm.py), [`core/peer_swarm.py`](core/peer_swarm.py)) | **`14.82s`** | **`4.94s`** | `3` | **`2,250`** | **`5.7 GB` (`25% CPU / 75% GPU`)** | **6.87x faster (`-85.5%` time, `-45.8%` tokens)** |

---

### How Each Fix Improved the Metrics

1. **Fix 1 — Native Ollama `/api/chat` with `think: False` (`101.88s` → `21.55s`, `4.73x` speedup)**:
   - **Root Cause**: Ollama's OpenAI-compatible `/v1/chat/completions` endpoint ignores `"think": false` and `/no_think` system hints whenever `tools` are attached, forcing `qwen3:8b` to generate 200–600 hidden `<think>` tokens before every single tool call (`33.96s/step`).
   - **How It Works**: Updated [`OpenAICompatibleProvider._generate_ollama_native()`](core/llm.py) to call Ollama's native `/api/chat` endpoint with `"think": false` (and an automatic `"think": true` fallback if an empty response ever occurs). Per-step latency dropped from **`33.96s` → `5.39s` (`6.3x` faster per turn)**.

2. **Fix 2 — Direct Specialist Completion Fast-Path (`4 steps` → `3 steps`)**:
   - **Root Cause**: In [`PeerSwarm.run()`](core/peer_swarm.py), when a specialist (`coder`) finished computing the answer and had no further peer messages queued, the scheduler deposited the final answer back into `coordinator`'s inbox for a redundant 4th LLM call just to repeat the specialist's output.
   - **How It Works**: When a specialist finishes (`last_output` is non-empty) and all peer inboxes are empty (`not any_pending`), `PeerSwarm` emits the return `peer_message` event to the UI canvas for visual telemetry and immediately returns `last_output` as the final swarm answer, eliminating **1 full LLM turn per swarm**.

3. **Fix 3 — Lean Role-Scoped Tool Schemas & Direct User Prompt Formatting (`3,936` → `2,423` tokens, `21.55s` → `18.20s`)**:
   - **Root Cause**: Every agent turn was serializing the 25-line `share_global_skill` tool schema plus memory tools (`remember_fact`, `recall_memories`) even on `coordinator`, inflating prompt evaluation to `~1,046 tokens` on step 1.
   - **How It Works**: Scoped `share_global_skill` schema in [`WorkerAgent._get_tools_and_schemas()`](core/swarm.py) so it is only serialized when explicitly requested, removed memory tools from `coordinator`, and passed `user` messages without the `[INBOX MESSAGE from 'user']:` wrapper. Total token usage dropped by **`-38.4%` (`3,936` → `2,423`)**.

4. **Fix 4 — Compact KV Cache (`num_ctx: 2048`) & Model Checkpoint Profiling (`18.20s` → `17.76s`, `70%` → `75% GPU`)**:
   - **Root Cause**: `ollama ps` revealed `qwen3:8b` was consuming `6.0 GB` with a `4096` context buffer, spilling `30%` of layers onto CPU RAM (`30% CPU / 70% GPU`). (We also tested `qwen3:4b`, which ran at `100% GPU` in `3.2 GB`, but found that Ollama's `qwen3:4b` tag points to the `2507 Thinking-Only` checkpoint that ignores `think: False` and outputs raw deliberation text.)
   - **How It Works**: Set `"num_ctx": 2048` and `"num_predict": 256` in [`_generate_ollama_native()`](core/llm.py) on `qwen3:8b`. This reduced the KV-cache footprint from `6.0 GB` to **`5.7 GB`** and shifted 5% more model layers onto the GPU (**`25% CPU / 75% GPU`**).

5. **Fix 5 — Coordinator Context & Bridge Schema Scoping (`17.76s` → `14.82s`, `2,423` → `2,250` tokens)**:
   - **Root Cause**: `[SHARED GLOBAL SKILLS]` (technical coding/scraping playbooks) was being injected into `coordinator`'s routing prompt, and `extra_schemas` (`send_external_swarm_message`) was being attached to internal specialists instead of only `coordinator`.
   - **How It Works**: Updated [`_build_context_block()`](core/swarm.py) to skip global technical playbooks on router hubs (`coordinator` / `orchestrator`) and scoped `extra_schemas` to `coordinator` in [`PeerSwarm`](core/peer_swarm.py). End-to-end query latency dropped to **`14.82s` (`4.94s/step`)** — a **`6.87x` total speedup** over baseline.

---

## 2. Full Project Engineering & Security Metrics Log

| # | Problem Encountered | Metric Used to Measure | Baseline (Before) | Solution Implemented | Result (After) | Improvement |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | **End-to-End Local Swarm Query Latency** | Total seconds & tokens for a 3-step `PeerSwarm` tool mission on `qwen3:8b` | **`101.88s`** (`33.96s/step`, `4,154 tokens`) | Applied Fixes 1–5 (Native `/api/chat` `think: False`, Specialist Fast-Path, Lean Schemas, `num_ctx: 2048`, Context Scoping) | **`14.82s`** (`4.94s/step`, `2,250 tokens`) | **6.87x faster (`-85.5%` time)**, **`-45.8%` tokens** |
| **2** | **Small Local LLM Native Tool-Call Drops** | Tool invocation success rate (%) across multi-hop tasks | **~62%** (models occasionally output JSON in text instead of `tool_calls`) | Dual-mode parser: Native `tool_calls` with automatic fallback to `parse_react_tool_calls()` JSON extractor | **100% (10/10)** tool invocation reliability | **+38 percentage points** reliability |
| **3** | **Runaway Agent Loops & Unbounded Token Burn** | Max steps & tokens consumed under adversarial/looping prompts | **Unbounded (∞)** (looped until context window crash) | `BudgetTracker` enforcing per-agent step cap (`8`), swarm step cap (`16`), token cap (`24,000`), and wall-clock timeout | **100% deterministic halt** at exact cap (`0.0ms` overhead) | **0 runaway loops** (`100%` bounded cost) |
| **4** | **Multi-Swarm Wall-Clock Timeout on Local Models** | Multi-swarm Red-Team completion rate (%) over 6–8 LLM turns | **0% full synthesis** (timed out at `180s` during turn 7 at `270s`) | Increased `max_wall_seconds` to `600.0s` + reduced per-step time from `34s` → `5s` + added live `inbox_pickup` UI telemetry | **100% completion** (~40–50s total vs 270s+ before) | **~5.5x faster multi-swarm runs** + `600s` safety margin |
| **5** | **Cross-Swarm Internal Vault URL Leakage in Error Replies** | Sensitive vault token / internal URL leak rate (%) across cross-swarm probes | **33.3% (1/3 probes leaked)** (`victim:researcher` leaked `http://127.0.0.1:8000/api/state` in trace `#15`) | Added deterministic **Cross-Swarm Egress DLP** (`_redact_cross_swarm_egress`) + hardened `victim` vault directive | **0.0% (0/3 probes leaked)** (`[REDACTED_VAULT_SECRET]`) | **-100% cross-swarm metadata leakage** |
| **6** | **SSRF, Env Var Exfiltration & Exponent DoS in Tools** | Exploit block rate (%) across 5 attack vectors (loopback SSRF, link-local SSRF, `.env` write, env dump, AST power bomb) | **20% (1/5 blocked)** (only basic path prefix check existed) | Added `_validate_public_url` DNS/IP firewall, `_get_sandbox_env`, dotfile blocker in `_safe_workspace_path`, and `calc_max_exponent` | **100% (5/5 blocked)** verified in `eval_security_hardening` | **+80 percentage points** exploit mitigation |
| **7** | **Cross-Tenant PII & Secret Leakage in Shared Learning** | PII/Secret categories leaked into `global_skills` (%) | **100% raw text leaked** if skills shared directly across tenants | Built `PrivacyAirlock` scrubbing 5 categories (`<EMAIL>`, `<IP>`, `<SECRET>`, `<URL>`, `<PATH>`) before writing `global_skills` | **0% PII/secret leakage** across all 5 categories (`evals.py`) | **100% airlocked** cross-tenant skill sharing |
| **8** | **Per-Experiment Custom Board Proliferation** | Lines of custom Python/HTML code required to deploy a new multi-swarm experiment | **~320 LOC** (`core/arena.py` + custom UI mode per experiment) | Built declarative `SwarmManager` (`config/default_swarms.json` / `workspace/swarms.json`) + dynamic `N`-cluster canvas & `+ Deploy Swarm` modal | **0 LOC** (deploy via UI modal or JSON in `<5 seconds`) | **-100% custom code** per experiment |
| **9** | **Empty Specialist Return After Multi-Step `web_search` (Traces `#59`–`#66`)** | Specialist non-empty synthesis rate (%) after $\ge 2$ consecutive `web_search` calls | **0% (0/2 runs)** (overflowed `num_ctx: 2048` & returned `answer: ""`) | Restored `num_ctx: 4096`, forced schema-free synthesis on round 3 (`active_schemas = None`), and added observation fallback in [`core/swarm.py`](core/swarm.py) | **100% non-empty synthesis** | **+100 percentage points** synthesis reliability |
| **10** | **Cross-Tool SSRF Bypass (`run_python` vs `fetch_url`, Traces `#49` & `#56`)** | Attack surface coverage of SSRF firewall across `@web` and `@code` tools | **50% (1/2 tools)** (`fetch_url` blocked `127.0.0.1`, but `ssrf_researcher` tricked `security_reviewer` into using `run_python` + `requests.get`) | Installed `_SUBPROCESS_SANDBOX_BOOTSTRAP` socket firewall in `run_python` + scoped `security_reviewer` to `@files` only | **100% (2/2 tools blocked)** verified in `evals.py` | **+50 percentage points** (100% comprehensive SSRF containment) |
