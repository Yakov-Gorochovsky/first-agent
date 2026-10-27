"""
core/config.py - Centralized Configuration Loader.

Loads structured settings from `config/settings.json` and overlays any environment
variables from `.env`, eliminating hardcoded magic numbers, URLs, and prompts across
the codebase.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

CONFIG_DIR = ROOT_DIR / "config"
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", str(ROOT_DIR / "workspace")))
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULT_ROLES_FILE = CONFIG_DIR / "default_roles.json"
DEFAULT_SWARMS_FILE = CONFIG_DIR / "default_swarms.json"


def _load_raw_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            import logging
            logging.getLogger("SwarmConfig").warning(
                "Failed to parse %s: %s. Using default settings.", SETTINGS_FILE, exc
            )
    return {}


@dataclass
class SwarmConfig:
    """Single source of truth for all runtime parameters (JSON config + .env overrides)."""

    # LLM settings
    api_base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model_name: str = "qwen3:8b"
    fast_thinking: bool = True
    react_temperature: float = 0.1
    verbose: bool = True

    # Budget settings
    max_swarm_steps: int = 64
    max_coordinator_steps: int = 24
    max_steps_per_agent: int = 16
    max_total_tokens: int = 300000
    max_wall_seconds: float = 900.0

    # Storage settings
    workspace_dir: Path = field(default_factory=lambda: WORKSPACE_DIR)
    db_path: Path = field(default_factory=lambda: WORKSPACE_DIR / "swarm.db")
    roles_path: Path = field(default_factory=lambda: WORKSPACE_DIR / "roles.json")
    swarms_path: Path = field(default_factory=lambda: WORKSPACE_DIR / "swarms.json")
    keys_path: Path = field(default_factory=lambda: WORKSPACE_DIR / "keys.json")
    default_roles_path: Path = field(default_factory=lambda: DEFAULT_ROLES_FILE)
    auth_enabled: bool = True
    max_trace_rows: int = 500
    max_session_turns: int = 30
    context_session_turns: int = 10
    max_global_skills: int = 10

    # Tool sandbox & limits
    weather_timeout_sec: int = 8
    search_timeout_sec: int = 10
    search_max_results: int = 5
    fetch_timeout_sec: int = 10
    fetch_max_chars: int = 4000
    python_timeout_sec: int = 15
    python_max_output_chars: int = 4000
    calc_max_exponent: int = 1000
    python_sandbox_mode: str = "auto"
    unsafe_allow_local_exec: bool = False
    secret_key: Optional[str] = None

    # Server settings
    host: str = "127.0.0.1"
    port: int = 8000
    max_body_bytes: int = 65536
    sse_queue_size: int = 200
    sse_heartbeat_sec: int = 15

    # Bundles & Coordinator prompts
    capability_bundles: Dict[str, List[str]] = field(default_factory=dict)
    orchestrator_prompt: str = ""
    coordinator_prompt: str = ""

    @classmethod
    def load(cls) -> "SwarmConfig":
        raw = _load_raw_settings()
        llm_cfg = raw.get("llm", {})
        budget_cfg = raw.get("budget", {})
        storage_cfg = raw.get("storage", {})
        tools_cfg = raw.get("tools", {})
        server_cfg = raw.get("server", {})
        bundles_cfg = raw.get("capability_bundles", {})
        prompts_cfg = raw.get("prompts", {})

        max_steps_env = int(os.getenv("MAX_STEPS", str(budget_cfg.get("max_steps_per_agent", 16))))
        max_coord_env = int(
            os.getenv("MAX_COORDINATOR_STEPS", str(budget_cfg.get("max_coordinator_steps", 24)))
        )
        max_swarm_env = int(
            os.getenv("MAX_SWARM_STEPS", str(budget_cfg.get("max_swarm_steps", 64)))
        )

        db_filename = os.getenv("DB_FILENAME", str(storage_cfg.get("db_filename", "swarm.db")))

        return cls(
            api_base_url=os.getenv("API_BASE_URL", llm_cfg.get("api_base_url", "http://localhost:11434/v1")),
            api_key=os.getenv("API_KEY", llm_cfg.get("api_key", "ollama")),
            model_name=os.getenv("MODEL_NAME", llm_cfg.get("model_name", "qwen3:8b")),
            fast_thinking=os.getenv("FAST_THINKING", str(llm_cfg.get("fast_thinking", True))).lower() == "true",
            react_temperature=float(os.getenv("REACT_TEMPERATURE", str(llm_cfg.get("react_temperature", 0.1)))),
            verbose=os.getenv("VERBOSE", "true").lower() == "true",
            max_swarm_steps=max_swarm_env,
            max_coordinator_steps=max_coord_env,
            max_steps_per_agent=max_steps_env,
            max_total_tokens=int(os.getenv("MAX_TOTAL_TOKENS", str(budget_cfg.get("max_total_tokens", 300000)))),
            max_wall_seconds=float(os.getenv("MAX_WALL_SECONDS", str(budget_cfg.get("max_wall_seconds", 900.0)))),
            db_path=WORKSPACE_DIR / db_filename,
            roles_path=WORKSPACE_DIR / "roles.json",
            default_roles_path=DEFAULT_ROLES_FILE,
            max_trace_rows=int(os.getenv("MAX_TRACE_ROWS", str(storage_cfg.get("max_trace_rows", 500)))),
            max_session_turns=int(storage_cfg.get("max_session_turns", 30)),
            context_session_turns=int(storage_cfg.get("context_session_turns", 10)),
            max_global_skills=int(storage_cfg.get("max_global_skills", 10)),
            weather_timeout_sec=int(tools_cfg.get("weather_timeout_sec", 8)),
            search_timeout_sec=int(tools_cfg.get("search_timeout_sec", 10)),
            search_max_results=int(tools_cfg.get("search_max_results", 5)),
            fetch_timeout_sec=int(tools_cfg.get("fetch_timeout_sec", 10)),
            fetch_max_chars=int(tools_cfg.get("fetch_max_chars", 4000)),
            python_timeout_sec=int(os.getenv("PYTHON_TIMEOUT_SEC", str(tools_cfg.get("python_timeout_sec", 15)))),
            python_max_output_chars=int(tools_cfg.get("python_max_output_chars", 4000)),
            calc_max_exponent=int(tools_cfg.get("calc_max_exponent", 1000)),
            python_sandbox_mode=os.getenv("PYTHON_SANDBOX_MODE", str(tools_cfg.get("python_sandbox_mode", "auto"))).lower(),
            unsafe_allow_local_exec=os.getenv("UNSAFE_ALLOW_LOCAL_EXEC", str(tools_cfg.get("unsafe_allow_local_exec", "false"))).lower() == "true",
            secret_key=os.getenv("SECRET_KEY", os.getenv("CHECKPOINT_HMAC_SECRET", None)),
            host=os.getenv("HOST", str(server_cfg.get("host", "127.0.0.1"))),
            port=int(os.getenv("PORT", str(server_cfg.get("port", 8000)))),
            max_body_bytes=int(server_cfg.get("max_body_bytes", 65536)),
            sse_queue_size=int(server_cfg.get("sse_queue_size", 200)),
            sse_heartbeat_sec=int(server_cfg.get("sse_heartbeat_sec", 15)),
            capability_bundles=dict(bundles_cfg),
            orchestrator_prompt=str(prompts_cfg.get("orchestrator", "")),
            coordinator_prompt=str(prompts_cfg.get("coordinator", "")),
            keys_path=WORKSPACE_DIR / "keys.json",
            auth_enabled=os.getenv("AUTH_ENABLED", str(server_cfg.get("auth_enabled", "true"))).lower() == "true",
        )


CONFIG = SwarmConfig.load()
