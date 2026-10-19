# Neural Swarm — Modular Multi-Agent AI Harness

A first-principles, loosely-coupled multi-agent AI engineering platform in Python featuring **Orchestrator $\rightarrow$ Worker delegation**, a **Decentralized Peer Inbox Network**, **Multi-Tenant SQLite Isolation with a Cross-Swarm Privacy Airlock**, **Declarative JSON Configuration & Role Bundles**, **Automated Security & Regression Evals**, and a **Real-Time SSE Event Dashboard UI**.

Built with zero heavy framework dependencies so every layer of the agent harness is transparent, testable, and swappable.

---

## Architecture Highlights (The 6-Step Progression)

1. **Step 1 — Single-Agent Harness ([`tools.py`](tools.py), [`core/llm.py`](core/llm.py))**:
   - Universal primitives (`run_python` execution sandbox, zero-key `web_search`, `fetch_url`, AST-safe `calculator`, workspace notes, live weather/time).
   - Dual-mode tool calling: Native OpenAI `tools=` API with automatic fallback to ReAct JSON prompting.
2. **Step 2 — Orchestrator $\rightarrow$ Workers ([`core/swarm.py`](core/swarm.py))**:
   - Hub-and-spoke task delegation (`delegate_to_worker`) with on-the-fly AI role recruiting (`spawn_specialist`).
3. **Step 3 — Decentralized Peer Inbox Network ([`core/peer_swarm.py`](core/peer_swarm.py))**:
   - Every specialist owns an asynchronous FIFO `inbox` queue (`deque[PeerMessage]`) and can message peers directly (`coordinator -> researcher -> coder -> critic`) via `send_peer_message`.
4. **Step 4 — Live SSE Event Dashboard UI ([`server.py`](server.py), [`static/index.html`](static/index.html))**:
   - Decoupled Pub/Sub [`EventBus`](core/bus.py) streaming live telemetry over Server-Sent Events (`/api/events`) to an HTML5 Canvas agent visualizer.
5. **Step 5 — Budget Guardrails & Automated Evals ([`core/budget.py`](core/budget.py), [`evals.py`](evals.py))**:
   - Hard caps on total swarm steps, per-agent steps, token burn, and wall-clock time, verified by an 11-test deterministic regression & security suite.
6. **Step 6 — Framework Equivalence ([`frameworks_comparison.py`](frameworks_comparison.py))**:
   - Runnable side-by-side implementations illustrating how **LangGraph** (`StateGraph`) and the **OpenAI Agents SDK** (`Agent` handoffs + `Runner`) routing patterns map onto our custom harness primitives (see `frameworks_comparison.py` for differences with production framework runtimes).

---

## Configuration & Loose Coupling

All settings, capability bundles, prompts, and roles live outside the Python code:
- **[`config/settings.json`](config/settings.json) & [`core/config.py`](core/config.py)**: Central source of truth for LLM settings, budget caps, SQLite retention limits, tool timeouts, capability bundles (`@web`, `@code`, `@files`, `@memory`), and coordinator/orchestrator system prompts.
- **[`config/default_roles.json`](config/default_roles.json) & [`core/roles.py`](core/roles.py)**: Seed specialist roles (`architect`, `coder`, `test_runner`, `critic`, `researcher`, `security_reviewer`) copied to `workspace/roles.json` on first run and editable via CLI (`/add-role`) or Web UI (`+ New Role`).
- **[`config/default_keys.json`](config/default_keys.json) & [`core/auth.py`](core/auth.py)**: Seed hashed API keys for role-scoped authentication (`admin` and `user`), with constant-time verification and cryptographically random fallback key generation.
- **`.env` Overrides**: Any environment variable in `.env` (see [`.env.example`](.env.example)) overrides `config/settings.json`.

---

## Security & Multi-Tenant Isolation

- **Strict Tenant Isolation ([`core/storage.py`](core/storage.py))**: Private facts (`tenant_memories`) and sessions (`tenant_sessions`) are partitioned by strict regex validation (`validate_tenant_id` enforcing `^[A-Za-z0-9_-]{1,64}$`) in SQLite (`WAL` mode) using parameterized queries with composite indexes.
- **API-Key Authentication & Scoped RBAC ([`core/auth.py`](core/auth.py), [`server.py`](server.py))**: SHA-256 hashed keys verified with `hmac.compare_digest` to prevent timing attacks. Scopes partition administrative operations (`admin`) from tenant operations (`user`). Tenant identity is derived strictly from verified credentials to prevent horizontal privilege escalation. Non-loopback host bindings (`0.0.0.0`) are blocked unless authentication is active. Per-tenant run locks (`_tenant_run_locks`) permit concurrent independent tenant missions while serializing intra-tenant executions.
- **Tenant-Scoped SSE Telemetry ([`server.py`](server.py))**: `SSEClientConnection` and `_broadcast_event` filter real-time telemetry to the authenticated connection's `tenant_id` (or `admin`), preventing cross-tenant event leakage.
- **Cross-Swarm `PrivacyAirlock` ([`core/storage.py`](core/storage.py))**: Scrubs emails (`<EMAIL>`), URLs (`<URL>`), IPs (`<IP>`), secrets (`<SECRET>`), file paths (`<PATH>`), and numbers (`<NUM>`) before storing lessons in `global_skills`.
- **Hardened Tool & Web Boundaries ([`tools.py`](tools.py), [`server.py`](server.py), [`static/index.html`](static/index.html))**:
  - **Container Sandboxing & Fail-Closed Execution**: `run_python` detects Docker/Podman container runtimes (`--network none`, `--read-only`, non-root, scrubbed env). If unavailable, it fails closed unless `CONFIG.unsafe_allow_local_exec` is explicitly enabled. Subprocess execution strips sensitive environment variables (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, `CREDENTIAL`).
  - **SSRF & TOCTOU DNS Rebinding Defense**: `fetch_url` uses `_resolve_and_validate_public_url` paired with `_pinned_getaddrinfo` to pin socket resolution strictly to the pre-validated public IP address while preserving TLS SNI, rejecting loopback, RFC-1918, link-local, and cloud metadata (`169.254.169.254`).
  - **AST, Math & Algorithmic DoS Guardrails**: `calculator` restricts evaluation to safe math functions, caps combinations/permutations and factorials ($n \le 1000$), enforces `calc_max_exponent: 1000`, and rejects expressions exceeding 300 characters.
  - **Multi-Tenant Workspace Namespacing**: `save_note`, `read_note`, and `list_notes` partition files under `workspace/tenants/{tenant_id}/`, block directory traversal, and reject restricted system files (`.env*`, `keys.json`, `swarm.db*`, `swarms.json`, `roles.json`, `memory.json`).
  - **Web UI & HTTP Hardening**: `server.py` enforces request payload caps (`max_body_bytes: 65536`), `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`; `static/index.html` HTML-escapes all dynamic telemetry and vault strings.

---

## Quickstart

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# 1. Launch the Live Event Dashboard Web UI (http://127.0.0.1:8000)
python server.py

# 2. Run Interactive Terminal CLI (Peer or Orchestrator mode)
python agent.py -m peer -t client_a

# 3. Run the 11-Test Deterministic Regression & Security Suite
python evals.py

# 4. Run Step 6 Framework Abstractions Comparison (LangGraph & OpenAI SDK)
python frameworks_comparison.py
```

Read **[ARCHITECTURE.md](ARCHITECTURE.md)** for a concise engineering breakdown of all modules and design decisions.
