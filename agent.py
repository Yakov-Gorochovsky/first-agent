"""
agent.py - Your First AI Agent!

Works with:
- Local Ollama models (e.g., `llama3:latest`, `qwen3:8b`)
- Any OpenAI-compatible API (Groq, Gemini, OpenRouter, OpenAI)

Supports:
- Native API tool-calling AND automatic ReAct JSON tool-calling fallback
- Universal primitives: Web Search, URL Reader, Python Code Sandbox
- Persistent Long-Term Memory (workspace/memory.json) & Session History
"""

import argparse
import json
import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from openai import OpenAI, BadRequestError

from tools import TOOL_SCHEMAS, WORKSPACE_DIR, execute_tool, load_memories

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:11434/v1")
API_KEY = os.getenv("API_KEY", "ollama")
MODEL_NAME = os.getenv("MODEL_NAME", "llama3:latest")
MAX_STEPS = int(os.getenv("MAX_STEPS", "8"))
VERBOSE = os.getenv("VERBOSE", "true").lower() == "true"

SESSION_FILE = WORKSPACE_DIR / "session_history.json"

BASE_SYSTEM_PROMPT = """You are a capable, concise AI agent with access to tools.
- Use `web_search` and `fetch_url` for live internet info or reading pages.
- Use `run_python` to write and execute Python code for data processing, complex math, or custom tasks. If your Python code errors, read the stderr and fix it.
- Use `remember_fact` whenever the user shares personal preferences, project details, or facts they want remembered across sessions.
- Use `calculator`, `get_current_time`, `get_weather`, and `save_note`/`read_note`/`list_notes` as needed.
Once you have the information you need from tool outputs, answer the user clearly."""


def get_system_prompt(react_mode: bool = False) -> str:
    """Build the system prompt, injecting any persistent long-term memories."""
    memories = load_memories()
    memory_section = ""
    if memories:
        mem_lines = "\n".join(f"- {k}: {v}" for k, v in memories.items())
        memory_section = f"\n\nPERSISTENT LONG-TERM MEMORY (Facts from previous sessions):\n{mem_lines}"

    base = BASE_SYSTEM_PROMPT + memory_section
    if not react_mode:
        return base

    tools_desc = []
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        props = fn["parameters"].get("properties", {})
        args_str = ", ".join(f'"{k}": <{v.get("type", "string")}>' for k, v in props.items())
        tools_desc.append(f'- {fn["name"]}: {fn["description"]} Args: {{{args_str}}}')
    tools_block = "\n".join(tools_desc)

    return (
        f"{base}\n\n"
        "AVAILABLE TOOLS:\n"
        f"{tools_block}\n\n"
        "HOW TO CALL A TOOL:\n"
        "If you need to use a tool, reply with ONLY a JSON object in this exact format:\n"
        '{"tool": "<tool_name>", "args": {<arguments>}}\n\n'
        "Wait for the Observation before giving your final answer. "
        "When you do NOT need to call a tool (or after receiving the Observation), "
        "reply directly in plain text to the user."
    )


