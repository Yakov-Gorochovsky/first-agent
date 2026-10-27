"""
tests/unit/test_self_healer.py - Evidence-based scorecard, tradeoff verdicts, edit safety.
"""

import json
import pathlib
import sqlite3
import time

from core.self_healer import FixProposer, Scorecard, ScorecardEngine, SelfHealer, is_editable


def _make_db(tmp: pathlib.Path, dup_fetch: bool, spawn: bool) -> str:
    db = tmp / "swarm.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE mission_checkpoints (mission_id TEXT, tenant_id TEXT, step_number INT, status TEXT, topology TEXT, stage_index INT, accumulated_output TEXT, messages_json TEXT, budget_json TEXT, idempotency_keys_json TEXT, timestamp REAL, hmac_signature TEXT)")
    c.execute("CREATE TABLE swarm_traces (id INTEGER PRIMARY KEY, tenant_id TEXT, agent_name TEXT, event_type TEXT, payload TEXT, created_at REAL)")
    c.execute("CREATE TABLE usage_ledger (call_id TEXT, mission_id TEXT, step_id TEXT, tenant_id TEXT, agent_name TEXT, provider TEXT, model TEXT, prompt_tokens INT, completion_tokens INT, cached_prompt_tokens INT, latency_ms REAL, cost_usd REAL, created_at REAL)")
    t0 = time.time() - 100
    cps = [
        (1, "INITIALIZING", "Task: audit example"),
        (2, "PLANNED", "Topology: peer, Roles: ['recon', 'auditor', 'critic']"),
        (3, "COMPLETED", "final report " * 50),
    ]
    for i, (n, st, out) in enumerate(cps):
        c.execute("INSERT INTO mission_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("m1", "t1", n, st, "peer", n, out, "[]", json.dumps({"total_tokens": 50000}) if st == "COMPLETED" else "{}", "[]", t0 + i * 30, "x"))
    traces = [("recon", "tool_call", {"tool": "fetch_url", "args": {"url": "https://x.test/"}})]
    if dup_fetch:
        traces.append(("auditor", "tool_call", {"tool": "fetch_url", "args": {"url": "https://x.test"}}))
    if spawn:
        traces.append(("recon", "tool_call", {"tool": "spawn_specialist", "args": {"name": "css_probe"}}))
    traces += [("auditor", "agent_finish", {}), ("critic", "agent_finish", {})]
    for j, (ag, et, pl) in enumerate(traces):
        c.execute("INSERT INTO swarm_traces (tenant_id, agent_name, event_type, payload, created_at) VALUES (?,?,?,?,?)",
                  ("t1", ag, et, json.dumps(pl), t0 + 5 + j))
    c.commit()
    c.close()
    return str(db)


def test_scorecard_measures_only_trace_evidence(tmp_path: pathlib.Path) -> None:
    root = pathlib.Path(__file__).resolve().parents[2]
    bad = ScorecardEngine(root, _make_db(tmp_path, dup_fetch=True, spawn=False)).evaluate("m1")
    assert bad.metrics["duplicate_fetches"] == 1
    assert bad.metrics["dynamic_spawns"] == 0
    assert bad.metrics["roster_participation_pct"] == 100.0
    assert bad.metrics["total_tokens"] == 50000
    rules = {f.rule_id for f in bad.findings}
    assert {"G-A2", "G-A3"} <= rules

    (tmp_path / "good").mkdir()
    good = ScorecardEngine(root, _make_db(tmp_path / "good", dup_fetch=False, spawn=True)).evaluate("m1")
    assert good.pillars["A"] > bad.pillars["A"]
    verdict = ScorecardEngine.tradeoff(bad, good)
    assert verdict["keep"] is True
    assert "G-A3" in verdict["deltas"]["findings_resolved"]
    assert ScorecardEngine.tradeoff(good, bad)["keep"] is False


def test_healer_cannot_edit_judge_tests_or_security() -> None:
    for f in ("core/self_healer.py", "core/security.py", "tests/unit/test_x.py", ".env",
              "config/swarm_guidelines.md", "../evil.py", "workspace/keys.json"):
        assert not is_editable(f), f
    assert is_editable("core/peer_swarm.py")
    assert is_editable("static/index.html")


def test_apply_edits_requires_unique_match_and_valid_syntax(tmp_path: pathlib.Path) -> None:
    (tmp_path / "core").mkdir()
    target = tmp_path / "core" / "demo.py"
    target.write_text("x = 1\ny = 1\n", encoding="utf-8")
    healer = SelfHealer(repo_root=tmp_path, db_path=str(tmp_path / "none.db"))
    _, err = healer._apply_edits([{"file": "core/demo.py", "search": "= 1", "replace": "= 2"}])
    assert err and "exactly 1" in err
    _, err = healer._apply_edits([{"file": "core/demo.py", "search": "x = 1", "replace": "x = ("}])
    assert err
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 1\n"
    files, err = healer._apply_edits([{"file": "core/demo.py", "search": "x = 1", "replace": "x = 2"}])
    assert err is None and files == ["core/demo.py"]


def test_fix_proposer_has_no_builtin_patches() -> None:
    import core.self_healer as mod
    assert not [n for n in dir(SelfHealer) if n.startswith("_heal_")]
    assert hasattr(FixProposer, "propose")
    assert hasattr(FixProposer, "repair")
    assert isinstance(mod.Scorecard, type) and Scorecard


def test_fix_proposer_repair_method() -> None:
    class DummyLLM:
        def generate(self, messages, tools=None):
            class Resp:
                content = json.dumps({
                    "diagnosis": "fixed the test failure",
                    "edits": [{"file": "core/resilience.py", "search": "x = 1", "replace": "x = 2"}],
                    "expected_effect": "passes tests",
                    "tradeoffs": "none",
                })
            return Resp()

    from core.self_healer import Finding
    proposer = FixProposer(DummyLLM(), pathlib.Path(__file__).resolve().parents[2])
    finding = Finding("G-D1", "D", "Large functions", "metric", 100, "<=80", 10.0, "evidence")
    proposal = {"diagnosis": "orig", "edits": [{"file": "core/resilience.py", "search": "x = 9", "replace": "x = 2"}]}
    res = proposer.repair(finding, proposal, ["core/resilience.py"], "search text occurs 0x", "guidelines")
    assert res.get("diagnosis") == "fixed the test failure"
    assert len(res.get("edits")) == 1
