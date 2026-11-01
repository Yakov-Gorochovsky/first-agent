"""
server.py - Step 4: Live SSE (Server-Sent Events) Backend for the Neural Swarm UI.

Built using Python's standard-library `ThreadingHTTPServer` (zero extra dependencies!).
Subscribes to `core/bus.py` (EventBus) and streams every agent thought, peer inbox
message, tool call, dynamic role creation, PrivacyAirlock skill, and budget update
in real time to the browser.

Usage:
    python server.py
    Then open http://localhost:8000 in your browser.
"""

import ipaddress
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from core.approval import GLOBAL_APPROVAL_GATE
from core.auth import APIKeyRecord, AuthManager
from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus, SwarmEvent
from core.checkpoint import CheckpointStore
from core.config import CONFIG
from core.llm import OpenAICompatibleProvider
from core.mission import IntegratedMissionRunner
from core.rate_limiter import (
    ConcurrencyLimitExceededError,
    DailyBudgetExceededError,
    RateLimitExceededError,
    TenantCostGovernor,
)
from core.roles import CAPABILITY_BUNDLES, RoleRegistry
from core.self_healer import SelfHealer
from core.simulation import run_cinematic_simulation
from core.storage import SQLiteStorage, validate_tenant_id
from core.swarm_manager import SwarmManager
from core.version import get_version_info

STATIC_DIR = Path(__file__).parent / "static"

# Shared application state initialized from centralized CONFIG
bus = EventBus()
storage = SQLiteStorage()
checkpoint_store = CheckpointStore(db_path=str(CONFIG.db_path))
role_registry = RoleRegistry()
budget = BudgetTracker(config=BudgetConfig(), bus=bus)
logger = logging.getLogger("neural_swarm.server")

cost_governor = TenantCostGovernor(
    storage=storage,
    default_rpm=float(CONFIG.max_steps_per_agent * 10),
    default_burst=10.0,
    max_concurrency_per_tenant=1,
    max_daily_usd_per_tenant=15.0,
)
llm = OpenAICompatibleProvider(bus=bus)
swarm_manager = SwarmManager(
    storage=storage, bus=bus, role_registry=role_registry, default_llm=llm
)
auth_manager = AuthManager(CONFIG.keys_path)
self_healer = SelfHealer(bus=bus)


@dataclass
class SSEClientConnection:
    client_queue: queue.Queue
    tenant_id: str
    is_admin: bool


# Connected tenant-scoped SSE client connections
_sse_clients_lock = threading.Lock()
_sse_clients: List[SSEClientConnection] = []
_tenant_run_locks_lock = threading.Lock()
_tenant_run_locks: Dict[str, threading.Lock] = {}


def _get_tenant_run_lock(tenant_id: str) -> threading.Lock:
    with _tenant_run_locks_lock:
        if tenant_id not in _tenant_run_locks:
            _tenant_run_locks[tenant_id] = threading.Lock()
        return _tenant_run_locks[tenant_id]


def check_host_binding_security(host: str, auth_enabled: bool, auth_manager: AuthManager) -> None:
    """Security Guardrail: Refuse to start on non-loopback host without configured authentication."""
    is_loopback = host.strip().lower() in {"127.0.0.1", "::1", "localhost"}
    if not is_loopback:
        if not auth_enabled or not auth_manager.list_records():
            raise RuntimeError(
                f"Security Guardrail Refusal: Server configured to bind on non-loopback host '{host}' "
                "without configured API-key authentication. Refusing to start."
            )


