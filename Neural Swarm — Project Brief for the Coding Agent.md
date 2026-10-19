# Neural Swarm — Project Brief for the Coding Agent

Project context, verified findings, and work plan · October 19, 2026

## Purpose and ground rules

Neural Swarm is a modular multi-agent AI harness in Python: orchestrator–worker and peer-inbox topologies, tool calling with a ReAct fallback, declarative roles and swarms, per-tenant storage, budget guardrails, and a live SSE dashboard. It is intended for real client deployments. Treat it as a product that operators and client engineers will run, audit, and maintain — not a demo, tutorial, or showcase.

Ground rules for every change:

- **Honest claims only.** Documentation and code comments state only what the code or tests enforce. Any number states its sample size and conditions. Avoid absolutes such as 'blocked 100%' unless a test enumerates the cases.
- **Small, reviewable changes.** One concern per change, with tests, and docs updated in the same change. No unrelated refactors. Propose a short plan before large work.
- **Core stays framework-free.** Keep the core behind its ports (`LLMProvider`, `StorageBackend`, `EventBus`). Do not add LangGraph, LangChain, or the OpenAI Agents SDK as core dependencies (see the framework decision below).
- **Treat these as untrusted:** model output, tool arguments, web content, and any tenant-supplied value.
- **Quality gates:** type hints, ruff, mypy, pytest. Ask before adding a runtime dependency or changing a public API or config key; provide a migration when config changes.
- **Never commit secrets.** Secrets come from the environment only.

## What exists today

- `core/`: ports and implementations — `llm.py` (OpenAI-compatible and native Ollama), `swarm.py` (orchestrator–worker), `peer_swarm.py` (inbox network), `swarm_manager.py` (multi-swarm and cross-swarm bridge), `roles.py`, `budget.py`, `bus.py`, `storage.py` (SQLite, `PrivacyAirlock`), `config.py`.
- `tools.py`: calculator, notes, `fetch_url`, `run_python`, web search.
- `server.py`: standard-library threaded HTTP server with SSE; `static/index.html` UI.
- `agent.py`: interactive CLI. `evals.py`: 10 deterministic harness tests run against a scripted mock LLM (the README says 9; fix it).

## Verified defects — fix first (P0)

These were confirmed by reading the code and running tests against it.

1. **`run_python` is not a sandbox.** User code is `exec`'d into the same globals as the socket-guard bootstrap. Two bypasses were reproduced against a local server: restoring `_orig_connect`, and spawning a child interpreter. Required: run code in an isolated container or OS sandbox (no network by default, read-only root, tmpfs workspace, non-root user, CPU/memory/PID limits, wall-clock timeout, scrubbed env). If isolation is unavailable, fail closed and disable the tool unless an explicit unsafe flag is set, with a startup warning. Keep the socket guard only as defense in depth. Add regression tests for both bypasses plus decimal/hex/IPv6-mapped loopback, DNS-rebinding hostnames, and `subprocess` network clients.
2. **Tenant IDs collide.** `sanitize_tenant_id` replaces invalid characters, so `acme.corp` and `acme_corp` map to the same tenant. Validate strictly (`^[A-Za-z0-9_-]{1,64}$`) and reject anything else (HTTP 400 at the API). Check existing databases for collisions and document a migration.
3. **No authentication; the tenant comes from a request parameter.** Anyone who can reach the port can read any tenant. Add API-key auth (keys stored hashed, each bound to one tenant and a scope), derive the tenant from the credential, require an admin scope for role/swarm/airlock endpoints, use constant-time comparison, and refuse to start on a non-loopback host without auth configured.
4. **SSE leaks across tenants.** `_broadcast_event` sends every event to every connected client. Filter the stream by the authenticated tenant, scope system events to admin, and add a test with two tenants proving no cross-delivery.
5. **Documentation overclaims.** `SECURITY_FINDINGS.md` and `PROBLEMS_AND_METRICS.md` use '100% blocked' language based on a handful of probes and one benchmark task. Rewrite per the documentation section below.

## Reliability and operations (P1)

- **LLM client resilience:** retries with exponential backoff and jitter on 429/5xx/timeouts (honor `Retry-After`), a circuit breaker, per-call and per-mission deadlines, an ordered provider fallback chain, and classified errors. Test with fault injection.
- **Budgets:** use `time.monotonic()` for durations. Pass `max_tokens` so overshoot is bounded (today a 1000-token cap can reach 1050). Add per-tenant budgets.
- **Event bus:** log subscriber exceptions instead of swallowing them, bound `history` (`deque(maxlen=...)`), make subscribe/emit thread-safe, and version and document the event schema.
- **Storage:** reuse connections per thread, set `busy_timeout`, add indexes on `(tenant_id, id)` for sessions and traces, prune on a schedule instead of on every insert, and add versioned migrations. Keep the `StorageBackend` port.
- **Concurrency:** remove the global single-mission lock (`_run_lock`). Use a per-tenant concurrency limit with a queue and backpressure, cancellable missions, and graceful shutdown (no fire-and-forget daemon threads).
- **Observability:** structured JSON logging via `logging` (no `print` in library code) with `request_id`, `mission_id`, and `tenant_id`. Add OpenTelemetry traces (mission → agent step → LLM call → tool call) and metrics (tokens, latency, tool errors, budget trips) exported over OTLP. Keep the EventBus for the UI.
- **Durable runs:** persist mission state so a crashed run can be inspected, resumed, or marked failed; make tool execution idempotent where possible.
- **Config:** validate at startup against a schema and fail fast; document every key.
- **Demo mode:** `_run_cinematic_simulation` emits hardcoded results. Rename it 'demo mode', disable it in production config, and never let it write to real tenant stores.
- **Packaging and CI:** `pyproject.toml` with extras (`dev`, `otel`, `postgres`), a pinned lockfile, a non-root Dockerfile, `docker-compose` (app plus optional collector/Jaeger/Prometheus/Grafana), pre-commit, and GitHub Actions running ruff, mypy, and pytest.
- **Tests:** move to pytest (unit and integration) and keep `evals.py` as harness tests. Add a separate model-quality eval set (golden tasks, pass rate with n, run manually or nightly against a real model).

