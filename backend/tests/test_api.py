"""HTTP and WebSocket surface: REST endpoints, the browser phone protocol, and
Twilio webhooks (signatures, TwiML, ConversationRelay)."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from vouch.api.app import create_app
from vouch.fixtures_data import persona_for
from vouch.providers.twilio_client import expected_signature, relay_twiml

from .conftest import make_settings


@pytest.fixture
def client_factory(tmp_path):
    clients = []

    def make(**overrides):
        settings = make_settings(tmp_path, start_engine=True, **overrides)
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def wait_until(fn, timeout=20.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def test_samples_config_and_full_run(client_factory):
    c = client_factory()
    assert c.get("/api/health").json() == {"ok": True}
    cfg = c.get("/api/config").json()
    assert cfg["llm"]["mode"] == "fake" and cfg["calls"]["default_channel"] == "simulated"
    assert cfg["phone_checks"] == {"ipqs": False, "twilio": False, "google": False}
    samples = c.get("/api/samples").json()
    assert {s["id"] for s in samples} == {"acme", "ridgeline"} and all(s["available"] for s in samples)

    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    detail = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())["status"] == "ready_for_review" and d)
    assert detail["decision_detail"]["recommended_limit"] == 25000
    assert len(detail["references"]) == 3 and detail["bank"]["status"] == "completed"
    scores = {r["company"]: r["checks"]["score"] for r in detail["references"]}
    assert scores["Bayview Packaging LLC"]["band"] == "likely_fake" and scores["Harbor Paper Co."]["band"] == "likely_genuine"
    assert {e["code"] for e in detail["evidence"]} >= {"app", "reg-applicant", "call-ref-1", "call-ref-2", "fraud-ref-3", "bank"}
    assert c.get(detail["document_url"]).headers["content-type"] == "application/pdf"
    assert any(e["type"] == "memo.ready" for e in c.get(f"/api/applications/{app_id}/events").json())

    approved = c.post(f"/api/applications/{app_id}/approve", json={"limit": 20000, "terms": "Net 30"}).json()
    assert approved["review"]["limit"] == 20000
    synced = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())["status"] == "synced" and d)
    assert synced["sync"]["customer_id"].startswith("CUST-")


def test_upload_validation(client_factory):
    c = client_factory()
    r = c.post("/api/applications", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    r = c.post("/api/applications", files={"file": ("empty.pdf", b"", "application/pdf")})
    assert r.status_code == 400
    # Offline mode can only read bundled samples; an unknown PDF fails clearly instead of hanging.
    app_id = c.post("/api/applications", files={"file": ("other.pdf", b"%PDF-1.4 fake", "application/pdf")}).json()["id"]
    detail = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())["status"] == "failed" and d)
    assert "ANTHROPIC_API_KEY" in detail["error"]


def test_browser_phone_call_over_websocket(client_factory):
    """A person answers in the /phone page; the agent runs the whole interview."""
    c = client_factory(call_channel="browser", call_ring_timeout_seconds=30)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    answered = set()
    for _ in range(2):  # Harbor and Coastal ring; Bayview is flagged and never called
        ringing = wait_until(lambda: [r for r in c.get("/api/calls/ringing").json() if r["id"] not in answered])
        call = ringing[0]
        answered.add(call["id"])
        script = persona_for(call["reference_company"])["attempts"][-1]["script"]
        with c.websocket_connect(f"/ws/phone/{call['id']}") as ws:
            ws.send_text(json.dumps({"type": "setup"}))
            lines = iter(script)
            heard = ""
            while True:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "end":
                    break
                assert msg["type"] == "text"
                heard += msg["token"]
                if msg["last"]:
                    assert heard.strip()
                    heard = ""
                    ws.send_text(json.dumps({"type": "prompt", "voicePrompt": next(lines), "last": True}))
    detail = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())["status"] == "ready_for_review" and d, timeout=30)
    harbor = next(r for r in detail["references"] if r["company"] == "Harbor Paper Co.")
    assert harbor["findings"]["high_credit"] == 40000
    assert harbor["calls"][0]["channel"] == "browser"
    assert harbor["calls"][0]["transcript"][0]["text"].startswith("Hi, this is Vouch, an AI assistant")


def test_relay_twiml_is_valid_xml():
    xml = relay_twiml("wss://example.ngrok.app/twilio/relay", "call_123", 'Hi "there" & welcome', "https://x/a?call_id=call_123", "ElevenLabs", None)
    root = ET.fromstring(xml)
    relay = root.find("./Connect/ConversationRelay")
    assert relay is not None and relay.get("url") == "wss://example.ngrok.app/twilio/relay"
    assert relay.get("welcomeGreeting") == 'Hi "there" & welcome'
    assert relay.find("./Parameter").get("value") == "call_123"


def test_twilio_webhooks_require_valid_signature(client_factory):
    c = client_factory(twilio_auth_token="secret", public_base_url="https://vouch.test")
    params = {"CallStatus": "ringing", "CallSid": "CA1"}
    assert c.post("/twilio/status?call_id=call_x", data=params).status_code == 403
    sig = expected_signature("secret", "https://vouch.test/twilio/status?call_id=call_x", params)
    assert c.post("/twilio/status?call_id=call_x", data=params, headers={"X-Twilio-Signature": sig}).status_code == 204


TWILIO = dict(
    call_channel="twilio", twilio_account_sid="AC123", twilio_auth_token="secret", twilio_from_number="+14155550100",
    public_base_url="https://vouch.test", twilio_amd=False, call_ring_timeout_seconds=30,
)


@pytest.fixture
def fake_dial(monkeypatch):
    """Stand in for Twilio's REST API. Set .error to make dialing fail."""

    class Dialer:
        error: Exception | None = None
        dialed: list[str] = []

        async def create_call(self_client, to, twiml, call_id, public_base):  # noqa: N805
            Dialer.dialed.append(call_id)
            if Dialer.error:
                raise Dialer.error
            return f"CA{len(Dialer.dialed)}"

        async def hangup(self_client, sid):  # noqa: N805
            return None

    Dialer.dialed = []
    monkeypatch.setattr("vouch.calls.TwilioClient.create_call", Dialer.create_call)
    monkeypatch.setattr("vouch.calls.TwilioClient.hangup", Dialer.hangup)
    return Dialer