def _broadcast_event(event: SwarmEvent) -> None:
    """Fan out telemetry events strictly scoped by tenant boundary."""
    with _sse_clients_lock:
        if not _sse_clients:
            return

    payload = json.dumps(event.to_dict(), default=str)
    event_tenant = str(getattr(event, "tenant_id", "") or "").lower().strip()
    event_data = getattr(event, "data", {}) or {}
    target_swarm = str(event_data.get("to_swarm") or "").lower().strip()

    with _sse_clients_lock:
        for client in list(_sse_clients):
            # Admin clients receive all events (including system telemetry)
            if client.is_admin:
                try:
                    client.client_queue.put_nowait(payload)
                except queue.Full:
                    pass
                continue

            # Non-admin clients ONLY receive events belonging to or addressed to their tenant
            if client.tenant_id and (
                client.tenant_id == event_tenant
                or (target_swarm and client.tenant_id == target_swarm)
            ):
                try:
                    client.client_queue.put_nowait(payload)
                except queue.Full:
                    pass


bus.subscribe(_broadcast_event)


def bootstrap_admin_key(client_ip: str) -> str:
    """The UI's one-click login. Only a client on this machine may receive the admin key."""
    if not ipaddress.ip_address(client_ip).is_loopback:
        return ""
    return getattr(auth_manager, "active_admin_key", "") or getattr(auth_manager, "initial_admin_key", "") or ""


class SwarmRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "connect-src 'self' ws: http://127.0.0.1:* http://localhost:*;",
        )
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self, required_scope: str = "user") -> Optional[APIKeyRecord]:
        """Authenticate request using Authorization: Bearer <key> header only.

        Returns:
            The authenticated APIKeyRecord, or None if authentication failed or scope is insufficient.
        """
        if not CONFIG.auth_enabled:
            return APIKeyRecord(name="Local Dev No-Auth", key_hash="", tenant_id="admin", scopes=["admin", "user"])

        auth_header = self.headers.get("Authorization", "").strip()
        raw_key: Optional[str] = None
        if auth_header.startswith("Bearer "):
            raw_key = auth_header[7:].strip()
        elif auth_header:
            raw_key = auth_header

        if not raw_key:
            parsed = urlparse(self.path)
            if parsed.path == "/api/events":
                # Standard browser EventSource cannot send custom HTTP headers; accept token from query parameter
                qs = parse_qs(parsed.query)
                if "token" in qs and qs["token"]:
                    raw_key = qs["token"][0].strip()

        if not raw_key:
            self._send_json(
                {"error": "Unauthorized: API key required in 'Authorization: Bearer <token>' header."},
                401,
            )
            return None

        record = auth_manager.verify_key(raw_key)
        if not record:
            self._send_json({"error": "Unauthorized: Invalid API key."}, 401)
            return None

        if not record.has_scope(required_scope):
            self._send_json(
                {"error": f"Forbidden: Insufficient privileges. Required scope: '{required_scope}'."},
                403,
            )
            return None

        return record

    GET_ROUTES = {
        "/": "_route_index",
        "/index.html": "_route_index",
        "/api/version": "_route_api_version",
        "/api/state": "_route_api_state",
        "/api/events": "_route_api_events",
        "/api/self-heal/status": "_route_api_self_heal_status",
    }

    POST_ROUTES = {
        "/api/swarms": "_route_api_swarms",
        "/api/swarms/delete": "_route_api_swarms_delete",
        "/api/roles": "_route_api_roles",
        "/api/roles/delete": "_route_api_roles_delete",
        "/api/evals": "_route_api_evals",
        "/api/approvals/resolve": "_route_api_approvals_resolve",
        "/api/missions/execute": "_route_api_missions_execute",
        "/api/run": "_route_api_missions_execute",
        "/api/missions/plan": "_route_api_missions_plan",
        "/api/skills/approve": "_route_api_skills_approve",
        "/api/airlock-test": "_route_api_airlock_test",
        "/api/memory": "_route_api_memory",
    }

    def _route_index(self, parsed: Any) -> None:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            self._send_json({"error": "static/index.html not found"}, 404)
            return
        html = index_file.read_text(encoding="utf-8")
        active_key = bootstrap_admin_key(self.client_address[0])
        ver_info = get_version_info().to_dict()
        bootstrap_script = (
            f"<script>\n"
            f"  window.__BOOTSTRAP_API_KEY__ = {json.dumps(active_key)};\n"
            f"  window.__SERVER_VERSION__ = {json.dumps(ver_info)};\n"
            f"</script>"
        )
        html = html.replace("<head>", f"<head>\n  {bootstrap_script}", 1)
        content = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(content)

    def _route_api_version(self, parsed: Any) -> None:
        self._send_json(get_version_info().to_dict())

    def _route_api_state(self, parsed: Any) -> None:
        auth = self._authenticate("user")
        if not auth:
            return

        qs = parse_qs(parsed.query)
        if auth.has_scope("admin"):
            default_id = swarm_manager.list_swarms()[0].swarm_id if swarm_manager.list_swarms() else "default"
            raw_tenant = qs.get("tenant", [default_id])[0]
            try:
                tenant_id = validate_tenant_id(raw_tenant)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
        else:
            tenant_id = auth.tenant_id

        active_budget = swarm_manager.get_last_budget(tenant_id) or budget
        self._send_json(
            {
                "tenant_id": tenant_id,
                "version": get_version_info().to_dict(),
                "model": CONFIG.model_name,
                "endpoint": CONFIG.api_base_url,
                "swarms": swarm_manager.list_dicts(),
                "roles": role_registry.list_dicts(),
                "capability_bundles": CAPABILITY_BUNDLES,
                "tenant_memories": storage.get_tenant_memories(tenant_id),
                "global_skills": storage.get_global_skills(limit=12),
                "session_turns": storage.get_session_turns(tenant_id, limit=10),
                "pending_approvals": GLOBAL_APPROVAL_GATE.list_pending(tenant_id),
                "approval_audit": GLOBAL_APPROVAL_GATE.get_audit_trail(limit=10, tenant_id=tenant_id),
                "checkpoints": checkpoint_store.list_tenant_checkpoints(tenant_id, limit=10),
                "budget": {
                    "total_steps": active_budget.total_steps,
                    "max_swarm_steps": active_budget.config.max_swarm_steps,
                    "total_tokens": active_budget.total_tokens,
                    "max_total_tokens": active_budget.config.max_total_tokens,
                    "elapsed_seconds": active_budget.elapsed_seconds,
                    "agent_steps": active_budget.agent_steps,
                },
            }
        )

    def _route_api_events(self, parsed: Any) -> None:
        auth = self._authenticate("user")
        if not auth:
            return

        self.send_response(HTTPStatus.OK)
        self._send_security_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        client_conn = SSEClientConnection(
            client_queue=queue.Queue(maxsize=CONFIG.sse_queue_size),
            tenant_id=auth.tenant_id,
            is_admin=auth.has_scope("admin"),
        )
        with _sse_clients_lock:
            _sse_clients.append(client_conn)

        try:
            self.wfile.write(b"data: {\"event_type\": \"connected\"}\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = client_conn.client_queue.get(timeout=CONFIG.sse_heartbeat_sec)
                    self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_clients_lock:
                if client_conn in _sse_clients:
                    _sse_clients.remove(client_conn)

    def _route_api_self_heal_status(self, parsed: Any) -> None:
        auth = self._authenticate("user")
        if not auth:
            return
        hq = parse_qs(parsed.query)
        mission_id = (hq.get("mission_id", [""])[0] or "").strip() or None
        baseline_id = (hq.get("baseline_id", [""])[0] or "").strip() or None
        self._send_json(self_healer.status(mission_id=mission_id, baseline_id=baseline_id))

    def _route_api_swarms(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return

        swarm_id = str(body.get("swarm_id", "")).strip()
        title = str(body.get("title", "")).strip()
        topology = str(body.get("topology", "peer")).strip().lower()
        model = str(body.get("model", "")).strip()
        color = str(body.get("color", "#38bdf8")).strip()
        roles = body.get("roles", None)
        allow_cross_swarm = bool(body.get("allow_cross_swarm", False))
        directive = str(body.get("directive", "")).strip()

        if not swarm_id or not title:
            self._send_json({"error": "swarm_id and title are required."}, 400)
            return

        try:
            validate_tenant_id(swarm_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return

        deployment_spec = swarm_manager.deploy_swarm(
            swarm_id=swarm_id,
            title=title,
            topology=topology,
            model=model,
            color=color,
            roles=roles if isinstance(roles, list) and roles else None,
            allow_cross_swarm=allow_cross_swarm,
            directive=directive,
        )
        self._send_json({"swarm": deployment_spec.to_dict(role_registry), "swarms": swarm_manager.list_dicts()})

    def _route_api_swarms_delete(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return

        swarm_id = str(body.get("swarm_id", "")).strip()
        try:
            validate_tenant_id(swarm_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        deleted = swarm_manager.delete_swarm(swarm_id)
        self._send_json({"deleted": deleted, "swarms": swarm_manager.list_dicts()})

    def _route_api_roles(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return

        name = str(body.get("name", "")).strip()
        title = str(body.get("title", "")).strip()
        instructions = str(body.get("instructions", "")).strip()
        capabilities = body.get("capabilities", ["@web", "@code"])
        color = str(body.get("color", "#00f0ff")).strip()
        raw_max_steps = body.get("max_steps")
        try:
            max_steps = int(raw_max_steps) if raw_max_steps is not None else None
        except (ValueError, TypeError):
            max_steps = None

        if not name or not title:
            self._send_json({"error": "Role name and title are required."}, 400)
            return

        spec = role_registry.register_role(
            name=name,
            title=title,
            instructions=instructions,
            capabilities=capabilities if isinstance(capabilities, list) else ["@web", "@code"],
            color=color,
            max_steps=max_steps,
        )
        bus.emit("role_created", agent_name="system", tenant_id="default", role=spec.to_dict())
        self._send_json({"role": spec.to_dict(), "roles": role_registry.list_dicts()})

    def _route_api_roles_delete(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return

        name = str(body.get("name", "")).strip()
        deleted = role_registry.delete_role(name)
        bus.emit("role_deleted", agent_name="system", tenant_id="default", name=name)
        self._send_json({"deleted": deleted, "roles": role_registry.list_dicts()})

    def _route_api_evals(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return

        from evals import run_eval_suite

        scorecard = run_eval_suite(bus=bus)
        lines = [
            f"{'✅' if r['status'] == 'PASS' else '❌'} {r['name']} ({r['ms']}ms) — {r['detail']}"
            for r in scorecard["results"]
        ]
        summary_text = (
            f"STEP 5 EVALUATION & REGRESSION SCORECARD: {scorecard['passed']}/{scorecard['total']} "
            f"Passed ({scorecard['pass_rate']}) in {scorecard['total_ms']}ms\n\n"
            + "\n".join(lines)
        )
        bus.emit(
            "swarm_complete",
            agent_name="critic",
            tenant_id="system",
            answer=summary_text,
        )
        self._send_json(scorecard)

    def _route_api_approvals_resolve(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("user")
        if not auth:
            return

        request_id = str(body.get("request_id", "")).strip()
        decision = str(body.get("decision", "approve")).strip().lower()
        reason = str(body.get("reason", "")).strip() or None
        if not request_id or decision not in {"approve", "deny"}:
            self._send_json({"error": "request_id and decision ('approve' or 'deny') are required."}, 400)
            return

        try:
            approver_name = auth.name or auth.tenant_id or "operator"
            scope = None if auth.has_scope("admin") else auth.tenant_id
            resolve = GLOBAL_APPROVAL_GATE.approve if decision == "approve" else GLOBAL_APPROVAL_GATE.deny
            rec = resolve(request_id, approver=approver_name, reason=reason, tenant_id=scope)
            self._send_json({
                "request_id": rec.request_id,
                "status": rec.status,
                "approver": rec.approver,
                "pending_approvals": GLOBAL_APPROVAL_GATE.list_pending(rec.tenant_id),
                "approval_audit": GLOBAL_APPROVAL_GATE.get_audit_trail(limit=10, tenant_id=rec.tenant_id),
            })
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 404 if "not found" in str(exc) else 400)

    def _route_api_missions_execute(self, body: Dict[str, Any]) -> None:
        self._handle_mission_post(urlparse(self.path).path, body)

    def _route_api_missions_plan(self, body: Dict[str, Any]) -> None:
        self._handle_mission_plan(body)

    def _route_api_skills_approve(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return
        try:
            skill_id = int(body.get("skill_id", ""))
        except (TypeError, ValueError):
            self._send_json({"error": "skill_id must be an integer."}, 400)
            return
        if not storage.approve_global_skill(skill_id):
            self._send_json({"error": f"Skill {skill_id} not found."}, 404)
            return
        self._send_json({"skill_id": skill_id, "approved": True})

    def _route_api_airlock_test(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("admin")
        if not auth:
            return

        category = str(body.get("category", "security"))
        problem = str(body.get("problem", ""))
        solution = str(body.get("solution", ""))
        raw_tenant = str(body.get("tenant_id", auth.tenant_id)).strip()
        try:
            tenant_id = validate_tenant_id(raw_tenant)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        sanitized = storage.add_global_skill(category, problem, solution)
        bus.emit("skill_learned", agent_name="privacy_airlock", tenant_id=tenant_id, **sanitized)
        self._send_json({"sanitized": sanitized})

    def _route_api_memory(self, body: Dict[str, Any]) -> None:
        auth = self._authenticate("user")
        if not auth:
            return

        if auth.has_scope("admin"):
            raw_tenant = str(body.get("tenant_id", "client_a")).strip()
            try:
                tenant_id = validate_tenant_id(raw_tenant)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
        else:
            tenant_id = auth.tenant_id

        key = str(body.get("key", "")).strip()
        fact = str(body.get("fact", "")).strip()
        if key and fact:
            storage.save_tenant_memory(tenant_id, key, fact)
        self._send_json({"tenant_memories": storage.get_tenant_memories(tenant_id)})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        handler = self.GET_ROUTES.get(path)
        if handler:
            getattr(self, handler)(parsed)
            return

        self._send_json({"error": "Not found"}, 404)

    def _handle_self_heal_post(self, path: str, body: Dict[str, Any]) -> bool:
        if not path.startswith("/api/self-heal/"):
            return False
        auth = self._authenticate("admin")
        if not auth:
            return True
        if path == "/api/self-heal/run":
            rules = body.get("rules")
            try:
                max_fixes = max(1, min(5, int(body.get("max_fixes", 3) or 3)))
            except (TypeError, ValueError):
                max_fixes = 3
            started = self_healer.start_cycle_async(
                mission_id=str(body.get("mission_id", "")).strip() or None,
                max_fixes=max_fixes,
                verify=bool(body.get("verify", True)),
                rules=rules if isinstance(rules, list) and rules else None,
            )
            self._send_json(
                {"started": started, "message": "Self-heal cycle started." if started else "A cycle is already running."},
                200 if started else 409,
            )
            return True
        if path == "/api/self-heal/guidelines":
            content = str(body.get("guidelines", "")).strip()
            if not content:
                self._send_json({"error": "Guidelines content cannot be empty."}, 400)
                return True
            self_healer.save_guidelines(content)
            self._send_json({"status": "saved"})
            return True
        if path == "/api/self-heal/restore":
            ref = str(body.get("ref", "")).strip()
            if not ref:
                self._send_json({"error": "Git ref is required."}, 400)
                return True
            if self_healer.running:
                self._send_json({"error": "Cannot restore while a heal cycle is running."}, 409)
                return True
            ok, msg = self_healer.git.restore(ref)
            self._send_json({"restored": ok, "message": msg}, 200 if ok else 400)
            return True
        return False

    def _mission_request(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Authenticate and authorize a mission body. Sends the error response and returns None on refusal."""
        auth = self._authenticate("user")
        if not auth:
            return None

        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            self._send_json({"error": "Prompt is required."}, 400)
            return None

        is_admin = auth.has_scope("admin")
        auto_approve = bool(body.get("auto_approve", False))
        allow_self_heal = bool(body.get("allow_self_heal", False))
        if (auto_approve or allow_self_heal) and not is_admin:
            self._send_json({"error": "auto_approve and allow_self_heal require the admin scope."}, 403)
            return None

        preset_spec = body.get("spec")
        if preset_spec is not None and not isinstance(preset_spec, dict):
            self._send_json({"error": "spec must be an object."}, 400)
            return None

        tenant_id = auth.tenant_id
        if is_admin:
            try:
                tenant_id = validate_tenant_id(str(body.get("tenant_id", "default")).strip() or "default")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return None

        mode = str(body.get("mode", "")).strip().lower()
        return {
            "prompt": prompt,
            "tenant_id": tenant_id,
            "mode": mode,
            "auto_approve": auto_approve,
            "allow_self_heal": allow_self_heal,
            "preset_spec": preset_spec,
            "topology_override": None if mode in ("", "auto") else mode,
        }

    def _acquire_mission_slot(self, tenant_id: str) -> Optional[Any]:
        """Apply rate, spend, and concurrency governance. Returns a held tracker, or None after refusing."""
        try:
            cost_governor.check_rate_limit(tenant_id)
            cost_governor.check_daily_spend(tenant_id)
            tracker = cost_governor.track_concurrency(tenant_id)
            tracker.__enter__()
            return tracker
        except RateLimitExceededError as rle:
            self._send_json({"error": str(rle), "retry_after": rle.retry_after}, 429)
        except DailyBudgetExceededError as dbe:
            self._send_json({"error": str(dbe), "current_spend": dbe.current_spend, "max_spend": dbe.max_spend}, 429)
        except ConcurrencyLimitExceededError as cle:
            self._send_json({"error": str(cle)}, 409)
        return None

    @staticmethod
    def _mission_runner() -> IntegratedMissionRunner:
        return IntegratedMissionRunner(
            llm=llm,
            checkpoint_store=checkpoint_store,
            approval_gate=GLOBAL_APPROVAL_GATE,
            bus=bus,
            storage=storage,
        )

    @staticmethod
    def _execute_mission(request: Dict[str, Any]) -> Any:
        return SwarmRequestHandler._mission_runner().execute(
            prompt=request["prompt"],
            tenant_id=request["tenant_id"],
            auto_approve_high_risk=request["auto_approve"],
            topology_override=request["topology_override"],
            allow_self_heal=request["allow_self_heal"],
            preset_spec=request["preset_spec"],
        )

    def _handle_mission_plan(self, body: Dict[str, Any]) -> None:
        """Design a swarm without running it. Planning spends tokens, so it is governed like a mission."""
        request = self._mission_request(body)
        if not request:
            return
        tracker = self._acquire_mission_slot(request["tenant_id"])
        if not tracker:
            return
        try:
            spec = self._mission_runner().plan(
                request["prompt"],
                tenant_id=request["tenant_id"],
                topology_override=request["topology_override"],
                allow_self_heal=request["allow_self_heal"],
            )
            self._send_json({"spec": spec.to_dict()})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception:
            logger.exception("Mission planning failed for tenant %s", request["tenant_id"])
            self._send_json({"error": "Mission planning failed. See server logs."}, 500)
        finally:
            tracker.__exit__(None, None, None)

    def _handle_mission_post(self, path: str, body: Dict[str, Any]) -> None:
        request = self._mission_request(body)
        if not request:
            return
        tracker = self._acquire_mission_slot(request["tenant_id"])
        if not tracker:
            return

        if bool(body.get("async", path == "/api/run")):
            worker = threading.Thread(
                target=self._run_mission_async,
                args=(request, bool(body.get("simulate", False)), tracker),
                daemon=True,
            )
            worker.start()
            self._send_json({"status": "started", "tenant_id": request["tenant_id"], "mode": request["mode"] or "auto"})
            return

        try:
            result = self._execute_mission(request)
            self._send_json({
                "mission_id": result.mission_id,
                "tenant_id": result.tenant_id,
                "status": result.status,
                "spec": result.spec,
                "output": result.output,
                "checkpoints_count": result.checkpoints_count,
                "latest_checkpoint_hash": result.latest_checkpoint_hash,
                "audit_events": result.audit_events,
            })
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception:
            logger.exception("Mission execution failed for tenant %s", request["tenant_id"])
            self._send_json({"error": "Mission execution failed. See server logs."}, 500)
        finally:
            tracker.__exit__(None, None, None)

    @staticmethod
    def _run_mission_async(request: Dict[str, Any], simulate: bool, tracker: Any) -> None:
        tenant_id = request["tenant_id"]
        mode = request["mode"] or "auto"
        try:
            if simulate:
                bus.emit("mission_start", agent_name="system", tenant_id=tenant_id, mode=mode, prompt=request["prompt"], simulate=True)
                run_cinematic_simulation(bus, budget, role_registry, storage, tenant_id, request["prompt"])
                reply = "Simulation complete."
            else:
                res = SwarmRequestHandler._execute_mission(request)
                reply = res.output
                bus.emit(
                    "swarm_complete",
                    agent_name="coordinator",
                    tenant_id=tenant_id,
                    answer=reply,
                    mission_id=res.mission_id,
                    checkpoints_count=res.checkpoints_count,
                    latest_checkpoint_hash=res.latest_checkpoint_hash,
                )
            bus.emit("mission_end", agent_name="system", tenant_id=tenant_id, mode=mode, reply=reply)
        except Exception as exc:
            logger.exception("Async mission failed for tenant %s", tenant_id)
            bus.emit("mission_error", agent_name="system", tenant_id=tenant_id, error=str(exc)[:400])
        finally:
            tracker.__exit__(None, None, None)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"error": "Invalid Content-Length header."}, 400)
            return

        if length < 0 or length > CONFIG.max_body_bytes:
            self._send_json(
                {"error": f"Request payload exceeds limit ({CONFIG.max_body_bytes} bytes)."},
                413,
            )
            return

        raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            body = {}

        if self._handle_self_heal_post(parsed.path, body):
            return

        handler = self.POST_ROUTES.get(parsed.path)
        if handler:
            getattr(self, handler)(body)
            return

        self._send_json({"error": "Unknown endpoint"}, 404)


def _install_error_log(server: ThreadingHTTPServer) -> None:
    """Instrumentation only: mirror unhandled request tracebacks into workspace/server_errors.log
    so the Self-Healer can measure runtime reliability (behaviour is otherwise unchanged)."""
    import traceback

    log_path = CONFIG.workspace_dir / "server_errors.log"
    log_path.touch(exist_ok=True)
    original = server.handle_error

    def handle_error(request: Any, client_address: Any) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.time():.3f}] TRACEBACK client={client_address}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        original(request, client_address)

    server.handle_error = handle_error  # type: ignore[method-assign]


def main() -> None:
    check_host_binding_security(CONFIG.host, CONFIG.auth_enabled, auth_manager)
    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), SwarmRequestHandler)
    _install_error_log(server)
    print("=" * 72)
    print(" NEURAL SWARM // EVENT DASHBOARD SERVER")
    print(f" URL:      http://{CONFIG.host}:{CONFIG.port}")
    print(f" Model:    {CONFIG.model_name} ({CONFIG.api_base_url})")
    print(f" Roles:    {', '.join(r.name for r in role_registry.list_roles())}")
    print(f" Auth:     {'ENABLED (' + str(len(auth_manager.list_records())) + ' keys)' if CONFIG.auth_enabled else 'DISABLED'}")
    print(f" Database: {CONFIG.db_path}")
    print("=" * 72)
    print("Press Ctrl+C to stop the server.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
