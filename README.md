# Neural Swarm — Modular Multi-Agent AI Harness

A first-principles, loosely-coupled multi-agent AI engineering platform in Python featuring **Orchestrator $\rightarrow$ Worker delegation**, a **Decentralized Peer Inbox Network**, **Multi-Tenant SQLite Isolation with a Cross-Swarm Privacy Airlock**, **Declarative Role & Capability Bundles**, **Automated Evals**, and a **Real-Time SSE Tactical Command Center UI**.

Built with zero heavy framework dependencies so every layer of the agent harness is transparent, testable, and swappable.

---

## Architecture Highlights (The 6-Step Progression)

1. **Step 1 — Single-Agent Harness ([`tools.py`](tools.py), [`core/llm.py`](core/llm.py))**:
   - Universal primitives (`run_python` sandbox, zero-key `web_search`, `fetch_url`, AST-safe `calculator`, workspace notes, live weather/time).
   - Dual-mode tool calling: Native OpenAI `tools=` API with automatic fallback to ReAct JSON prompting for models without native tool templates.
2. **Step 2 — Orchestrator $\rightarrow$ Workers ([`core/swarm.py`](core/swarm.py))**:
   - Hub-and-spoke task delegation (`delegate_to_worker`) with on-the-fly AI role recruiting (`spawn_specialist`).
3. **Step 3 — Decentralized Peer Inbox Network ([`core/peer_swarm.py`](core/peer_swarm.py))**:
   - Every specialist owns an asynchronous FIFO `inbox` queue (`deque[PeerMessage]`) and can message peers directly (`coordinator -> researcher -> coder -> critic`) via `send_peer_message`.
4. **Step 4 — Live SSE Tactical Command Center UI ([`server.py`](server.py), [`static/index.html`](static/index.html))**:
   - Decoupled Pub/Sub [`EventBus`](core/bus.py) streaming live telemetry over Server-Sent Events (`/api/events`) to a Retina HTML5 Canvas neural network visualizer with curved Bezier comet trails.
5. **Step 5 — Budget Guardrails & Automated Evals ([`core/budget.py`](core/budget.py), [`evals.py`](evals.py))**:
   - Hard caps on total swarm steps, per-agent steps, token burn, and wall-clock time, verified by an 8-test deterministic regression suite (`100% pass` in `<150ms`).
6. **Step 6 — Framework Equivalence ([`frameworks_comparison.py`](frameworks_comparison.py))**:
   - Runnable side-by-side implementations showing how **LangGraph** (`StateGraph`) and the **OpenAI Agents SDK** (`Agent` handoffs + `Runner`) map 1-to-1 onto our custom harness.

---

## Multi-Tenant Isolation & The Privacy Airlock ([`core/storage.py`](core/storage.py))

- **Strict Tenant Isolation**: Private client facts (`tenant_memories`) and chat history (`tenant_sessions`) are partitioned by `tenant_id` in SQLite (`WAL` mode).
- **Cross-Swarm Procedural Learning (`PrivacyAirlock`)**: Reusable technical lessons are scrubbed of emails (`<EMAIL>`), URLs (`<URL>`), IPs (`<IP>`), secrets (`<SECRET>`), file paths (`<PATH>`), and numbers (`<NUM>`) before entering the shared `global_skills` table.

---

## Quickstart

### 1. Install Dependencies
```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Launch the Live Command Center Web UI
```bash
python server.py
```
Open **http://localhost:8000** to:
- Click **`⚡ Simulate Flow`** for an instant 8-second multi-agent visualization, or **`▶ Dispatch`** to run live against your local Ollama model (`qwen3:8b` / `llama3`) or cloud API.
- Click **`➕ New Role`** to spawn new specialist roles with `@web`, `@code`, `@files`, or `@memory` Capability Bundles.
- Click **`✓ Run Evals`** or **`🛡️ Test Airlock`** to verify guardrails live.

### 3. Run the Terminal CLI, Evals, or Framework Comparison
```bash
# Run interactive Peer Inbox Network CLI
python agent.py -m peer -t client_a

# Run Step 5 Automated Evaluation & Regression Suite
python evals.py

# Run Step 6 Framework Abstractions Comparison (LangGraph & OpenAI SDK)
python frameworks_comparison.py
```

---

## Deep-Dive Documentation
Read **[ARCHITECTURE.md](ARCHITECTURE.md)** for a complete handbook covering every design decision, interface contract, and module in the codebase.
