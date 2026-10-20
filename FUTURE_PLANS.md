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
| **FP-01** | **Autonomous Swarm & Agent Factory (with Editable Lifecycle & Scoped Auth)** | Core Architecture / Runtime | **P0** | **Approved** | Formal Factory DP for agents and swarms, prompt-driven auto-generation, full swarm editability, and strict tenant-bound authorization. |
| **FP-02** | **ML-Powered Prompt Optimization, Semantic Vector Partitions & Exemplar Memory** | Applied ML / Optimization | **P1 (Next Target)** | **Approved** | Dense vector embeddings, multi-tenant vector partitions, tunable hybrid retrieval ($\alpha$), dynamic few-shot exemplar injection, per-role decoding profiles, and RLHF dataset logger. |

---

## 3. Approved Specification: FP-01 Swarm & Agent Factory System

### Architectural Goals
1. **Formal Factory Design Pattern**:
   - `AgentFactory`: Centralizes `WorkerAgent` and `PeerNode` instantiation, capability resolution (`@code`, `@files`, `@web`, `@memory`), tenant filesystem namespace injection, and role instruction binding.
   - `SwarmFactory`: Pluggable topology registry (`register_topology()`, `create_swarm()`). Zero procedural `if/else` branching when adding new topologies.
2. **Autonomous Swarm Generation (Meta-Planner)**:
   - Given a natural language mission (e.g. *"Audit smart contracts for reentrancy bugs"*), an AI meta-planner generates a candidate `SwarmDeploymentSpec` with inferred specialist roles, tools, directive, and topology.
3. **Editable Swarm Lifecycle (CRUD)**:
   - Users can automatically generate or manually define a swarm, but can freely **inspect, edit, and reconfigure** its roles, directives, model, and topology before or after running.
4. **Tenant-Bound Authorization & Ownership**:
   - **`admin`**: Full authority to create, edit, reassign, or delete swarms across all tenants.
   - **`user`**: Strictly scoped to `auth.tenant_id`. A tenant can only create, list, edit, or delete swarms belonging to their verified tenant partition. Cross-tenant mutation attempts are deterministically blocked with HTTP 403/404.
5. **Clean Code & No Legacy Burden**:
   - Build cleanly from first principles; no legacy compatibility wrappers required.

---

## 4. Approved Specification: FP-02 ML-Powered Prompt Optimization, Semantic Vector Partitions & Exemplar Memory

### Core Invariant: Universal Domain Agnosticism

The Neural Swarm harness is a **general-purpose cognitive runtime**. It must NEVER hardcode domain assumptions (such as assuming every swarm builds software or writes code).
- A swarm may be configured for **financial analysis**, **legal contract auditing**, **medical research synthesis**, **customer support**, **investigative journalism**, or **software development**.
- All factories, schemas, memory stores, and optimization loops must be strictly domain-neutral:
  - Roles are defined purely by `(instructions, capabilities, tools)`.
  - Exemplars capture arbitrary `(task, reasoning_steps, tool_calls, synthesis)`.
  - User profiles capture arbitrary domain preferences (e.g. `jurisdiction: 'EU-GDPR'`, `citation_style: 'APA'`, `tone: 'formal'`, `risk_tolerance: 'conservative'`).

---

### Architectural Objectives & Machine Learning Principles
Elevate the harness from static string prompts into an adaptive, mathematically grounded learning and retrieval system:
1. **Vector Embedding Layer & Semantic Retrieval**: Replace naive top-$K$ frequency sorting of skills with dense embedding similarity search across any problem domain.
2. **Tunable Hybrid Dense/Sparse Scoring**: Allow operators to control the trade-off weight $\alpha$ between conceptual semantic similarity (Dense) and exact keyword matching (BM25/Sparse).
3. **Multi-Tenant Vector Partitions**: Partition vector indices strictly by `(tenant_id, category)` to guarantee zero cross-tenant embedding leakage.
4. **Dynamic Few-Shot In-Context Learning (Prompt Improvement)**: Automatically distill verified successful swarm trajectories into "Exemplars" and inject the top matching exemplars into agent prompts at runtime.
5. **Universal User Personalization & Non-Parametric Memory**: Persist user-specific preferences, stylistic constraints, and domain knowledge in an embedded/relational database, bridging the gap between frozen model weights and tailored user results.
6. **Role-Specific Decoding Hyperparameters**: Expose per-role decoding profiles (temperature $T$, nucleus top-$p$, frequency penalty) matched to each role's cognitive objective (deterministic analysis vs. exploratory research).
7. **LLM-as-a-Judge Evaluation & RLHF Dataset Logger**: Quantitative multi-rubric scoring (Groundedness, Relevance, Tool Efficiency) and active learning logging (`datasets/chosen.jsonl` vs `datasets/rejected.jsonl`).

