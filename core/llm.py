"""
core/llm.py - Decoupled LLM Provider Interface & Adapters.

Agents talk only to `LLMProvider.generate(...)`. You can swap between:
- Local Ollama (`http://localhost:11434/v1`)
- Hugging Face Serverless Inference (`https://router.huggingface.co/v1`)
- Groq / Gemini / OpenAI
without changing a single line of agent or swarm code.
"""

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional, Protocol
from openai import BadRequestError, OpenAI


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_message: Optional[Any] = None
    used_react_fallback: bool = False


class LLMProvider(Protocol):
    """Abstract interface for any model backend."""

    model: str

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse: ...


def parse_react_tool_calls(text: str) -> List[ToolCallRequest]:
    """Extract {"tool": ..., "args": ...} JSON objects from raw model output."""
    decoder = json.JSONDecoder()
    calls: List[ToolCallRequest] = []
    idx = 0
    call_num = 1
    while idx < len(text):
        if text[idx] == "{":
            try:
                data, end_offset = decoder.raw_decode(text[idx:])
                if isinstance(data, dict) and "tool" in data:
                    calls.append(
                        ToolCallRequest(
                            id=f"react_call_{call_num}",
                            name=str(data["tool"]),
                            arguments=data.get("args") or data.get("arguments") or {},
                        )
                    )
                    call_num += 1
                    idx += end_offset
                    continue
            except json.JSONDecodeError:
                pass
        idx += 1
    return calls


def format_react_tool_instructions(tools: List[Dict[str, Any]]) -> str:
    """Build prompt instructions for models that lack native API tool templates."""
    tools_desc = []
    for schema in tools:
        fn = schema["function"]
        props = fn["parameters"].get("properties", {})
        args_str = ", ".join(f'"{k}": <{v.get("type", "string")}>' for k, v in props.items())
        tools_desc.append(f'- {fn["name"]}: {fn["description"]} Args: {{{args_str}}}')
    tools_block = "\n".join(tools_desc)
    return (
        "\n\nAVAILABLE TOOLS:\n"
        f"{tools_block}\n\n"
        "HOW TO CALL A TOOL:\n"
        "If you need to use a tool, reply with ONLY a JSON object in this exact format:\n"
        '{"tool": "<tool_name>", "args": {<arguments>}}\n'
        "When you do NOT need to call a tool (or after receiving the Observation), "
        "reply directly in plain text."
    )


class OpenAICompatibleProvider:
    """Adapter for Ollama, Hugging Face Router, Groq, Gemini, and OpenAI."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.native_tools_supported: Optional[bool] = None

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        if tools and self.native_tools_supported is not False:
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                )
                self.native_tools_supported = True
                msg = resp.choices[0].message
                usage = getattr(resp, "usage", None)
                p_toks = getattr(usage, "prompt_tokens", 0) or 0
                c_toks = getattr(usage, "completion_tokens", 0) or 0

                parsed_calls: List[ToolCallRequest] = []
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        parsed_calls.append(
                            ToolCallRequest(
                                id=tc.id,
                                name=tc.function.name,
                                arguments=args,
                            )
                        )

                return LLMResponse(
                    content=msg.content or "",
                    tool_calls=parsed_calls,
                    prompt_tokens=p_toks,
                    completion_tokens=c_toks,
                    raw_message=msg,
                    used_react_fallback=False,
                )
            except BadRequestError as exc:
                if "does not support tools" in str(exc).lower():
                    self.native_tools_supported = False
                else:
                    raise

        # Fallback: ReAct JSON prompt mode
        react_messages = [dict(m) for m in messages]
        if tools and react_messages and react_messages[0].get("role") == "system":
            react_messages[0]["content"] = (
                react_messages[0]["content"] + format_react_tool_instructions(tools)
            )

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=react_messages,
            temperature=0.1,
            stop=["Observation:"],
        )
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        usage = getattr(resp, "usage", None)
        p_toks = getattr(usage, "prompt_tokens", 0) or 0
        c_toks = getattr(usage, "completion_tokens", 0) or 0

        parsed_calls = parse_react_tool_calls(content) if tools else []
        return LLMResponse(
            content=content,
            tool_calls=parsed_calls,
            prompt_tokens=p_toks,
            completion_tokens=c_toks,
            raw_message=msg,
            used_react_fallback=True,
        )
