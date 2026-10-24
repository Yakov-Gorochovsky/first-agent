# Neural Swarm Engine: Roadmap and Engineering Plan

Revision 3 · October 22, 2026 · Supersedes Revision 2

This document is the engineering plan for the Neural Swarm Engine. It is written for the maintainers, the coding agent, and any client security reviewer. It states what will be built, in what order, how each change will be measured, and what must be secured before it ships. It is sized for a single developer: only Tier 1 is committed work, and everything else is a backlog released by measurement (section 1).

---

## 0. How to read this document

**Status vocabulary**

- **Planned**: specified, not started. **In progress**: being built behind a feature flag. **Shipped**: passed its gate and enabled by default. **Deferred**: intentionally postponed. **Rejected**: will not be built, with the reason recorded.

**Claims policy (applies to this document, the README, and any external material)**

1. A number is a **result** only if a committed evaluation report produced it. Everything else is a **hypothesis** and is labeled `Target (hypothesis)`.
2. Every result states its sample size, conditions, model and provider versions, and confidence interval.
3. Words such as "guarantees", "100%", "zero", and "eliminates" are allowed only when a test enumerates the cases, and then the count of cases is stated.
4. A prompt is not a control. Any rule that must hold (tenant isolation, compliance constraints, tool permissions) is enforced in code, not by instructing the model.
5. A plan is not evidence. Items in this document are not capabilities until they are Shipped.

---

## 1. Operating model and change control

### 1.1 Capacity and operating model

This plan is for a single developer working part-time without a deadline. The goal is steady, verifiable progress, not completeness.

- **Tiers.** Tier 1 is committed work. Tier 2 starts only when the Tier 1 gates pass and a measurement shows a need. Tier 3 starts only on a client requirement, or after Tiers 1 and 2 show headroom. The 20 specifications in section 6 are a backlog, not a commitment.
- **Integration Gate.** A feature or component is not done or considered shipped merely because it exists as a library with unit tests. It must be wired into an active runtime path (`server.py`, `agent.py`, or `core/mission.py`) and verified by an end-to-end integration test. One wired, measured vertical slice is prioritized over unwired library modules.
- **One thing at a time.** At most one Tier 1 or Tier 2 feature is in progress at once.
- **Vertical slices.** Build the smallest end-to-end version first (for FP-00: ten tasks and one metric), then deepen it.
- **Time boxes.** Each feature gets a time box, by default 2 to 4 weeks of part-time work. At the end the outcome is one of: ship (gate passed), keep behind a flag, or stop and record the result. A well-measured null or negative result is a valid outcome and is committed.
- **Proportionate governance.** Process scales with risk (section 1.2). Nothing in this document requires an evaluation run, a threat model, or a statistical gate per pull request.

### 1.2 Change classes

| Class | Examples | Required |
| :-- | :-- | :-- |
| S, small | Bug fix, refactor, docs, test | Normal pull request with mock-LLM CI. If it touches tenant scoping, authentication, tools, or storage, add a probe test. |
| F, feature | Any FP item | A one-page feature note: problem, simplest alternative, primary metric and how the baseline is obtained, hypothesis and ship rule written before the run, the threats listed for that FP in section 8.3 with the controls chosen, flag name, rollback. Standard-tier evaluation at the gate. |
| G, phase gate | Closing a phase | Full-tier evaluation wherever a claim is made, the phase's security gate in section 8.6, and a refreshed baseline. |

### 1.3 Definition of Done (feature)

Tests pass; the flag exists with a default; baseline and treatment reports are committed at the tier above; security items for that FP are closed or explicitly deferred with a recorded reason; docs are updated; the rollback has been exercised once.

---

## 2. Invariants

1. **Generic runtime, domain-specific packs.** The core never assumes a domain, a fixed role list, or a swarm type. See 2.1.
2. **Ports and adapters.** The core depends only on `LLMProvider`, `StorageBackend`, `EventBus`, `EmbeddingProvider`, and `SandboxRunner` protocols. Frameworks are optional adapters, never core dependencies.
3. **Tenant isolation is enforced below the model.** Tenant identity comes from the authenticated credential, never from a request parameter or model output. Every storage query is tenant-scoped in code and covered by a cross-tenant test.
4. **Untrusted by default.** Model output, tool output, web content, retrieved text, learned memory, and user-supplied profile fields are all untrusted input to the next stage.
5. **Append-only context by default.** Prompts are built so the stable prefix does not change within a mission. Any mutation of earlier context is a deliberate, measured exception.
6. **Learned behavior is gated.** Nothing learned automatically (exemplars, skills, prompts) changes agent behavior until it passes an evaluation gate and human approval.
7. **Evidence before optimization.** No optimization ships without a baseline, a treatment run, and a decision under the rule in section 5.6.
8. **Reversible.** Every feature is behind a flag with a tested rollback.
9. **Policy is enforced in code.** Regulated or dual-use capabilities are governed by operator-authorized policy packs, scope allow-lists, approvals, and audit logs, never by prompt wording.

### 2.1 Generic swarm model

A swarm is composed for the task it receives. The runtime does not know in advance whether the task is software development, research, security testing, document review, or anything else.

- **Roles are data.** A `RoleSpec` holds a name, purpose, instructions, capability list, tool list, model policy, skill tags, and limits. No role name appears in core code paths. Any built-in roles live in an optional starter library used by tests and examples, and the runtime does not depend on them.
- **Capabilities and tools are registered.** The registry declares each entry's `risk_class`, `side_effects`, and `data_classification`. Names such as `@code`, `@files`, `@web`, and `@memory` are examples. New tools register without changing the core.
- **Topologies are plugins.** Orchestrator-worker, peer network, pipeline, review loop, and others are entries in a registry. The planner selects one per task. None is the default for every task.
- **Composition happens per task.** The planner synthesizes roles, tools, topology, and budget from the task description. Roles may be ephemeral (one mission) or promoted to the tenant's library after approval.
- **Skills attach to capability and task-tag signatures, not role names**, because roles are dynamic. A skill is injected into a role only if the role holds every capability the skill requires.
- **Domain packs are optional.** A pack bundles role templates, tools, a policy pack, skill seeds, and evaluation tasks for one domain (for example software, research, security testing, support, finance). The core ships only a minimal general-purpose pack used by tests.
- **Dual-use is policy, not a swarm type.** A "security testing" swarm gets offensive or scanning capabilities only through an operator-authorized policy pack with explicit scope allow-lists, stronger approval, and full audit. The same rule applies to any domain with regulated or sensitive capabilities.
- **Conformance tests.** (1) A static check that `core/` contains no domain role names or domain-specific prompts. (2) The runtime completes a mission using arbitrary synthetic roles. (3) Evaluation spans at least 3 domain packs plus 1 held-out domain. (4) No feature ships if it regresses any pack beyond its margin.

---

## 3. Current-state caveats and prerequisites

- Items listed as complete in section 9 are the owner's statements. They are **not** independently verified. Section 8.5 lists the tests that must be re-run before any claim is relied on.
- The existing 10 `evals.py` checks use a scripted mock LLM. They test the harness, not model quality. They remain as harness tests and are not quality evidence.
- Existing performance figures come from a single benchmark task and very small samples. Treat them as anecdotes until FP-00 reproduces them.
- Prerequisites tracked in the project brief and required before FP-00 benchmarking, because provider errors contaminate measurements: LLM client retries with backoff and jitter, timeouts, a circuit breaker, and structured JSON logging with correlation IDs.

---

## 4. Roadmap (ordered by implementation)

IDs are stable identifiers. The `Order` column is the implementation sequence. Legacy IDs from the previous revisions appear in parentheses.

