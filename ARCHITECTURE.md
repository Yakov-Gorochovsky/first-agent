# Architecture & Engineering Handbook (`first-agent`)

A concise reference covering the core design patterns, decoupled interfaces, configuration system, and security architecture of `first-agent`.

---

## 1. Core Mental Model: "Brain" vs. "Harness"

1. **The Brain (LLM Weights — `qwen3:8b`, `llama3`, Gemini, Groq)**: Stateless text/tool predictor. Cannot execute code, browse the web, persist state, or loop on its own.
2. **The Harness (This Codebase)**: The runtime wrapped around the model providing **memory, tools, budgets, multi-agent communication, and a `Think -> Act -> Observe` loop**.

---

## 2. Key Architectural Decisions

### 1. Hexagonal Architecture (Loose Coupling via `Protocol`)
All swarm engines depend only on abstract interfaces in `core/`, never concrete implementations:
- **[`LLMProvider`](core/llm.py)**: `llm.generate(messages, tools)` — swap Ollama, Groq, Gemini, Hugging Face, or `ScriptedMockLLM` with zero changes to swarm code.
- **[`StorageBackend`](core/storage.py)**: `save_tenant_memory`, `add_global_skill`, `append_session_turn`, `log_trace` — swap SQLite for Postgres in one class.
- **[`EventBus`](core/bus.py)**: Decoupled Pub/Sub bus (`bus.subscribe` / `bus.emit`). Swarms emit events without knowing whether the CLI, SSE Web Server, or Eval Runner is listening.

### 2. Zero Hardcoding: Layered Configuration ([`config/settings.json`](config/settings.json) + [`core/config.py`](core/config.py))
Every timeout, budget limit, database path, capability bundle (`@web`, `@code`, `@files`, `@memory`), and coordinator/orchestrator prompt template lives in [`config/settings.json`](config/settings.json), with seed specialist roles in [`config/default_roles.json`](config/default_roles.json). Environment variables in `.env` override JSON defaults via [`SwarmConfig`](core/config.py).

### 3. Dual-Mode Tool Calling + Qwen3 Fast Mode ([`core/llm.py`](core/llm.py))
- **Auto-Fallback**: `OpenAICompatibleProvider` attempts native OpenAI `tools=` calling first and automatically falls back to **ReAct JSON prompting** if a model rejects tool schemas.
- **`/no_think` Optimization**: Prepends `/no_think` for `qwen3` models when `fast_thinking=true`, cutting local multi-agent routing latency by 5–10x.

### 4. Two Swarm Topologies ([`core/swarm.py`](core/swarm.py) vs. [`core/peer_swarm.py`](core/peer_swarm.py))

```mermaid
flowchart LR
    subgraph Step2["Step 2: Orchestrator -> Workers (core/swarm.py)"]
        O["Orchestrator"] <-->|"delegate_to_worker()"| W["Workers"]
    end

    subgraph Step3["Step 3: Peer Inbox Network (core/peer_swarm.py)"]
        C["Coordinator [Inbox]"] -->|"send_peer_message()"| R["Researcher [Inbox]"]
        R -->|"send_peer_message()"| D["Coder [Inbox]"]
        D -->|"send_peer_message()"| Q["Critic [Inbox]"]
    end
```

- **Step 2 (`OrchestratorSwarm`)**: Central hub delegates tasks and synthesizes results.
- **Step 3 (`PeerSwarm`)**: Decentralized peer network where each specialist owns a FIFO `deque[PeerMessage]` inbox and hands off work directly to peers (`yield_on_tools=["send_peer_message", "finish_swarm"]`).

### 5. Declarative `RoleRegistry` & Capability Bundles ([`core/roles.py`](core/roles.py))
Roles are data-driven (`RoleSpec`) and use capability tags (`@web`, `@code`, `@files`, `@memory`, `@all`) instead of hardcoded tool lists. New roles can be created via `config/default_roles.json`, the Web UI (`+ New Role`), the CLI (`/add-role`), or autonomously mid-run by the AI (`spawn_specialist`).

---

## 3. Security Architecture & Threat Mitigations

