# Neural Swarm — Modular Multi-Agent AI Harness

A first-principles, loosely-coupled multi-agent AI engineering platform in Python featuring **Orchestrator $\rightarrow$ Worker delegation**, a **Decentralized Peer Inbox Network**, **Multi-Tenant SQLite Isolation with a Cross-Swarm Privacy Airlock**, **Declarative JSON Configuration & Role Bundles**, **Automated Security & Regression Evals**, and a **Real-Time SSE Tactical Command Center UI**.

Built with zero heavy framework dependencies so every layer of the agent harness is transparent, testable, and swappable.

---

## Architecture Highlights (The 6-Step Progression)

1. **Step 1 — Single-Agent Harness ([`tools.py`](tools.py), [`core/llm.py`](core/llm.py))**:
   - Universal primitives (`run_python` sandbox, zero-key `web_search`, `fetch_url`, AST-safe `calculator`, workspace notes, live weather/time).
   - Dual-mode tool calling: Native OpenAI `tools=` API with automatic fallback to ReAct JSON prompting.
2. **Step 2 — Orchestrator $\rightarrow$ Workers ([`core/swarm.py`](core/swarm.py))**:
   - Hub-and-spoke task delegation (`delegate_to_worker`) with on-the-fly AI role recruiting (`spawn_specialist`).
3. **Step 3 — Decentralized Peer Inbox Network ([`core/peer_swarm.py`](core/peer_swarm.py))**:
   - Every specialist owns an asynchronous FIFO `inbox` queue (`deque[PeerMessage]`) and can message peers directly (`coordinator -> researcher -> coder -> critic`) via `send_peer_message`.
4. **Step 4 — Live SSE Tactical Command Center UI ([`server.py`](server.py), [`static/index.html`](static/index.html))**:
   - Decoupled Pub/Sub [`EventBus`](core/bus.py) streaming live telemetry over Server-Sent Events (`/api/events`) to a Retina HTML5 Canvas neural network visualizer.
5. **Step 5 — Budget Guardrails & Automated Evals ([`core/budget.py`](core/budget.py), [`evals.py`](evals.py))**:
   - Hard caps on total swarm steps, per-agent steps, token burn, and wall-clock time, verified by a 9-test deterministic regression & security suite (`100% pass` in `<200ms`).
6. **Step 6 — Framework Equivalence ([`frameworks_comparison.py`](frameworks_comparison.py))**:
   - Runnable side-by-side implementations showing how **LangGraph** (`StateGraph`) and the **OpenAI Agents SDK** (`Agent` handoffs + `Runner`) map 1-to-1 onto our custom harness.

---

## Configuration & Loose Coupling

All settings, capability bundles, prompts, and roles live outside the Python code:
- **[`config/settings.json`](config/settings.json) & [`core/config.py`](core/config.py)**: Central source of truth for LLM settings, budget caps, SQLite retention limits, tool timeouts, capability bundles (`@web`, `@code`, `@files`, `@memory`), and coordinator/orchestrator system prompts.
- **[`config/default_roles.json`](config/default_roles.json) & [`core/roles.py`](core/roles.py)**: Seed specialist roles (`researcher`, `coder`, `critic`, `security_reviewer`) copied to `workspace/roles.json` on first run and editable via CLI (`/add-role`) or Web UI (`+ New Role`).
- **`.env` Overrides**: Any environment variable in `.env` (see [`.env.example`](.env.example)) overrides `config/settings.json`.

---

## Security & Multi-Tenant Isolation

- **Strict Tenant Isolation ([`core/storage.py`](core/storage.py))**: Private facts (`tenant_memories`) and sessions (`tenant_sessions`) are partitioned by `sanitize_tenant_id(tenant_id)` in SQLite (`WAL` mode) using parameterized queries.
- **Cross-Swarm `PrivacyAirlock` ([`core/storage.py`](core/storage.py))**: Scrubs emails (`<EMAIL>`), URLs (`<URL>`), IPs (`<IP>`), secrets (`<SECRET>`), file paths (`<PATH>`), and numbers (`<NUM>`) before storing lessons in `global_skills`.
- **Hardened Tool & Web Boundaries ([`tools.py`](tools.py), [`server.py`](server.py), [`static/index.html`](static/index.html))**:
  - **SSRF Protection**: `fetch_url` resolves DNS via `socket.getaddrinfo` and blocks loopback (`127.0.0.1`), RFC-1918 private networks, and cloud metadata IPs (`169.254.169.254`).
  - **Subprocess Secret Scrubbing**: `run_python` strips all environment variables containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, or `CREDENTIAL` before spawning child processes.
  - **AST & Exponent DoS Guardrails**: `calculator` restricts evaluation to a strict `ast` whitelist and caps exponents (`calc_max_exponent: 1000`).
  - **Workspace Path Confinement**: `save_note` and `read_note` verify paths via `Path.resolve().relative_to(WORKSPACE_DIR)` and block dotfile (`.env`) access.
  - **Web UI & HTTP Hardening**: `server.py` enforces request payload caps (`max_body_bytes: 65536`), `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`; `static/index.html` HTML-escapes all dynamic telemetry and vault strings.

---

## Quickstart

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# 1. Launch the Live Command Center Web UI (http://127.0.0.1:8000)
python server.py

# 2. Run Interactive Terminal CLI (Peer or Orchestrator mode)
python agent.py -m peer -t client_a

# 3. Run the 9-Test Deterministic Regression & Security Suite
python evals.py

# 4. Run Step 6 Framework Abstractions Comparison (LangGraph & OpenAI SDK)
python frameworks_comparison.py
```

Read **[ARCHITECTURE.md](ARCHITECTURE.md)** for a concise engineering breakdown of all modules and design decisions.
