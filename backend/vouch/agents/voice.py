"""Voice agent: calls a trade reference and turns the conversation into data.

The conversation runs over the Twilio ConversationRelay message protocol. Twilio
does speech-to-text and text-to-speech; we receive the caller's words as
`prompt` messages and stream our reply back as `text` tokens. The in-browser
phone and the simulated reference speak the same protocol, so this module does
not know or care which one is on the other end.

Three Claude jobs happen per call:
  1. live turns (fast model, streamed) that speak to the reference
  2. live field capture after each answer (fast model) for the review screen
  3. a final read of the whole transcript (smart model) into a CallResult
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from ..llm import LLM, fake_handler, fake_voice, text_of
from ..schemas import CallResult, ReferenceFindings

END_CALL_TOOL = {
    "name": "end_call",
    "description": (
        "Hang up. Call this in the same turn as your goodbye, once you have the answers, "
        "or when the person asks for a callback, declines, says it is a wrong number, "
        "or you reached voicemail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": ["completed", "callback_requested", "declined", "wrong_number", "voicemail"]},
            "callback_at": {"type": "string", "description": "When they asked to be called back, in their words."},
            "alternate_contact": {"type": "string", "description": "Email or person they referred you to."},
        },
        "required": ["reason"],
    },
}


def greeting(seller: str, applicant: str, contact_name: Optional[str]) -> str:
    who = f" for {contact_name.split()[0]}" if contact_name else ""
    return (
        f"Hi, this is Vouch, an AI assistant calling{who} on behalf of {seller}'s credit team, "
        f"on a recorded line. {applicant} listed you as a trade reference on their credit application. "
        f"Do you have two minutes for a few quick questions?"
    )


SYSTEM = """You are Vouch, an AI assistant on {seller}'s credit team, on a live phone call \
with {contact} at {reference}. {applicant} applied to {seller} for trade credit and listed \
{reference} as a trade reference, and signed an authorization allowing {seller} to contact \
its references. You already greeted them, said you are an AI, and said the call is recorded.

Your job is to collect, about {applicant}:
1. how long they have bought from {reference} on credit (customer since)
2. the payment terms {reference} gives them
3. the highest balance or credit line extended (high credit)
4. the current balance owed
5. whether anything is past due, and how much
6. how many days they usually take to pay
7. whether {reference} would extend them more credit