def twilio_calls(c, app_id, status="ringing"):
    d = c.get(f"/api/applications/{app_id}").json()
    return [call for r in d["references"] for call in r["calls"] if call["channel"] == "twilio" and call["status"] == status]


def test_twilio_conversation_relay_session(client_factory, fake_dial):
    """Drive the Twilio relay endpoint exactly as ConversationRelay would."""
    c = client_factory(**TWILIO)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    call = wait_until(lambda: twilio_calls(c, app_id))[0]
    ref = next(r for r in c.get(f"/api/applications/{app_id}").json()["references"] if any(x["id"] == call["id"] for x in r["calls"]))
    script = persona_for(ref["company"])["attempts"][-1]["script"]
    with c.websocket_connect("/twilio/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA1", "customParameters": {"call_id": call["id"]}}))
        lines = iter(script)
        # Twilio already spoke the welcome greeting, so we answer first.
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": next(lines), "last": True}))
        while True:
            msg = json.loads(ws.receive_text())
            if msg["type"] == "end":
                assert json.loads(msg["handoffData"])["reason"] == "completed"
                break
            if msg["last"]:
                ws.send_text(json.dumps({"type": "prompt", "voicePrompt": next(lines), "last": True}))
    got = wait_until(lambda: (r := c.get(f"/api/calls/{call['id']}").json())["status"] == "ended" and r)
    assert got["outcome"] == "completed"
    assert got["result"]["findings"]["high_credit"] in (40000, 20000)


def test_twilio_relay_refuses_calls_it_should_not_take(client_factory, fake_dial):
    """A late answer on a call that already ended, an unknown id, or a non-Twilio
    call must not start an interview."""
    c = client_factory(**TWILIO)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    call = wait_until(lambda: twilio_calls(c, app_id))[0]
    c.post("/twilio/status?call_id=" + call["id"], data={"CallStatus": "busy", "CallSid": "CA1"},
           headers={"X-Twilio-Signature": expected_signature("secret", f"https://vouch.test/twilio/status?call_id={call['id']}", {"CallStatus": "busy", "CallSid": "CA1"})})
    wait_until(lambda: c.get(f"/api/calls/{call['id']}").json()["status"] == "ended")
    for call_id in (call["id"], "call_unknown"):
        with c.websocket_connect("/twilio/relay") as ws:
            ws.send_text(json.dumps({"type": "setup", "callSid": "CA9", "customParameters": {"call_id": call_id}}))
            assert json.loads(ws.receive_text())["type"] == "end"
    assert c.get(f"/api/calls/{call['id']}").json()["outcome"] == "no_answer"  # not revived


def test_twilio_dial_failure_retries_promptly(client_factory, fake_dial):
    """If Twilio refuses the call, the step moves on in seconds, not after the
    ten-minute call timeout."""
    fake_dial.error = RuntimeError("21219: unverified number")
    c = client_factory(**TWILIO)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    detail = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())["status"] == "ready_for_review" and d, timeout=20)
    called = [r for r in detail["references"] if not r["flagged"]]
    assert all(r["status"] == "unreachable" and len(r["calls"]) == 3 for r in called)
    assert detail["decision_detail"]["decision"] == "request_info"


