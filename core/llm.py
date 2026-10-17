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
import re
from typing import Any, Dict, List, Optional, Protocol
from openai import BadRequestError, OpenAI
from core.config import CONFIG

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_tags(text: str) -> str:
    """Remove internal <think>...</think> reasoning blocks from model outputs."""
    return _THINK_TAG_RE.sub("", text or "").strip()


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

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        fast_qwen_mode: Optional[bool] = None,
    ) -> None:
        self.client = OpenAI(
            base_url=base_url or CONFIG.api_base_url,
            api_key=api_key or CONFIG.api_key,
        )
        self.model = model or CONFIG.model_name
        self.native_tools_supported: Optional[bool] = None
        self.fast_qwen_mode = CONFIG.fast_thinking if fast_qwen_mode is None else fast_qwen_mode

    def _is_local_ollama(self) -> bool:
        base = str(getattr(self.client, "base_url", "")).lower()
        return "11434" in base

    def _ollama_native_chat_url(self) -> str:
        base = str(getattr(self.client, "base_url", "http://localhost:11434/v1")).rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}/api/chat"

    def _generate_ollama_native(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        Call Ollama's native `/api/chat` endpoint, which honors `think: False` alongside
        native `tools` (reducing Qwen3 tool-call latency from ~34s to ~4s per step).
        """
        import requests

        norm_messages: List[Dict[str, Any]] = []
        for m in messages:
            if isinstance(m, dict):
                msg_copy = dict(m)
                if msg_copy.get("role") == "tool" and "name" in msg_copy and "tool_name" not in msg_copy:
                    msg_copy["tool_name"] = msg_copy["name"]
                norm_messages.append(msg_copy)
            else:
                # Convert OpenAI ChatCompletionMessage object if present
                role = getattr(m, "role", "assistant")
                content = getattr(m, "content", "") or ""
                entry: Dict[str, Any] = {"role": role, "content": content}
                raw_tcs = getattr(m, "tool_calls", None)
                if raw_tcs:
                    entry["tool_calls"] = [
                        {
                            "function": {
                                "name": tc.function.name,
                                "arguments": json.loads(tc.function.arguments or "{}")
                                if isinstance(tc.function.arguments, str)
                                else (tc.function.arguments or {}),
                            }
                        }
                        for tc in raw_tcs
                    ]
                norm_messages.append(entry)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": norm_messages,
            "think": not self.fast_qwen_mode,
            "stream": False,
            "options": {
                "temperature": CONFIG.react_temperature,
                "num_ctx": 2048,
                "num_predict": 256,
            },
        }
        if tools:
            payload["tools"] = tools

        resp = requests.post(self._ollama_native_chat_url(), json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        p_toks = int(data.get("prompt_eval_count", 0) or 0)
        c_toks = int(data.get("eval_count", 0) or 0)

        if not msg.get("content") and not msg.get("tool_calls") and payload.get("think") is False:
            payload["think"] = True
            payload["options"]["num_predict"] = 384
            resp = requests.post(self._ollama_native_chat_url(), json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            msg = data.get("message", {})
            p_toks += int(data.get("prompt_eval_count", 0) or 0)
            c_toks += int(data.get("eval_count", 0) or 0)

        parsed_calls: List[ToolCallRequest] = []
        raw_tool_calls = msg.get("tool_calls") or []
        for idx, tc in enumerate(raw_tool_calls):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = dict(raw_args or {})
            parsed_calls.append(
                ToolCallRequest(
                    id=tc.get("id") or f"call_{idx + 1}",
                    name=str(fn.get("name", "")),
                    arguments=args,
                )
            )

        content = strip_think_tags(str(msg.get("content", "") or ""))
        if not parsed_calls and tools and content:
            parsed_calls = parse_react_tool_calls(content)

        return LLMResponse(
            content=content,
            tool_calls=parsed_calls,
            prompt_tokens=p_toks,
            completion_tokens=c_toks,
            raw_message=msg,
            used_react_fallback=False,
        )

    def _prepare_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Inject /no_think for Qwen3 models when fast_qwen_mode is enabled."""
        if not self.fast_qwen_mode or "qwen3" not in self.model.lower():
            return messages
        prepared = []
        for idx, m in enumerate(messages):
            if isinstance(m, dict) and idx == 0 and m.get("role") == "system":
                content = str(m.get("content", ""))
                if "/no_think" not in content:
                    content = "/no_think\n" + content
                prepared.append({**m, "content": content})
            else:
                prepared.append(m)
        return prepared

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        if self._is_local_ollama():
            return self._generate_ollama_native(messages, tools=tools)

        prepared_messages = self._prepare_messages(messages)

        if tools and self.native_tools_supported is not False:
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=prepared_messages,
                    tools=tools,
                    temperature=CONFIG.react_temperature,
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
                    content=strip_think_tags(msg.content or ""),
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
        react_messages = [dict(m) if isinstance(m, dict) else m for m in prepared_messages]
        if tools and react_messages and isinstance(react_messages[0], dict) and react_messages[0].get("role") == "system":
            react_messages[0]["content"] = (
                react_messages[0]["content"] + format_react_tool_instructions(tools)
            )

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=react_messages,
            temperature=CONFIG.react_temperature,
            stop=["Observation:"],
        )
        msg = resp.choices[0].message
        content = strip_think_tags(msg.content or "")
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