How to talk:
- This is a phone call. Say one or two short sentences per turn. Ask at most two related questions at a time.
- No lists, no markdown, no emojis. Say numbers the way a person would.
- Skip anything they already answered. Do not repeat a question they answered.
- If they need a moment to look something up, say something brief like "Of course, take your time." and wait.
- When you have the answers, read back the key numbers in one sentence and ask if that is right. \
After they confirm or correct it, thank them, say goodbye, and call end_call with reason completed.
- If they ask you to call back later, ask when if they did not say, thank them, and call end_call \
with reason callback_requested and callback_at.
- If they will not give a reference by phone, ask where to send a written request if they did not say, \
thank them, and call end_call with reason declined and alternate_contact.
- If it is a wrong number, apologize and call end_call with reason wrong_number.
- If you reach a voicemail greeting, say a one-sentence message that {seller}'s credit team will call back \
about a trade reference for {applicant}, then call end_call with reason voicemail.
- Do not reveal how much credit {applicant} requested or anything else from their application. \
If asked why you are calling, say {applicant} applied for credit terms and listed them as a reference.
- If they ask whether you are a person, say you are an AI assistant working for {seller}'s credit team.
"""


def system_prompt(seller: str, applicant: str, reference: str, contact_name: Optional[str]) -> str:
    return SYSTEM.format(seller=seller, applicant=applicant, reference=reference, contact=contact_name or "someone")


def opening_messages(greeting_text: str) -> list[dict]:
    """Anthropic conversations start with a user turn; mark the pickup, then our greeting."""
    return [
        {"role": "user", "content": "[The reference picked up the phone.]"},
        {"role": "assistant", "content": greeting_text},
    ]


def transcript_text(transcript: list[dict], agent_label: str = "Vouch", other_label: str = "Reference") -> str:
    lines = []
    for turn in transcript:
        label = agent_label if turn["role"] == "agent" else other_label
        lines.append(f"{label}: {turn['text']}")
    return "\n".join(lines)


LIVE_TOOL = "record_reference_findings"
FINAL_TOOL = "record_call_result"


async def live_findings(llm: LLM, applicant: str, reference: str, transcript: list[dict]) -> ReferenceFindings:
    return await llm.structured(
        role="fast",
        system=(
            f"You listen to a phone call where an AI asks {reference} for a trade reference on {applicant}. "
            "Record only what the reference has clearly stated so far. Leave everything else null."
        ),
        content=[{"type": "text", "text": f"Transcript so far:\n{transcript_text(transcript)}\n\nCall {LIVE_TOOL}."}],
        output=ReferenceFindings,
        tool_name=LIVE_TOOL,
        tool_description="Record the reference's answers stated so far.",
        max_tokens=800,
    )


async def final_result(
    llm: LLM,
    applicant: str,
    reference: str,
    transcript: list[dict],
    end_hint: Optional[dict],
) -> CallResult:
    hint = f"\nThe agent ended the call with: {json.dumps(end_hint)}" if end_hint else ""
    return await llm.structured(
        role="smart",
        system=(
            f"You review recorded trade reference calls for a credit team. The call was about {applicant}, "
            f"with {reference}. Record what the reference actually said. Numbers must come from the reference, "
            "not from the agent's questions or read-back, unless the reference confirmed the read-back. "
            "If the reference corrected a number, use the corrected value."
        ),
        content=[{"type": "text", "text": f"Transcript:\n{transcript_text(transcript)}{hint}\n\nCall {FINAL_TOOL}."}],
        output=CallResult,
        tool_name=FINAL_TOOL,
        tool_description="Record the outcome of the reference call and the reference's answers.",
        max_tokens=1200,
    )


# ---------------------------------------------------------------------------
# Offline behavior: a scripted agent and a regex reader so the flow runs without Claude.

_FAKE_QUESTIONS = [
    "Thank you. How long has {applicant} bought from you on credit, and on what terms?",
    "Got it. What's the highest balance you've extended to them, and what do they owe right now?",
    "Is anything past due, and about how many days do they usually take to pay?",
    "Would you extend them more credit?",
]


def _applicant_from_system(system: str) -> str:
    m = re.search(r"at .+?\. (.+?) applied to", system)
    return m.group(1) if m else "the applicant"


@fake_voice("agent")
def _fake_agent(system: str, messages: list[dict]) -> tuple[str, Optional[dict]]:
    last_user = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user = m["content"] if isinstance(m["content"], str) else text_of(m["content"])
            break
    low = last_user.lower()
    if "call me back" in low or "call back" in low:
        when = re.search(r"back (.+?)[?.!]", last_user)
        callback = when.group(1) if when else None
        return f"Of course, I'll call back {callback or 'later'}. Thanks for your time.", {
            "name": "end_call",
            "input": {"reason": "callback_requested", **({"callback_at": callback} if callback else {})},
        }
    if "written reference" in low or "only give written" in low:
        email = re.search(r"[\w.+-]+@[\w.-]+\.\w+", last_user)
        return "Understood, we'll send a written request. Thank you.", {
            "name": "end_call",
            "input": {"reason": "declined", **({"alternate_contact": email.group(0).rstrip('.')} if email else {})},
        }
    if "wrong number" in low:
        return "Sorry to bother you. Goodbye.", {"name": "end_call", "input": {"reason": "wrong_number"}}
    if "leave a message" in low or "after the tone" in low:
        return "This is Vouch calling for a trade reference. We'll call back. Thank you.", {"name": "end_call", "input": {"reason": "voicemail"}}
    asked = sum(1 for m in messages if m["role"] == "assistant") - 1  # minus greeting
    if asked < len(_FAKE_QUESTIONS):
        return _FAKE_QUESTIONS[asked].format(applicant=_applicant_from_system(system)), None
    return "Perfect, that's everything I needed. Thanks so much for your help. Goodbye.", {"name": "end_call", "input": {"reason": "completed"}}


_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "a": 1,
}
_SCALES = {"hundred": 100, "thousand": 1000}


def words_to_number(text: str) -> Optional[float]:
    """'twelve thousand four hundred' -> 12400, 'forty-two hundred' -> 4200, '28' -> 28."""
    text = text.lower().replace("-", " ").replace(",", "").strip()
    digits = re.match(r"\$?(\d+(?:\.\d+)?)\s*(k|thousand)?", text)
    if digits:
        value = float(digits.group(1))
        if digits.group(2):
            value *= 1000
        return value
    total, current, seen = 0, 0, False
    for word in text.split():
        if word in _UNITS:
            current += _UNITS[word]
            seen = True
        elif word in _SCALES:
            current = max(current, 1) * _SCALES[word]
            if word == "thousand":
                total += current
                current = 0
            seen = True
        elif word in ("and",):
            continue
        elif seen:
            break
    return float(total + current) if seen else None


def _amount_after(text: str, keyword: str) -> Optional[float]:
    m = re.search(keyword + r"[^.?!]*?((?:\$?\d[\d,]*(?:\.\d+)?\s*(?:k|thousand)?)|(?:\b(?:a(?=\s+(?:hundred|thousand))|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\b(?:[\s-]+(?:and|one|two|three|four|five|six|seven|eight|nine|hundred|thousand|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\b)*))", text, re.IGNORECASE)
    return words_to_number(m.group(1)) if m else None


def regex_findings(reference_lines: str) -> ReferenceFindings:
    t = reference_lines
    low = t.lower()
    since = re.search(r"since (\d{4})", t)
    terms = re.search(r"net\s*(\d+)", low)
    days = re.search(r"(?:about|in about|right at|average is about|takes?)\s+(\d+)\s+days", low)
    avg_days: Optional[float] = float(days.group(1)) if days else (14.0 if "two weeks" in low else None)
    past_due = 0.0 if ("nothing is past due" in low or "nothing past due" in low or "never late" in low) else None
    extend = True if re.search(r"(extend more|happily extend|i'd extend|yes, we'd|absolutely|solid account|lovely people)", low) else None
    terms_value = f"Net {terms.group(1)}" if terms else None
    within = None
    if avg_days is not None and terms:
        within = avg_days <= int(terms.group(1)) + 5
    return ReferenceFindings(
        customer_since=since.group(1) if since else ("long time" if "forever" in low else None),
        terms=terms_value,
        high_credit=_amount_after(t, r"high credit"),
        current_balance=_amount_after(t, r"(?:owe|current balance)"),
        past_due_amount=past_due,
        avg_days_to_pay=avg_days,
        pays_within_terms=within,
        would_extend_more_credit=extend,
    )


def _reference_lines(content: list[dict]) -> tuple[str, str]:
    text = text_of(content)
    ref_lines = "\n".join(line.split(":", 1)[1] for line in text.splitlines() if line.startswith("Reference:"))
    return text, ref_lines


@fake_handler(LIVE_TOOL)
def _fake_live(system: str, content: list[dict]) -> dict:
    _, ref_lines = _reference_lines(content)
    return regex_findings(ref_lines).model_dump()


@fake_handler(FINAL_TOOL)
def _fake_final(system: str, content: list[dict]) -> dict:
    text, ref_lines = _reference_lines(content)
    hint = re.search(r"ended the call with: (\{.*\})", text)
    end = json.loads(hint.group(1)) if hint else {}
    reason = end.get("reason") or ("completed" if ref_lines else "no_answer")
    findings = regex_findings(ref_lines) if reason == "completed" else ReferenceFindings()
    summaries = {
        "completed": "The reference answered every question.",
        "callback_requested": f"The reference asked us to call back {end.get('callback_at') or 'later'}.",
        "declined": "The reference only gives written references.",
        "wrong_number": "Wrong number.",
        "voicemail": "Reached voicemail.",
        "no_answer": "No answer.",
    }
    return CallResult(
        outcome=reason,
        findings=findings,
        callback_at=end.get("callback_at"),
        alternate_contact=end.get("alternate_contact"),
        summary=summaries.get(reason, reason),
    ).model_dump()


def estimate_speech_seconds(text: str) -> float:
    """Rough time for TTS to finish a sentence, so we do not hang up mid-goodbye."""
    return min(8.0, 0.6 + len(text.split()) / 2.6)


async def sleep_for_speech(text: str) -> None:
    await asyncio.sleep(estimate_speech_seconds(text))
