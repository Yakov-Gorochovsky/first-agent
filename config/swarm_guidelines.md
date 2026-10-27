# Swarm Engineering Guidelines (Goals & Measurable Targets)

These guidelines describe **what good looks like**, not how to implement it. The Self-Healer
measures every mission against these targets using only real data (mission checkpoints,
`swarm_traces`, workspace artifacts, server error log, and an AST scan of the code). It then
investigates the code itself to find root causes and proposes fixes.

Edit freely. Each rule has an ID, a goal, and the metric + target the scorecard uses.

---

## Pillar A — Mission Quality & Depth

- **[G-A1] Every planned specialist contributes.**
  Metric: `roster_participation` = planned roles with ≥1 tool call or finish ÷ planned roles. Target: 100%.
- **[G-A2] The team adapts to what discovery finds.**
  Large or multi-domain missions should grow the team based on discovered evidence rather than fixing the
  full roster before anything has been inspected.
  Metric: `dynamic_spawns` = successful `spawn_specialist` calls during the mission. Target: ≥1 on missions with ≥3 specialists.
- **[G-A3] No repeated work.**
  The same resource should not be fetched twice in one mission; downstream agents should go deeper, not re-do discovery.
  Metric: `duplicate_fetches` = `fetch_url` calls for a URL already fetched earlier in the mission. Target: 0.
- **[G-A4] Use the cheapest adequate tool.**
  High-risk tools (code execution) should not be used for things low-risk tools can do (reading/counting workspace files).
  Metric: `misused_code_exec` = `run_python` calls whose code only reads/parses workspace files. Target: 0.
- **[G-A5] Findings cite verbatim evidence.**
  Metric: `verbatim_citations` = distinct inline/fenced code quotes in notes written during the mission. Target: ≥3.
- **[G-A6] The user receives the complete deliverable.**
  Metric: `deliverable_ratio` = final answer length ÷ largest report artifact written during the mission. Target: ≥80%, no budget-crash markers.

## Pillar B — Token Economy & Scaling

- **[G-B1] Every mission's token and cost usage is measurable.**
  Metric: `tokens_measured` = final checkpoint or usage ledger records total tokens for the mission. Target: yes.
- **[G-B2] Little wasted effort.**
  Metric: `wasted_call_ratio` = (duplicate fetches + misused code exec + errored/blocked tool calls) ÷ total tool calls. Target: ≤5%.
- **[G-B3] Tokens buy quality.**
  Spending more tokens is acceptable when it buys measurable quality; spending more for equal or lower quality is a regression.
  Metric: `quality_per_10k_tokens`. Target: higher than previous comparable run.
- **[G-B4] Cost must scale sub-quadratically with team size** so large swarms are feasible.
  Metric: `tokens_per_specialist`. Target: does not grow as the number of specialists grows.

## Pillar C — Speed & Autonomy

- **[G-C1] Missions finish promptly.** Metric: `wall_clock_sec`. Target: ≤300s for a 4-specialist audit.
- **[G-C2] Humans are interrupted only for genuinely risky actions.** Metric: `approval_requests`. Target: 0 for read-only audits.

## Pillar D — Code Health & Reliability

- **[G-D1] Functions stay small.** Metric: `% functions ≤ 80 lines` and `largest function`. Target: ≥98%, ≤150 lines.
- **[G-D2] No silent failures.** Metric: `bare_excepts`. Target: 0.
- **[G-D3] The server runs without unhandled tracebacks.** Metric: tracebacks in `workspace/server_errors.log` (last 7 days). Target: 0.
- **[G-D4] All tests pass.** Metric: `pytest` pass rate. Target: 100%.

---

## Healer Rules (enforced in code, not editable here)

- Never edits: `core/security.py`, `core/auth.py`, `core/approval.py`, `core/rate_limiter.py`,
  `core/self_healer.py` (the judge), `tests/`, `.env`, `workspace/`, this file.
- Every cycle creates a Git restore tag first; any change that fails tests or scores worse on a real
  verification run is reverted.
