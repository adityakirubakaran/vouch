"""Claude access for every agent.

Two entry points:
  * structured(): one forced tool call whose input must validate against a
    Pydantic model. Used for extraction, transcript parsing, bank replies, memos.
  * voice_turn(): a streamed turn for the live phone agent. Text deltas are
    spoken as they arrive; an optional end_call tool use closes the call.

FakeLLM implements the same interface with deterministic handlers so the whole
workflow runs (and is tested) without an API key.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger("vouch.llm")

T = TypeVar("T", bound=BaseModel)


def inline_schema(model: Type[BaseModel]) -> dict:
    """Pydantic emits $defs/$ref; tool input schemas are simpler and more reliable
    when fully inlined."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                merged = copy.deepcopy(defs[name])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                merged.update(extra)
                return resolve(merged)
            # drop schema "title" annotations, but keep a property that happens to be named title
            return {k: resolve(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


@dataclass
class VoiceEvent:
    kind: str  # "text" | "tool" | "done"
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: str = ""
    assistant_content: list = field(default_factory=list)  # full content blocks for history


class LLMError(RuntimeError):
    pass


class LLM:
    mode = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def model_for(self, role: str) -> str:
        return self.settings.model_fast if role == "fast" else self.settings.model_smart

    async def structured(
        self,
        *,
        role: str,
        system: str,
        content: list[dict],
        output: Type[T],
        tool_name: str,
        tool_description: str,
        max_tokens: int = 4000,
    ) -> T:
        raise NotImplementedError

    def voice_turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> AsyncIterator[VoiceEvent]:
        raise NotImplementedError


class AnthropicLLM(LLM):
    mode = "anthropic"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)

    async def structured(
        self,
        *,
        role: str,
        system: str,
        content: list[dict],
        output: Type[T],
        tool_name: str,
        tool_description: str,
        max_tokens: int = 4000,
    ) -> T:
        tool = {"name": tool_name, "description": tool_description, "input_schema": inline_schema(output)}
        messages: list[dict] = [{"role": "user", "content": content}]
        last_error: Optional[str] = None
        for attempt in range(2):
            resp = await self.client.messages.create(
                model=self.model_for(role),
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
            )
            block = next((b for b in resp.content if b.type == "tool_use"), None)
            if block is None:
                last_error = "model did not call the tool"
                continue
            try:
                return output.model_validate(block.input)
            except ValidationError as exc:
                last_error = str(exc)
                log.warning("structured output failed validation (attempt %s): %s", attempt + 1, exc)
                messages = messages + [
                    {"role": "assistant", "content": [b.model_dump() for b in resp.content]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "is_error": True,
                                "content": f"Validation failed, call {tool_name} again with corrected input: {exc}",
                            }
                        ],
                    },
                ]
        raise LLMError(f"{tool_name}: {last_error}")

    async def voice_turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> AsyncIterator[VoiceEvent]:
        async with self.client.messages.stream(
            model=self.model_for("fast"),
            max_tokens=400,
            system=system,
            messages=messages,
            tools=tools,
        ) as stream:
            async for text in stream.text_stream:
                yield VoiceEvent(kind="text", text=text)
            final = await stream.get_final_message()
        blocks = [b.model_dump() for b in final.content]
        for b in final.content:
            if b.type == "tool_use":
                yield VoiceEvent(kind="tool", tool_name=b.name, tool_input=dict(b.input), tool_use_id=b.id, assistant_content=blocks)
        yield VoiceEvent(kind="done", assistant_content=blocks)


# ---------------------------------------------------------------------------
# Offline fake

FakeHandler = Callable[[str, list[dict]], dict]
FakeVoice = Callable[[str, list[dict]], tuple[str, Optional[dict]]]

FAKE_HANDLERS: dict[str, FakeHandler] = {}
FAKE_VOICE: dict[str, FakeVoice] = {}


def fake_handler(tool_name: str) -> Callable[[FakeHandler], FakeHandler]:
    def register(fn: FakeHandler) -> FakeHandler:
        FAKE_HANDLERS[tool_name] = fn
        return fn

    return register


def fake_voice(persona: str) -> Callable[[FakeVoice], FakeVoice]:
    def register(fn: FakeVoice) -> FakeVoice:
        FAKE_VOICE[persona] = fn
        return fn

    return register


class FakeLLM(LLM):
    """Deterministic stand-in for Claude. Each agent registers a handler that
    produces plausible output from its inputs; nothing here calls the network."""

    mode = "fake"

    async def structured(
        self,
        *,
        role: str,
        system: str,
        content: list[dict],
        output: Type[T],
        tool_name: str,
        tool_description: str,
        max_tokens: int = 4000,
    ) -> T:
        handler = FAKE_HANDLERS.get(tool_name)
        if handler is None:
            raise LLMError(f"offline mode has no handler for {tool_name}")
        return output.model_validate(handler(system, content))

    async def voice_turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> AsyncIterator[VoiceEvent]:
        voice = FAKE_VOICE.get("agent")
        if voice is None:
            raise LLMError("offline mode has no voice script")
        text, tool = voice(system, messages)
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
            for word in text.split(" "):
                yield VoiceEvent(kind="text", text=word + " ")
        if tool:
            tool_id = f"toolu_fake_{len(messages)}"
            blocks.append({"type": "tool_use", "id": tool_id, "name": tool["name"], "input": tool["input"]})
            yield VoiceEvent(kind="tool", tool_name=tool["name"], tool_input=tool["input"], tool_use_id=tool_id, assistant_content=blocks)
        yield VoiceEvent(kind="done", assistant_content=blocks)


def build_llm(settings: Settings) -> LLM:
    if settings.llm_mode == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMError("VOUCH_LLM=anthropic but ANTHROPIC_API_KEY is not set")
        return AnthropicLLM(settings)
    return FakeLLM(settings)


def text_of(content: list[dict]) -> str:
    """Concatenate the text blocks of a message content list."""
    out = []
    for block in content:
        if block.get("type") == "text":
            out.append(block["text"])
        elif block.get("type") == "tool_result":
            c = block.get("content")
            out.append(c if isinstance(c, str) else json.dumps(c))
    return "\n".join(out)