---

### Component Specifications & Interfaces

#### 1. Vector Embeddings Protocol (`core/embeddings.py`)
```python
class EmbeddingProvider(Protocol):
    """Abstract interface for dense vector embedding generation."""
    dimension: int

    def embed_text(self, text: str) -> List[float]:
        """Generate a dense vector for a single string."""
        ...

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Batch generate embeddings for multiple strings."""
        ...
```
- **Implementations**:
  - `GeminiEmbeddingProvider`: Uses `models/text-embedding-004` (768-dim) via Google's API key.
  - `DeterministicHashEmbeddingProvider`: Fast, zero-dependency 64-dim n-gram feature hashing fallback for unit tests and local environments without API keys.

#### 2. Vector Partition Store & SQLite Schema (`core/vector_store.py`)
- **Database Schema (`workspace/swarm.db`)**:
  ```sql
  CREATE TABLE IF NOT EXISTS tenant_vector_store (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id TEXT NOT NULL,
      category TEXT NOT NULL,
      item_id TEXT NOT NULL,
      text_content TEXT NOT NULL,
      metadata_json TEXT NOT NULL,
      vector_blob BLOB NOT NULL,  -- packed struct.pack(f'{dim}f', *vector)
      created_at REAL NOT NULL,
      UNIQUE(tenant_id, category, item_id)
  );

  CREATE INDEX IF NOT EXISTS idx_vector_tenant_cat 
  ON tenant_vector_store(tenant_id, category);
  ```
- **Similarity & Hybrid Scoring Formulas**:
  - **Cosine Similarity**:
    $$\text{CosineSim}(\mathbf{u}, \mathbf{v}) = \frac{\mathbf{u} \cdot \mathbf{v}}{\|\mathbf{u}\|_2 \|\mathbf{v}\|_2} = \frac{\sum_{i=1}^d u_i v_i}{\sqrt{\sum_{i=1}^d u_i^2} \sqrt{\sum_{i=1}^d v_i^2}}$$
  - **Tunable Hybrid Score ($\alpha \in [0.0, 1.0]$)**:
    $$\text{Score}(q, d) = \alpha \cdot \text{NormalizedDense}(e_q, e_d) + (1 - \alpha) \cdot \text{NormalizedBM25}(q, d)$$
    - $\alpha = 1.0 \implies$ Pure conceptual semantic search.
    - $\alpha = 0.0 \implies$ Pure exact keyword matching.
    - Default $\alpha = 0.7 \implies$ Best-of-both hybrid retrieval.

#### 3. Exemplar Distillation & Prompt Injection (`core/exemplars.py`)
- **Exemplar Record Schema**:
  - `task_pattern`: String describing the problem solved.
  - `tool_chain`: List of `(tool_name, arguments, result_summary)`.
  - `final_synthesis`: The verified solution.
  - `score`: Confidence score $[0.0, 1.0]$ assigned by critic or operator.
- **In-Context Injection Protocol ([`core/swarm.py`](core/swarm.py))**:
  When preparing `messages` for `execute_task`:
  1. Embed the incoming `task_prompt`.
  2. Query `tenant_vector_store` for partition `(tenant_id, role)` with $K=2$ nearest neighbors.
  3. Format matching exemplars into the system context:
     ```text
     [VERIFIED SUCCESSFUL DEMONSTRATIONS (FEW-SHOT EXEMPLARS)]:
     Example 1:
       Task: "Calculate compound interest for $5,000 at 6% for 5 years"
       Action: calculator(expression="5000 * (1 + 0.06)**5")
       Observation: 6691.1278
       Output: "The investment grows to $6,691.13 over 5 years."
     ```

#### 4. Role-Specific Decoding Profiles (`core/decoding.py`)
```python
@dataclass
class DecodingProfile:
    temperature: float = 0.1
    top_p: float = 0.9
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
```
- **Profiles mapped in `config/settings.json`**:
  - `"coder"`: `temperature=0.0`, `top_p=0.1` (greedy, deterministic code/math).
  - `"critic"`: `temperature=0.1`, `top_p=0.2` (strict verification).
  - `"researcher"`: `temperature=0.4`, `top_p=0.85`, `presence_penalty=0.2` (query diversity).
  - `"architect"`: `temperature=0.5`, `top_p=0.9`, `presence_penalty=0.3` (creative decomposition).