## Capabilities to build only when a client needs them (P2)

Postgres backend with Row-Level Security for tenants; Redis for shared rate limits and budgets and for multi-instance SSE fan-out; an async runtime; a retrieval tool on pgvector with recall@k and faithfulness evals; extra providers (Bedrock, Azure OpenAI); an MCP server exposing the tools; human-approval gates for risky tools; an audit log; tenant data export and deletion; retention policy.

## Framework decision: LangGraph and the OpenAI Agents SDK

Decision: keep the custom core. Do not add either framework now. The reasons below replace the earlier justification, which had errors:

- The Agents SDK is not OpenAI-only. It runs non-OpenAI models through the LiteLLM extension or any OpenAI-compatible endpoint, with caveats (structured-output gaps on some providers, tracing defaults pointing at OpenAI).
- The \~45-line `StateGraph` and \~35-line `SDKRunner` in `frameworks_comparison.py` are teaching re-implementations of the routing idea. They do not provide what makes LangGraph valuable: durable checkpointing, parallel supersteps with reducers, interrupts for human-in-the-loop, streaming, and persistence stores. The EventBus plus SSE is live telemetry, not an equivalent of LangSmith (no persistent trace store, datasets, or evals).
- Neither framework supplies this project's real differentiators: sandbox isolation, tenant authentication, egress controls, and budgets.

Adopt a framework only if a client requires one of: durable execution and resume after a crash, human approval pauses, parallel fan-out and join, or a mandated framework or tracing stack. If that happens, add it as an optional adapter behind the existing ports (a packaging extra with its own tests), not as a core rewrite. Prefer OpenTelemetry over vendor tracing.

## Documentation standards and tasks

Style: neutral, factual, present tense. No marketing or decorative language (remove 'Tactical Command Center', 'Retina', 'cinematic', 'Palantir' styling references). Label each feature Implemented or Planned. Every quantitative claim links to a test or benchmark and states n and conditions. Date assessments. Pick one product name — the repo uses both 'Neural Swarm' and `first-agent` — and apply it to the package, docs, and UI.

Deliverables:

- **README.md** (operator and client audience): what it is and is not; implemented vs planned features; requirements; quickstart (container and local); configuration reference listing every key, env var, default, and meaning; run modes; API reference (auth, endpoints, request/response, SSE event schema); deployment (ports, volumes, secrets, sizing); security summary with a link; troubleshooting; versioning; license. Drop the '6-step progression' tutorial framing.
- **ARCHITECTURE.md:** system context, component diagram, mission sequence diagram (Mermaid), data model, ports and adapters, topologies, concurrency model, failure modes and recovery, scaling limits (SQLite is single-node), and an ADR index.
- **docs/adr/:** ports and adapters; SQLite then Postgres; two topologies; dual-mode tool calling; custom harness vs frameworks (the decision above); sandbox isolation model.
- **SECURITY.md:** reporting process, supported versions, attacker model and trust boundaries, and for each control the enforcing mechanism and its known limits, plus a deployment hardening checklist. Convert `SECURITY_FINDINGS.md` into a dated assessment with open/fixed status and honest wording ('not reproduced in the tested cases').
- **docs/performance.md** (from `PROBLEMS_AND_METRICS.md`): hardware, model and versions, prompt, at least 10 runs, mean and standard deviation, and a reproducible `bench/` script. Keep harness-test results separate from model-quality results.
- **docs/operations.md:** start/stop, backup and restore, retention, log locations, monitoring and alerts, incident response, upgrades.
- **CHANGELOG.md, CONTRIBUTING.md, LICENSE** (the owner decides the license; likely proprietary for client work).
- **`frameworks_comparison.py`:** move to `examples/` and state in its header that it is an illustrative re-implementation that does not import either framework.

## Working agreement

Before code changes, post a short plan. Work in the order P0, P1, docs, P2. After each item run the full test suite and update the docs in the same change. List assumptions and anything you could not verify. If a requirement here conflicts with the code you find, say so instead of guessing.

## Definition of done

- All P0 items fixed with regression tests; the two sandbox bypasses are covered.
- CI green on ruff, mypy, and pytest; the container builds and runs as non-root.
- No documentation claim lacks a test or a benchmark with stated n.
- A new operator can deploy from the README alone.