| Order | ID | Feature | Tier | Phase | Depends on | Effort | Risk |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| 1 | FP-00 | Evaluation, Telemetry and Measurement Foundation (built in stages) | 1 | A | prerequisites | High | Low |
| 2 | FP-01 | Hardened Code Execution (was Candidate C) | 1 | A | none | Medium | High |
| 3 | FP-02 | Untrusted-Content Boundary and Prompt-Injection Defense | 1 | A | FP-00 | Medium | High |
| 4 | FP-03 | Cost Governance and Metering | 1 | A | FP-00 | Medium | Medium |
| 5 | FP-12 | Role, Agent and Swarm Factory (roles as data), Lifecycle and Authorization (was FP-01, part A) | 1 | B | FP-01, FP-02 | Medium | High |
| 6 | FP-11 | Human Approval Gates (lite in Tier 1, durable in Tier 2) | 1 | B | FP-02 | Medium | High |
| 7 | FP-13 | Task-Driven Swarm Generation (was FP-01, part B) | 1 | B | FP-00, FP-02, FP-11, FP-12 | Medium | High |
| 8 | FP-14 | Embedding Layer, Vector Store and Hybrid Retrieval (was FP-02, part A) | 2 | C | FP-00, FP-02 | High | High |
| 9 | FP-06 | Cache-Stable Prompt Layout and Final-Turn Experiments (was FP-04 pillar 5) | 2 | C | FP-00 | Medium | Low |
| 10 | FP-05 | Tool-Ingestion Cleaning (was FP-04 pillar 6) | 2 | C | FP-00, FP-02 | Medium | Medium |
| 11 | FP-08 | Typed Handoffs (was FP-04 pillar 1) | 2 | C | FP-00, FP-02 | Medium | Medium |
| 12 | FP-04 | Per-Role Model Routing and Provider Policy (was Candidate B) | 2 | C | FP-00, FP-03 | Low-Medium | Medium |
| 13 | FP-10 | Durable Mission State and Resume | 2 | C | FP-00 | High | High |
| 14 | FP-07 | Call-by-Reference Scratchpad (was FP-04 pillar 2) | 3 | D | FP-02, FP-06 | Medium | High |
| 15 | FP-09 | Restorable Context Compaction (was FP-04 pillar 3) | 3 | D | FP-06, FP-07 | Medium | Medium |
| 16 | FP-15 | Exemplar Memory (was FP-02, part B) | 3 | D | FP-14, FP-02, FP-11 | Medium | High |
| 17 | FP-16 | User Profiles and Preference Memory (was FP-02, part C) | 3 | D | FP-02 | Low-Medium | Medium |
| 18 | FP-17 | Feedback Log (was the "RLHF dataset logger") | 3 | D | FP-00 | Low-Medium | High |
| 19 | FP-18 | Skill Distiller (was FP-03) | 3 | D | FP-10, FP-11, FP-14, FP-15 | High | Critical |
| 20 | FP-19 | Prompt Optimization Loop | 3 | D | FP-00, FP-15 | High | Medium |

**Deferred**: D-1 per-role decoding profiles (experimental only), D-2 multi-turn web chat and session replay (after FP-10), D-3 interactive swarm debugger (after FP-10 and FP-11). **Rejected**: D-4 cross-tenant or global skill promotion (section 7).

**Where to start.** FP-00 stage 1 and 2 (usage ledger and a 10-task smoke evaluation across at least 3 domains), then FP-01. Everything after that is chosen by what the measurements show.

**Phase intent**

- **Phase A, foundation (Tier 1)**: measurement, real code isolation, the core injection defenses, and cost metering. Everything else depends on these.
- **Phase B, generic swarm (Tier 1)**: roles as data, the factory, a lightweight approval gate, and the task-driven planner. This is the product's central capability.
- **Phase C, retrieval and efficiency (Tier 2)**: retrieval and the cost and latency levers, each independently measurable and ordered so measurements stay uncontaminated.
- **Phase D, memory and self-improvement (Tier 3)**: the highest-risk work, started only after everything that constrains it exists and only when a need is shown.

Security gates between phases are in section 8.6. A phase does not start until its gate passes.

---

## 5. Measurement framework

### 5.1 Principles

1. Measure before changing. Every optimization is compared to a frozen baseline.
2. Change one factor per experiment. Combined effects are measured only after each factor is measured alone.
3. Use paired comparisons: the same tasks, the same seeds where supported, the same model versions.
4. Report uncertainty. A point estimate without a confidence interval is not a result.
5. Optimize cost per **successful** mission, never raw token count alone. A cheaper wrong answer is not an improvement.
6. Separate harness tests (mock LLM, deterministic, run on every commit) from model-quality evaluations (real model, stochastic, run on a schedule and on demand).

### 5.2 Metric catalog

**Tokens and cost**

| Metric | Definition | Source |
| :-- | :-- | :-- |
| `prompt_tokens`, `completion_tokens` | Per call, step, role, mission | Provider usage fields (Ollama: `prompt_eval_count`, `eval_count`). Never estimate with a tokenizer when exact counts exist. |
| `cached_prompt_tokens`, `cache_hit_ratio` | `cached_prompt_tokens / prompt_tokens` | Provider usage where reported. Where not reported (e.g. local engines), use prompt-evaluation time and `prompt_eval_count` as a proxy and label it as a proxy. |
| `cumulative_prompt_tokens` | Sum of prompt tokens over all calls in a mission | Usage ledger |
| `peak_context_tokens` | Largest single-call prompt | Usage ledger |
| `overhead_share` | Share of prompt made of non-task content (system prompt, tool schemas, skills, exemplars, memory) | Prompt builder instrumentation |
| `cost_usd`, `cost_per_success` | Priced tokens; total cost divided by successful missions | Versioned price table |

Per-call context size grows linearly with steps because each step appends content. Cumulative prompt tokens grow quadratically unless the context is bounded or cached. Report both curves against step number.

**Latency**

| Metric | Definition |
| :-- | :-- |
| `mission_latency` p50 / p95 / p99 | End to end, warm runs; cold runs reported separately |
| `ttft` | Time to first token |
| `llm_call_latency`, `tool_latency`, `queue_wait` | Per call, per tool, per scheduler queue |
| `background_interference` | Foreground p95 with versus without a background job running on the same inference resource |

**Quality**

| Metric | Definition |
| :-- | :-- |
| `task_success_rate` | Fraction passing a programmatic checker. Preferred over any model-graded metric. |
| `rubric_score` | Judge-graded, calibrated per section 5.5 |
| `faithfulness` | Claim-level: fraction of output claims supported by tool results |
| `tool_call_validity` | Schema-valid and executable calls divided by all calls |
| `redundant_call_rate` | Calls with identical tool and arguments to a prior call in the mission, divided by all calls |
| `steps_to_completion`, `stall_rate` | Steps used; missions that hit a step cap or loop |
| `handoff_parse_failure_rate` | Handoffs failing schema validation on first attempt |

**Retrieval**

`recall@k`, `MRR`, `nDCG@10` on a labeled query set; retrieval latency p95 versus partition size; index build time; bytes per vector; `cross_tenant_leak_count` (must be 0, with the number of probe queries stated).

**Reliability**

Error rate by class, retry rate, circuit-breaker trips, budget trips, crash-resume success rate, duplicate side-effect count.

**Safety**

Attack success rate (ASR) per attack category with n and a 95% interval; benign block rate (false positives); sandbox escape count against a named vector suite; secret and PII leakage count in outputs and stores.

**Operations**

Throughput (missions per minute at concurrency c), saturation point, CPU and memory per mission, database growth per mission, queue depth.

### 5.3 Evaluation tiers and experimental protocol

| Tier | Purpose | Tasks | Repeats | When | Typical size |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Smoke | Catch breakage and gross regressions | 10 (at least one per category, at least 3 domains) | 1 | During development, daily, on a local model | 10 to 20 missions |
| Standard | Decide whether a feature ships | 30 to 50 | 3 | At each feature gate, treatment only (baseline reused) | about 90 to 150 missions per treatment |
| Full | Phase gates and any claim made publicly | 100 or more | 3 | At phase gates | about 300 or more missions per treatment |

- **Cost control.** Missions per run equal tasks × repeats × new conditions. Baseline results are stored by config hash and reused until the model, prompt, or tool set changes, so only the treatment is re-run. Iterate on a local model; use cloud models only for gate runs, under a spend cap set before the run. Pull-request CI runs only the mock-LLM harness tests, never model evaluations.
- **Statistics (Standard and Full).** Report mean, median, standard deviation, and a bootstrap 95% confidence interval over tasks. Use a paired bootstrap for continuous metrics and McNemar's test for paired binary outcomes. Smoke runs report raw pass counts and support no inference.
- **Power.** With about 40 paired tasks only large success-rate differences (roughly 15 to 20 percentage points) are detectable, and about 100 tasks detect roughly 10 points. Below the detectable effect, claim nothing. Token counts, cache hit ratio, and latency are far less noisy than success rate and need fewer repeats, so most efficiency features can be decided at the Standard tier.
- **Pin and record**: model name and date-stamped version, provider, temperature and sampling settings, prompt hash, config hash, git SHA, library versions, price table version, and for local inference the GPU, quantization, context size, and engine version.
- **Latency claims**: at least 30 warm runs per condition, first run discarded, one condition at a time on idle hardware. Cold start is reported separately. Short or mocked tasks are acceptable for isolating overhead.
- **Mock-LLM runs** verify plumbing only and never appear in quality or cost claims.

### 5.4 Evaluation set requirements

