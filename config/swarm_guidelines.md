# Neural Swarm — Autonomous Self-Healing & Engineering Guidelines

This document defines the measurable engineering, quality, token-economy, scalability, and code-cleanliness standards enforced by the **4-Pillar Self-Healer (`core/self_healer.py`)**. You can edit these guidelines at any time in the UI (`🛠️ Self-Heal & Scorecard`) or directly on disk.

---

## Pillar A: Mission Quality & Discovery-Driven Depth (`0–100`)

1. **[G-A1] Lean Seed Roster & Discovery-Driven Dynamic Spawning (`20 pts`)**
   - For multi-domain audits and open-ended investigations, `TaskDrivenMetaPlanner` (`core/meta_planner.py`) MUST start with a **lean 2-role seed roster** (`discovery_recon_agent` + `verification_critic_agent`) rather than pre-baking 4+ static domain specialists before recon begins.
   - Stage-1 Discovery MUST inspect the target first, write a structured `recon_manifest.json`, and dynamically recruit 1–3 targeted vector specialists via `spawn_specialist` based on what actually exists on the target.

2. **[G-A2] Progressive Deepening & Zero Root Re-Fetch (`15 pts`)**
   - Once Stage-1 Discovery fetches the root target URL/file, downstream specialists MUST NOT re-fetch the exact same root URL.
   - Downstream specialists must read `recon_manifest.json` via `read_note` and spend 100% of their `@web`/`@files` probes on **unprobed sub-assets** (`.js`, `.css`, `.json`, forms, API endpoints, secondary modules).

3. **[G-A3] Strict Tool Discipline — Never Use `run_python` to Read Notes (`15 pts`)**
   - Specialists and Verification/Critic agents MUST use `read_note` and `list_notes` to read, verify, and count items in workspace `.md`/`.json`/`.txt` files.
   - Invoking `run_python` merely to open, parse, or count workspace note files is strictly forbidden because it triggers unnecessary human approval popups and burns extra tokens.

4. **[G-A4] Verbatim Empirical Citations (`15 pts`)**
   - Every specialist finding must quote exact verbatim evidence (HTTP headers, JS regexes, CSS rules, HTML tags, or source lines) inside backticks or code blocks.

5. **[G-A5] Complete Executive Deliverable in UI Drawer (`15 pts`)**
   - When the final Critic/Coordinator writes a full report artifact (e.g., `executive_audit_report.md` via `write_note`) and then calls `finish_swarm` with a short summary teaser, the runtime (`core/peer_swarm.py` / `core/swarm.py`) MUST automatically attach the full report artifact body so the user receives the complete report directly in the UI drawer ($\ge 80\%$ of the longest report note).

---

## Pillar B: Token Economy, $O(1)$ Scaling & Tradeoff ROI (`0–100`)

1. **[G-B1] $O(1)$ Compact Artifact Index for Large Swarms (`30 pts`)**
   - To scale cleanly to 10–20+ specialists without $O(N^2)$ prompt token growth, `WorkerAgent._build_context_block` (`core/swarm.py`) MUST NOT inline 3,000+ characters of every prior specialist file into every turn.
   - Instead, inject a compact **[WORKSPACE ARTIFACT INDEX]** (`filename`, byte size, and a 280-char preview) and instruct specialists to call `read_note` on relevant files (while keeping `recon_manifest.json` concisely available).

2. **[G-B2] 100% Per-Step Token & Cost Telemetry Persistence (`35 pts`)**
   - `IntegratedMissionRunner` (`core/mission.py`) MUST serialize `budget.get_summary()` into `mission_checkpoints.budget_json` on every checkpoint save, and `WorkerAgent` (`core/swarm.py`) MUST log LLM call token usage into `usage_ledger` when storage is present.

3. **[G-B3] Tradeoff-Aware Token ROI (`35 pts`)**
   - **Worthwhile Investment**: Spending more tokens is rewarded when it increases **Sub-Assets Probed** or **Mission Quality** (`+10 pts` quality lift with `Wasted Token Ratio <= 10%`).
   - **Unacceptable Bloat**: Spending tokens on duplicate root fetches, blocked tool loops, or redundant `run_python` file reads is penalized and must be eliminated.

---

## Pillar C: Execution Speed, Autonomy & UI Graph Clarity (`0–100`)

1. **[G-C1] Clean Active-Mission UI Canvas (`static/index.html`)**
   - When a new mission starts (`mission_start` / `mission_planned`), the UI graph must clear stale `missionDynamicRoles` from prior runs and render **only** the roles belonging to the active mission (plus any newly spawned specialists during that mission), avoiding dead/unused default nodes on the canvas.

2. **[G-C2] Zero Unnecessary Approval Friction (`30 pts`)**
   - Web, UX/RTL, and documentation audits should complete autonomously (`0` unnecessary `run_python` popups) by using `@web` (`fetch_url`) and `@files` (`read_note`, `write_note`).

---

## Pillar D: Codebase Cleanliness, Modularity & Runtime Resilience (`0–100`)

1. **[G-D1] Suppress Client Socket Abort Tracebacks (`20 pts`)**
   - `SwarmRequestHandler` / `ThreadingHTTPServer` in `server.py` MUST cleanly catch `(ConnectionAbortedError, ConnectionResetError, BrokenPipeError)` (such as Windows `[WinError 10053]`) during request handling so browser keep-alive disconnects never print unhandled tracebacks.

2. **[G-D2] Function Modularity & Test Suite Integrity (`80 pts`)**
   - Keep codebase functions modular (`<= 80` lines target; extract helper methods from monolithic functions `> 200` lines), maintain `0` bare `except:` blocks, and require `100%` `pytest` pass rate before accepting any self-heal patch.

---

## Immutable Security Boundary (Never Modified by Self-Healer)

- The Self-Healer is strictly prohibited from modifying:
  `core/security.py`, `core/auth.py`, `core/approval.py`, `core/rate_limiter.py`, `.env`, `workspace/keys.json`.
