"""
agent.py - Your First AI Agent!

Works with:
- Local Ollama models (e.g., `llama3:latest`, `qwen3:8b`)
- Any OpenAI-compatible API (Groq, Gemini, OpenRouter, OpenAI)

Supports both native API tool-calling AND automatic ReAct JSON tool-calling
for models (like base `llama3:latest`) that do not have native tool templates.
"""

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from openai import OpenAI, BadRequestError

from tools import TOOL_SCHEMAS, execute_tool

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:11434/v1")
API_KEY = os.getenv("API_KEY", "ollama")
MODEL_NAME = os.getenv("MODEL_NAME", "llama3:latest")
MAX_STEPS = int(os.getenv("MAX_STEPS", "5"))
VERBOSE = os.getenv("VERBOSE", "true").lower() == "true"


SYSTEM_PROMPT = """You are a helpful, concise AI assistant with access to tools.
Use tools whenever you need accurate math, current time/date, live weather, or file operations.
Once you have the information you need from tool outputs, answer the user clearly."""


def build_react_system_prompt() -> str:
    """Build a prompt-based tool-calling instruction for models without native tool APIs."""
    tools_desc = []
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        props = fn["parameters"].get("properties", {})
        args_str = ", ".join(f'"{k}": <{v.get("type", "string")}>' for k, v in props.items())
        tools_desc.append(f'- {fn["name"]}: {fn["description"]} Args: {{{args_str}}}')
    tools_block = "\n".join(tools_desc)

    return (
        f"{SYSTEM_PROMPT}\n\n"
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
    def __init__(self) -> None:
        self.client = OpenAI(base_url=API_BASE_URL, api_key=API_KEY)
        self.model = MODEL_NAME
        self.native_tools_supported: Optional[bool] = None
        self.history: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def run(self, user_input: str) -> str:
        """Process a user message through the Think -> Act -> Observe loop."""
        if self.native_tools_supported is False:
            return self._run_react(user_input)

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
                    # Remove the just-appended user message and switch to ReAct history
                    self.history.pop()
                    self.history[0] = {
                        "role": "system",
                        "content": build_react_system_prompt(),
                    }
                    return self._run_react(user_input)
                raise

            msg = response.choices[0].message

            if not msg.tool_calls:
                answer = msg.content or ""
                self.history.append({"role": "assistant", "content": answer})
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
                    print(f"  [Observation]   {result}")

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
        if self.history[0]["content"] == SYSTEM_PROMPT:
            self.history[0]["content"] = build_react_system_prompt()

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
                    print(f"  [Observation]   {result}")

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
    args = parser.parse_args()

    print("=" * 60)
    print(f" First AI Agent | Model: {MODEL_NAME} | Endpoint: {API_BASE_URL}")
    print(" Tools: calculator, get_current_time, get_weather, save/read/list_notes")
    print("=" * 60)

    agent = Agent()

    if args.prompt:
        print(f"\nYou: {args.prompt}")
        reply = agent.run(args.prompt)
        print(f"\nAgent: {reply}\n")
        return

    print("Type your message and press Enter (or type 'exit' / 'quit' to stop).\n")
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

        try:
            reply = agent.run(user_input)
            print(f"\nAgent: {reply}\n")
        except Exception as exc:
            print(f"\n[Error] {exc}\n")


if __name__ == "__main__":
    main()