def parse_react_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Extract all {"tool": ..., "args": ...} JSON objects from a model response."""
    decoder = json.JSONDecoder()
    calls: List[Dict[str, Any]] = []
    idx = 0
    while idx < len(text):
        if text[idx] == "{":
            try:
                data, end_offset = decoder.raw_decode(text[idx:])
                if isinstance(data, dict) and "tool" in data:
                    calls.append(
                        {
                            "name": str(data["tool"]),
                            "arguments": data.get("args") or data.get("arguments") or {},
                        }
                    )
                    idx += end_offset
                    continue
            except json.JSONDecodeError:
                pass
        idx += 1
    return calls


class Agent:
    def __init__(self, resume_session: bool = True) -> None:
        self.client = OpenAI(base_url=API_BASE_URL, api_key=API_KEY)
        self.model = MODEL_NAME
        self.native_tools_supported: Optional[bool] = None
        self.history: List[Dict[str, Any]] = [
            {"role": "system", "content": get_system_prompt(react_mode=False)}
        ]
        if resume_session:
            self._load_session()

    def _refresh_system_prompt(self) -> None:
        react_mode = self.native_tools_supported is False
        self.history[0] = {
            "role": "system",
            "content": get_system_prompt(react_mode=react_mode),
        }

    def _load_session(self) -> None:
        """Load persisted user/assistant turns from disk if available."""
        if not SESSION_FILE.exists():
            return
        try:
            saved = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                for msg in saved[-12:]:  # Keep last 12 conversational messages for context
                    if isinstance(msg, dict) and msg.get("role") in {"user", "assistant"}:
                        self.history.append(
                            {"role": msg["role"], "content": str(msg.get("content", ""))}
                        )
        except Exception:
            pass

    def _save_session(self) -> None:
        """Persist clean user/assistant conversation turns to disk."""
        clean_turns = [
            {"role": m["role"], "content": m["content"]}
            for m in self.history[1:]
            if isinstance(m, dict)
            and m.get("role") in {"user", "assistant"}
            and isinstance(m.get("content"), str)
        ]
        try:
            SESSION_FILE.write_text(
                json.dumps(clean_turns[-20:], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def clear_session(self) -> None:
        """Clear current conversation history while keeping long-term memory."""
        self.history = [self.history[0]]
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()

    def run(self, user_input: str) -> str:
        """Process a user message through the Think -> Act -> Observe loop."""
        self._refresh_system_prompt()

        if self.native_tools_supported is False:
            reply = self._run_react(user_input)
            self._save_session()
            return reply

        self.history.append({"role": "user", "content": user_input})

        for step in range(1, MAX_STEPS + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.history,
                    tools=TOOL_SCHEMAS,
                )
                self.native_tools_supported = True
            except BadRequestError as exc:
                if "does not support tools" in str(exc).lower():
                    if VERBOSE:
                        print(
                            f"  [Info] Model '{self.model}' uses ReAct JSON mode "
                            "(native tool template not present)."
                        )
                    self.native_tools_supported = False
                    self.history.pop()
                    self._refresh_system_prompt()
                    reply = self._run_react(user_input)
                    self._save_session()
                    return reply
                raise

            msg = response.choices[0].message

            if not msg.tool_calls:
                answer = msg.content or ""
                self.history.append({"role": "assistant", "content": answer})
                self._save_session()
                return answer

            self.history.append(msg)
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                if VERBOSE:
                    print(f"  [Tool Call #{step}] {fn_name}({fn_args})")

                result = execute_tool(fn_name, fn_args)

                if VERBOSE:
                    preview = result if len(result) <= 300 else result[:300] + " ... [truncated]"
                    print(f"  [Observation]   {preview}")

                if fn_name == "remember_fact":
                    self._refresh_system_prompt()

                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": fn_name,
                        "content": result,
                    }
                )

        return "Reached maximum agent steps without a final response."

    def _run_react(self, user_input: str) -> str:
        """Fallback ReAct loop for models without native API tool support."""
        self._refresh_system_prompt()
        self.history.append({"role": "user", "content": user_input})

        for step in range(1, MAX_STEPS + 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.history,
                temperature=0.1,
                stop=["Observation:"],
            )
            content = (response.choices[0].message.content or "").strip()
            tool_reqs = parse_react_tool_calls(content)

            if not tool_reqs:
                self.history.append({"role": "assistant", "content": content})
                return content

            observations = []
            called_list = []
            for tool_req in tool_reqs:
                fn_name = tool_req["name"]
                fn_args = tool_req["arguments"]

                if VERBOSE:
                    print(f"  [Tool Call #{step}] {fn_name}({fn_args})")

                result = execute_tool(fn_name, fn_args)

                if VERBOSE:
                    preview = result if len(result) <= 300 else result[:300] + " ... [truncated]"
                    print(f"  [Observation]   {preview}")

                if fn_name == "remember_fact":
                    self._refresh_system_prompt()

                called_list.append({"tool": fn_name, "args": fn_args})
                observations.append(f"Observation from {fn_name}: {result}")

            self.history.append(
                {"role": "assistant", "content": "\n".join(json.dumps(c) for c in called_list)}
            )
            self.history.append(
                {
                    "role": "user",
                    "content": (
                        "\n".join(observations)
                        + "\nUse the observation(s) above to give the final answer in plain text "
                        "(do NOT output JSON unless you still need another tool)."
                    ),
                }
            )

        return "Reached maximum agent steps without a final response."


def main() -> None:
    parser = argparse.ArgumentParser(description="Run your first AI Agent.")
    parser.add_argument(
        "-p",
        "--prompt",
        type=str,
        help="Run a single prompt and exit (useful for quick testing).",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Start with a fresh session history.",
    )
    args = parser.parse_args()

    print("=" * 68)
    print(f" First AI Agent | Model: {MODEL_NAME} | Endpoint: {API_BASE_URL}")
    print(" Tools: web_search, fetch_url, run_python, remember/recall, notes, weather, math")
    print("=" * 68)

    agent = Agent(resume_session=not args.new_session)
    if args.new_session:
        agent.clear_session()

    if args.prompt:
        print(f"\nYou: {args.prompt}")
        reply = agent.run(args.prompt)
        print(f"\nAgent: {reply}\n")
        return

    print("Commands: '/memory' to view saved facts | '/clear' to reset chat | 'exit' to quit\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            print("Goodbye!")
            break
        if user_input.lower() == "/clear":
            agent.clear_session()
            print("  [Session history cleared. Long-term memories are still kept.]\n")
            continue
        if user_input.lower() == "/memory":
            print(f"\n{execute_tool('recall_memories', {})}\n")
            continue

        try:
            reply = agent.run(user_input)
            print(f"\nAgent: {reply}\n")
        except Exception as exc:
            print(f"\n[Error] {exc}\n")


if __name__ == "__main__":
    main()
