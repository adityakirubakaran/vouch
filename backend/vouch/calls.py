"""Placing reference calls and running the conversation.

Three channels share one conversation engine (RelaySession):
  twilio     real phone call; Twilio ConversationRelay handles speech
  browser    a person answers in the app's /phone page (Web Speech API)
  simulated  Claude plays the reference from a persona, for unattended demos

All three exchange ConversationRelay-shaped messages, so RelaySession is the
same code no matter who is on the other end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Optional, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select

from .agents import voice
from .db import Application, Call, Reference, Step, utcnow
from .fixtures_data import persona_for
from .llm import fake_handler, text_of
from .providers.twilio_client import TwilioClient, relay_twiml, say_and_hangup_twiml
from .schemas import CallResult, ReferenceFindings
from .util import normalize_phone

if TYPE_CHECKING:  # pragma: no cover
    from .services import Services

log = logging.getLogger("vouch.calls")

MAX_AGENT_TURNS = 14


class Transport(Protocol):
    async def send(self, msg: dict) -> None: ...

    async def receive(self) -> Optional[dict]: ...

    async def close(self) -> None: ...


class QueueTransport:
    def __init__(self, inbox: "asyncio.Queue[Optional[dict]]", outbox: "asyncio.Queue[Optional[dict]]") -> None:
        self.inbox = inbox
        self.outbox = outbox
        self.closed = False

    async def send(self, msg: dict) -> None:
        if not self.closed:
            await self.outbox.put(msg)

    async def receive(self) -> Optional[dict]:
        return await self.inbox.get()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.outbox.put(None)


def transport_pair() -> tuple[QueueTransport, QueueTransport]:
    a: asyncio.Queue = asyncio.Queue()
    b: asyncio.Queue = asyncio.Queue()
    return QueueTransport(inbox=a, outbox=b), QueueTransport(inbox=b, outbox=a)


class WebSocketTransport:
    def __init__(self, ws) -> None:  # starlette WebSocket
        self.ws = ws
        self.closed = False

    async def send(self, msg: dict) -> None:
        if self.closed:
            return
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:  # noqa: BLE001 - peer went away
            self.closed = True

    async def receive(self) -> Optional[dict]:
        try:
            raw = await self.ws.receive_text()
        except Exception:  # noqa: BLE001 - disconnect
            self.closed = True
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"type": "error", "description": "invalid json"}

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass


class CallContext(BaseModel):
    call_id: str
    reference_id: str
    application_id: str
    seller: str
    applicant: str
    reference: str
    contact_name: Optional[str] = None
    attempt: int = 1
    channel: str = "simulated"


class RelaySession:
    """One live conversation between the voice agent and a reference."""

    def __init__(self, services: "Services", ctx: CallContext, greeting_spoken: bool) -> None:
        self.s = services
        self.ctx = ctx
        self.greeting_spoken = greeting_spoken
        self.transcript: list[dict] = []
        self.history: list[dict] = []
        self.end_hint: Optional[dict] = None
        self._live_task: Optional[asyncio.Task] = None
        self._live_dirty = False
        self._inbox: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
        self._reader: Optional[asyncio.Task] = None
        self._barge_in = False  # set by the reader the moment an interrupt arrives

    async def _read(self, transport: Transport) -> None:
        """Read the socket in its own task so an interrupt is seen while the agent
        is still streaming its reply, not after."""
        while True:
            msg = await transport.receive()
            if msg is not None and msg.get("type") == "interrupt":
                self._barge_in = True
            await self._inbox.put(msg)
            if msg is None:
                return

    async def run(self, transport: Transport, setup: Optional[dict] = None) -> None:
        started = False
        try:
            if setup is None:
                setup = await transport.receive()
                if setup is None:
                    return
            if not self.s.calls.mark_in_progress(self.ctx.call_id, (setup or {}).get("callSid")):
                # This call already ended (timed out, failed, or Vouch restarted). Do not
                # interview the reference on a call the workflow has moved past.
                await transport.send({"type": "end", "handoffData": json.dumps({"reason": "call_closed"})})
                return
            started = True
            self._reader = asyncio.create_task(self._read(transport))
            text = voice.greeting(self.ctx.seller, self.ctx.applicant, self.ctx.contact_name)
            if not self.greeting_spoken:
                await self._speak(transport, text)
            self._add_turn("agent", text)
            self.history = voice.opening_messages(text)
            system = voice.system_prompt(self.ctx.seller, self.ctx.applicant, self.ctx.reference, self.ctx.contact_name)
            pending_user = ""
            turns = 0
            while True:
                msg = await self._inbox.get()
                if msg is None:
                    break
                kind = msg.get("type")
                if kind == "prompt":
                    pending_user = (pending_user + " " + (msg.get("voicePrompt") or "")).strip()
                    if not msg.get("last", True) or not pending_user:
                        continue
                    said, pending_user = pending_user, ""
                    self._add_turn("reference", said)
                    self._push_user(said)
                    self._schedule_live()
                    turns += 1
                    if turns > MAX_AGENT_TURNS:
                        self._push_user("[The call has run long. Thank them, say goodbye, and end the call.]")
                    hint = await self._agent_turn(transport, system)
                    if hint is not None:
                        self.end_hint = hint
                        await asyncio.sleep(voice.estimate_speech_seconds(self.transcript[-1]["text"]) * self._time_scale())
                        await transport.send({"type": "end", "handoffData": json.dumps(hint)})
                        break
                elif kind == "interrupt":
                    self._apply_interrupt(msg.get("utteranceUntilInterrupt") or "")
                elif kind == "error":
                    log.warning("relay error on %s: %s", self.ctx.call_id, msg.get("description"))
        except Exception:  # noqa: BLE001
            log.exception("relay session %s crashed", self.ctx.call_id)
            if self.end_hint is None:
                self.end_hint = {"reason": "failed"}
        finally:
            if self._reader is not None:
                self._reader.cancel()
            await transport.close()
            if started:
                await self._finalize()

    def _time_scale(self) -> float:
        # Real phone calls need real pauses; simulated ones can run fast in tests.
        return 1.0 if self.ctx.channel == "twilio" else self.s.settings.sim_time_scale

    # -------------------------------------------------------------- speaking

    async def _speak(self, transport: Transport, text: str) -> None:
        words = text.split(" ")
        for i, w in enumerate(words):
            await transport.send({"type": "text", "token": w + (" " if i < len(words) - 1 else ""), "last": i == len(words) - 1})

    async def _agent_turn(self, transport: Transport, system: str) -> Optional[dict]:
        """One reply from the agent. A Claude error mid-stream (an overloaded API, a
        dropped connection) is retried once; history only changes after a reply
        finishes, so the retry sees the same conversation. If the retry fails too,
        the agent apologizes and hangs up, and the call is retried later."""
        self._barge_in = False
        for attempt in range(2):
            try:
                return await self._stream_reply(transport, system)
            except Exception as exc:  # noqa: BLE001
                log.warning("agent turn failed on %s (attempt %s): %s", self.ctx.call_id, attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(0.5 * self._time_scale())
        said = "Sorry, I'm having trouble on my end. We'll call you back another time. Thank you."
        await transport.send({"type": "text", "token": said, "last": False})
        await transport.send({"type": "text", "token": " ", "last": True})
        self.history.append({"role": "assistant", "content": said})
        self._add_turn("agent", said)
        return {"reason": "failed"}

    async def _stream_reply(self, transport: Transport, system: str) -> Optional[dict]:
        parts: list[str] = []
        tool: Optional[dict] = None
        stream = self.s.llm.voice_turn(system=system, messages=self.history, tools=[voice.END_CALL_TOOL])
        try:
            async for ev in stream:
                if self._barge_in:
                    break  # the reference started talking: stop sending speech
                if ev.kind == "text" and ev.text:
                    parts.append(ev.text)
                    await transport.send({"type": "text", "token": ev.text, "last": False})
                    self.s.bus.publish(
                        "call.token", application_id=self.ctx.application_id, persist=False,
                        data={"call_id": self.ctx.call_id, "reference_id": self.ctx.reference_id, "token": ev.text},
                    )
                elif ev.kind == "tool" and ev.tool_name == "end_call":
                    tool = ev.tool_input
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
        said = "".join(parts).strip()
        if self._barge_in:
            await transport.send({"type": "text", "token": " ", "last": True})
            self.history.append({"role": "assistant", "content": (said or "...") + " [interrupted]"})
            self._add_turn("agent", said or "...")
            return None  # never hang up on someone who is mid-sentence
        if not said:
            said = "Thank you for your time. Goodbye." if tool else "Sorry, could you say that again?"
            await transport.send({"type": "text", "token": said, "last": False})
        await transport.send({"type": "text", "token": " ", "last": True})
        self.history.append({"role": "assistant", "content": said})
        self._add_turn("agent", said)
        return tool

    def _push_user(self, text: str) -> None:
        if self.history and self.history[-1]["role"] == "user":
            self.history[-1]["content"] = f"{self.history[-1]['content']} {text}"
        else:
            self.history.append({"role": "user", "content": text})

    def _apply_interrupt(self, heard: str) -> None:
        if self.history and self.history[-1]["role"] == "assistant" and heard:
            self.history[-1]["content"] = heard + " [interrupted]"
        for turn in reversed(self.transcript):
            if turn["role"] == "agent":
                if heard:
                    turn["text"] = heard
                turn["interrupted"] = True
                break
        self.s.calls.save_transcript(self.ctx.call_id, self.transcript)

    def _add_turn(self, role: str, text: str) -> None:
        turn = {"role": role, "text": text, "ts": utcnow().isoformat()}
        self.transcript.append(turn)
        self.s.calls.save_transcript(self.ctx.call_id, self.transcript)
        self.s.bus.publish(
            "call.transcript", application_id=self.ctx.application_id, persist=False,
            data={"call_id": self.ctx.call_id, "reference_id": self.ctx.reference_id, **turn},
        )

    # -------------------------------------------------------- live capture

    def _schedule_live(self) -> None:
        self._live_dirty = True
        if self._live_task is None or self._live_task.done():
            self._live_task = asyncio.create_task(self._live_worker())

    async def _live_worker(self) -> None:
        while self._live_dirty:
            self._live_dirty = False
            try:
                findings = await voice.live_findings(self.s.llm, self.ctx.applicant, self.ctx.reference, list(self.transcript))
                self.s.calls.save_live_findings(self.ctx.call_id, findings)
            except Exception as exc:  # noqa: BLE001
                log.warning("live findings failed: %s", exc)

    # ------------------------------------------------------------ wrap up

    async def _finalize(self) -> None:
        if self._live_task and not self._live_task.done():
            try:
                await asyncio.wait_for(self._live_task, timeout=10)
            except Exception:  # noqa: BLE001
                pass
        voicemail = self.s.calls.voicemail_detected(self.ctx.call_id)
        spoke = any(t["role"] == "reference" for t in self.transcript)
        if voicemail:
            result = CallResult(outcome="voicemail", summary="Reached voicemail.")
        elif not spoke:
            result = CallResult(outcome="no_answer", summary="The call connected but no one spoke.")
        elif (self.end_hint or {}).get("reason") == "failed":
            live = self.s.calls.live_findings(self.ctx.call_id) or {}
            result = CallResult(
                outcome="failed", findings=ReferenceFindings.model_validate(live),
                summary="Vouch had a technical problem during the call and will call back.",
            )
        else:
            try:
                result = await voice.final_result(self.s.llm, self.ctx.applicant, self.ctx.reference, self.transcript, self.end_hint)
            except Exception as exc:  # noqa: BLE001
                log.warning("final transcript read failed, using live findings: %s", exc)
                live = self.s.calls.live_findings(self.ctx.call_id) or {}
                result = CallResult(
                    outcome=(self.end_hint or {}).get("reason", "completed"),  # type: ignore[arg-type]
                    findings=ReferenceFindings.model_validate(live),
                    callback_at=(self.end_hint or {}).get("callback_at"),
                    alternate_contact=(self.end_hint or {}).get("alternate_contact"),
                    summary="Read from live notes; the final transcript review failed.",
                )
        if self.end_hint is None and result.outcome == "completed" and result.findings.high_credit is None:
            # The line dropped (or the reference hung up) before the agent got the
            # numbers that matter. Treat it as a call to retry, not a finished reference.
            result.outcome = "failed"
            result.summary = f"{result.summary} The call ended before the interview finished.".strip()
        self.s.calls.finish(self.ctx.call_id, result)


# ---------------------------------------------------------------------------
# Simulated reference


class PersonaReply(BaseModel):
    text: str = Field(description="What the reference says next, exactly as spoken. One to three sentences.")


PERSONA_TOOL = "say_as_reference"


class PersonaBot:
    """Plays the reference on a simulated call."""

    def __init__(self, services: "Services", ctx: CallContext, persona: dict, attempt: dict) -> None:
        self.s = services
        self.ctx = ctx
        self.persona = persona
        self.attempt = attempt
        self.heard: list[dict] = []

    async def run(self, transport: Transport) -> None:
        await transport.send({"type": "setup", "callSid": f"SIM-{self.ctx.call_id}", "customParameters": {"call_id": self.ctx.call_id}})
        buffer = ""
        turn = 0
        try:
            while True:
                msg = await transport.receive()
                if msg is None:
                    break
                if msg.get("type") == "text":
                    buffer += msg.get("token", "")
                    if msg.get("last"):
                        said, buffer = buffer.strip(), ""
                        if not said:
                            continue
                        self.heard.append({"role": "agent", "text": said})
                        reply = await self._reply(turn)
                        turn += 1
                        if not reply:
                            continue
                        self.heard.append({"role": "reference", "text": reply})
                        await asyncio.sleep(min(2.5, 0.5 + len(said.split()) / 6) * self.s.settings.sim_time_scale)  # let the line "play"
                        await transport.send({"type": "prompt", "voicePrompt": reply, "lang": "en-US", "last": True})
                elif msg.get("type") == "end":
                    break
        finally:
            await transport.close()

    async def _reply(self, turn: int) -> str:
        facts = self.attempt.get("facts")
        payload = {
            "turn": turn,
            "script": self.attempt.get("script", []),
            "conversation": self.heard,
        }
        system = (
            f"You are role-playing {self.persona.get('person', 'someone in accounts receivable')} answering a phone call. "
            f"The caller is an AI assistant from {self.ctx.seller}'s credit team asking for a trade reference on {self.ctx.applicant}, "
            f"a customer of {self.ctx.reference}. Speak naturally, like a real person on the phone, in one to three short sentences. "
            f"Behavior: {self.attempt.get('style', 'Cooperative and brief.')} "
            + (f"Facts you know (answer only what you are asked, in your own words): {json.dumps(facts)}. " if facts else
               "Make up plausible, unremarkable account details if asked. ")
            + (f"You want a callback {self.attempt['callback']}. " if self.attempt.get("callback") else "")
            + (f"Your written reference address is {self.attempt['alternate_contact']}. " if self.attempt.get("alternate_contact") else "")
            + "If the caller says goodbye, say a short goodbye."
        )
        convo = "\n".join(f"{'Caller' if t['role'] == 'agent' else 'You'}: {t['text']}" for t in self.heard)
        try:
            reply = await self.s.llm.structured(
                role="fast",
                system=system,
                content=[{"type": "text", "text": f"{json.dumps(payload)}\n\nConversation so far:\n{convo}\n\nCall {PERSONA_TOOL} with your next line."}],
                output=PersonaReply,
                tool_name=PERSONA_TOOL,
                tool_description="Say your next line on the call.",
                max_tokens=300,
            )
            return reply.text.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("persona reply failed: %s", exc)
            return ""


@fake_handler(PERSONA_TOOL)
def _fake_persona(system: str, content: list[dict]) -> dict:
    raw = text_of(content).split("\n\nConversation so far:")[0]
    payload = json.loads(raw)
    script = payload.get("script") or GENERIC_SCRIPT
    turn = payload["turn"]
    return {"text": script[turn] if turn < len(script) else ""}


GENERIC_SCRIPT = [
    "Accounts receivable. Sure, go ahead.",
    "They've been a customer since 2021, on Net 30.",
    "High credit is about fifteen thousand. They owe around three thousand right now.",
    "Nothing past due. They pay in about 30 days.",
    "Yes, we would.",
    "That's right.",
    "Bye.",
]

GENERIC_PERSONA = {
    "person": "an accounts receivable clerk",
    "attempts": [{"behavior": "answer", "style": "Cooperative and brief.", "facts": None, "script": GENERIC_SCRIPT}],
}


# ---------------------------------------------------------------------------


class CallManager:
    def __init__(self, services: "Services") -> None:
        self.s = services
        self._tasks: set[asyncio.Task] = set()
        self._voicemail: set[str] = set()

    # ------------------------------------------------------------ helpers

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def context(self, call_id: str) -> CallContext:
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            assert call is not None
            ref = s.get(Reference, call.reference_id)
            app = s.get(Application, call.application_id)
            assert ref is not None and app is not None
            return CallContext(
                call_id=call.id,
                reference_id=ref.id,
                application_id=app.id,
                seller=self.s.settings.seller_name,
                applicant=(app.extracted or {}).get("applicant", {}).get("dba") or app.applicant_name or "the applicant",
                reference=ref.company,
                contact_name=ref.contact_name,
                attempt=call.attempt,
                channel=call.channel,
            )

    def resolve_channel(self, requested: Optional[str]) -> str:
        channel = requested or self.s.settings.call_channel
        if channel == "twilio" and not (self.s.settings.twilio_configured and self.s.settings.public_base_url):
            log.warning("Twilio requested but not configured; using the browser phone instead")
            return "browser"
        return channel

    def create_call(self, reference_id: str, attempt: int, channel: str) -> str:
        with self.s.db.session() as s:
            ref = s.get(Reference, reference_id)
            assert ref is not None
            dialed = ref.phone
            if channel == "twilio" and self.s.settings.demo_reference_phone:
                dialed = normalize_phone(self.s.settings.demo_reference_phone)
            call = Call(reference_id=ref.id, application_id=ref.application_id, attempt=attempt, channel=channel, dialed_number=dialed)
            s.add(call)
            s.flush()
            ref.status = "calling"
            call_id, app_id = call.id, ref.application_id
            company = ref.company
        self.s.bus.publish(
            "call.created", f"Calling {company} (attempt {attempt}, {channel})", application_id=app_id,
            data={"call_id": call_id, "reference_id": reference_id, "channel": channel, "attempt": attempt},
        )
        return call_id

    def _set(self, call_id: str, **fields) -> Optional[Call]:
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            if call is None:
                return None
            for k, v in fields.items():
                setattr(call, k, v)
            return call

    def mark_in_progress(self, call_id: str, provider_sid: Optional[str] = None) -> bool:
        """Mark a call as answered. Returns False (and changes nothing) if the call
        is unknown or already ended, so a late answer cannot revive it."""
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            if call is None or call.status == "ended":
                return False
            call.status = "in_progress"
            call.started_at = utcnow()
            if provider_sid and not call.provider_sid:
                call.provider_sid = provider_sid
            app_id, ref_id = call.application_id, call.reference_id
        self.s.bus.publish("call.updated", "Reference picked up", application_id=app_id,
                           data={"call_id": call_id, "reference_id": ref_id, "status": "in_progress"})
        return True

    def save_transcript(self, call_id: str, transcript: list[dict]) -> None:
        self._set(call_id, transcript=[dict(t) for t in transcript])

    def save_live_findings(self, call_id: str, findings: ReferenceFindings) -> None:
        call = self._set(call_id, live_findings=findings.model_dump())
        if call:
            self.s.bus.publish("call.findings", application_id=call.application_id, persist=False,
                               data={"call_id": call_id, "reference_id": call.reference_id, "findings": findings.model_dump()})

    def live_findings(self, call_id: str) -> Optional[dict]:
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            return dict(call.live_findings) if call and call.live_findings else None

    def voicemail_detected(self, call_id: str) -> bool:
        return call_id in self._voicemail

    def finish(self, call_id: str, result: CallResult) -> None:
        """Record the outcome once and wake the waiting workflow step."""
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            if call is None or call.status == "ended":
                return
            call.status = "ended"
            call.outcome = result.outcome
            call.result = result.model_dump()
            call.ended_at = utcnow()
            ref_id, app_id = call.reference_id, call.application_id
            step = s.scalars(select(Step).where(Step.reference_id == ref_id, Step.kind == "call")).first()
            step_id = step.id if step else None
        self.s.bus.publish("call.ended", f"Call ended: {result.summary}", application_id=app_id,
                           data={"call_id": call_id, "reference_id": ref_id, "outcome": result.outcome})
        if step_id:
            self.s.engine.signal(step_id, {"call_id": call_id})

    # ------------------------------------------------------------- placing

    async def place(self, call_id: str) -> None:
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            assert call is not None
            channel = call.channel
        if channel == "simulated":
            self._spawn(self._run_simulated(call_id))
        elif channel == "browser":
            self._ring_browser(call_id)
        elif channel == "twilio":
            await self._dial_twilio(call_id)
        else:  # pragma: no cover
            raise ValueError(f"unknown channel {channel}")

    async def _run_simulated(self, call_id: str) -> None:
        ctx = self.context(call_id)
        persona = persona_for(ctx.reference) or GENERIC_PERSONA
        attempts = persona["attempts"]
        attempt = attempts[min(ctx.attempt - 1, len(attempts) - 1)]
        scale = self.s.settings.sim_time_scale
        self._set(call_id, status="ringing", ring_deadline=utcnow() + timedelta(seconds=6))
        self.s.bus.publish("call.updated", "Ringing", application_id=ctx.application_id,
                           data={"call_id": call_id, "reference_id": ctx.reference_id, "status": "ringing"})
        if attempt.get("behavior") == "no_answer":
            await asyncio.sleep(4 * scale)
            self.finish(call_id, CallResult(outcome="no_answer", summary="No answer after several rings."))
            return
        await asyncio.sleep(1.5 * scale)
        agent_side, person_side = transport_pair()
        session = RelaySession(self.s, ctx, greeting_spoken=False)
        bot = PersonaBot(self.s, ctx, persona, attempt)
        await asyncio.gather(session.run(agent_side), bot.run(person_side))

    def _ring_browser(self, call_id: str) -> None:
        timeout = self.s.settings.call_ring_timeout_seconds
        call = self._set(call_id, status="ringing", ring_deadline=utcnow() + timedelta(seconds=timeout))
        assert call is not None
        self.s.bus.publish("call.ringing", "Ringing the browser phone", application_id=call.application_id,
                           data={"call_id": call_id, "reference_id": call.reference_id, "status": "ringing"})

        async def watchdog() -> None:
            await asyncio.sleep(timeout)
            with self.s.db.session() as s:
                current = s.get(Call, call_id)
                still_ringing = current is not None and current.status == "ringing"
            if still_ringing:
                self.finish(call_id, CallResult(outcome="no_answer", summary="No one answered the phone."))

        self._spawn(watchdog())

    async def answer_browser(self, call_id: str, transport: Transport) -> None:
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            ringing = call is not None and call.status == "ringing" and call.channel == "browser"
        if not ringing:
            await transport.send({"type": "end", "handoffData": json.dumps({"reason": "not_ringing"})})
            await transport.close()
            return
        await RelaySession(self.s, self.context(call_id), greeting_spoken=False).run(transport)

    # -------------------------------------------------------------- Twilio

    async def _dial_twilio(self, call_id: str) -> None:
        ctx = self.context(call_id)
        base = (self.s.settings.public_base_url or "").rstrip("/")
        ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/twilio/relay"
        twiml = relay_twiml(
            ws_url=ws_url,
            call_id=call_id,
            greeting=voice.greeting(ctx.seller, ctx.applicant, ctx.contact_name),
            action_url=f"{base}/twilio/connect-action?call_id={call_id}",
            tts_provider=self.s.settings.twilio_tts_provider,
            voice=self.s.settings.twilio_voice,
        )
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            assert call is not None
            to = call.dialed_number
        if not to:
            self.finish(call_id, CallResult(outcome="failed", summary="No phone number for this reference."))
            return
        try:
            sid = await TwilioClient(self.s.settings).create_call(to=to, twiml=twiml, call_id=call_id, public_base=base)
        except Exception as exc:  # noqa: BLE001
            log.error("twilio dial failed: %s", exc)
            self.finish(call_id, CallResult(outcome="failed", summary=f"Twilio could not place the call: {exc}"))
            return
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            if call is None:
                return
            if not call.provider_sid:
                call.provider_sid = sid
            # A status callback (busy, failed) can land before create_call returns,
            # and the reference can even pick up. Only a call still queued moves to ringing.
            if call.status != "queued":
                return
            call.status = "ringing"
            call.ring_deadline = utcnow() + timedelta(seconds=self.s.settings.call_ring_timeout_seconds)
        self.s.bus.publish("call.ringing", f"Dialing {to}", application_id=ctx.application_id,
                           data={"call_id": call_id, "reference_id": ctx.reference_id, "status": "ringing"})

    async def run_twilio_relay(self, transport: Transport) -> None:
        setup = await transport.receive()
        if not setup or setup.get("type") != "setup":
            await transport.close()
            return
        call_id = (setup.get("customParameters") or {}).get("call_id")
        with self.s.db.session() as s:
            call = s.get(Call, call_id) if call_id else None
            live = call is not None and call.channel == "twilio" and call.status != "ended"
        if not live:
            # Unknown, finished, or not a Twilio call: hang up instead of interviewing anyone.
            await transport.send({"type": "end", "handoffData": json.dumps({"reason": "call_closed"})})
            await transport.close()
            return
        await RelaySession(self.s, self.context(call_id), greeting_spoken=True).run(transport, setup=setup)

    def twilio_status(self, call_id: str, status: str) -> None:
        mapping = {"busy": "no_answer", "no-answer": "no_answer", "failed": "failed", "canceled": "failed"}
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            if call is None:
                return
            current = call.status
            app_id, ref_id = call.application_id, call.reference_id
        self.s.bus.publish("call.updated", f"Twilio status: {status}", application_id=app_id, persist=False,
                           data={"call_id": call_id, "reference_id": ref_id, "provider_status": status})
        if status in mapping and current != "ended":
            self.finish(call_id, CallResult(outcome=mapping[status], summary=f"Call {status.replace('-', ' ')}."))  # type: ignore[arg-type]
        elif status == "completed" and current in ("queued", "ringing"):
            outcome = "voicemail" if call_id in self._voicemail else "no_answer"
            self.finish(call_id, CallResult(outcome=outcome, summary="The call ended before a conversation started."))  # type: ignore[arg-type]

    async def twilio_amd(self, call_id: str, answered_by: str) -> None:
        if not answered_by.startswith("machine") and answered_by != "fax":
            return
        self._voicemail.add(call_id)
        ctx = self.context(call_id)
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            sid = call.provider_sid if call else None
        if sid:
            msg = (
                f"Hi, this is Vouch, an AI assistant for {ctx.seller}'s credit team, calling about a trade reference "
                f"for {ctx.applicant}. We'll try you again later. Thank you."
            )
            try:
                await TwilioClient(self.s.settings).update_call_twiml(sid, say_and_hangup_twiml(msg))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not leave voicemail: %s", exc)

    async def hangup(self, call_id: str) -> None:
        with self.s.db.session() as s:
            call = s.get(Call, call_id)
            sid, channel = (call.provider_sid, call.channel) if call else (None, None)
        if channel == "twilio" and sid:
            try:
                await TwilioClient(self.s.settings).hangup(sid)
            except Exception as exc:  # noqa: BLE001
                log.warning("hangup failed: %s", exc)

    def recover_orphans(self) -> int:
        """Calls that were live when the previous process stopped cannot continue:
        record them as failed so their workflow steps retry, and hang up any real
        phone call that may still be ringing so nobody answers into silence."""
        with self.s.db.session() as s:
            orphans = [(c.id, c.channel, c.provider_sid) for c in s.scalars(select(Call).where(Call.status != "ended")).all()]
        for call_id, channel, sid in orphans:
            self.finish(call_id, CallResult(outcome="failed", summary="Vouch restarted during this call; it will be retried."))
            if channel == "twilio" and sid:
                try:
                    self._spawn(self._hangup_sid(sid))
                except RuntimeError:  # no running event loop (called outside the server)
                    log.warning("could not hang up orphaned call %s", sid)
        return len(orphans)

    async def _hangup_sid(self, sid: str) -> None:
        try:
            await TwilioClient(self.s.settings).hangup(sid)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not hang up orphaned call %s: %s", sid, exc)

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