- **Categories** (each domain-neutral; at least 3 tasks each at the Smoke tier, 8 at Standard): tool-use correctness, multi-step research with synthesis, data analysis, long-context retrieval, faithful summarization, refusal and policy compliance, adversarial (prompt injection, exfiltration), and multi-agent handoff.
- **Domain packs**: tasks come from at least 3 distinct domains (for example research and synthesis, data analysis, document review). Software tasks are one pack among several, never the default. At least one **held-out domain** is reserved for generalization checks and is never used for tuning. Results are reported per pack and as a macro-average, and a feature must not regress any pack beyond its margin.
- Each task has an ID, category, pack, prompt, allowed tools, checker type (programmatic, string or schema match, judge, or human), expected result, difficulty tag, and a `dev` or `test` split.
- The `test` split is frozen. No prompt, skill, exemplar, or parameter is tuned on it.
- **Memory-write isolation**: evaluation runs execute in a dedicated tenant with learned-memory writes disabled, or against a frozen memory snapshot. Otherwise exemplars and skills created from test tasks leak into later test runs and inflate scores.
- The set is versioned. Growth from production feedback is allowed only after scrubbing, consent, and review (FP-17).

### 5.5 Judge protocol (for any model-graded metric)

Prefer programmatic checkers wherever a task allows one. A judge is introduced only when a metric cannot be checked programmatically, and it starts as advisory.

1. Use a judge from a different model family than the generator, temperature 0, a written rubric, and randomized order for pairwise comparisons.
2. **Stage 1 calibration**: 30 to 50 human-labeled outputs. Report agreement as Cohen's kappa for categorical scores or Spearman correlation for ordinal scores. Target (hypothesis): kappa of at least 0.6. A judge below the target stays advisory and never decides a gate.
3. **Stage 2**: expand to about 100 labeled outputs only if the judge becomes a gating metric.
4. Run the judge 3 times per item and report the variance.
5. Treat the output under evaluation as untrusted input to the judge. Delimit it and instruct the judge to ignore instructions inside it. Include injection cases in the calibration set.
6. Re-calibrate whenever the judge model, rubric, or generator changes materially.

### 5.6 Decision rule and report template

Before running, record in the feature note (a short paragraph is enough): hypothesis, primary metric, guardrail metrics with tolerance margins, sample size, minimum detectable effect, and the ship rule.

**Ship rule**: ship only if the primary metric improves with a confidence interval excluding zero **and** no guardrail regresses beyond its pre-set margin. Negative and null results are committed and linked from the feature entry.

Each report contains: config hash, n tasks, k repeats, conditions, a metrics table with intervals, per-category breakdown, failure analysis (which tasks regressed and why), cost of the run, and the decision.

### 5.7 Known measurement hazards

Nondeterminism (even at temperature 0, batching can change outputs); provider-side model drift (pin versions and re-run baselines on a schedule); cache warm-up effects; shared-GPU contention; train and test contamination; Goodhart effects when a metric becomes a target; tiny samples; judge bias and judge injection; outliers from provider incidents (log and exclude by a pre-written rule, not by hand).

---

## 6. Feature specifications

Thresholds below are **proposed gates** and are revised once the baseline exists. Security details for each feature are in section 8.3 under the matching ID.

### FP-00: Evaluation, Telemetry and Measurement Foundation

- **Problem**: no feature in this plan can be justified, and no existing claim can be trusted, without reproducible measurement.
- **Staged scope.** Build in this order. Each stage is useful on its own.
  1. **Telemetry and usage ledger**: per-call tokens (in, out, cached), latency, and cost; mission and step IDs; OpenTelemetry spans for mission, agent step, LLM call, and tool call. No evaluation yet.
  2. **Smoke tier**: 10 tasks across at least 3 domain packs with programmatic checkers, results stored by config hash, and a committed `baseline-v0`.
  3. **Standard tier**: 30 to 50 tasks, repeats, a report generator with confidence intervals and paired comparison, spend caps, and baseline reuse.
  4. **Judge calibration**, only when a metric needs it (section 5.5).
  5. **Red-team category** and the **held-out domain**.
  The Full tier is the same machinery run on a larger set at phase gates.
- **Metrics (of the harness itself)**: run-to-run variance of an identical configuration (confidence-interval width on pass rate), harness overhead per call, cost per run at each tier, category and pack coverage, judge kappa once a judge exists.
- **Gate**: stage 2 is done when `baseline-v0` is committed and re-running the identical configuration reproduces the metrics within noise. Stage 3 is done when two identical Standard runs agree within their intervals.

### FP-01: Hardened Code Execution

- **Problem**: the in-process socket guard executes untrusted code in the same interpreter namespace as the guard. Two bypasses were reproduced against the previous implementation (restoring the saved original `connect`, and spawning a child interpreter). It is a guardrail, not a sandbox.
- **Design**: per-run ephemeral container using rootless Podman where possible. No network by default. Read-only root filesystem, `tmpfs` workspace, non-root user, all Linux capabilities dropped, `no-new-privileges`, default seccomp profile, CPU, memory, PID, and wall-clock limits, scrubbed environment, image pinned by digest. Files are copied in and out, never bind-mounted from tenant storage. The Docker or Podman socket is never exposed to the application process. If isolation is unavailable the tool is disabled (fail closed). Optional allow-listed egress proxy when a task explicitly needs the network. The in-process guard is retained only as a second layer.
- **Metrics**: escape-suite pass count against a named vector list (restore-original-connect, child interpreter, raw socket, `subprocess` clients, decimal/hex/octal/IPv4-mapped-IPv6 loopback, DNS rebinding, symlink and path traversal on copy-out, fork bomb, memory bomb, disk fill, infinite loop); time to terminate runaway runs; cold-start overhead p50 and p95 versus in-process; concurrent-run throughput; behavior when the runtime is absent.
- **Gate**: every enumerated vector blocked (state the count; this is not a proof of safety); overhead documented; fail-closed verified.
- **Rollback**: `sandbox.mode = container | disabled`. The production profile never allows an in-process mode.

### FP-02: Untrusted-Content Boundary and Prompt-Injection Defense

- **Problem**: any content the agent reads (web pages, files, search results, tool errors, retrieved memory) can carry instructions. Prompt wording alone does not stop them.
- **Design**: (1) every context item carries a trust label (`system`, `operator`, `user`, `tool_untrusted`, `memory_candidate`); (2) untrusted content is delimited and the system prompt states the instruction hierarchy, treated as defense in depth only; (3) capability-based tool permissions per role under least privilege; (4) **taint tracking**: once untrusted content enters a mission, high-risk tools (write, execute, egress, cross-swarm send) are blocked or require approval (FP-11); (5) optional reader/actor split, where the agent that reads untrusted content has no write or egress tools; (6) fetch allow- and deny-lists; (7) normalize and strip invisible and control characters, including Unicode tag characters and zero-width text, and hidden HTML text before content reaches the model; (8) the UI renders model output as inert text, with no automatic remote image or link loading; (9) egress DLP and canary tokens to detect system-prompt leakage.
- **Tier-1 subset**: build items (1), (2), (3), (4), (7), and (8) first. The reader/actor split (5), allow-lists beyond the basics (6), and canaries (9) follow when red-team results show they are needed. Until FP-11 exists, taint tracking blocks high-risk tools outright; afterwards it relaxes to approval-gated.
- **Metrics**: ASR per category (direct injection, indirect via web page, via file, via tool error, via retrieved memory, markdown-image exfiltration, multi-tool pivot), at least 10 trials per category at the Standard tier and 30 at the Full tier, before and after, with intervals; benign task success (non-inferiority); benign block rate; added tokens and latency per mission.
- **Gate**: ASR reduced with an interval excluding no change; zero successful exfiltration across the enumerated cases (state the count); benign success non-inferior within the pre-set margin.
- **Honesty note**: no defense is complete. A nonzero ASR is expected and is reported. Residual risk is recorded in section 8.7.

### FP-03: Cost Governance and Metering

- **Design**: append-only usage ledger; per-tenant, per-mission, and per-day budgets; token-bucket rate limits and concurrency caps; a hard `max_tokens` on every call so overshoot is bounded by one call; versioned price table; per-tenant provider allow-list; alerts at budget thresholds. A shared store (Redis) is introduced only when running more than one process.
- **Tier-1 subset**: the usage ledger, per-tenant and per-mission budgets, a hard `max_tokens`, and a single-process rate limiter. Invoice reconciliation, per-tenant provider allow-lists (with FP-04), and the shared Redis store come later.
- **Metrics**: budget overshoot (percent over cap, bounded by one call); reconciliation of metered cost against provider invoices; limiter correctness tests (concurrent missions cannot exceed a cap); enforcement overhead p95; noisy-neighbor test (one tenant's flood versus another's p95 latency).
- **Gate**: reconciliation within the proposed 2% tolerance; no cap bypass under a concurrency test; noisy-neighbor degradation within the pre-set margin.

