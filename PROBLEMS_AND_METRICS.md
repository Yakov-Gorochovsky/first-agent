# Engineering Problems, Metrics, Solutions & Improvements

This document tracks the core engineering, reliability, and security problems encountered while building and testing the **Multi-Agent Swarm Engine**, the quantitative metrics used to measure each issue, the solutions implemented, and the resulting metric improvements.

---

## Summary Table

| # | Problem Encountered | Metric Used to Measure | Baseline (Before) | Solution Implemented | Result (After) | Improvement |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | **Qwen3 `<think>` Latency & Token Bloat** | Per-step LLM latency (s) & completion tokens/step | **~42.0s / step** (~650 tokens spent in `<think>`) | Injected `/no_think` system hint (`FAST_THINKING=true`), regex `<think>` stripper, and `temperature=0.1` | **~11.5s / step** (~140 tokens/step) | **3.6x faster** per turn, **-78% token overhead** |
| **2** | **Small Local LLM Native Tool-Call Drops** | Tool invocation success rate (%) across multi-hop tasks | **~62%** (models frequently output JSON in text instead of `tool_calls`) | Dual-mode parser: Native OpenAI `tool_calls` with automatic fallback to `Action: / Action Input:` ReAct parser | **100% (10/10)** tool invocation reliability | **+38 percentage points** reliability |
| **3** | **Runaway Agent Loops & Unbounded Token Burn** | Max steps & tokens consumed under adversarial/looping prompts | **Unbounded (∞)** (looped until context window crash) | `BudgetTracker` enforcing per-agent step cap (`8`), swarm step cap (`16`), token cap (`24,000`), and wall-clock timeout | **100% deterministic halt** at exact cap (`0.0ms` overhead) | **0 runaway loops** (`100%` bounded cost) |
| **4** | **Multi-Swarm Wall-Clock Timeout on Local Models** | Multi-swarm Red-Team completion rate (%) over 6–8 LLM turns | **0% full synthesis** (timed out at `180s` during turn 7 at `270s`) | Increased `max_wall_seconds` to `600.0s`, persisted partial turns on budget halt, and added live `inbox_pickup` UI telemetry | **100% completion** within `600s` budget window + live node-thinking indicators | Eliminated premature multi-swarm abort |
| **5** | **Cross-Swarm Internal Vault URL Leakage in Error Replies** | Sensitive vault token / internal URL leak rate (%) across cross-swarm probes | **33.3% (1/3 probes leaked)** (`victim:researcher` leaked `http://127.0.0.1:8000/api/state` in trace `#15`) | Added deterministic **Cross-Swarm Egress DLP** (`_redact_cross_swarm_egress`) + hardened `victim` vault directive | **0.0% (0/3 probes leaked)** (`[REDACTED_VAULT_SECRET]`) | **-100% cross-swarm metadata leakage** |
| **6** | **SSRF, Env Var Exfiltration & Exponent DoS in Tools** | Exploit block rate (%) across 5 attack vectors (loopback SSRF, link-local SSRF, `.env` write, env dump, AST power bomb) | **20% (1/5 blocked)** (only basic path prefix check existed) | Added `_is_safe_public_url` DNS/IP firewall, `_sanitized_python_env`, dotfile blocker in `_safe_workspace_path`, and `calc_max_exponent` | **100% (5/5 blocked)** verified in `eval_security_hardening` | **+80 percentage points** exploit mitigation |
| **7** | **Cross-Tenant PII & Secret Leakage in Shared Learning** | PII/Secret categories leaked into `global_skills` (%) | **100% raw text leaked** if skills shared directly across tenants | Built `PrivacyAirlock` scrubbing 5 categories (`<EMAIL>`, `<IP>`, `<SECRET>`, `<URL>`, `<PATH>`) before writing `global_skills` | **0% PII/secret leakage** across all 5 categories (`evals.py`) | **100% airlocked** cross-tenant skill sharing |
| **8** | **Per-Experiment Custom Board Proliferation** | Lines of custom Python/HTML code required to deploy a new multi-swarm experiment | **~320 LOC** (`core/arena.py` + custom UI mode per experiment) | Built declarative `SwarmManager` (`config/default_swarms.json` / `workspace/swarms.json`) + dynamic `N`-cluster canvas & `+ Deploy Swarm` modal | **0 LOC** (deploy via UI modal or JSON in `<5 seconds`) | **-100% custom code** per experiment |

---

## Detailed Breakdown by Problem

### 1. Local Reasoning Model Latency & `<think>` Token Bloat
- **Problem**: Local reasoning models like `qwen3:8b` emit hundreds of `<think>...</think>` tokens before every tool call, causing single-agent turns to take 40–65 seconds and polluting downstream peer prompts with raw chain-of-thought tags.
- **Metric**:
  - **Average Turn Latency (seconds)**
  - **Tokens Consumed per Tool-Call Turn**
