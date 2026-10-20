# Future Plans & Feature Roadmap

This document serves as the collaborative feature backlog and architectural planning space for the **Neural Swarm Engine**.

---

## 1. Collaborative Intake Protocol

Whenever a new feature or capability is proposed:
1. **Idea Discussion**: The user proposes the idea or requirement.
2. **Architectural Evaluation & Opinion**: Before writing or committing code, we evaluate:
   - **Feasibility & Complexity**: Low / Medium / High implementation effort.
   - **Architectural Fit**: Does it respect our Hexagonal Architecture (zero heavy framework locks, decoupled interfaces)?
   - **Security & Safety Impact**: Does it create new attack surfaces (SSRF, arbitrary execution, cross-tenant leaks)?
   - **Performance & Latency**: Impact on turn latency, token budgets, and local vs. cloud inference.
   - **Alternative Approaches**: Can we achieve the outcome with simpler primitives?
3. **Roadmap Recording**: Once aligned, the feature is logged below with technical specifications, trade-offs, and test criteria before implementation begins.

---

## 2. Feature Backlog & Ideas Under Review

| ID | Feature Proposal | Category | Priority | Status | Summary & Technical Impact |
| :--- | :--- | :--- | :--- | :--- | :--- |
| *—* | *(Awaiting your first proposal)* | *—* | *—* | *Under Review* | *—* |

---

## 3. Candidate Ideas for Consideration

The following items are potential enhancements identified during architecture reviews:

### A. Multi-Turn Interactive Web Chat & Session Replay
- **Category**: UI & Observability
- **Description**: Add conversational persistence so users can chat back-and-forth with an active swarm on the web dashboard (backed by `tenant_sessions` in SQLite) and replay past missions step-by-step on the canvas.
- **Estimated Effort**: Medium
- **Status**: *Candidate*

### B. Per-Role Model Routing (Heterogeneous Swarms)
- **Category**: Agent Runtime / LLM Adapters
- **Description**: Allow different roles within the same swarm to use different models (e.g. lightweight, ultra-fast Gemini Flash for `coordinator` and deep reasoning models like Claude 3.5 Sonnet or Gemini Pro for `coder`/`critic`).
- **Estimated Effort**: Low-Medium
- **Status**: *Candidate*

### C. Docker/Podman Container Sandbox Runner
- **Category**: Security & Tool Runtime
- **Description**: Provide a `Dockerfile.sandbox` with zero network access (`--network none`) and read-only filesystem mounts for strict OS-level container isolation when executing untrusted Python scripts via `run_python`.
- **Estimated Effort**: Medium
- **Status**: *Candidate*

### D. Interactive Swarm Debugger (Pause, Step-by-Step, Resume)
- **Category**: Developer Tooling
- **Description**: Allow operators to pause active peer handoffs, inspect specialist inboxes in real-time, step forward one turn at a time, or inject manual steer prompts into an agent's inbox.
- **Estimated Effort**: Medium
- **Status**: *Candidate*

---

## 4. Completed Milestones Archive

- [x] **Decoupled Hexagonal Core**: Abstract `LLMProvider`, `StorageBackend`, and `EventBus` protocols.
- [x] **Two Swarm Topologies**: Step 2 Orchestrator delegation + Step 3 Peer Inbox network.
- [x] **Multi-Tenant SQLite Vault**: WAL mode, composite indexes, and strict regex tenant validation.
- [x] **Cross-Swarm Privacy Airlock & Egress DLP**: 5-category PII regex scrubber + token redaction.
- [x] **Security Hardening**: AST calculator DoS caps, SSRF & DNS rebinding socket pinning, fail-closed sandbox, tenant workspace isolation.
- [x] **API Key Auth & Scoped RBAC**: Constant-time verification, non-loopback binding guardrail, tenant SSE isolation.
- [x] **Automated Testing Suite & CI**: 39-test pytest suite (unit, integration, e2e) + GitHub Actions CI workflow.
- [x] **Cloud LLM Support**: High-speed Google Gemini (`gemini-3.5-flash-lite`, ~1.2s per turn).
