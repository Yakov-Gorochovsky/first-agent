"""swarm_tools/self_heal.py - Operator-authorized framework diagnosis and repair tools."""

from typing import Optional


def scorecard_diagnose(mission_id: Optional[str] = None) -> str:
    """Diagnose codebase health and mission performance across the 4 Pillars (Quality, Tokens, Speed, Code)."""
    try:
        from core.self_healer import ScorecardEngine
        engine = ScorecardEngine()
        card = engine.evaluate(mission_id)
        lines = [
            f"=== 4-PILLAR SCORECARD: {card.mission_id} ===",
            f"Composite Score: {card.composite}/100",
            f"Pillars: Quality(A)={card.pillars['A']}/100, Tokens(B)={card.pillars['B']}/100, Speed(C)={card.pillars['C']}/100, Code(D)={card.pillars['D']}/100",
            f"Open Findings ({len(card.findings)}):",
        ]
        for f in card.findings:
            lines.append(f"- [{f.rule_id} · Pillar {f.pillar}] {f.title}: measured {f.measured} (target {f.target}) [-{f.points_lost} pts]")
            if f.evidence:
                lines.append(f"  Evidence: {f.evidence[:160]}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error diagnosing scorecard: {exc}"


def self_heal_codebase(max_fixes: int = 3, verify: bool = True, rules: Optional[str] = None) -> str:
    """Run an autonomous self-healing cycle: diagnoses findings, LLM proposes patches, tests with interactive repair, runs verification mission in isolated child process, evaluates tradeoffs, and commits wins or reverts."""
    try:
        from core.self_healer import SelfHealer
        healer = SelfHealer()
        rule_list = [r.strip() for r in rules.split(",") if r.strip()] if isinstance(rules, str) else None
        report = healer.run_cycle(max_fixes=max_fixes, verify=verify, rules=rule_list)
        status = report.get("status")
        if status == "no_fix":
            return "Self-heal cycle completed: No fix survived test/syntax validation. Codebase was left unchanged."
        if status == "nothing_to_fix":
            return "Self-heal cycle completed: All targets already met. Nothing to fix."
        if status in ("kept", "reverted"):
            b, a = report.get("before", {}).get("composite"), report.get("after", {}).get("composite")
            trade = report.get("tradeoff", {})
            applied = report.get("applied", [])
            lines = [
                f"Self-heal cycle status: {status.upper()}",
                f"Verdict: {trade.get('verdict')} — {trade.get('explanation')}",
                f"Composite score: {b} -> {a}",
                f"Fixes applied: {', '.join(a['rule_id'] for a in applied) if applied else 'none'}",
            ]
            if report.get("commit"):
                lines.append(f"Git commit: {report['commit']}")
            return "\n".join(lines)
        return f"Self-heal cycle result: {report}"
    except Exception as exc:
        return f"Error executing self-heal: {exc}"