| Layer | Threat / Vector | Mitigation Implemented |
| :--- | :--- | :--- |
| **Network (`fetch_url`)** | Server-Side Request Forgery (SSRF) to localhost, internal LAN, or cloud metadata (`169.254.169.254`) | `_validate_public_url()` in [`tools.py`](tools.py) restricts schemes to `http`/`https`, resolves DNS via `socket.getaddrinfo`, blocks loopback/private/link-local/reserved IPs, and disables HTTP redirects (`allow_redirects=False`). |
| **Subprocess (`run_python`)** | Exfiltration of `API_KEY` or cloud tokens via `os.environ` in generated Python code | `_get_sandbox_env()` in [`tools.py`](tools.py) strips all env vars matching `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, or `CREDENTIAL`, enforces `python_timeout_sec`, and scopes `cwd` to `workspace/`. |
| **Math (`calculator`)** | Arbitrary code execution (`eval`) or CPU/RAM Denial-of-Service (`999999 ** 999999`) | Whitelisted `ast` node evaluator in [`tools.py`](tools.py) + `_safe_pow()` enforcing `calc_max_exponent` (`1000`). |
| **Filesystem (`save_note`/`read_note`)** | Path traversal (`../../`) or overwriting `.env` | `_safe_workspace_path()` in [`tools.py`](tools.py) strips directory components, blocks dotfiles, and verifies `target.resolve().relative_to(WORKSPACE_DIR.resolve())`. |
| **Multi-Tenant DB (`SQLiteStorage`)** | Cross-tenant memory leakage, SQL injection, or PII leakage into shared skills | Parameterized SQL queries, `sanitize_tenant_id()` validation, rolling row pruning, and [`PrivacyAirlock.sanitize()`](core/storage.py) scrubbing `<EMAIL>`, `<URL>`, `<IP>`, `<SECRET>`, `<PATH>`, and `<NUM>`. |
| **Web Server & UI (`server.py`, `index.html`)** | DOM XSS from untrusted web/tool output, clickjacking, or memory exhaustion via giant POST bodies | `escapeHtml()` & `safeColor()` in [`static/index.html`](static/index.html); `max_body_bytes` (`64 KB`) cap, `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY` in [`server.py`](server.py). |
| **Swarm Runtime (`BudgetTracker`)** | Runaway agent loops or unbounded token spend | [`BudgetTracker`](core/budget.py) enforces `max_swarm_steps`, `max_steps_per_agent`, `max_total_tokens`, and `max_wall_seconds` before every LLM call. |

---

## 4. Module Reference

| File | Responsibility |
| :--- | :--- |
| [`config/settings.json`](config/settings.json) | Central JSON configuration for LLM, budgets, storage, tools, server, capability bundles, and system prompts |
| [`config/default_roles.json`](config/default_roles.json) | Default seed specialist roles (`researcher`, `coder`, `critic`, `security_reviewer`) |
| [`core/config.py`](core/config.py) | `SwarmConfig` loader merging `config/settings.json` with `.env` overrides |
| [`core/bus.py`](core/bus.py) | `EventBus` & `SwarmEvent` pub/sub telemetry |
| [`core/budget.py`](core/budget.py) | `BudgetConfig`, `BudgetTracker`, `BudgetExceededError` |
| [`core/storage.py`](core/storage.py) | `StorageBackend`, `SQLiteStorage` (WAL mode + auto-pruning), `PrivacyAirlock` |
| [`core/llm.py`](core/llm.py) | `LLMProvider`, `OpenAICompatibleProvider` (native tools + ReAct fallback + Qwen `/no_think`) |
| [`core/roles.py`](core/roles.py) | `RoleSpec`, `RoleRegistry`, capability bundle resolver, `spawn_specialist` tool factory |
| [`core/swarm.py`](core/swarm.py) | `WorkerAgent` execution loop & Step 2 `OrchestratorSwarm` |
| [`core/peer_swarm.py`](core/peer_swarm.py) | Step 3 `PeerSwarm` decentralized FIFO inbox network |
| [`tools.py`](tools.py) | Hardened atomic & universal tools + OpenAI JSON schemas |
| [`agent.py`](agent.py) | Interactive multi-tenant CLI supporting both `peer` and `orchestrator` topologies |
| [`server.py`](server.py) | Zero-dependency HTTP + Live SSE streaming backend |
| [`static/index.html`](static/index.html) | Linear/Palantir dark-slate Command Center UI with Retina Canvas graph |
| [`evals.py`](evals.py) | Step 5 deterministic evaluation, regression & security audit suite (9 checks) |
| [`frameworks_comparison.py`](frameworks_comparison.py) | Step 6 side-by-side LangGraph (`StateGraph`) & OpenAI Agents SDK (`SDKRunner`) equivalence |

---

## 5. Step 6: Framework Equivalence Mapping ([`frameworks_comparison.py`](frameworks_comparison.py))

| Concept | Custom Implementation | Framework Equivalent |
| :--- | :--- | :--- |
| **Single-Agent Tool Loop** | [`WorkerAgent.execute_task()`](core/swarm.py) | LangChain `AgentExecutor` / Claude Agent SDK |
| **Hub-and-Spoke Delegation** | [`OrchestratorSwarm`](core/swarm.py) (`delegate_to_worker`) | Anthropic Subagents / CrewAI Hierarchical |
| **Stateful Graph Routing** | [`PeerSwarm`](core/peer_swarm.py) + [`StateGraph`](frameworks_comparison.py) | **LangGraph** (`StateGraph`, `add_node`, `add_conditional_edges`) |
| **Decentralized Handoffs** | `yield_on_tools=["send_peer_message"]` + [`SDKRunner`](frameworks_comparison.py) | **OpenAI Agents SDK** (`Agent(handoffs=[...])`, `Runner.run_sync`) |
| **Persistence & Memory** | [`SQLiteStorage`](core/storage.py) + [`PrivacyAirlock`](core/storage.py) | LangGraph `SqliteSaver` + `BaseStore` |
| **Observability** | [`EventBus`](core/bus.py) + [`server.py`](server.py) SSE Stream | LangSmith / Langfuse / Arize Phoenix |
