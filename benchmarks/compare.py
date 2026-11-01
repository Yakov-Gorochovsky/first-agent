"""
benchmarks/compare.py - Paired comparison: task-selected swarm vs. one agent, on the same tasks.

Each task runs twice through the real mission pipeline: once with the topology chosen by the task
('auto') and once forced to 'single'. The report gives discordant pairs and cost per success, because
a pass-rate gap on a small task set means little by itself. See benchmarks/README.md.
"""

import argparse
import json
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.runner import (
    MeteredProvider,
    check_contains,
    check_numeric,
    check_regex,
    compute_config_hash,
    get_git_commit_sha,
)
from core.bus import EventBus
from core.checkpoint import CheckpointStore
from core.config import CONFIG
from core.llm import LLMProvider, OpenAICompatibleProvider
from core.mission import IntegratedMissionRunner
from core.storage import SQLiteStorage

DEFAULT_TASKS = PROJECT_ROOT / "benchmarks" / "eval_tasks.json"
CONDITIONS = {"swarm": None, "single": "single"}


def task_passed(task: Dict[str, Any], output: str) -> bool:
    """Apply a task's programmatic checker. An unknown checker fails rather than passing silently."""
    checker = task["checker_type"]
    if checker == "numeric":
        return check_numeric(output, task["expected_value"], task.get("tolerance", 0.05))
    if checker == "contains":
        return check_contains(output, task["expected_substr"])
    if checker == "regex":
        return check_regex(output, task["pattern"])
    return False


def mcnemar_exact_p(swarm_only: int, single_only: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    discordant = swarm_only + single_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(swarm_only, single_only) + 1))
    return min(1.0, 2 * tail / 2**discordant)