### FP-04: Per-Role Model Routing and Provider Policy

- **Design**: model and provider configurable per role; ordered fallback chain; a policy router that enforces which providers each tenant may use (data residency and contractual limits); static routing rules first, no learned router.
- **Metrics**: for each (role, model) pair on the evaluation set: success rate, cost per success, latency p95, tool-call validity; fallback rate; **policy violations must be 0**, verified by tests that attempt to route restricted tenants to disallowed providers.
- **Gate**: the selected configuration either dominates the baseline on cost and latency with non-inferior quality, or meets a stated cost target with non-inferior quality.

### FP-05: Tool-Ingestion Cleaning

- **Design**: `fetch_url` converts HTML to clean text or markdown using a maintained library, strips scripts, styles, navigation, and hidden text; `run_python` returns bounded summaries (row and column caps, shape, head) while the full output is kept for retrieval via FP-07. Cleaning never hides errors.
- **Metrics**: tokens per fetch (raw versus cleaned); answer-span retention rate on labeled pages; downstream task success on web tasks (cleaned versus raw); cleaning latency; failure rate on script-rendered pages and malformed HTML.
- **Gate (proposed)**: at least 30% fewer tokens per fetch with task success non-inferior.

### FP-06: Cache-Stable Prompt Layout

- **Design**: three tiers: an immutable prefix (system persona, tool schemas, stable policy), a semi-static body (approved role practices), and a dynamic tail (memory, inbox, timestamps). Deterministic serialization (sorted keys, stable tool order, no timestamps or random IDs in the prefix). Explicit cache breakpoints where a provider requires them. Tools are never added or removed mid-mission.
- **Metrics**: `prefix_stable_rate` (hash of the prefix is identical across steps; verified by test); `cache_hit_ratio`; TTFT; billed prompt tokens. Report per provider and engine, because behavior differs by minimum prefix length, time to live, and breakpoint rules.
- **Final-turn tool handling (experiment)**: compare (a) the baseline, (b) tools kept in the prompt with calls forbidden through the provider's mechanism (such as `tool_choice` none or constrained decoding), and (c) tools removed on the final synthesis turn. Removing the schema saves its tokens on that turn but changes the rendered prompt, and whether the saving beats the lost cache reuse depends on schema size, context length, the model's template, and the engine. Some models also behave differently when a schema disappears mid-conversation. Measure net billed tokens, TTFT, synthesis quality, and the rate of hallucinated tool calls per provider, then set the default per provider from the result. Enable (c) only where it measurably wins.
- **Claim limit**: cache hit rate cannot be guaranteed. Only measured rates are stated.
- **Gate (proposed)**: prefix stability 100% by construction and verified; measurable hit-ratio or TTFT improvement on at least one target provider or engine.

### FP-07: Call-by-Reference Scratchpad

- **Design**: payloads above a size threshold are stored once and passed as references. A reference is an opaque random ID (at least 128 bits), tenant-scoped, with a content hash, a size cap, and a time to live. The ID is never mapped to a filesystem path. Authorization is checked on every dereference. The summary attached to a pointer is deterministic by default (head, size, structure); a model-written summary is optional and its cost is measured. A reference to untrusted content stays untrusted.
- **Metrics**: tokens saved per handoff; dereference rate (if recipients dereference most references, pointer passing saves little); task success non-inferiority; dereference latency; storage growth; time-to-live cleanup correctness.
- **Gate (proposed)**: net token reduction with non-inferior success; cross-tenant and guessed-ID dereference attempts all denied (state the count).

### FP-08: Typed Handoffs

- **Replaces** the previous free-form "M2M YAML" protocol.
- **Design**: each handoff type has a versioned JSON Schema. Agents emit handoffs through the provider's structured-output or tool-call mechanism, validated on receipt, with a bounded repair retry. Prose is used only at the user boundary. Field contents remain untrusted. The UI renders fields as escaped text. Compact JSON with short keys is the default wire format, and YAML through a safe loader is acceptable. The format is chosen by the ablation below, not assumed.
- **Metrics**: first-attempt schema-valid rate; repair rate; handoff tokens versus a natural-language baseline; downstream task success (non-inferiority); ablation of terse versus descriptive field names, since a more compact format can reduce model accuracy.
- **Gate (proposed)**: schema-valid rate at or above the pre-set target with non-inferior success. Any token-reduction figure is reported from measurement. The earlier 65% to 75% figures were guesses.

### FP-09: Restorable Context Compaction

- **Replaces** unconditional in-place observation rewriting. Rewriting earlier context changes the prefix FP-06 protects, so compaction is batched and measured.
- **Design**: context is append-only by default. When the prompt exceeds a configured fraction of the window, old observations are replaced in a **batch** by a digest plus a restorable reference (FP-07). One cache miss is accepted per compaction and measured. System, policy, approval, and provenance content is never compacted. Final-turn tool handling is an experiment under FP-06.
- **Metrics**: cumulative prompt tokens, peak context, cache hit ratio change, success rate, restore frequency, and net billed cost, comparing four strategies: none, truncation, digest, and summarization.
- **Gate (proposed)**: lower net cost per success than the best alternative, with non-inferior quality. If compaction hurts quality on long tasks, it ships off by default.

### FP-10: Durable Mission State and Resume

- **Design**: checkpoint after every step (messages, tool results, budget, inbox state), tenant-scoped and integrity-protected. Resume or fail after a crash. Tools declare whether they have side effects; side-effecting tools use idempotency keys. Resume re-checks authorization (key still valid, tenant still enabled). Missions are cancellable.
- **Metrics**: resume success rate under fault injection (kill at every step, at least 3 runs per step); duplicate side-effect count (must be 0 for flagged tools); checkpoint overhead (latency and bytes per step); recovery time.
- **Gate**: zero duplicate side effects across the injection suite; overhead documented.

### FP-11: Human Approval Gates

- **Design**: a policy maps tool risk classes to `auto`, `approve`, or `deny`. A gated step pauses, persists (FP-10), and notifies the approver. The approval binds the exact action and a hash of its arguments, expires, and is single use. The approver sees the raw arguments, not the agent's description. Timeout means deny. Approvals and denials are audit-logged with the approver identity.
- **Lite version (Tier 1)**: a single-process gate. The mission thread blocks on an approval request, a timeout means deny, the approval is bound to the action and argument hash, and an audit entry is written. It does not survive a restart. The durable version (pause, persist, resume across restarts) arrives with FP-10 in Tier 2.
- **Metrics**: approval latency; share of steps gated; denial rate; bypass-test pass count (changed arguments after approval, replayed approval, expired approval, wrong approver); behavior on timeout.
- **Gate**: all bypass tests blocked (state the count).

### FP-12: Role, Agent and Swarm Factory (roles as data), Lifecycle and Authorization

- **Problem**: the runtime must serve any kind of task. A fixed set of roles or swarm types would make it a software-development tool in practice.
- **Design**
  - `RoleSpec` is data: name, purpose, instructions, capability list, tool list, model policy, skill tags, limits. No role name appears in core code. Existing built-in roles move to an optional starter library used by tests and examples.
  - A capability and tool registry declares `risk_class`, `side_effects`, and `data_classification` for every entry. New tools register without changing the core.
  - A topology registry (orchestrator-worker, peer network, pipeline, review loop) replaces procedural branching. No topology is the default for every task.
  - `AgentFactory` and `SwarmFactory` instantiate from specs, inject tenant workspace namespaces, and resolve capabilities against the registry.
  - A versioned `SwarmSpec` (roles, topology, budgets, policy references) is validated against allow-lists, with bounds on roles, steps, and budget. A role is ephemeral (one mission) or saved to the tenant's role library after approval.
  - Domain packs are loadable bundles: role templates, tools, policy pack, skill seeds, evaluation tasks.
  - Create, read, update, and delete with optimistic concurrency. Editing a running swarm is rejected or applied to a snapshot. Authorization comes from the credential: a tenant principal acts only on its own resources, an admin principal is scoped and audit-logged. Cross-tenant access returns 404, not 403. Client-supplied owner, tenant, or capability fields are ignored (no mass assignment). Schema versioning with forward migrations applies, because clients cannot absorb breaking changes.
  - A shared structured-output helper (JSON Schema validation with a bounded repair retry) used by FP-13 and FP-08.
