"""
core/self_healer.py - Evidence-based 4-Pillar Scorecard + LLM-driven Self-Healer.

Design principles
-----------------
1. Measurement uses ONLY real data: mission checkpoints, swarm_traces in the mission's
   time window, workspace artifacts written during the mission, workspace/server_errors.log,
   and an AST scan of the source tree. Nothing is hard-coded per mission.
2. Guidelines (config/swarm_guidelines.md) describe goals + metrics, never fixes.
3. Fixes are proposed by an LLM that reads the findings and the relevant source files.
   This module contains NO pre-written patches.
4. Every cycle: Git restore tag -> per-fix apply + syntax check + pytest -> optional real
   verification mission in a fresh subprocess -> tradeoff verdict -> keep (commit) or revert.
5. The healer can never edit itself, the tests, security modules, or the guidelines.
"""

import ast
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.bus import EventBus
from core.config import CONFIG

GUIDELINES_PATH = pathlib.Path("config/swarm_guidelines.md")
PROBLEMS_MD_PATH = pathlib.Path("PROBLEMS_AND_METRICS.md")
HEAL_STATE_DIR = pathlib.Path("workspace/self_heal")
SERVER_ERROR_LOG = pathlib.Path("workspace/server_errors.log")

EDITABLE_PREFIXES = (
    "core/", "tools.py", "server.py", "agent.py", "static/index.html",
    "config/settings.json", "config/default_roles.json",
)
FORBIDDEN_FILES = {
    "core/security.py", "core/auth.py", "core/approval.py", "core/rate_limiter.py",
    "core/self_healer.py", "config/swarm_guidelines.md", ".env",
}

_CODE_READ_PATTERN = re.compile(r"(?<![A-Za-z_.])open\(|json\.load|read_text|os\.listdir|glob\(|workspace/", re.IGNORECASE)
_CODE_SPAN_PATTERN = re.compile(r"```[\s\S]+?```|`[^`\n]{4,}`")
_HUB_AGENTS = {"coordinator", "orchestrator", "system", "user", "meta_planner"}