def test_reference_hanging_up_early_is_retried(client_factory):
    """A browser call that drops before the agent gets the numbers is not a
    completed reference."""
    c = client_factory(call_channel="browser", call_ring_timeout_seconds=30)
    c.post("/api/applications/from-sample", json={"sample_id": "acme"})
    call = wait_until(lambda: c.get("/api/calls/ringing").json())[0]
    with c.websocket_connect(f"/ws/phone/{call['id']}") as ws:
        ws.send_text(json.dumps({"type": "setup"}))
        while not json.loads(ws.receive_text())["last"]:
            pass
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Sure, go ahead.", "last": True}))
        while not json.loads(ws.receive_text())["last"]:
            pass
    ended = wait_until(lambda: (r := c.get(f"/api/calls/{call['id']}").json())["status"] == "ended" and r)
    assert ended["outcome"] == "failed"
    retry = wait_until(lambda: [r for r in c.get("/api/calls/ringing").json() if r["reference_company"] == call["reference_company"]])
    assert retry[0]["id"] != call["id"]


def test_call_channel_chosen_before_verification_applies(client_factory):
    c = client_factory(call_ring_timeout_seconds=30)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    assert c.post(f"/api/applications/{app_id}/call-channel", json={"channel": "browser"}).json()["call_channel"] == "browser"
    ringing = wait_until(lambda: c.get("/api/calls/ringing").json())
    assert {r["application_id"] for r in ringing} == {app_id}
    assert c.get(f"/api/applications/{app_id}").json()["call_channel"] == "browser"
    assert c.post(f"/api/applications/{app_id}/call-channel", json={"channel": "fax"}).status_code == 400


def test_demo_phone_rings_for_one_reference_at_a_time(client_factory, fake_dial):
    """With DEMO_REFERENCE_PHONE every call rings one phone, so the second reference
    waits until the first call is over instead of calling at the same moment."""
    c = client_factory(**TWILIO, demo_reference_phone="+14155550199", call_retry_seconds=0.5)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    first = wait_until(lambda: twilio_calls(c, app_id))[0]
    time.sleep(1.5)
    assert len(fake_dial.dialed) == 1 and len(twilio_calls(c, app_id)) == 1
    assert first["dialed_number"] == "(415) 555-0199"
    params = {"CallStatus": "no-answer", "CallSid": "CA1"}
    url = f"https://vouch.test/twilio/status?call_id={first['id']}"
    c.post(f"/twilio/status?call_id={first['id']}", data=params, headers={"X-Twilio-Signature": expected_signature("secret", url, params)})
    wait_until(lambda: len(fake_dial.dialed) >= 2)
    assert len(twilio_calls(c, app_id)) == 1


async def test_twilio_create_call_sends_a_form_the_async_client_accepts(tmp_path, monkeypatch):
    """Runs the real request code against a fake Twilio. A list-of-tuples body is
    sent by httpx as a sync stream, which AsyncClient refuses; this caught that."""
    from urllib.parse import parse_qs

    import httpx

    from vouch.providers.twilio_client import TwilioClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(201, json={"sid": "CA42"})

    real = httpx.AsyncClient
    monkeypatch.setattr("vouch.providers.twilio_client.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    settings = make_settings(tmp_path, **TWILIO)
    sid = await TwilioClient(settings).create_call("+14155550199", "<Response/>", "call_1", "https://vouch.test")
    assert sid == "CA42"
    req = seen["request"]
    assert req.url.path == "/2010-04-01/Accounts/AC123/Calls.json"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = parse_qs(req.content.decode())
    assert form["To"] == ["+14155550199"] and form["Timeout"] == ["30"]
    assert form["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
    assert form["StatusCallback"] == ["https://vouch.test/twilio/status?call_id=call_1"]


def test_real_call_limit_keeps_later_references_simulated(client_factory, fake_dial):
    """REAL_CALL_LIMIT=1: the first reference rings the real phone, the rest are simulated."""
    c = client_factory(**TWILIO, demo_reference_phone="+14155550199", real_call_limit=1)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    wait_until(lambda: twilio_calls(c, app_id))
    detail = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())
                        and next(r for r in d["references"] if r["company"].startswith("Coastal"))["status"] == "completed" and d)
    refs = {r["company"]: r for r in detail["references"]}
    assert refs["Harbor Paper Co."]["call_channel"] is None and refs["Harbor Paper Co."]["calls"][0]["channel"] == "twilio"
    assert refs["Coastal Flour Mills Inc."]["call_channel"] == "simulated"
    assert {x["channel"] for x in refs["Coastal Flour Mills Inc."]["calls"]} == {"simulated"}
    assert fake_dial.dialed == [refs["Harbor Paper Co."]["calls"][0]["id"]]