- **Metrics**: authorization matrix (principal × operation × ownership) with test count and 100% expected result; spec-validation rejection tests; **conformance checks** (no domain role names in `core/`, a mission completes with arbitrary synthetic roles, the same engine runs at least 3 domain packs); concurrent-edit handling; migration success on snapshots of real databases; CRUD latency p95; role instantiation overhead.
- **Gate**: full matrix passes; conformance checks green; migrations tested forward and in reverse on snapshots.

### FP-13: Task-Driven Swarm Generation

- **Problem**: choose roles, tools, topology, and budget from the task description, for any domain, instead of from a fixed template.
- **Design**
  - The planner returns a `SwarmSpec` through structured output. Its prompt is domain-agnostic: decompose the task into the functions it needs, map them to registered capabilities, choose a topology from the registry, and set a budget. It may retrieve similar past specs or domain-pack templates (once FP-14 exists), but it must work from scratch when none match.
  - A deterministic validator, not a model, enforces the allow-lists and a **privilege ceiling**: a generated spec cannot request capabilities beyond the requester's credential. Dual-use or regulated capabilities are available only when an operator-authorized policy pack is present, and risky capabilities require human approval (FP-11) before the first run.
  - Roles are ephemeral by default. Promotion to the tenant library requires approval. In interactive use a dry-run preview shows roles, tools, topology, and budget before the first run.
  - Planning has its own token and step budget. The mission text is untrusted input to a privileged component and is treated accordingly.
- **Metrics**
  - Validity: first-attempt spec validity; human acceptance without edits; edit distance between generated and accepted specs; planning tokens, cost, and latency.
  - Safety: over-privilege rate (capabilities requested beyond what a labeled mission needs).
  - **Generality**: success of generated swarms versus a hand-built baseline, reported **per domain pack and on the held-out domain**; role collapse rate (how often the planner produces the same few roles regardless of the task, measured as the distribution of role and capability sets across domains); minimality (unused capabilities per role); cost of generated versus hand-built swarms.
- **Gate**: generated swarms non-inferior to the hand-built baseline on each pack and on the held-out domain within the pre-set margin; every over-privilege attempt blocked by the validator (state the count); role collapse below the pre-set threshold.

### FP-14: Embedding Layer, Vector Store and Hybrid Retrieval

- **Design**
  - `EmbeddingProvider` protocol. The default is a locally hosted embedding model so client text does not leave the deployment. A cloud embedding provider is opt-in per tenant with explicit consent. The model is a pinned configuration value. The previously named `text-embedding-004` was shut down on January 14, 2026 and must not be referenced. Embedding models are deprecated on provider schedules, so a deprecation watch and a re-embed job are part of this feature.
  - Every vector row stores `embedding_model`, `embedding_version`, `dimension`, `normalized`, `content_hash`, `created_at`, and `expires_at`. Similarity is computed only between vectors of the same model and version.
  - The hash-based embedding is test-only, tagged as such, and never used as a production fallback. The production fallback when embeddings fail is BM25-only retrieval.
  - Vectors are pre-normalized. Partition size is capped. Brute-force cosine in SQLite is the baseline; a PostgreSQL `pgvector` adapter with an approximate index is introduced at a threshold determined by the latency benchmark.
  - Hybrid retrieval defaults to **reciprocal rank fusion** (constant 60). A weighted dense and sparse blend is used only if its weight is tuned on a labeled set. The earlier default of 0.7 was arbitrary.
  - Every query is tenant-scoped in code. Retrieval indexes, caches, and BM25 statistics are all partitioned by tenant.
- **Metrics**: `recall@k`, `MRR`, `nDCG@10` on a labeled set (at least 30 queries for a first pass and 50 or more at the Full tier, plus a negative set); retrieval p95 at 1k, 10k, and 100k vectors per partition; index build time; bytes per vector; embedding latency and cost; BM25-only fallback quality drop; re-embed job correctness; cross-tenant leak probes (state the count).
- **Gate**: retrieval improves recall or MRR over BM25 alone with a confidence interval excluding zero, or the dense component stays off; zero cross-tenant results across the probe set.

### FP-15: Exemplar Memory (dynamic few-shot)

- **Design**: exemplars are stored with provenance (mission ID, trace hash, trust level of any untrusted content that influenced them) and a status of `candidate`, `approved`, or `disabled`. Only `approved` exemplars are retrievable. At most k exemplars are injected, within a fixed token budget, delimited as untrusted examples with an instruction not to follow directions inside them. Scope policy is explicit: exemplars are retrievable only within the tenant, and within the same user unless the tenant opts in to sharing. Approval is by the tenant administrator after an evaluation check.
- **Metrics**: success-rate lift versus no exemplars on held-out tasks, excluding every task used to create an exemplar; ablation over k in {0, 1, 2, 4}; prompt-token overhead; exemplar hit rate; harm rate (tasks where injection lowers success); staleness.
- **Gate**: lift with an interval excluding zero, or it ships off.

### FP-16: User Profiles and Preference Memory

- **Design**: validated key and value schema with length caps; values are escaped and delimited when injected. Constraints that must hold (for example a compliance requirement) are enforced in code through policy, with the prompt line serving only as guidance. Data minimization: no field is stored that is not used.
- **Metrics**: preference adherence rate (programmatic checks where possible); token overhead; cross-user and cross-tenant leak probes (state the count); deletion completeness.
- **Gate**: leak probes pass; adherence improves over no-profile baseline.

### FP-17: Feedback Log

- **Replaces** the "RLHF dataset logger". Single-output thumbs up or down is a pointwise label, not a preference pair. Direct Preference Optimization needs chosen and rejected outputs for the same prompt, and RLHF needs a reward model and a reinforcement-learning stage. No training is planned, so the claims are removed.
- **Design**: store pointwise labels and critiques. Create preference pairs only when two outputs for the same prompt exist (regenerate or A/B). Each record carries a consent flag and purpose: evaluation and regression by default; any model training requires explicit contractual consent. Scrub PII before storage. Retention and erasure apply to all derived datasets.
- **Metrics**: feedback coverage rate; agreement between user labels and the calibrated judge; PII found by scanning stored records (count, must be 0 after scrubbing, with scanner recall reported); provenance completeness.
- **Gate**: scan clean; consent and retention enforced in tests.

### FP-18: Skill Distiller

- **Problem and rationale**: asking worker agents to self-report skills mid-task splits their attention and adds schema tokens. A separate post-mission observer is a sound design and matches published work on skill libraries and experiential memory. It is also the riskiest feature in this plan, because it writes persistent instructions derived from untrusted content.
- **Design**
  - Runs after the mission, in the background, with its own low-privilege credentials and a hard token budget. It uses low-priority or idle-time scheduling, because background work consumes tokens and contends for the inference device. It is not zero-cost.
  - **Success signal**: it learns only from missions with an explicit signal (programmatic checker pass, user confirmation, or a calibrated judge above a threshold). No signal, no learning.
  - **Skills are keyed by capability and task-tag signature, not by role name.** Roles are dynamic, so a skill declares the capabilities it requires and the task tags it applies to (a controlled vocabulary plus free-text tags that are never used for authorization). At role instantiation, a skill is injected only if the role holds every required capability and the task matches the tags. A skill for one capability set is never injected into a role that lacks it.
  - Output is schema-constrained. Skills start as `candidate` with provenance, expiry, version, and supersession links. A skill is rejected if it contains imperative text addressed to the system, capability or tool names outside its declared signature, URLs, or content flagged by injection heuristics.
  - **Promotion pipeline**: candidate, then an evaluation gate (A/B on held-out tasks from the relevant domain packs), then tenant-administrator approval, then `active`. Limits on active skills and tokens per role. Deduplication and contradiction checks. A kill switch and one-step rollback.
  - Skills cannot grant or alter tool permissions. Cross-tenant or global promotion is not part of this feature.
- **Metrics**: candidate yield; approval rate; post-activation success lift on held-out tasks (excluding tasks the skill was derived from), reported per domain pack; harm and rollback count; foreground p95 latency with and without the distiller running; cost per candidate; duplicate and contradiction rate; **planted-injection test** (traces containing planted instructions must never produce an active skill; state the number of planted cases); **signature-leak test** (a skill is never injected into a role lacking its required capabilities); per-role skill token overhead; staleness.
- **Gate**: planted-injection and signature-leak suites blocked in full; measured lift with an interval excluding zero; foreground latency impact within the pre-set margin.

### FP-19: Prompt Optimization Loop