def is_editable(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    if not p or p in FORBIDDEN_FILES or ".." in p or p.startswith(("tests/", "workspace/", "/")):
        return False
    return any(p == pre or p.startswith(pre) for pre in EDITABLE_PREFIXES)


@dataclass
class Finding:
    rule_id: str
    pillar: str
    title: str
    metric: str
    measured: Any
    target: str
    points_lost: float
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Scorecard:
    mission_id: str
    prompt: str
    measured_at: float
    metrics: Dict[str, Any]
    pillars: Dict[str, float]
    composite: float
    findings: List[Finding] = field(default_factory=list)
    unmeasured: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


# ----------------------------------------------------------------------------
# Git safety
# ----------------------------------------------------------------------------
class GitSafetyController:
    def __init__(self, repo_root: pathlib.Path) -> None:
        self.repo_root = repo_root

    def git(self, args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(
                ["git", *args], cwd=str(self.repo_root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except Exception as exc:
            return 1, "", str(exc)

    def head(self) -> str:
        return self.git(["rev-parse", "--short", "HEAD"])[1] or "unknown"

    def dirty_files(self) -> List[str]:
        out = self.git(["status", "--porcelain"])[1]
        return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]

    def tag(self, label: str) -> str:
        name = f"self-heal/{re.sub(r'[^a-zA-Z0-9_-]', '-', label)[:24]}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.git(["tag", "-f", name])
        return name

    def revert_files(self, files: List[str]) -> None:
        if files:
            self.git(["checkout", "--", *files])

    def commit(self, files: List[str], message: str) -> str:
        self.git(["add", "--", *files])
        self.git(["commit", "-m", message])
        return self.head()

    def list_restore_points(self, limit: int = 12) -> List[Dict[str, str]]:
        pts: List[Dict[str, str]] = []
        rc, out, _ = self.git([
            "for-each-ref", "--sort=-creatordate",
            "--format=%(refname:short)|%(objectname:short)|%(creatordate:iso8601)", "refs/tags/self-heal",
        ])
        if rc == 0 and out:
            for line in out.splitlines()[:limit]:
                ref, commit, date = (line.split("|") + ["", "", ""])[:3]
                pts.append({"ref": ref, "commit": commit, "date": date, "subject": "safe restore tag"})
        rc, out, _ = self.git(["log", f"-n{limit}", "--pretty=format:%h|%ci|%s"])
        if rc == 0 and out:
            for line in out.splitlines():
                commit, date, subj = (line.split("|", 2) + ["", "", ""])[:3]
                pts.append({"ref": commit, "commit": commit, "date": date, "subject": subj})
        return pts

    def restore(self, ref: str) -> Tuple[bool, str]:
        ref = ref.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_./-]+", ref):
            return False, f"Invalid ref '{ref}'."
        stashed = False
        if self.dirty_files():
            self.git(["stash", "push", "-u", "-m", f"self-heal pre-restore {int(time.time())}"])
            stashed = True
        rescue = self.tag("pre-restore")
        rc, out, err = self.git(["reset", "--hard", ref])
        if rc != 0:
            return False, f"git reset failed: {err or out}"
        note = " Uncommitted work was saved to `git stash`." if stashed else ""
        return True, f"Restored to {ref} (HEAD {self.head()}). Previous state tagged {rescue}.{note} Restart server.py to load the restored code."


# ----------------------------------------------------------------------------
# Scorecard engine (evidence only)
# ----------------------------------------------------------------------------
class ScorecardEngine:
    def __init__(self, repo_root: Optional[pathlib.Path] = None, db_path: Optional[str] = None) -> None:
        self.repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent
        self.db_path = str(db_path or CONFIG.db_path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def list_missions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT mission_id, tenant_id, MIN(timestamp) t0, MAX(timestamp) t1, "
                "MAX(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) done "
                "FROM mission_checkpoints GROUP BY mission_id ORDER BY t1 DESC LIMIT ?", (limit,),
            ).fetchall()
        return [
            {
                "mission_id": r["mission_id"], "tenant_id": r["tenant_id"], "started": r["t0"],
                "ended": r["t1"], "completed": bool(r["done"]), "prompt": self.mission_prompt(r["mission_id"])[:160],
            }
            for r in rows
        ]

    def latest_completed_mission(self) -> Optional[str]:
        return next((m["mission_id"] for m in self.list_missions(50) if m["completed"]), None)

    def mission_prompt(self, mission_id: str) -> str:
        with self._conn() as c:
            r = c.execute(
                "SELECT accumulated_output FROM mission_checkpoints WHERE mission_id=? ORDER BY step_number LIMIT 1",
                (mission_id,),
            ).fetchone()
        text = str(r["accumulated_output"] or "") if r else ""
        return text[6:] if text.startswith("Task: ") else text

    def _load_mission(self, mission_id: str) -> Dict[str, Any]:
        with self._conn() as c:
            cps = [dict(r) for r in c.execute(
                "SELECT * FROM mission_checkpoints WHERE mission_id=? ORDER BY step_number", (mission_id,)
            ).fetchall()]
            if not cps:
                raise ValueError(f"No checkpoints for mission '{mission_id}'.")
            tenant, t0, t1 = cps[0]["tenant_id"], cps[0]["timestamp"], cps[-1]["timestamp"]
            traces = [dict(r) for r in c.execute(
                "SELECT * FROM swarm_traces WHERE tenant_id=? AND created_at BETWEEN ? AND ? ORDER BY id",
                (tenant, t0 - 1.0, t1 + 2.0),
            ).fetchall()]
            led = c.execute(
                "SELECT COUNT(*) n, SUM(prompt_tokens) p, SUM(completion_tokens) cpl, SUM(cost_usd) usd "
                "FROM usage_ledger WHERE mission_id=?", (mission_id,),
            ).fetchone()
        for t in traces:
            try:
                t["payload"] = json.loads(t.get("payload") or "{}")
            except Exception:
                t["payload"] = {}
        return {"checkpoints": cps, "traces": traces, "tenant": tenant, "t0": t0, "t1": t1, "ledger": dict(led)}

    def scan_code(self) -> Dict[str, Any]:
        files = sorted((self.repo_root / "core").glob("*.py")) + [
            self.repo_root / n for n in ("tools.py", "server.py", "agent.py")
        ]
        loc = funcs = bare = 0
        long_funcs: List[Tuple[int, str]] = []
        for f in files:
            if not f.exists():
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            loc += len(src.splitlines())
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            rel = f.relative_to(self.repo_root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs += 1
                    n = (node.end_lineno or node.lineno) - node.lineno + 1
                    if n > 80:
                        long_funcs.append((n, f"{rel}:{node.name}"))
                elif isinstance(node, ast.ExceptHandler) and node.type is None:
                    bare += 1
        long_funcs.sort(reverse=True)
        return {
            "total_loc": loc,
            "total_functions": funcs,
            "functions_le_80_pct": round(100.0 * (funcs - len(long_funcs)) / max(1, funcs), 1),
            "long_functions": [f"{name} ({n} lines)" for n, name in long_funcs[:8]],
            "largest_function_lines": long_funcs[0][0] if long_funcs else 0,
            "bare_excepts": bare,
        }

    def server_tracebacks(self, days: int = 7) -> Optional[int]:
        p = self.repo_root / SERVER_ERROR_LOG
        if not p.exists():
            return None
        cutoff = time.time() - days * 86400
        n = 0
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            mm = re.match(r"^\[(\d+(?:\.\d+)?)\] TRACEBACK", line)
            if mm and float(mm.group(1)) >= cutoff:
                n += 1
        return n

    def last_pytest(self) -> Optional[Dict[str, Any]]:
        p = self.repo_root / HEAL_STATE_DIR / "last_pytest.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    def _collect_trace_metrics(self, traces: List[Dict[str, Any]]) -> Dict[str, Any]:
        active: set = set()
        calls: List[Dict[str, Any]] = []
        approvals, errors = [], 0
        for t in traces:
            ag, et, pl = t["agent_name"], t["event_type"], t["payload"]
            if et == "approval_required":
                approvals.append(t["id"])
            elif et == "tool_call":
                args = pl.get("args") or pl.get("arguments") or {}
                calls.append({"id": t["id"], "agent": ag, "tool": pl.get("tool", ""), "args": args if isinstance(args, dict) else {}})
                if ag not in _HUB_AGENTS:
                    active.add(ag)
            elif et == "agent_finish" and ag not in _HUB_AGENTS:
                active.add(ag)
            elif et in ("tool_error", "mission_error"):
                errors += 1
        seen: Dict[str, int] = {}
        dups, misused, spawns = [], [], []
        for tc in calls:
            if tc["tool"] == "fetch_url":
                u = str(tc["args"].get("url", "")).strip().rstrip("/")
                if u in seen:
                    dups.append(f"#{tc['id']} {tc['agent']} re-fetched {u} (first at #{seen[u]})")
                else:
                    seen[u] = tc["id"]
            elif tc["tool"] == "run_python":
                code = str(tc["args"].get("code", ""))
                if _CODE_READ_PATTERN.search(code) and "requests" not in code:
                    misused.append(f"#{tc['id']} {tc['agent']}: {' '.join(code.split())[:90]}")
            elif tc["tool"] == "spawn_specialist":
                spawns.append(f"#{tc['id']} {tc['agent']} -> {tc['args'].get('name', '?')}")
        return {"active": active, "calls": calls, "approvals": approvals, "errors": errors,
                "urls": seen, "dups": dups, "misused": misused, "spawns": spawns}

    def evaluate(self, mission_id: Optional[str] = None) -> Scorecard:
        mid = mission_id or self.latest_completed_mission()
        if not mid:
            raise ValueError("No completed missions found to evaluate.")
        data = self._load_mission(mid)
        prompt = self.mission_prompt(mid)
        cps = data["checkpoints"]
        tm = self._collect_trace_metrics(data["traces"])
        findings: List[Finding] = []
        unmeasured: List[str] = []
        m: Dict[str, Any] = {}

        planned: List[str] = []
        for cp in cps:
            mm = re.search(r"Roles:\s*\[([^\]]*)\]", str(cp.get("accumulated_output") or ""))
            if mm:
                planned = [x.strip().strip("'\"") for x in mm.group(1).split(",") if x.strip()]
                break
        participated = [p for p in planned if p in tm["active"]]
        m["tenant"] = data["tenant"]
        m["planned_roles"] = planned
        m["active_specialists"] = sorted(tm["active"])
        m["roster_participation_pct"] = round(100.0 * len(participated) / len(planned), 1) if planned else None
        m["dynamic_spawns"] = len(tm["spawns"])
        m["tool_calls"] = len(tm["calls"])
        m["unique_urls_fetched"] = len(tm["urls"])
        m["duplicate_fetches"] = len(tm["dups"])
        m["misused_code_exec"] = len(tm["misused"])
        m["approval_requests"] = len(tm["approvals"])
        m["tool_errors"] = tm["errors"]

        ws = self.repo_root / "workspace" / "tenants" / data["tenant"]
        notes = [p for p in ws.iterdir() if p.is_file() and data["t0"] - 1 <= p.stat().st_mtime <= data["t1"] + 10] if ws.exists() else []
        quotes: set = set()
        largest_report = 0
        for p in notes:
            body = p.read_text(encoding="utf-8", errors="replace")
            quotes.update(q.strip() for q in _CODE_SPAN_PATTERN.findall(body))
            if p.suffix.lower() == ".md":
                largest_report = max(largest_report, len(body))
        final = str(cps[-1].get("accumulated_output") or "") if cps[-1].get("status") == "COMPLETED" else ""
        crashed = (not final) or "[Continuation:" in final or "BudgetExceeded" in final
        m["notes_written_during_mission"] = [p.name for p in notes]
        m["verbatim_citations"] = len(quotes) if notes else None
        m["final_answer_chars"] = len(final)
        m["largest_report_chars"] = largest_report
        if crashed:
            m["deliverable_ratio_pct"] = 0.0
        elif largest_report:
            m["deliverable_ratio_pct"] = round(100.0 * min(1.0, len(final) / largest_report), 1)
        else:
            m["deliverable_ratio_pct"] = None
        if not notes:
            unmeasured.append("verbatim_citations / deliverable_ratio: no workspace notes have a modification time inside this mission's window (a later run may have overwritten them).")

        tokens: Optional[int] = None
        try:
            tokens = int(json.loads(cps[-1].get("budget_json") or "{}").get("total_tokens") or 0) or None
        except Exception:
            tokens = None
        led = data["ledger"]
        if not tokens and led.get("n"):
            tokens = int((led.get("p") or 0) + (led.get("cpl") or 0)) or None
        m["total_tokens"] = tokens
        m["cost_usd"] = round(float(led.get("usd") or 0), 5) if led.get("n") else None
        m["wall_clock_sec"] = round(data["t1"] - data["t0"], 1)
        wasted = len(tm["dups"]) + len(tm["misused"]) + tm["errors"]
        m["wasted_call_ratio_pct"] = round(100.0 * wasted / max(1, len(tm["calls"])), 1)
        n_spec = max(1, len(tm["active"]))
        m["tokens_per_specialist"] = round(tokens / n_spec) if tokens else None

        code = self.scan_code()
        m.update({f"code_{k}": v for k, v in code.items()})
        m["server_tracebacks_7d"] = self.server_tracebacks()
        pt = self.last_pytest()
        m["pytest"] = pt.get("summary") if pt else None
        m["pytest_passed"] = pt.get("passed") if pt else None
        if m["server_tracebacks_7d"] is None:
            unmeasured.append("server_tracebacks: workspace/server_errors.log does not exist yet (start the server once).")
        if pt is None:
            unmeasured.append("pytest: not run yet (a heal cycle runs it).")

        def add(rule: str, pillar: str, title: str, metric: str, measured: Any, target: str, lost: float, evidence: str) -> None:
            if lost > 0.05:
                findings.append(Finding(rule, pillar, title, metric, measured, target, round(lost, 1), evidence))

        # ---------- Pillar A (0-100) ----------
        a = 0.0
        rp = m["roster_participation_pct"]
        s = 20.0 if rp is None else 20.0 * rp / 100.0
        a += s
        add("G-A1", "A", "Not every planned specialist participated", "roster_participation_pct", rp, "100%", 20 - s,
            f"Planned {planned}; participated {participated}.")
        is_multi_domain = any(w in prompt.lower() for w in ("security", "audit", "accessibility", "rtl", "vulnerabilit", "ui/ux", "multi", "cross"))
        if is_multi_domain or len(planned) >= 3:
            s = 20.0 if tm["spawns"] else (10.0 if len(participated) >= 3 else 5.0)
            target_str = ">=1 dynamic spawn on multi-domain mission"
        else:
            s = 20.0
            target_str = ">=1 dynamic spawn when >=3 specialists"
        a += s
        add("G-A2", "A", "Team did not adapt to discovery", "dynamic_spawns", len(tm["spawns"]), target_str, 20 - s,
            f"{len(planned)} roles were fixed at planning time ({planned}); active specialists: {participated}; spawn_specialist calls during mission: {tm['spawns'] or 'none'}.")
        s = max(0.0, 15.0 - 5.0 * len(tm["dups"]))
        a += s
        add("G-A3", "A", "Repeated fetches of the same resource", "duplicate_fetches", len(tm["dups"]), "0", 15 - s, "; ".join(tm["dups"]))
        s = max(0.0, 15.0 - 5.0 * len(tm["misused"]))
        a += s
        add("G-A4", "A", "Code execution used where a file-read tool suffices", "misused_code_exec", len(tm["misused"]), "0", 15 - s, "; ".join(tm["misused"]))
        vc = m["verbatim_citations"]
        s = 15.0 if vc is None else 15.0 * min(1.0, vc / 3.0)
        a += s
        add("G-A5", "A", "Findings lack verbatim evidence", "verbatim_citations", vc, ">=3", 15 - s, f"{vc} distinct code quotes in {m['notes_written_during_mission']}.")
        dr = m["deliverable_ratio_pct"]
        s = 15.0 if dr is None else 15.0 * min(1.0, dr / 80.0)
        a += s
        add("G-A6", "A", "User did not receive the complete deliverable", "deliverable_ratio_pct", dr, ">=80%", 15 - s,
            f"Final answer {len(final)} chars vs largest report artifact {largest_report} chars" + (" (budget crash marker present)" if crashed else "") + ".")

        # ---------- Pillar B (0-100) ----------
        b = 0.0
        s = 30.0 if tokens else 0.0
        b += s
        add("G-B1", "B", "Mission token usage is not recorded", "total_tokens", tokens, "recorded", 30 - s,
            f"Final checkpoint budget_json={cps[-1].get('budget_json')!r}; usage_ledger rows for mission={led.get('n')}.")
        wr = m["wasted_call_ratio_pct"]
        s = 40.0 * max(0.0, 1.0 - max(0.0, wr - 5.0) / 45.0)
        b += s
        add("G-B2", "B", "Wasted tool calls", "wasted_call_ratio_pct", wr, "<=5%", 40 - s,
            f"{wasted} wasted of {len(tm['calls'])} tool calls (dups={len(tm['dups'])}, misused exec={len(tm['misused'])}, errors={tm['errors']}).")
        if tokens:
            m["quality_per_10k_tokens"] = round(a / (tokens / 10000.0), 2)
            b += 30.0  # ROI is judged relatively in the tradeoff verdict; absolute value only recorded
        else:
            m["quality_per_10k_tokens"] = None
            unmeasured.append("quality_per_10k_tokens / tokens_per_specialist: require recorded token usage (G-B1).")

        # ---------- Pillar C (0-100) ----------
        c = 0.0
        w = m["wall_clock_sec"]
        s = 50.0 * max(0.0, min(1.0, (600.0 - w) / 300.0))
        c += s
        add("G-C1", "C", "Mission took too long", "wall_clock_sec", w, "<=300s", 50 - s, f"Mission ran {w}s from first to last checkpoint.")
        s = max(0.0, 50.0 - 15.0 * len(tm["approvals"]))
        c += s
        add("G-C2", "C", "Human interrupted with approval requests", "approval_requests", len(tm["approvals"]), "0 for read-only audits", 50 - s,
            f"approval_required trace ids: {tm['approvals']}.")

        # ---------- Pillar D (0-100) ----------
        d = 0.0
        pct, big = code["functions_le_80_pct"], code["largest_function_lines"]
        s = 15.0 * max(0.0, min(1.0, (pct - 90.0) / 8.0)) + 15.0 * max(0.0, min(1.0, (400.0 - big) / 250.0))
        d += s
        add("G-D1", "D", "Large functions reduce modularity", "functions_le_80_pct / largest_function_lines", f"{pct}% / {big}", ">=98% / <=150", 30 - s,
            "Largest: " + ", ".join(code["long_functions"][:5]))
        s = max(0.0, 15.0 - 5.0 * code["bare_excepts"])
        d += s
        add("G-D2", "D", "Bare except blocks", "bare_excepts", code["bare_excepts"], "0", 15 - s, "")
        tb = m["server_tracebacks_7d"]
        s = 25.0 if tb is None else max(0.0, 25.0 - 5.0 * tb)
        d += s
        add("G-D3", "D", "Unhandled server tracebacks", "server_tracebacks_7d", tb, "0", 25 - s, self._recent_traceback_sample())
        s = 30.0 if (pt is None or pt.get("passed")) else 0.0
        d += s
        add("G-D4", "D", "Test suite failing", "pytest", m["pytest"], "100% pass", 30 - s, str(m["pytest"]))

        pillars = {"A": round(a, 1), "B": round(b, 1), "C": round(c, 1), "D": round(d, 1)}
        findings.sort(key=lambda f: -f.points_lost)
        return Scorecard(
            mission_id=mid, prompt=self.mission_prompt(mid), measured_at=time.time(), metrics=m,
            pillars=pillars, composite=round(sum(pillars.values()) / 4.0, 1), findings=findings, unmeasured=unmeasured,
        )

    def _recent_traceback_sample(self) -> str:
        p = self.repo_root / SERVER_ERROR_LOG
        if not p.exists():
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = [ln for ln in lines[-200:] if ln.strip()]
        excs = [ln for ln in tail if re.search(r"(Error|Exception)\b", ln) and not ln.startswith(" ")]
        return " | ".join(dict.fromkeys(excs[-4:]))[:600]

    @staticmethod
    def tradeoff(before: Scorecard, after: Scorecard) -> Dict[str, Any]:
        dq = round(after.pillars["A"] - before.pillars["A"], 1)
        dcomp = round(after.composite - before.composite, 1)
        tb, ta = before.metrics.get("total_tokens"), after.metrics.get("total_tokens")
        tok_pct = round(100.0 * (ta - tb) / tb, 1) if (tb and ta) else None
        waste_b, waste_a = before.metrics.get("wasted_call_ratio_pct", 0), after.metrics.get("wasted_call_ratio_pct", 0)
        deltas = {
            "composite": dcomp,
            **{f"pillar_{k}": round(after.pillars[k] - before.pillars[k], 1) for k in "ABCD"},
            "tokens_pct": tok_pct,
            "wasted_call_ratio_pct": round(waste_a - waste_b, 1),
            "findings_resolved": sorted({f.rule_id for f in before.findings} - {f.rule_id for f in after.findings}),
            "findings_new": sorted({f.rule_id for f in after.findings} - {f.rule_id for f in before.findings}),
        }
        if dq < -2.0 or dcomp < -1.0:
            v, keep, why = "REGRESSION", False, f"Quality {dq:+} pts, composite {dcomp:+} pts."
        elif tok_pct is not None and tok_pct > 15.0 and dq < 5.0:
            v, keep, why = "TOKEN_BLOAT", False, f"Tokens {tok_pct:+}% without a meaningful quality gain ({dq:+} pts)."
        elif tok_pct is not None and tok_pct > 0 and dq >= 5.0:
            v, keep, why = "WORTHWHILE_INVESTMENT", True, f"Tokens {tok_pct:+}% bought {dq:+} quality pts."
        elif dcomp > 0:
            v, keep, why = "STRICT_WIN", True, f"Composite {before.composite} -> {after.composite} ({dcomp:+})."
        else:
            v, keep, why = "NEUTRAL", True, f"No measurable change (composite {dcomp:+})."
        return {"verdict": v, "keep": keep, "explanation": why, "deltas": deltas}


# ----------------------------------------------------------------------------
# LLM fix proposer
# ----------------------------------------------------------------------------
def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except Exception:
        return None


class FixProposer:
    """Asks an LLM to locate the root cause of a finding and propose exact search/replace edits."""

    def __init__(self, llm: Any, repo_root: pathlib.Path) -> None:
        self.llm = llm
        self.repo_root = repo_root

    def repo_map(self) -> str:
        lines = []
        cands = sorted((self.repo_root / "core").glob("*.py")) + [
            self.repo_root / n for n in ("tools.py", "server.py", "agent.py", "static/index.html")
        ]
        for f in cands:
            rel = f.relative_to(self.repo_root).as_posix()
            if not f.exists() or not is_editable(rel):
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            defs = []
            if f.suffix == ".py":
                try:
                    for node in ast.parse(src).body:
                        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                            defs.append(node.name)
                except SyntaxError:
                    pass
            lines.append(f"- {rel} ({len(src.splitlines())} lines): {', '.join(defs[:25])}")
        return "\n".join(lines)

    def _ask(self, system: str, user: str) -> str:
        resp = self.llm.generate(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], tools=None)
        return resp.content or ""

    def propose(self, finding: Finding, guidelines: str, card: Scorecard, history: str) -> Dict[str, Any]:
        system = (
            "You are a senior engineer maintaining a multi-agent LLM swarm framework. You fix root causes in "
            "a general way that works for ANY mission type (never special-case a specific website, prompt, or role name). "
            "Prefer small, surgical, well-tested changes. Respond ONLY with JSON."
        )
        ctx = (
            f"GUIDELINES:\n{guidelines}\n\nFINDING TO FIX:\n{json.dumps(finding.to_dict(), indent=1)}\n\n"
            f"MISSION METRICS:\n{json.dumps(card.metrics, default=str)[:3000]}\n\n"
            f"PREVIOUS ATTEMPTS (do not repeat failed ideas):\n{history or 'none'}\n\n"
        )
        pick = self._ask(system, ctx + f"REPO MAP (editable files only):\n{self.repo_map()}\n\n"
                         'Which files (max 3) must be read to find the root cause? Reply {"files": [...], "hypothesis": "..."}')
        sel = _extract_json(pick) or {}
        files = [f for f in sel.get("files", []) if is_editable(f) and (self.repo_root / f).exists()][:3]
        if not files:
            return {"error": f"LLM did not select valid editable files: {pick[:300]}"}
        sources = "\n\n".join(f"===== FILE: {f} =====\n{(self.repo_root / f).read_text(encoding='utf-8', errors='replace')}" for f in files)
        prompt = (
            ctx + f"HYPOTHESIS: {sel.get('hypothesis', '')}\n\n{sources}\n\n"
            "Propose the fix. Each edit's `search` must be copied EXACTLY (including indentation) from the file and be unique in it. "
            'Reply as JSON: {"diagnosis": "root cause in 1-3 sentences", "edits": [{"file": "...", "search": "...", "replace": "..."}], '
            '"expected_effect": "which metric should change and how", "tradeoffs": "token/speed/quality tradeoffs"}'
        )
        prop = _extract_json(self._ask(system, prompt))
        if not prop or not isinstance(prop.get("edits"), list) or not prop["edits"]:
            return {"error": "LLM returned no usable edits."}
        return prop

    def repair(self, finding: Finding, proposal: Dict[str, Any], files: List[str], error_info: str, guidelines: str) -> Dict[str, Any]:
        """Ask LLM to fix a proposed patch that failed pytest or had syntax/search issues."""
        system = (
            "You are a senior engineer debugging a failed patch in a Python codebase. "
            "Your previous edit caused an error or failed pytest. "
            "Analyze the failure, preserve the original fix intent, and provide a corrected patch. "
            "Each edit's `search` must be copied EXACTLY (including exact whitespace/indentation) from the CURRENT file content. "
            "Respond ONLY with JSON."
        )
        valid_files = [f for f in files if is_editable(f) and (self.repo_root / f).exists()]
        sources = "\n\n".join(f"===== FILE: {f} =====\n{(self.repo_root / f).read_text(encoding='utf-8', errors='replace')}" for f in valid_files)
        prompt = (
            f"GUIDELINES:\n{guidelines}\n\nFINDING TO FIX:\n{json.dumps(finding.to_dict(), indent=1)}\n\n"
            f"PREVIOUS DIAGNOSIS:\n{proposal.get('diagnosis', '')}\n\n"
            f"PREVIOUS EDITS:\n{json.dumps(proposal.get('edits', []), indent=1)}\n\n"
            f"TEST / EXECUTION FAILURE:\n{error_info}\n\n"
            f"CURRENT SOURCE CODE:\n{sources}\n\n"
            "Provide the corrected fix that resolves this error and passes tests. "
            'Reply as JSON: {"diagnosis": "explanation of what failed and how you corrected it", '
            '"edits": [{"file": "...", "search": "...", "replace": "..."}], '
            '"expected_effect": "...", "tradeoffs": "..."}'
        )
        rep = _extract_json(self._ask(system, prompt))
        if not rep or not isinstance(rep.get("edits"), list) or not rep["edits"]:
            return {"error": "LLM returned no usable repair edits."}
        return rep


# ----------------------------------------------------------------------------
# Self-healer orchestration
# ----------------------------------------------------------------------------
class SelfHealer:
    def __init__(self, bus: Optional[EventBus] = None, llm: Any = None, repo_root: Optional[pathlib.Path] = None, db_path: Optional[str] = None) -> None:
        self.bus = bus
        self.repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent
        self.engine = ScorecardEngine(self.repo_root, db_path)
        self.git = GitSafetyController(self.repo_root)
        self._llm = llm
        self._lock = threading.Lock()
        self.running = False
        self.progress: List[Dict[str, Any]] = []
        (self.repo_root / HEAL_STATE_DIR).mkdir(parents=True, exist_ok=True)

    # ---------- infra ----------
    @property
    def llm(self) -> Any:
        if self._llm is None:
            from core.llm import OpenAICompatibleProvider
            self._llm = OpenAICompatibleProvider(model=os.getenv("HEALER_MODEL") or None)
        return self._llm

    def _emit(self, stage: str, message: str, **extra: Any) -> None:
        entry = {"t": time.time(), "stage": stage, "message": message}
        self.progress.append(entry)
        self.progress = self.progress[-200:]
        if self.bus:
            self.bus.emit("self_heal_progress", agent_name="self_healer", tenant_id="default", stage=stage, message=message, **extra)

    def _history_path(self) -> pathlib.Path:
        return self.repo_root / HEAL_STATE_DIR / "history.jsonl"

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        p = self._history_path()
        if not p.exists():
            return []
        rows = []
        for ln in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
        return rows

    def _record(self, entry: Dict[str, Any]) -> None:
        with self._history_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def get_guidelines(self) -> str:
        p = self.repo_root / GUIDELINES_PATH
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def save_guidelines(self, content: str) -> None:
        (self.repo_root / GUIDELINES_PATH).write_text(content.strip() + "\n", encoding="utf-8")

    def run_pytest(self) -> Dict[str, Any]:
        try:
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                               cwd=str(self.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
            summary = lines[-1] if lines else "no output"
            tail = "\n".join(lines[-45:]) if len(lines) > 45 else "\n".join(lines)
            res = {"passed": p.returncode == 0, "summary": summary, "tail": tail, "t": time.time()}
        except Exception as exc:
            res = {"passed": False, "summary": f"pytest error: {exc}", "tail": "", "t": time.time()}
        (self.repo_root / HEAL_STATE_DIR / "last_pytest.json").write_text(json.dumps(res), encoding="utf-8")
        return res

    def run_verification_mission(self, prompt: str, tenant: str = "default", timeout: int = 1200) -> Optional[str]:
        """Run the benchmark mission in a FRESH process so patched code is actually loaded."""
        before = {m["mission_id"] for m in self.engine.list_missions(100)}
        try:
            subprocess.run([sys.executable, "agent.py", "-p", prompt, "-t", tenant], cwd=str(self.repo_root),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            self._emit("verify", f"Verification mission timed out after {timeout}s.")
        new = [m for m in self.engine.list_missions(100) if m["mission_id"] not in before]
        return new[0]["mission_id"] if new else None

    # ---------- status ----------
    def status(self, mission_id: Optional[str] = None, baseline_id: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "running": self.running, "progress": self.progress[-40:], "guidelines": self.get_guidelines(),
            "missions": self.engine.list_missions(15), "restore_points": self.git.list_restore_points(),
            "history": self.history(), "head": self.git.head(),
        }
        try:
            cur = self.engine.evaluate(mission_id)
            out["current"] = cur.to_dict()
            if baseline_id and baseline_id != cur.mission_id:
                base = self.engine.evaluate(baseline_id)
                out["baseline"] = base.to_dict()
                out["tradeoff"] = ScorecardEngine.tradeoff(base, cur)
        except Exception as exc:
            out["error"] = str(exc)
        return out

    # ---------- edit application ----------
    def _apply_edits(self, edits: List[Dict[str, Any]]) -> Tuple[List[str], Optional[str]]:
        originals: Dict[str, str] = {}
        try:
            for e in edits:
                f = str(e.get("file", "")).replace("\\", "/")
                if not is_editable(f):
                    raise ValueError(f"'{f}' is not editable by the healer.")
                path = self.repo_root / f
                src = path.read_text(encoding="utf-8")
                originals.setdefault(f, src)
                search, replace = str(e.get("search", "")), str(e.get("replace", ""))
                if not search or src.count(search) != 1:
                    raise ValueError(f"search text occurs {src.count(search) if search else 0}x in {f} (must be exactly 1).")
                new = src.replace(search, replace, 1)
                if f.endswith(".py"):
                    ast.parse(new)
                path.write_text(new, encoding="utf-8")
            return list(originals), None
        except Exception as exc:
            for f, src in originals.items():
                (self.repo_root / f).write_text(src, encoding="utf-8")
            return [], str(exc)

    # ---------- main cycle ----------
    def start_cycle_async(self, **kwargs: Any) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.run_cycle, kwargs=kwargs, daemon=True).start()
        return True

    def run_cycle(self, mission_id: Optional[str] = None, max_fixes: int = 3, verify: bool = True,
                  rules: Optional[List[str]] = None) -> Dict[str, Any]:
        with self._lock:
            self.running = True
            self.progress = []
            try:
                return self._run_cycle(mission_id, max_fixes, verify, rules)
            except Exception as exc:
                self._emit("error", f"Cycle aborted: {exc}")
                return {"status": "error", "message": str(exc)}
            finally:
                self.running = False

    def _run_cycle(self, mission_id: Optional[str], max_fixes: int, verify: bool, rules: Optional[List[str]]) -> Dict[str, Any]:
        dirty = [f for f in self.git.dirty_files() if not f.startswith("workspace/")]
        if dirty:
            msg = f"Working tree has uncommitted changes ({dirty[:5]}). Commit or stash them first so the healer can revert safely."
            self._emit("blocked", msg)
            return {"status": "blocked", "message": msg}

        self._emit("inspect", "Scoring baseline mission from real traces + code scan...")
        before = self.engine.evaluate(mission_id)
        self._emit("pytest", "Running baseline test suite...")
        base_tests = self.run_pytest()
        if not base_tests["passed"]:
            msg = f"Baseline tests already fail ({base_tests['summary']}); fix them before healing."
            self._emit("blocked", msg)
            return {"status": "blocked", "message": msg}
        before = self.engine.evaluate(before.mission_id)
        targets = [f for f in before.findings if (not rules or f.rule_id in rules)][:max_fixes]
        self._emit("findings", f"Baseline {before.mission_id}: composite {before.composite}/100 {before.pillars}. "
                   f"Targeting: {[f'{f.rule_id} (-{f.points_lost})' for f in targets]}")
        if not targets:
            return {"status": "nothing_to_fix", "before": before.to_dict()}

        tag = self.git.tag("pre-heal")
        self._emit("checkpoint", f"Restore point {tag} at {self.git.head()}.")
        past = "\n".join(f"- {h.get('rule_id')}: {h.get('outcome')} — {h.get('diagnosis', '')[:160]}" for h in self.history(30) if h.get("rule_id"))
        proposer = FixProposer(self.llm, self.repo_root)
        guidelines = self.get_guidelines()
        applied: List[Dict[str, Any]] = []
        touched: List[str] = []

        for f in targets:
            self._emit("diagnose", f"[{f.rule_id}] {f.title} — measured {f.measured}, target {f.target}. Asking LLM to locate root cause...")
            prop = proposer.propose(f, guidelines, before, past)
            if prop.get("error"):
                self._emit("skip", f"[{f.rule_id}] {prop['error']}")
                self._record({"t": time.time(), "rule_id": f.rule_id, "outcome": "no_proposal", "diagnosis": prop["error"]})
                continue

            max_repairs = 2
            current_prop = prop
            files: List[str] = []
            applied_cleanly = False
            last_err = ""

            for attempt in range(max_repairs + 1):
                if attempt > 0:
                    self._emit("repair", f"[{f.rule_id}] Attempting repair {attempt}/{max_repairs} for: {last_err[:160]}...")
                    read_files = list(set([e.get("file") for e in current_prop.get("edits", []) if e.get("file")] + files))
                    rep = proposer.repair(f, current_prop, read_files, last_err, guidelines)
                    if rep.get("error"):
                        self._emit("skip", f"[{f.rule_id}] Repair proposal failed: {rep['error']}")
                        last_err = rep["error"]
                        break
                    current_prop = rep

                self._emit("patch", f"[{f.rule_id}] Applying patch" + (f" (repair {attempt})" if attempt > 0 else "") + f": {current_prop.get('diagnosis', '')[:160]}")
                patch_files, err = self._apply_edits(current_prop["edits"])
                if err:
                    self._emit("skip", f"[{f.rule_id}] Edit rejected: {err}")
                    last_err = f"Edit application error: {err}"
                    continue

                files = patch_files
                self._emit("pytest", f"[{f.rule_id}] Patched {files}; running tests...")
                res = self.run_pytest()
                if res["passed"]:
                    self._emit("ok", f"[{f.rule_id}] Fix passes tests" + (f" (after {attempt} repairs)" if attempt > 0 else "") + ".")
                    applied_cleanly = True
                    break
                else:
                    self._emit("repair_needed", f"[{f.rule_id}] Tests failed ({res['summary']}).")
                    last_err = f"Pytest failed with:\n{res.get('tail') or res.get('summary')}"
                    self.git.revert_files(files)
                    files = []

            if not applied_cleanly:
                if files:
                    self.git.revert_files(files)
                self._emit("revert", f"[{f.rule_id}] Tests/edits failed after {max_repairs} repairs; cleanly reverted.")
                self._record({"t": time.time(), "rule_id": f.rule_id, "outcome": "tests_failed", "diagnosis": current_prop.get("diagnosis", ""), "error": last_err})
                continue

            touched.extend(x for x in files if x not in touched)
            applied.append({"rule_id": f.rule_id, "files": files, **{k: current_prop.get(k, "") for k in ("diagnosis", "expected_effect", "tradeoffs")}})

        if not applied:
            self._emit("complete", "No fix survived. Codebase unchanged.")
            return {"status": "no_fix", "before": before.to_dict()}

        after = self.engine.evaluate(before.mission_id)  # code-level metrics re-scored
        verify_id = None
        if verify:
            self._emit("verify", f"Re-running the same mission in a fresh process to measure real behaviour: {before.prompt[:120]}...")
            verify_id = self.run_verification_mission(before.prompt, tenant=before.metrics.get("tenant", "default"))
            if verify_id:
                after = self.engine.evaluate(verify_id)
            else:
                self._emit("verify", "Verification run produced no mission; judging on code metrics only.")
        trade = ScorecardEngine.tradeoff(before, after)
        self._emit("verdict", f"{trade['verdict']}: {trade['explanation']}")

        if not trade["keep"]:
            self.git.revert_files(touched)
            outcome = "reverted"
            commit = None
        else:
            self._append_metrics_row(before, after, trade, applied, verify_id)
            commit = self.git.commit(touched + [PROBLEMS_MD_PATH.as_posix()],
                                     f"self-heal: {','.join(a['rule_id'] for a in applied)} | composite {before.composite}->{after.composite} ({trade['verdict']})")
            outcome = "kept"
        for a in applied:
            self._record({"t": time.time(), "rule_id": a["rule_id"], "outcome": outcome, "diagnosis": a["diagnosis"],
                          "verdict": trade["verdict"], "before": before.composite, "after": after.composite, "verify_mission": verify_id, "commit": commit})
        needs_restart = any(x in ("server.py", "static/index.html") or x.startswith("core/") for x in touched)
        report = {
            "status": outcome, "restore_tag": tag, "commit": commit, "applied": applied, "before": before.to_dict(),
            "after": after.to_dict(), "tradeoff": trade, "verify_mission": verify_id,
            "restart_required": needs_restart and outcome == "kept",
        }
        self._emit("complete", f"Cycle {outcome}. Composite {before.composite} -> {after.composite}." + (" Restart server.py to load the changes." if report["restart_required"] else ""), report=report)
        (self.repo_root / HEAL_STATE_DIR / "last_report.json").write_text(json.dumps(report, default=str, indent=1), encoding="utf-8")
        return report

    def _append_metrics_row(self, before: Scorecard, after: Scorecard, trade: Dict[str, Any], applied: List[Dict[str, Any]], verify_id: Optional[str]) -> None:
        p = self.repo_root / PROBLEMS_MD_PATH
        if not p.exists():
            return
        src = p.read_text(encoding="utf-8")
        nums = [int(x) for x in re.findall(r"^\|\s*\*\*(\d+)\*\*\s*\|", src, flags=re.MULTILINE)]
        n = max(nums) + 1 if nums else 1
        fixes = "; ".join(f"[{a['rule_id']}] {a['diagnosis'][:180]} ({', '.join(a['files'])})" for a in applied)
        bp, ap = before.pillars, after.pillars
        row = (
            f"| **{n}** | **Self-Heal: {', '.join(a['rule_id'] for a in applied)}** | 4-Pillar composite (A quality, B tokens, C speed, D code) measured on "
            f"`{before.mission_id}` → verification run `{verify_id or 'n/a (code metrics only)'}` | **{before.composite}/100** (A {bp['A']}, B {bp['B']}, C {bp['C']}, D {bp['D']}) | "
            f"{fixes} | **{after.composite}/100** (A {ap['A']}, B {ap['B']}, C {ap['C']}, D {ap['D']}) | **{trade['verdict']}** {trade['deltas']['composite']:+} pts; resolved {trade['deltas']['findings_resolved']} |\n"
        )
        marker = "\n---\n\n## 3."
        idx = src.find(marker)
        src = (src[:idx].rstrip("\n") + "\n" + row.rstrip("\n") + "\n" + src[idx:]) if idx != -1 else src + "\n" + row
        p.write_text(src, encoding="utf-8")
