# Roadmap

Revision 4 · November 1, 2026 · Replaces Revision 3

This roadmap lists only work that is not finished. Shipped work is described in [ARCHITECTURE.md](ARCHITECTURE.md). Revision 3, with its full feature specifications and per-feature threat notes, stays in git history; use it as a reference when an item here starts.

Items run in order, because each one depends on or is justified by those before it. Do one at a time.

The main goal after proving the swarm works is that it **gets better with use**, under human control and with measured results.

**Status:** `Next` is ready to start. `Planned` is waiting on an earlier item. `If measured` starts only when a measurement shows the need.

## 1. Prove it works

| # | Item | Status | Done when |
| :-- | :-- | :-- | :-- |
| 1 | **Green CI on Linux.** Confirm tests, lint, and types pass on Python 3.11 and 3.12. | Next | The GitHub Actions run passes on `main`. |
| 2 | **Reliable live runs.** Add a real fallback to another model or provider when one is out of quota: nothing passes a `ModelPolicyRouter` into the factory today, and the planner strips `model_policy`, so the existing fallback chain can't run. | Next | A mission survives a forced 429 on its primary model. |
| 4 | **Tune the planner against the results.** The first live evaluation ([report](benchmarks/reports/compare_gemini_flash_lite.json), `gemini-3.5-flash-lite`) passed 30/30 for both swarm and single agent with no discordant pairs, and the planner chose `single` for 28 of 30 tasks. The set is too easy to separate the two: add harder multi-part tasks where a team should help, then check whether topology choice varies sensibly and whether the planner keeps producing the same few roles. | Next | Selection is reviewed on a harder evaluation set, and the prompt changes are re-measured. |
| 5 | **Recorded demo.** A short recording of one live mission for the README. | Planned | Linked from the README. |

## 2. Learn from results

The loop: a mission finishes, the user rates it, a good result produces a candidate skill or reusable plan, a human approves it, and later similar tasks use it. Nothing learned reaches agents without that approval.

| # | Item | Status | Done when |
| :-- | :-- | :-- | :-- |
| 6 | **Mission feedback.** Wire `experimental/feedback.py` into the server, CLI, and UI so users can rate a result. All later learning depends on this signal. | Planned | A rating is stored against its mission, per tenant. |
| 7 | **Skill distiller.** Wire `experimental/distiller.py` to run after well-rated missions and propose candidate skills into the existing admin approval flow. | Planned | A good mission produces an unapproved candidate, and a planted-injection test set never produces an approved skill. |
| 8 | **Relevant skill retrieval.** Agents currently get the top 5 skills overall. Use `core/retrieval.py` to select approved skills for the current task, within what the tenant may see. | Planned | Skills are selected by task, and cross-tenant probes return nothing. |
| 9 | **Plan exemplars.** Wire `experimental/exemplars.py` so the planner can reuse approved team designs from similar tasks. | Planned | The planner receives relevant approved exemplars as untrusted examples. |
| 10 | **User preferences.** Wire `experimental/profiles.py` into mission context. | Planned | Preferences reach agents, and leak tests pass. |
| 11 | **Measure the learning.** Run `benchmarks/compare.py` with learning on and off, excluding tasks that produced a skill. Turn off anything that doesn't help. | Planned | A committed report shows the effect of learning, whatever it is. |

## 3. See what it does

| # | Item | Status | Done when |
| :-- | :-- | :-- | :-- |
| 12 | **OpenTelemetry observability.** Emit spans for mission, agent step, LLM call, and tool call, with attributes for tenant, topology, role, tokens, and cost. Export over OTLP, and keep the SDK optional so the core runs without it. The `EventBus` is the natural integration point. Prompts and tool output stay out of spans by default, because they can hold client data. | Planned | One mission appears as a single trace in a local collector, and the tests pass with the SDK absent. |
| 13 | **Plan preview in the Web UI.** The API and CLI already support it; the UI needs a view to inspect and edit the team before it runs. | Planned | A user can edit a plan in the browser and run it. |
| 14 | **Mission playbooks.** Starter chips in the UI for common task types. | Planned | Chips fill the task box without changing the planner. |

## 4. Harden it

| # | Item | Status | Done when |
| :-- | :-- | :-- | :-- |
| 15 | **Pin the sandbox image by digest**, and scan it. | Planned | `run_python` containers are referenced by digest. |
| 16 | **Container image test.** Build and run the Dockerfile in CI; it has not been built yet. | Planned | CI builds the image and passes a health check. |
| 17 | **Red-team suite.** Prompt-injection attempts per category, including through feedback and learned skills, reporting attack success rate with sample size. | Planned | Results are committed, including any attacks that succeed. |
| 18 | **Durable approvals.** Pending approvals live in memory and are lost on restart. Persist them and allow a mission to resume after one. | Planned | A mission waiting on approval survives a restart. |
| 19 | **Split the largest remaining units.** `server.py` and `core/self_healer.py` are over the 800-line limit, and several functions are too long, led by `PeerSwarm.run` and `agent.main`. | Planned | Files and functions meet the project limits, with tests unchanged. |
| 20 | **Shared limits.** Rate and concurrency limits are per-process. Move them to a shared store when running more than one process. | If measured | Limits hold across two server processes. |

## 5. Extend it

| # | Item | Status | Done when |
| :-- | :-- | :-- | :-- |
| 21 | **MCP client.** Register external MCP tools with the same risk classes and privilege ceiling as built-in tools. | Planned | A mission uses an MCP tool under the approval gate. |
| 22 | **Per-role model tiers.** Building on item 2, let planning and verification use a stronger model than routine steps. | Planned | Tiers are configured per role, and restricted tenants cannot reach disallowed providers. |
| 23 | **Domain packs.** Optional bundles of role templates, tools, policy, and evaluation tasks, with dual-use capabilities only through operator-authorized policy. | If measured | One pack loads with no core change. |

## 6. Wire in only if measured

These modules exist with unit tests but no runtime path. They aim at cost or speed more than at smarter results. Each gets wired in only if an evaluation shows it helps, and otherwise stays out or is removed.

| # | Item | Module | Measure first |
| :-- | :-- | :-- | :-- |
| 24 | Typed handoffs | `core/handoffs.py` | Handoff tokens and downstream success |
| 25 | Cache-stable prompt layout | `core/prompt_layout.py` | Cache hit ratio and billed tokens per provider |
| 26 | Scratchpad and compaction | `core/scratchpad.py`, `core/compaction.py` | Net cost per success on long missions |
| 27 | Prompt optimizer | `experimental/prompt_opt.py` | Lift on held-out tasks, once the evaluation set is large enough |

## Rejected

Promoting skills across tenants automatically. Shared skills reach agents only after admin review, and a regex scrubber is not sufficient protection for client data.

## Rules that outlast any item

**Claims**

1. A number is a result only if a committed report produced it. Anything else is labelled a hypothesis.
2. Every result states its sample size, model, and conditions.
3. Words like "guarantees", "100%", and "zero" appear only when a test enumerates the cases, and the count is stated.
4. A plan is not evidence. Nothing here is a capability until it ships.

**Engineering**

1. The core assumes no domain. Roles, tools, and topologies are data or registered plugins.
2. The task decides the swarm's shape unless a person chooses it.
3. A prompt is not a control. Anything that must hold is enforced in code.
4. Tenant identity comes from the credential, never from a request field or model output.
5. Model output, tool output, web content, and stored memory are untrusted.
6. Nothing learned automatically changes agent behavior without human approval.
7. A feature counts as shipped only when a runtime path uses it and an end-to-end test covers it.