#### 5. Evaluation Judge & Active Learning Dataset Generator (`core/evaluation_judge.py`)
- **Automated Rubrics**:
  - **Faithfulness Score $[0.0, 1.0]$**: Verification that all factual statements in the output match facts returned by tool executions.
  - **Relevance Score $[0.0, 1.0]$**: Cosine similarity between user prompt embedding and final output embedding.
  - **Tool Efficiency $[0.0, 1.0]$**: $1.0 - \min(1.0, \frac{\text{redundant\_calls}}{\text{total\_calls}})$.
- **Preference Data Collection**:
  - `/api/feedback` endpoint records `(prompt, swarm_output, user_critique)`.
  - Upvoted runs $\to$ appended to `datasets/chosen.jsonl`.
  - Downvoted runs $\to$ appended to `datasets/rejected.jsonl`.
  - Yields clean Direct Preference Optimization (DPO) and RLHF datasets.

#### 6. Universal User Personalization & Non-Parametric Memory (`core/storage.py`)
- **Domain-Neutral User Profile Schema (`workspace/swarm.db`)**:
  ```sql
  CREATE TABLE IF NOT EXISTS user_profiles (
      tenant_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      display_name TEXT NOT NULL,
      domain_focus TEXT NOT NULL DEFAULT '',     -- e.g., 'Biomedical Research', 'Quantitative Finance', 'Fullstack Engineering'
      preferences_json TEXT NOT NULL DEFAULT '{}', -- Key-value constraints: {"tone": "formal", "compliance": "HIPAA", "citation_format": "APA"}
      updated_at REAL NOT NULL,
      PRIMARY KEY(tenant_id, user_id)
  );
  ```
- **Universal Context Injection Protocol**:
  When any specialist agent initializes its context block in `WorkerAgent._build_context_block()`:
  ```text
  [USER PREFERENCES & DOMAIN CONSTRAINTS (tenant='{tenant_id}', user='{user_id}')]:
    - Domain Context: Biomedical Research
    - Response Style: Formal, evidence-backed
    - Mandatory Constraints: Always cite PubMed IDs; flag experimental drug interactions explicitly.
  ```
- **Database Architecture Evolution**:
  - **Embedded Baseline**: SQLite (WAL mode) with packed binary vector blobs for zero-setup local execution.
  - **Enterprise Migration**: Ready-to-plug PostgreSQL + `pgvector` adapter implementing the identical `StorageBackend` Protocol, supporting combined relational user metadata and vector similarity queries (`ORDER BY embedding <=> query_vector`).

---

### Security, Isolation & Validation Rules
1. **Tenant Isolation Invariant**: All vector store queries MUST include `WHERE tenant_id = ?` using `validate_tenant_id()`. Zero cross-tenant embedding comparisons.
2. **Privacy Airlock**: Any exemplar promoted from tenant-private memory to global cross-swarm memory MUST pass through `PrivacyAirlock.sanitize()` to scrub emails, IPs, URLs, secrets, and file paths.
3. **Graceful Degradation**: If vector services or embedding APIs fail, the harness automatically falls back to keyword matching without crashing.

---

## 5. Candidate Ideas for Consideration

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

## 6. Completed Milestones Archive

- [x] **Decoupled Hexagonal Core**: Abstract `LLMProvider`, `StorageBackend`, and `EventBus` protocols.
- [x] **Two Swarm Topologies**: Step 2 Orchestrator delegation + Step 3 Peer Inbox network.
- [x] **Multi-Tenant SQLite Vault**: WAL mode, composite indexes, and strict regex tenant validation.
- [x] **Cross-Swarm Privacy Airlock & Egress DLP**: 5-category PII regex scrubber + token redaction.
- [x] **Security Hardening**: AST calculator DoS caps, SSRF & DNS rebinding socket pinning, fail-closed sandbox, tenant workspace isolation.
- [x] **API Key Auth & Scoped RBAC**: Constant-time verification, non-loopback binding guardrail, tenant SSE isolation.
- [x] **Automated Testing Suite & CI**: 39-test pytest suite (unit, integration, e2e) + GitHub Actions CI workflow.
- [x] **Cloud LLM Support**: High-speed Google Gemini (`gemini-3.5-flash-lite`, ~1.2s per turn).