- **Replaces** "ML-powered prompt optimization". What the previous plan described was exemplar retrieval. Prompt optimization proper has a metric, a dev split, and a search procedure.
- **Design**: use an established approach (for example DSPy-style or reflective evolutionary search) over the `dev` split with a metric from FP-00. Candidate prompts are artifacts with versions. They are evaluated once on the frozen `test` split and promoted only through the approval pipeline.
- **Metrics**: dev-to-test generalization gap; lift with interval on `test`; cost of the optimization run; regression on other roles and categories; judge-gaming checks, such as comparing judge scores to programmatic outcomes.
- **Gate**: lift on `test` with an interval excluding zero and no regression beyond margins.

---

## 7. Deferred and rejected

- **D-1 Per-role decoding profiles (deferred, experimental).** Setting temperature and top-p together is redundant, and several current models restrict or ignore sampling parameters. Introduce only as a capability-aware option behind a flag, with an ablation showing a measurable effect on the evaluation set. Greedy settings do not guarantee deterministic outputs.
- **D-2 Multi-turn web chat and session replay (deferred).** Requires FP-10 for durable state. Replay needs the same tenant scoping and redaction as live traces.
- **D-3 Interactive swarm debugger (deferred).** Requires FP-10 and FP-11. Injecting steering prompts into a running agent is a privileged action and must be authorized and audited.
- **D-4 Cross-tenant or global skill and exemplar promotion (rejected for v1).** A regex scrubber is not sufficient protection for client data. Reconsider only with explicit contractual consent and a reviewed anonymization method.
- **Removed claims**: RLHF and DPO dataset generation; "mathematically grounded" framing; relevance scoring by embedding cosine between prompt and answer; "guarantees 80%+ cache hits"; "zero quality loss at 100% task completion"; "0 ms latency overhead"; unconditional removal of tool schemas on the final turn (retained only as a measured experiment in FP-06); in-place observation rewriting as a default; free-form, unvalidated M2M YAML.

---

## 8. Security audit

### 8.1 Scope and method

Every proposal in section 6 was reviewed for threats, required controls, and verification tests. Severity reflects impact if the feature were shipped without the listed controls.

- **Critical**: remote code execution, cross-tenant data exposure, or persistent compromise of agent behavior.
- **High**: data exfiltration, privilege escalation, or integrity loss with a plausible attack path.
- **Medium**: limited disclosure, denial of service, or abuse requiring unusual conditions.
- **Low**: hardening items.

A feature with an open Critical or High finding is not enabled by default.

**Proportionality.** Tier 1 features receive the full review in this section before they are enabled. Tier 2 features receive their listed checks before they are enabled by default. Tier 3 features are reviewed in full when they are built, and not before. Section 8.4 is a checklist to apply when touching the relevant area, not a per-pull-request requirement.

### 8.2 Threat model summary

- **Assets**: tenant data and traces; provider and API keys; learned memory (exemplars, skills, prompts); vectors and indexes; feedback datasets; checkpoints; audit logs.
- **Actors**: a malicious tenant user; an author of content the agent reads (indirect injection); a compromised or manipulated model output; a malicious or careless administrator; a network attacker; a supply-chain attacker (dependencies, base images, model files); a curious co-tenant on shared infrastructure.
- **Trust boundaries**: client and API; API and agent runtime; runtime and LLM provider; runtime and tools or sandbox; runtime and storage; runtime and the open web.
- **Assumptions to verify, not to rely on**: the host is patched; secrets are supplied only through the environment or a secret store; the inference provider's retention and training terms are known and acceptable to each client.

### 8.3 Findings by proposal

**FP-00 Evaluation (Medium).** Evaluation data may contain client content; the judge can be prompt-injected by the output it grades; evaluation runs hold real provider keys and can overspend; stored prompts may contain PII; test contamination silently invalidates conclusions.
Check and control: scrub evaluation inputs; run the judge with delimited untrusted input and injection cases in calibration; enforce spend caps; run evaluation tasks with tools inside the sandbox; isolate evaluation tenants and disable memory writes; restrict access to result stores.

**FP-01 Sandbox (Critical).** Container escape, exposed container socket (root-equivalent), symlink and path traversal on copy-in and copy-out, archive extraction flaws, resource exhaustion, secrets inherited through the environment, DNS-based egress, supply chain of the base image.
Check and control: rootless runtime, no socket exposure, capability drop, seccomp, read-only root, no bind mounts of tenant storage, safe extraction that rejects absolute paths and `..`, size and count caps on outputs, scrubbed environment, digest-pinned image with scanning, fail closed. Verify with the escape suite in 6/FP-01.

**FP-02 Injection defense (Critical).** Indirect injection leading to tool misuse and exfiltration; markdown-image and link exfiltration through the UI; hidden and invisible-character instructions; encoding tricks (base64, homoglyphs); multi-tool pivots after reading untrusted content.
Check and control: taint tracking with blocked or approval-gated high-risk tools; capability allow-lists; inert rendering in the UI; invisible-character normalization; DLP and canaries; red-team regression suite with ASR reported. Accept and document that residual risk remains.

**FP-03 Cost governance (Medium to High).** Cost-based denial of service through many missions or long loops; race conditions in budget counters; bypass through parallel missions; ledger tampering; unfair resource use between tenants.
Check and control: atomic counters, caps enforced before each call, hard `max_tokens`, per-key rate limits, append-only ledger with integrity checks, noisy-neighbor tests, alerts.

**FP-04 Routing (Medium).** Data sent to a provider a tenant has not approved (including through fallback); credential mix-ups between providers; provider-specific tool-call parsing edge cases.
Check and control: policy router enforced in code with tests that attempt violations; provider credentials scoped per provider; fallback respects the tenant allow-list; provider data-handling terms recorded per tenant.

**FP-05 Tool-ingestion cleaning (High).** Hidden text and comments carrying instructions survive parsing; parser vulnerabilities and pathological input (ReDoS, decompression bombs, deeply nested markup); content-type confusion; encoding issues.
Check and control: strip hidden elements and invisible characters; size, depth, and time limits on parsing; a maintained parser, pinned and scanned; fuzz tests on malformed pages; cleaned output remains labeled untrusted.

**FP-06 Cache-stable layout (Medium).** On shared self-hosted inference, prefix caches can create timing side channels that reveal whether another tenant's prompt shared a prefix; tenant data accidentally placed in a shared prefix.
Check and control: only static, non-tenant content in the shared prefix; tenant-specific content in the tail; cache isolation per tenant on shared engines if the engine supports it; timing-probe test across tenants.

**FP-07 Scratchpad references (High).** Insecure direct object reference; guessable IDs; path traversal if an ID becomes a path; confused-deputy dereference where an injected prompt makes the agent fetch another reference; time-to-live and cleanup races; secrets stored in scratchpad; storage exhaustion.
Check and control: random 128-bit IDs; authorization on every dereference including tenant check; no ID-to-path mapping; size caps and quotas; encrypted storage where required; tests with guessed, cross-tenant, and expired IDs.

**FP-08 Typed handoffs (Medium).** Schema-validation bypass; unsafe deserialization if YAML is used with a full loader (remote code execution); cross-site scripting when the dashboard renders handoff fields; oversized payloads; version confusion. A typed field is not a trusted field.
Check and control: JSON only, or YAML only through a safe loader; strict schema with size limits; escape on render (text nodes, no HTML injection); schema version pinning; fuzz tests.

**FP-09 Compaction (Medium).** Digests inherit and can launder injected text; an attacker can push critical instructions out of the window; information loss on safety-relevant content.
Check and control: digests inherit the lowest trust of their sources; never compact system, policy, approval, or provenance content; restorable raw store; tests that plant instructions and verify they do not gain trust after compaction.

**FP-10 Durable state (High).** Checkpoint tampering to inject state into a resumed mission; replay of side effects; sensitive data at rest; resuming after a key is revoked or a tenant is disabled; idempotency-key collisions.
Check and control: integrity protection (HMAC or authenticated encryption) and tenant scoping; encryption at rest; re-authorization on resume; per-tenant idempotency namespaces; fault-injection tests including tampered checkpoints.

**FP-11 Approval gates (High).** Approval bypass; time-of-check to time-of-use (arguments changed after approval); approver spoofing; UI that shows a summary different from the real arguments; default-allow on timeout; replay of an old approval; social engineering through the agent's own rationale text.
Check and control: bind approval to an argument hash; single-use, expiring tokens; authenticated approver; display raw arguments; timeout equals deny; audit log; the bypass test list in 6/FP-11.