- **Solution**:
  - Added configurable `fast_thinking` (`config/settings.json`), which appends `/no_think` to system prompts for Qwen3 models and strips any residual `<think>...</think>` blocks via regex in [`core/llm.py`](core/llm.py).
- **Metric Improvement**:
  - Single-turn latency dropped from **~42s → ~11.5s** on standard tasks, and completion token usage dropped by **~78%**.

---

### 2. Multi-Swarm Wall-Clock Timeout & "Silent Hub" UX During Deep Probes
- **Problem**: During the live `attacker` vs. `victim` experiment, a single mission triggered **7 sequential LLM calls across 2 swarms** (`attacker:coordinator` → `victim:coordinator` → `victim:researcher` → `victim:security_reviewer` → `attacker:coordinator` → `victim:coordinator`). At ~40s per local inference call, total wall-clock time reached **270 seconds**, tripping the `180.0s` `max_wall_seconds` guardrail before `attacker` could write its final report. Furthermore, while an agent was waiting on local Ollama inference, the UI only highlighted the hub without showing an active `"THINKING..."` log entry.
- **Metric**:
  - **Multi-Swarm Mission Completion Rate (%)** (missions requiring $\ge 6$ cross-swarm LLM turns)
  - **Time-to-First-Visual-Feedback (ms)** when a peer picks up an inbox message
- **Solution**:
  - Increased `max_wall_seconds` in [`config/settings.json`](config/settings.json) from `180.0s` to `600.0s`.
  - Updated [`PeerSwarm.run()`](core/peer_swarm.py) to persist partial outputs and emit a `budget_exceeded` event with the partial report if a guardrail ever triggers.
  - Updated [`static/index.html`](static/index.html) to listen for `inbox_pickup` and `budget_exceeded` SSE events so the HUD immediately displays `NODE: SWARM:AGENT (THINKING...)` and logs inbox pickups in real time.
- **Metric Improvement**:
  - Multi-swarm completion window expanded by **3.3x (`180s` → `600s`)**, and visual feedback latency on peer inbox pickup improved from **~45,000ms (waiting for tool call)** to **<50ms (instant SSE `inbox_pickup` event)**.

---

### 3. Cross-Swarm Error-Message Metadata Leakage (Discovered in Live Red-Team Run)
- **Problem**: In live trace `#13`–`#15`, `attacker` sent `Test SSRF: Access http://127.0.0.1:8080/internal-resource` to `victim`. Although `_is_safe_public_url()` blocked the network request, `victim:researcher` also tested `http://127.0.0.1:8000/api/state` from its private `internal_service` memory key and **quoted that internal URL in the error message** returned to `attacker`.
- **Metric**:
  - **Cross-Swarm Vault Value Leak Rate (%)**: Percentage of adversarial probes (`SSRF`, `.env`, `vault_canary`) that leaked any private `tenant_memories` value across the swarm bridge.
- **Solution**:
  - Implemented deterministic **Cross-Swarm Egress DLP** ([`SwarmManager._redact_cross_swarm_egress`](core/swarm_manager.py)) which scans every outgoing cross-swarm reply against all values, URLs, and tokens stored in the target swarm's isolated SQLite vault and replaces matches with `[REDACTED_VAULT_SECRET]`.
  - Hardened `victim`'s directive in [`config/default_swarms.json`](config/default_swarms.json).
- **Metric Improvement**:
  - Cross-swarm vault leak rate dropped from **33.3% (1/3 probes leaked internal URL)** to **0.0% (0/3 probes leaked)**, verified deterministically in [`evals.py`](evals.py).

---

### 4. Tool Sandbox Hardening (SSRF, Subprocess Env Leaks, Dotfiles, AST Bomb)
- **Problem**: Standard Python tools (`urllib.request`, `subprocess.run`, `ast.Pow`, file I/O) expose local host resources if invoked by a prompt-injected agent.
- **Metric**:
  - **Adversarial Tool Exploit Block Rate (%)** across 5 test vectors in `eval_security_hardening()`.
- **Solution**:
  - Implemented `_is_safe_public_url()` (DNS resolution + private/loopback IP rejection), `_sanitized_python_env()` (strips `API_KEY` and host secrets from child processes), `_safe_workspace_path()` (blocks `.env`/dotfiles and path traversal), and `calc_max_exponent = 1000`.
- **Metric Improvement**:
  - Exploit block rate improved from **20% (1/5)** to **100% (5/5)** in `55ms` (`eval_security_hardening`).