def _condition_summary(rows: List[Dict[str, Any]], condition: str) -> Dict[str, Any]:
    selected = [row for row in rows if row["condition"] == condition]
    successes = sum(1 for row in selected if row["passed"])
    cost = sum(row["cost_usd"] for row in selected)
    return {
        "tasks": len(selected),
        "passed": successes,
        "pass_rate": round(successes / len(selected), 4) if selected else 0.0,
        "total_cost_usd": round(cost, 6),
        "cost_per_success_usd": round(cost / successes, 6) if successes else None,
        "mean_seconds": round(sum(row["seconds"] for row in selected) / len(selected), 2) if selected else 0.0,
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize paired results. Only tasks that ran under both conditions count as pairs."""
    by_task: Dict[str, Dict[str, bool]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], {})[row["condition"]] = row["passed"]
    pairs = [pair for pair in by_task.values() if len(pair) == len(CONDITIONS)]
    swarm_only = sum(1 for pair in pairs if pair["swarm"] and not pair["single"])
    single_only = sum(1 for pair in pairs if pair["single"] and not pair["swarm"])

    topologies: Dict[str, int] = {}
    for row in rows:
        if row["condition"] == "swarm" and row.get("topology"):
            topologies[row["topology"]] = topologies.get(row["topology"], 0) + 1

    return {
        "paired_tasks": len(pairs),
        "swarm": _condition_summary(rows, "swarm"),
        "single": _condition_summary(rows, "single"),
        "swarm_only_passes": swarm_only,
        "single_only_passes": single_only,
        "mcnemar_exact_p": round(mcnemar_exact_p(swarm_only, single_only), 4),
        "selected_topologies": topologies,
    }


def run_task(task: Dict[str, Any], condition: str, provider: LLMProvider, workdir: Path) -> Dict[str, Any]:
    """Run one task under one condition. A crash is a failed result, not an aborted benchmark."""
    metered = MeteredProvider(provider)
    tenant = f"eval_{task['id'].lower().replace('-', '_')}_{condition}"
    runner = IntegratedMissionRunner(
        llm=metered,
        bus=EventBus(),
        checkpoint_store=CheckpointStore(db_path=str(workdir / "checkpoints.db"), hmac_secret=b"benchmark-only" * 3),
        storage=SQLiteStorage(db_path=str(workdir / "swarm.db")),
    )
    started = time.perf_counter()
    output, topology, error = "", None, None
    try:
        # Unattended: no gate means tainted high-risk calls are blocked rather than left waiting.
        result = runner.execute(
            task["prompt"], tenant_id=tenant, topology_override=CONDITIONS[condition], auto_approve_high_risk=True
        )
        output, topology = result.output, result.spec.get("topology")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
    shutil.rmtree(CONFIG.workspace_dir / "tenants" / tenant, ignore_errors=True)
    return {
        "task_id": task["id"],
        "pack": task["pack"],
        "condition": condition,
        "passed": error is None and task_passed(task, output),
        "topology": topology,
        "seconds": round(time.perf_counter() - started, 2),
        "tokens": metered.prompt_tokens + metered.completion_tokens,
        "cost_usd": round(metered.cost_usd, 6),
        "error": error,
        "output": output[:1500],
    }


def load_completed_rows(path: Path) -> List[Dict[str, Any]]:
    """Rows worth keeping from an earlier report. A crashed mission says nothing about the task, so it reruns."""
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8")).get("results", [])
    return [row for row in rows if row.get("error") is None]


def build_report(
    rows: List[Dict[str, Any]], model_name: str, spend_cap_usd: float, stopped_early: bool, complete: bool
) -> Dict[str, Any]:
    return {
        "git_commit_sha": get_git_commit_sha(),
        "config_hash": compute_config_hash(),
        "model_name": model_name,
        "timestamp": time.time(),
        "spend_cap_usd": spend_cap_usd,
        "complete": complete,
        "stopped_early_at_spend_cap": stopped_early,
        "summary": summarize(rows),
        "results": rows,
    }


def write_report(report: Dict[str, Any], path: Path) -> None:
    """Replace the report in one step, so a kill mid-write can't damage the results so far."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temp_path.replace(path)


def run_comparison(
    tasks: List[Dict[str, Any]],
    provider: LLMProvider,
    spend_cap_usd: float,
    prior_rows: Sequence[Dict[str, Any]] = (),
    out: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run every task and condition not already in prior_rows, saving to out after each mission."""
    rows = list(prior_rows)
    done = {(row["task_id"], row["condition"]) for row in rows}
    model_name = getattr(provider, "model", "unknown")
    spent = sum(row["cost_usd"] for row in rows)
    stopped_early = False
    for task in tasks:
        if spent >= spend_cap_usd:
            stopped_early = True
            break
        for condition in CONDITIONS:
            if (task["id"], condition) in done:
                continue
            with tempfile.TemporaryDirectory(prefix="swarm-eval-") as workdir:
                row = run_task(task, condition, provider, Path(workdir))
            rows = [*rows, row]
            spent += row["cost_usd"]
            verdict = "PASS" if row["passed"] else "FAIL"
            print(f"[{task['id']}] {condition:<6} {verdict} topology={row['topology']} ${row['cost_usd']:.4f}", flush=True)
            if out:
                write_report(build_report(rows, model_name, spend_cap_usd, stopped_early, False), out)
    report = build_report(rows, model_name, spend_cap_usd, stopped_early, not stopped_early)
    if out:
        write_report(report, out)
    return report


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Compare the task-selected swarm with one agent (live LLM).")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--limit", type=int, default=0, help="Run only the first N tasks.")
    parser.add_argument("--spend-cap", type=float, default=2.0, help="Stop starting new tasks past this USD total.")
    parser.add_argument("--out", type=Path, help="Write the JSON report here, updated after every mission.")
    parser.add_argument("--resume", action="store_true", help="Keep finished missions from --out and run the rest.")
    args = parser.parse_args(argv)
    if args.resume and not args.out:
        parser.error("--resume needs --out")

    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))
    selected = tasks[: args.limit] if args.limit else tasks
    prior_rows = load_completed_rows(args.out) if args.resume else []
    report = run_comparison(selected, OpenAICompatibleProvider(), args.spend_cap, prior_rows, args.out)
    print(json.dumps(report["summary"], indent=2))
    if args.out:
        print(f"Report saved to {args.out}")


if __name__ == "__main__":
    main()