**FP-12 Role and swarm factory (High).** Insecure direct object references; mass assignment of owner, tenant, or capability fields; administrator overreach; capability escalation through role edits; role-library poisoning (a saved role later reused with broader permissions); a role spec changed between approval and run; races between edit and run; audit-log tampering; response differences that reveal existence; plugin tools registered with understated risk classes.
Check and control: tenant taken from the credential; server-side field allow-lists; role and spec versions hashed and recorded per mission, with approval bound to the spec hash; registry entries code-owned and reviewed; admin actions scoped and logged; 404 for cross-tenant; optimistic concurrency; append-only audit log; the authorization matrix test.

**FP-13 Task-driven swarm generation (Critical).** A privileged planner driven by user-influenced text is a confused deputy: injected text can make it grant itself code, web, or file capabilities; planner output becomes instructions for future agents; planning loops cause cost abuse. A generic planner asked for a "security testing" swarm could also assemble offensive capabilities. Ephemeral roles are harder to audit afterwards.
Check and control: deterministic validator and privilege ceiling outside the model; dual-use capabilities only through an operator-authorized policy pack with scope allow-lists; human approval for risky capabilities; planning budget; planner cannot modify its own permissions; role-spec hash logged per mission; red-team cases against the planner in the evaluation set.

**FP-14 Embeddings and retrieval (High).** Cross-tenant leakage through a missing filter, a shared index, a shared BM25 statistic, or a cache keyed without tenant; vectors are not anonymized and can leak content; text sent to a third-party embedding API; model-version mixing; malformed blobs and wrong dimensions causing crashes; SQL injection through category or item identifiers; resource exhaustion through huge inputs; incomplete deletion (vectors, indexes, caches, backups); retrieval corpus poisoning that surfaces attacker content ahead of legitimate content.
Check and control: tenant filter enforced in a single data-access layer with tests; parameterized queries and validated identifiers; per-tenant consent for cloud embeddings; size limits; deletion that cascades to every derived store and is tested; retrieved text labeled untrusted; poisoning tests.

**FP-15 Exemplar memory (High).** Persistent prompt injection through exemplars; one user's task content shown to another user; PII inside exemplars; poisoning through manipulated feedback or repeated attacker-controlled missions.
Check and control: approval gate and provenance; scope policy enforced in code; PII scanning; feedback-integrity limits (rate limits, per-user weight caps); injection filters; evaluation gate before activation.

**FP-16 User profiles (Medium).** Prompt injection through profile fields; a user writing "constraints" that weaken safety instructions; PII; insecure direct object reference on profiles; prompt-only compliance that is not actually enforced.
Check and control: schema and length validation; escape and delimit; policy enforced in code; authorization tests; data minimization.

**FP-17 Feedback log (High, privacy).** Export of client data inside datasets; consent not captured; feedback spam and poisoning; PII in free-text critiques; erasure requests versus immutable datasets; file path handling for dataset files.
Check and control: consent and purpose flags enforced at write and export; PII scrubbing and scanning; rate limits and abuse detection; erasure that rewrites derived datasets; generated file names, no user-controlled paths.

**FP-18 Skill distiller (Critical).** Self-modifying instructions derived from untrusted traces. Specific attack paths: planting content that is distilled into a malicious skill; crafting tags so a malicious skill applies to a privileged role; inflating a skill's learned count through repeated attacker-controlled missions; overwriting a skill by choosing its name in a table with a uniqueness constraint; skills that memorize secrets present in traces; a background process running with excessive credentials; starvation of the foreground by background inference; gradual behavior drift without audit.
Check and control: candidate state with evaluation and human approval; provenance and trust inheritance; capability-signature checks that ignore free-text tags for authorization; schema, content, and injection filters; secret scanning stronger than regex alone; low-privilege credentials for the distiller; versioned skills, audit log, rollback, kill switch; caps on active skills; no cross-tenant promotion; planted-injection and signature-leak suites.

**FP-19 Prompt optimization (Medium).** Reward hacking; leakage of the test split into prompts; runaway optimization cost; sensitive examples written into prompt artifacts; injection carried in from dataset content; supply chain of optimization libraries.
Check and control: frozen test split and leak checks; spend caps; scrub examples; prompts promoted only through the approval pipeline; pinned and scanned dependencies.

### 8.4 Cross-cutting checklist

**Authentication and authorization**
- Credentials are high-entropy, stored hashed, scoped, rotatable, and revocable, with last-use tracking and per-key rate limits; comparison is constant-time.
- Tenant identity comes only from the credential. Every endpoint has an authorization test covering missing, invalid, expired, wrong-tenant, and insufficient-scope credentials.
- Server-Sent Events cannot carry custom headers from the browser `EventSource`. Do not place long-lived keys in URLs, where they reach logs and history. Use fetch-based streaming or short-lived signed tokens.
- The server refuses to start on a non-loopback address without authentication configured.
- CORS is an explicit allow-list. State-changing endpoints resist cross-site request forgery.

**Tenant isolation**
- Tenant identifiers are validated strictly and normalized (Unicode normalization and case folding). Check collisions caused by case-insensitive filesystems (macOS, Windows default) and reserved Windows device names, since development appears to occur on Windows.
- Every query path (relational, vector, BM25, cache, scratchpad, checkpoint, dataset, log) is tenant-scoped in one data-access layer and covered by cross-tenant probe tests.
- Telemetry, logs, and traces are tenant-scoped and redacted. Prompts and tool outputs are not written to shared logs in clear text.

**Injection and unsafe handling**
- SQL: parameterized queries only; validated identifiers.
- Command and path: no shell string building; no user-controlled paths; safe archive handling.
- Deserialization: no `pickle`; YAML only through a safe loader; `struct.unpack` on stored blobs validates length first.
- UI: render all model, tool, and profile content as text. No inner-HTML injection. No automatic remote resource loading.
- Prompt injection: see FP-02. Treat every non-system context item as untrusted.

**Network and egress**
- Server-side request forgery: resolve once and connect to the resolved address (no second resolution); block loopback, link-local (including the cloud metadata address), private ranges, IPv4-mapped IPv6, and unusual encodings; handle redirects explicitly; resolve parser differences in URLs (embedded credentials, backslashes).
- Egress from tools is deny-by-default with an allow-list proxy where needed.

**Secrets and keys**
- Secrets only from the environment or a secret store; never in code, config files committed to the repository, traces, or prompts. Secret scanning in CI. Key rotation procedure documented and tested. Separate keys per provider and per environment.

**Data protection**
- Encryption in transit everywhere; encryption at rest for databases, checkpoints, scratchpads, and backups. Retention schedule per data class. Erasure cascades to vectors, indexes, caches, exemplars, skills, datasets, and backups, and is tested.
- A subprocessor list of LLM and embedding providers, with their retention and training terms recorded per client.

**Supply chain and build**
- Pinned dependencies with hashes; dependency vulnerability scanning; static analysis for Python; container image scanning; pinned base images by digest; software bill of materials. Review any gateway or proxy library that holds provider credentials with extra care.

**Abuse, availability, and cost**
- Rate limits per credential and per tenant; concurrency caps; request and payload size limits; per-mission step and token caps; backpressure on queues; timeouts at every boundary.

**Generic roles and dual-use capabilities**
- Capability registry entries declare risk class, side effects, and data classification. The validator and approval policy use these declarations, never role names.
- Dual-use capabilities (network scanning, exploitation tooling, personal-data access, financial actions) are unavailable unless an operator-authorized policy pack grants them with explicit scope allow-lists (targets, datasets), stronger approval, and full audit. This applies equally to a "security testing" swarm and to any other domain.
- Role and skill tags are a controlled vocabulary plus free text. Free-text tags are untrusted and never used for authorization.
- Each mission records the hash of its role specs, tools, topology, and active skills, so any run can be reconstructed and audited.

**Logging and audit**
- Append-only audit log for authentication, administrative actions, approvals, skill and exemplar state changes, and data export. Logs avoid secrets and minimize prompt content. Time source is monotonic for durations.

**Operations**
- Backup and restore tested. Incident response procedure and contact. Vulnerability disclosure policy in `SECURITY.md`. Pen test before any client production use.

### 8.5 Re-verify items marked complete

The following are recorded as complete by the owner. Re-run these checks and keep the evidence before relying on them.

