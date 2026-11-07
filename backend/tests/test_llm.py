"""AnthropicLLM request handling against a stubbed SDK client (no network)."""

from __future__ import annotations

import httpx
import pytest
from anthropic import BadRequestError
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from vouch.llm import AnthropicLLM, LLMError
from vouch.schemas import ReferenceFindings

from .conftest import make_settings


def message(*blocks, stop="tool_use") -> Message:
    return Message(
        id="msg_1", type="message", role="assistant", model="m", content=list(blocks),
        stop_reason=stop, stop_sequence=None, usage=Usage(input_tokens=1, output_tokens=1),
    )


def bad_request(text: str) -> BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return BadRequestError(text, response=httpx.Response(400, request=req), body={"error": {"message": text}})


class StubMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(tmp_path, script) -> tuple[AnthropicLLM, StubMessages]:
    llm = AnthropicLLM(make_settings(tmp_path, vouch_llm="anthropic", anthropic_api_key="sk-test"))
    stub = StubMessages(script)
    llm.client = type("C", (), {"messages": stub})()
    return llm, stub


def tool_block(data, name="record_reference_findings") -> ToolUseBlock:
    return ToolUseBlock(id="toolu_1", type="tool_use", name=name, input=data)


async def call(llm, role="smart"):
    return await llm.structured(
        role=role, system="s", content=[{"type": "text", "text": "t"}], output=ReferenceFindings,
        tool_name="record_reference_findings", tool_description="d",
    )


async def test_falls_back_to_auto_when_forcing_is_rejected(tmp_path):
    llm, stub = make_llm(tmp_path, [
        bad_request("tool_choice type 'tool' is not supported for this model"),
        message(tool_block({"high_credit": 40000})),
        message(tool_block({"high_credit": 20000})),
    ])
    out = await call(llm, role="fast")
    assert out.high_credit == 40000
    assert stub.calls[0]["tool_choice"]["type"] == "tool" and stub.calls[1]["tool_choice"]["type"] == "auto"
    await call(llm, role="fast")  # remembered: no second forced attempt
    assert stub.calls[2]["tool_choice"]["type"] == "auto"


async def test_smart_role_uses_auto_and_nudges_once(tmp_path):
    llm, stub = make_llm(tmp_path, [
        message(TextBlock(type="text", text="Let me look."), stop="end_turn"),
        message(tool_block({"terms": "Net 30"})),
    ])
    out = await call(llm, role="smart")
    assert out.terms == "Net 30"
    assert stub.calls[0]["tool_choice"] == {"type": "auto"}
    assert "Call the record_reference_findings tool now" in stub.calls[1]["messages"][-1]["content"]


async def test_validation_error_is_sent_back_for_correction(tmp_path):
    llm, stub = make_llm(tmp_path, [
        message(tool_block({"high_credit": "lots"})),
        message(tool_block({"high_credit": 15000})),
    ])
    out = await call(llm, role="fast")
    assert out.high_credit == 15000
    retry = stub.calls[1]["messages"][-1]["content"][0]
    assert retry["type"] == "tool_result" and retry["is_error"]


async def test_other_bad_requests_are_raised(tmp_path):
    llm, _ = make_llm(tmp_path, [bad_request("prompt is too long")])
    with pytest.raises(BadRequestError):
        await call(llm, role="fast")


async def test_gives_up_after_three_tries(tmp_path):
    no_tool = message(TextBlock(type="text", text="hmm"), stop="end_turn")
    llm, _ = make_llm(tmp_path, [no_tool, no_tool, no_tool])
    with pytest.raises(LLMError):
        await call(llm, role="smart")
