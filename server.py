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

import json
import queue
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from core.auth import APIKeyRecord, AuthManager
from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus, SwarmEvent
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
from core.storage import SQLiteStorage, validate_tenant_id
from core.swarm_manager import SwarmManager

STATIC_DIR = Path(__file__).parent / "static"

# Shared application state initialized from centralized CONFIG
bus = EventBus()
storage = SQLiteStorage()
role_registry = RoleRegistry()
budget = BudgetTracker(config=BudgetConfig(), bus=bus)
cost_governor = TenantCostGovernor(
    storage=storage,
    default_rpm=float(CONFIG.max_steps_per_agent * 10),
    default_burst=10.0,
    max_concurrency_per_tenant=1,
    max_daily_usd_per_tenant=15.0,
)
llm = OpenAICompatibleProvider()
swarm_manager = SwarmManager(
    storage=storage, bus=bus, role_registry=role_registry, default_llm=llm
)
auth_manager = AuthManager(CONFIG.keys_path)


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


def _run_cinematic_simulation(tenant_id: str, prompt: str) -> None:
    """Emit a realistic multi-agent peer-network flow using active roles from RoleRegistry."""
    budget.reset()
    budget.tenant_id = tenant_id
    roles = role_registry.list_roles()
    r1 = roles[0].name if len(roles) > 0 else "researcher"
    r2 = roles[1].name if len(roles) > 1 else r1
    r3 = roles[-1].name if len(roles) > 2 else r2

    def step_sleep(sec: float = 0.85) -> None:
        time.sleep(sec)

    bus.emit(
        "peer_message",
        agent_name="user",
        tenant_id=tenant_id,
        from_agent="user",
        to_agent="coordinator",
        message=prompt,
        inboxes={"coordinator": 1},
    )
    step_sleep(0.7)

    bus.emit("agent_start", agent_name="coordinator", tenant_id=tenant_id, task=prompt)
    budget.check_and_increment_step("coordinator")
    budget.record_tokens("coordinator", 310, 95)
    step_sleep(0.8)

    bus.emit(
        "peer_message",
        agent_name="coordinator",
        tenant_id=tenant_id,
        from_agent="coordinator",
        to_agent=r1,
        message=f"Fetch live telemetry & benchmarks, then forward raw metrics directly to '{r2}' inbox.",
        inboxes={"coordinator": 0, r1: 1},
    )
    step_sleep(0.9)

    bus.emit("agent_start", agent_name=r1, tenant_id=tenant_id, task="Fetch live telemetry")
    budget.check_and_increment_step(r1)
    bus.emit(
        "tool_call",
        agent_name=r1,
        tenant_id=tenant_id,
        tool="web_search",
        arguments={"query": prompt[:60]},
    )
    step_sleep(0.9)
    bus.emit(
        "tool_result",
        agent_name=r1,
        tenant_id=tenant_id,
        tool="web_search",
        result="Found live benchmark data: latency_p95=42ms, throughput=14,200 req/s, temp=23C.",
    )
    budget.record_tokens(r1, 480, 140)
    step_sleep(0.7)

    bus.emit(
        "peer_message",
        agent_name=r1,
        tenant_id=tenant_id,
        from_agent=r1,
        to_agent=r2,
        message=f"Raw telemetry ready (p95=42ms, 14200 rps). Compute efficiency score and forward to '{r3}'.",
        inboxes={r1: 0, r2: 1},
    )
    step_sleep(0.9)

    bus.emit("agent_start", agent_name=r2, tenant_id=tenant_id, task="Run Python analysis")
    budget.check_and_increment_step(r2)
    bus.emit(
        "tool_call",
        agent_name=r2,
        tenant_id=tenant_id,
        tool="run_python",
        arguments={"code": "rps, p95 = 14200, 42\nscore = round((rps / p95) * 1.15, 2)\nprint(f'Efficiency Index: {score}')"},
    )
    step_sleep(0.9)
    bus.emit(
        "tool_result",
        agent_name=r2,
        tenant_id=tenant_id,
        tool="run_python",
        result="STDOUT:\nEfficiency Index: 388.81",
    )
    budget.record_tokens(r2, 520, 165)

    skill = storage.add_global_skill(
        category="telemetry_analysis",
        problem=f"Client {tenant_id} endpoint https://api.{tenant_id}.internal:8443 reported raw p95 metrics",
        solution="Normalize throughput-to-latency ratio (RPS / p95_ms) in Python sandbox before QA handoff.",
    )
    bus.emit("skill_learned", agent_name=r2, tenant_id=tenant_id, **skill)
    step_sleep(0.8)

    bus.emit(
        "peer_message",
        agent_name=r2,
        tenant_id=tenant_id,
        from_agent=r2,
        to_agent=r3,
        message="Computed Efficiency Index = 388.81 via Python sandbox. Please verify accuracy.",
        inboxes={r2: 0, r3: 1},
    )
    step_sleep(0.9)

    bus.emit("agent_start", agent_name=r3, tenant_id=tenant_id, task="Verify calculation")
    budget.check_and_increment_step(r3)
    bus.emit(
        "tool_call",
        agent_name=r3,
        tenant_id=tenant_id,
        tool="calculator",
        arguments={"expression": "(14200 / 42) * 1.15"},
    )
    step_sleep(0.7)
    bus.emit(
        "tool_result",
        agent_name=r3,
        tenant_id=tenant_id,
        tool="calculator",
        result="(14200 / 42) * 1.15 = 388.8095238095238",
    )
    budget.record_tokens(r3, 410, 110)
    step_sleep(0.6)

    final_text = (
        f"[Peer Swarm Verified Report for '{tenant_id}']\n"
        f"1. '{r1}' gathered live benchmark metrics (14,200 req/s @ 42ms p95).\n"
        f"2. '{r2}' executed Python sandbox analysis -> Efficiency Index: 388.81.\n"
        f"3. '{r3}' independently verified the math (388.8095) and approved the output.\n"
        "4. PrivacyAirlock distilled the reusable normalization rule into Shared Global Skills."
    )
    storage.save_tenant_memory(tenant_id, "last_efficiency_index", "388.81 (Verified)")
    storage.append_session_turn(tenant_id, "user", prompt)
    storage.append_session_turn(tenant_id, "assistant", final_text)

    bus.emit("swarm_complete", agent_name=r3, tenant_id=tenant_id, answer=final_text)
    bus.emit("agent_finish", agent_name=r3, tenant_id=tenant_id, answer=final_text)


class SwarmRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'",
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/index.html"}:
            index_file = STATIC_DIR / "index.html"
            if not index_file.exists():
                self._send_json({"error": "static/index.html not found"}, 404)
                return
            content = index_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._send_security_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/api/state":
            auth = self._authenticate("user")
            if not auth:
                return

            qs = parse_qs(parsed.query)
            if auth.has_scope("admin"):
                default_id = swarm_manager.list_swarms()[0].swarm_id if swarm_manager.list_swarms() else "victim"
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
                    "model": CONFIG.model_name,
                    "endpoint": CONFIG.api_base_url,
                    "swarms": swarm_manager.list_dicts(),
                    "roles": role_registry.list_dicts(),
                    "capability_bundles": CAPABILITY_BUNDLES,
                    "tenant_memories": storage.get_tenant_memories(tenant_id),
                    "global_skills": storage.get_global_skills(limit=12),
                    "session_turns": storage.get_session_turns(tenant_id, limit=10),
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
            return

        if path == "/api/events":
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
            return

        self._send_json({"error": "Not found"}, 404)

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

        if parsed.path == "/api/swarms":
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
            return

        if parsed.path == "/api/swarms/delete":
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
            return

        if parsed.path == "/api/roles":
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
            return

        if parsed.path == "/api/roles/delete":
            auth = self._authenticate("admin")
            if not auth:
                return

            name = str(body.get("name", "")).strip()
            deleted = role_registry.delete_role(name)
            bus.emit("role_deleted", agent_name="system", tenant_id="default", name=name)
            self._send_json({"deleted": deleted, "roles": role_registry.list_dicts()})
            return

        if parsed.path == "/api/evals":
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
            return

        if parsed.path == "/api/missions/execute":
            auth = self._authenticate("user")
            if not auth:
                return

            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                self._send_json({"error": "Prompt is required."}, 400)
                return

            if auth.has_scope("admin"):
                raw_tenant = str(body.get("tenant_id", "default")).strip()
                try:
                    tenant_id = validate_tenant_id(raw_tenant)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
            else:
                tenant_id = auth.tenant_id

            try:
                runner = IntegratedMissionRunner(llm=llm, bus=bus)
                result = runner.execute(prompt=prompt, tenant_id=tenant_id)
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
            except Exception as exc:
                self._send_json({"error": f"Mission execution failed: {exc}"}, 500)
            return

        if parsed.path == "/api/run":
            auth = self._authenticate("user")
            if not auth:
                return

            prompt = str(body.get("prompt", "")).strip()
            mode = str(body.get("mode", "")).strip().lower()
            simulate = bool(body.get("simulate", False))

            if not prompt:
                self._send_json({"error": "Prompt is required"}, 400)
                return

            # Tenant derivation: Admins may target any valid tenant; user tokens are locked to their credential's tenant
            if auth.has_scope("admin"):
                raw_tenant = str(body.get("tenant_id", "attacker")).strip()
                try:
                    tenant_id = validate_tenant_id(raw_tenant)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
            else:
                tenant_id = auth.tenant_id

            # FP-03: Rate Limiting & Daily Spend Cap Governance
            try:
                cost_governor.check_rate_limit(tenant_id)
            except RateLimitExceededError as rle:
                self._send_json({"error": str(rle), "retry_after": rle.retry_after}, 429)
                return

            try:
                cost_governor.check_daily_spend(tenant_id)
            except DailyBudgetExceededError as dbe:
                self._send_json(
                    {
                        "error": str(dbe),
                        "current_spend": dbe.current_spend,
                        "max_spend": dbe.max_spend,
                    },
                    429,
                )
                return

            try:
                tracker = cost_governor.track_concurrency(tenant_id)
                tracker.__enter__()
            except ConcurrencyLimitExceededError as cle:
                self._send_json({"error": str(cle)}, 409)
                return

            def _worker() -> None:
                try:
                    bus.emit(
                        "mission_start",
                        agent_name="system",
                        tenant_id=tenant_id,
                        mode=mode or "peer",
                        prompt=prompt,
                        simulate=simulate,
                    )
                    if simulate:
                        _run_cinematic_simulation(tenant_id, prompt)
                        reply = "Simulation complete."
                    else:
                        reply = swarm_manager.run_swarm(
                            swarm_id=tenant_id,
                            prompt=prompt,
                            topology_override=mode if mode in {"peer", "orchestrator"} else None,
                        )

                    bus.emit(
                        "mission_end",
                        agent_name="system",
                        tenant_id=tenant_id,
                        mode=mode or "peer",
                        reply=reply,
                    )
                except Exception as exc:
                    bus.emit(
                        "mission_error",
                        agent_name="system",
                        tenant_id=tenant_id,
                        error=str(exc),
                    )
                finally:
                    tracker.__exit__(None, None, None)

            threading.Thread(target=_worker, daemon=True).start()
            self._send_json({"status": "started", "tenant_id": tenant_id, "mode": mode})
            return

        if parsed.path == "/api/airlock-test":
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
            return

        if parsed.path == "/api/memory":
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
            return

        self._send_json({"error": "Unknown endpoint"}, 404)


def main() -> None:
    check_host_binding_security(CONFIG.host, CONFIG.auth_enabled, auth_manager)
    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), SwarmRequestHandler)
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