def test_reference_line_can_be_switched_and_called(client_factory, fake_dial):
    """Switch one reference to the real phone after its simulated call, then Call now."""
    c = client_factory(**{**TWILIO, "call_channel": "simulated"})
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    detail = wait_until(lambda: (d := c.get(f"/api/applications/{app_id}").json())["status"] == "ready_for_review" and d)
    coastal = next(r for r in detail["references"] if r["company"].startswith("Coastal"))
    assert c.post(f"/api/references/{coastal['id']}/call-channel", json={"channel": "fax"}).status_code == 400
    assert c.post(f"/api/references/{coastal['id']}/call-channel", json={"channel": "twilio"}).json()["resolved"] == "twilio"
    assert c.post(f"/api/references/{coastal['id']}/call-now", json={"channel": "twilio"}).json() == {"ok": True}
    wait_until(lambda: twilio_calls(c, app_id))
    after = next(r for r in c.get(f"/api/applications/{app_id}").json()["references"] if r["id"] == coastal["id"])
    assert after["call_channel"] == "twilio" and after["calls"][-1]["channel"] == "twilio"
    c.post(f"/api/references/{coastal['id']}/call-channel", json={"channel": None})
    assert next(r for r in c.get(f"/api/applications/{app_id}").json()["references"] if r["id"] == coastal["id"])["call_channel"] is None


def test_voicemail_on_a_real_call_is_not_treated_as_a_person(client_factory, fake_dial, monkeypatch):
    """A carrier greeting arrives as "Your call" then "has been forwarded...". The agent
    must not start the interview; when Twilio's machine detection reports a machine,
    Vouch leaves its message and records the call as voicemail."""
    updates = []

    async def update_call_twiml(self_client, sid, twiml):  # noqa: N805
        updates.append((sid, twiml))

    monkeypatch.setattr("vouch.calls.TwilioClient.update_call_twiml", update_call_twiml)
    c = client_factory(**TWILIO, demo_reference_phone="+14155550199", real_call_limit=1)
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    call = wait_until(lambda: twilio_calls(c, app_id))[0]
    with c.websocket_connect("/twilio/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA1", "customParameters": {"call_id": call["id"]}}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Your call", "last": True}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "has been forwarded to an automated voice messaging system.", "last": True}))
        time.sleep(0.3)
        params = {"AnsweredBy": "machine_end_beep", "CallSid": "CA1"}
        url = f"https://vouch.test/twilio/amd?call_id={call['id']}"
        assert c.post(f"/twilio/amd?call_id={call['id']}", data=params,
                      headers={"X-Twilio-Signature": expected_signature("secret", url, params)}).status_code == 204
        time.sleep(0.2)
    got = wait_until(lambda: (r := c.get(f"/api/calls/{call['id']}").json())["status"] == "ended" and r)
    assert got["outcome"] == "voicemail" and got["result"]["summary"] == "Reached voicemail and left a message."
    agent = [t for t in got["transcript"] if t["role"] == "agent"]
    assert len(agent) == 2 and agent[-1].get("voicemail_message")  # the greeting, then the message; no interview
    assert all(t.get("voicemail") for t in got["transcript"] if t["role"] == "reference")
    assert updates and "try you again later" in updates[0][1]  # Twilio plays it after the beep


def test_presenter_mode_waits_for_next_at_each_stage(client_factory):
    """With PRESENTER_MODE the demo pauses before the reference checks, the calls and
    the bank email, and moves on only when the presenter presses Next."""
    c = client_factory(presenter_mode=True)
    assert c.get("/api/config").json()["presenter_mode"] is True
    app_id = c.post("/api/applications/from-sample", json={"sample_id": "acme"}).json()["id"]
    detail = lambda: c.get(f"/api/applications/{app_id}").json()  # noqa: E731

    d = wait_until(lambda: (x := detail())["presenter"]["paused"] and x)
    assert d["presenter"]["next"] == "Check the references" and d["extracted"] is not None
    time.sleep(0.5)
    assert all(r["checks"] is None for r in detail()["references"])  # nothing ran ahead

    assert c.post(f"/api/applications/{app_id}/advance").json() == {"started": "Check the references"}
    d = wait_until(lambda: (x := detail())["presenter"]["paused"] and x["presenter"]["next"] == "Start the reference calls" and x)
    assert any(r["flagged"] for r in d["references"]) and all(not r["calls"] for r in d["references"])

    c.post(f"/api/applications/{app_id}/advance")
    d = wait_until(lambda: (x := detail())["presenter"]["paused"] and x["presenter"]["next"] == "Email the bank" and x, timeout=30)
    assert d["emails"] == []  # the bank waits for its scene

    c.post(f"/api/applications/{app_id}/advance")
    wait_until(lambda: detail()["status"] == "ready_for_review", timeout=30)
    assert c.post(f"/api/applications/{app_id}/advance").status_code == 409
