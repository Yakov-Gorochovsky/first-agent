# Benchmarks

| Tool | Question | Needs an API key |
| :-- | :-- | :-- |
| `runner.py` | Does the mission pipeline still pass the 10 smoke tasks? | Yes |
| `compare.py` | Does the task-selected swarm beat one agent, and at what cost? | Yes |
| `../demo.py` | Does the selection path work? Scripted, proves plumbing only. | No |

## Swarm vs. one agent

```bash
python benchmarks/compare.py --spend-cap 2 --out benchmarks/reports/compare.json
```

Each task in `eval_tasks.json` runs twice through the real pipeline: once with the topology chosen by the task, once forced to `single`. Prompts never name a tool, so tool choice is part of what is measured.

Read the report in this order:

1. `swarm_only_passes` and `single_only_passes`: tasks where exactly one condition passed. These carry the evidence.
2. `mcnemar_exact_p`: with 30 tasks, only a large difference will reach significance. A nonsignificant result means no difference was shown, not that the two are equal.
3. `cost_per_success_usd`: the price of each correct answer. A swarm that costs five times as much for the same pass rate has lost.
4. `selected_topologies`: if nearly every task lands on one topology, selection is not doing its job.

**Status:** `compare.py` has not been run against a live model yet, so there is no swarm-vs-single result to report.

## Existing baselines

`baselines/smoke_live_gemini_flash.json` was produced by the earlier runner, which used the planner's first role as **one worker agent**. It measures that agent's tool use, not multi-agent performance. `runner.py` now uses the real mission pipeline, so new runs are not directly comparable to it.

The checkers are simple pattern matches. A pass shows the answer contains what was expected, not that all of the output is good.