- **Container or in-process sandbox ("fail-closed")**: replay the two known bypasses (restoring the saved original `connect`; spawning a child interpreter) plus the vector list in 6/FP-01. All must fail.
- **SSRF and DNS rebinding pinning**: verify the connection uses the pinned address (not a second lookup); test decimal, hex, and octal IPs, IPv4-mapped IPv6, `0.0.0.0`, link-local and cloud metadata addresses, redirects to internal hosts, URL parser tricks, and a rebinding server with a very short TTL.
- **Strict tenant validation**: collision tests (Unicode normalization, case, trailing characters), reserved names, path traversal attempts through tenant IDs.
- **API-key authentication and RBAC**: full authorization matrix; scope escalation; credential in query string handling; timing behavior; non-loopback startup refusal.
- **Tenant SSE isolation**: two-tenant test that includes system events, reconnect, and late subscription.
- **Privacy airlock and DLP**: measure precision and recall on a labeled corpus of PII and secrets (including IPv6, obfuscated emails, multi-line keys). The scrubber is a heuristic. Report its recall rather than describing it as complete.
- **Test suite and CI**: confirm the 39 tests run on every pull request, are required to pass, and report line and branch coverage for authentication, tenant, sandbox, and network modules. Add mutation testing on those modules. The count of tests is not a measure of assurance.

### 8.6 Phase security and integration gates

- **Integration Gate (Required for all features)**: A feature is Shipped only after it is wired into a runtime execution path and validated with an integration test suite. As of Revision 3, the vertical slice (`TaskDrivenMetaPlanner` $\rightarrow$ `SwarmFactory` $\rightarrow$ `BaseTopology` $\rightarrow$ `ApprovalGate` $\rightarrow$ `CheckpointStore`) is wired in `core/mission.py` and validated by `tests/integration/test_vertical_slice.py`.
- **Gate A (before Phase B)**: FP-01 and the FP-02 core pass; the re-verification in 8.5 is complete; FP-00 stages 1 and 2 exist (usage ledger and smoke baseline); FP-03 ledger and budgets are in place.
- **Gate B (before Phase C)**: FP-11 lite and the FP-12 authorization matrix pass; the FP-13 validator, privilege ceiling, and dual-use policy are verified; the conformance checks are green on at least 3 domain packs and the held-out domain.
- **Gate C (before Phase D)**: the Tier 2 features that were enabled passed their listed checks; retrieval has verified tenant isolation and a tested deletion cascade; FP-10 and durable FP-11 pass bypass and fault-injection suites.
- **Gate D (before any client production data, independent of phase)**: third-party penetration test, data-processing agreements, subprocessor list, incident response procedure, and a restore test. Tier 3 memory features also require an independent security review before being enabled.

### 8.7 Residual risks and non-goals

- Prompt injection cannot be eliminated at the model level. Controls reduce likelihood and limit blast radius. ASR is reported, not assumed zero.
- Regex-based redaction misses names, addresses, and novel secret formats. It is a mitigation, not a boundary.
- SQLite is a single-node store. It does not provide row-level security or multi-instance scaling; the PostgreSQL path addresses this.
- Third-party model providers are trusted according to their contracts, not by this system.
- A compromised host or container runtime defeats the sandbox. Isolation depth (rootless runtime, optional user-space kernel or microVM) is a deployment decision.

### 8.8 Compliance and data-handling notes

These are engineering considerations, not legal advice. Confirm obligations with counsel. Expect clients to require: purpose limitation for stored data and feedback; data-processing agreements covering model and embedding providers; data residency options (local embedding and local inference); deletion and export on request, including derived artifacts; audit logs; and documented retention. Training or fine-tuning on client data requires explicit written consent and is out of scope for this plan.

---

## 9. Completed milestones (owner-reported, not independently verified)

- Decoupled core with `LLMProvider`, `StorageBackend`, and `EventBus` protocols.
- Two swarm topologies: orchestrator delegation and peer inbox network.
- Multi-tenant SQLite store with WAL mode, composite indexes, and strict tenant ID validation.
- Cross-swarm privacy airlock and egress DLP (5-category regex scrubber, token redaction).
- Security hardening: calculator DoS caps, SSRF and DNS-rebinding pinning, fail-closed sandbox, tenant workspace isolation.
- API-key authentication and scoped RBAC: constant-time verification, non-loopback binding guard, tenant SSE isolation.
- 144-test pytest suite (`tests/unit`, `tests/integration`, `tests/e2e`) and GitHub Actions CI.
- Cloud LLM support through Google Gemini (`gemini-3.5-flash-lite`).
- Unified default execution pipeline (`IntegratedMissionRunner` wired to `/api/missions/execute`, Web UI, and `agent.py`) with dynamic task-driven role planning (`TaskDrivenMetaPlanner`), native function-calling structured output (`emit_structured_output`), live interactive `ApprovalGate` (`auto_approve_high_risk=False`, `/api/approvals/resolve`), and step-by-step HMAC-SHA256 checkpoints (`CheckpointStore`).
- Committed live 10-task Smoke Benchmark evaluation (`benchmarks/baselines/smoke_live_gemini_flash.json`) across 5 domain packs on `gemini-3.5-flash-lite` (10/10 passed, 26,846 prompt tokens, 2,619 completion tokens, $0.002799 USD).

Each line above maps to the verification in section 8.5.

---

## 10. Revision notes

### Revision 3: response to an external review, a scope correction, and the generic-swarm requirement

| Review point | Assessment | Action taken |
| :-- | :-- | :-- |
| Scope explosion and process overhead for a solo developer | Accepted. The 20-item backlog and its governance were sized for a team. | Tiers, one feature at a time, time boxes, and proportionate change classes (section 1). Only Tier 1 is committed. |
| Cost of evaluation | Partly accepted. Per-pull-request evaluation was never required, but the plan did not say so, and the sample sizes were heavy for a first pass. The review's figure of 600 missions assumes both conditions re-run at 100 tasks. | Smoke, Standard, and Full tiers; baseline reuse (a Standard treatment run is roughly 90 to 150 missions); local model for iteration; staged judge calibration (sections 5.3 and 5.5). |
| Final-turn tool stripping | Partly accepted. Describing it as removed overstated the case: the net effect is empirical. The claim that it is proven and saves 800 or more tokens is plausible but is not measured in this project, and the saving depends on schema size. | Kept out as a default, restored as a measured per-provider experiment in FP-06. |
| Compact formats between agents | Accepted in part. JSON and safe YAML are safe, and the plan never banned them. That they drastically outperform prose is a hypothesis. | FP-08 allows compact JSON or safe YAML and decides by ablation. |
| New requirement: swarms must be generic, with roles and skills adapted to the task | Accepted and made structural. | Section 2.1; FP-12 (roles as data, registries, domain packs); FP-13 (domain-agnostic planner, generality metrics, held-out domain); FP-18 (skills keyed by capability signature); evaluation across at least 3 domain packs (section 5.4); dual-use policy (section 8). |

### Revision 2 changes


- Added FP-00. The previous plan proposed many optimizations with no way to prove they worked.
- Reordered by dependency. Measurement and security controls come first; learning features come last.
- Split FP-02 into retrieval, exemplars, profiles, and feedback, because they have different risks and different evidence needs.
- Split FP-04 into five independently measurable changes. Removed final-turn tool stripping and in-place observation rewriting, which break the cache prefix the plan also tried to protect.
- Replaced the free-form YAML wire protocol with typed, schema-validated handoffs.
- Replaced "RLHF dataset logger" with a feedback log and corrected the preference-data claim.
- Replaced "ML-powered prompt optimization" with accurate descriptions: exemplar retrieval (FP-15) and a real optimization loop (FP-19).
- Removed the dead embedding model reference and added embedding versioning, a local-first default, and a re-embed job.
- Replaced the arbitrary hybrid weight with reciprocal rank fusion as the default.
- Added approval, provenance, and rollback to the skill distiller, and removed global promotion.
- Replaced "no legacy compatibility" with schema versioning and migrations.
- Promoted the container sandbox and per-role routing from candidates to scheduled work.
- Deferred decoding profiles, chat and replay, and the debugger.
- Added section 5 (measurement) and section 8 (security audit).

**ID mapping from the previous revision**

| Previous | Now |
| :-- | :-- |
| FP-01 Factory | FP-12, FP-13 |
| FP-02 ML optimization and vector memory | FP-14, FP-15, FP-16, FP-17, FP-19, D-1 |
| FP-03 Skill Distiller | FP-18 |
| FP-04 Token optimization | FP-05, FP-06, FP-07, FP-08, FP-09 |
| Candidate A Chat and replay | D-2 |
| Candidate B Model routing | FP-04 |
| Candidate C Container sandbox | FP-01 |
| Candidate D Debugger | D-3 |

**Decisions needed from the owner**

1. The default local embedding model and the hardware it will run on.
2. The license and distribution model for client deployments.
3. Which providers are permitted for which client categories.
4. Who acts as tenant administrator for approvals, and the approval service levels.
5. The target regulatory exposure of first clients, which determines Gate D requirements.
