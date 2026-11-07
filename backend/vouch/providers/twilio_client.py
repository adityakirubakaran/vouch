"""Minimal Twilio REST + webhook helpers (no SDK needed)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Optional
from xml.sax.saxutils import quoteattr

import httpx

from ..config import Settings


class TwilioClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}"
        self.auth = (settings.twilio_account_sid or "", settings.twilio_auth_token or "")

    async def create_call(self, to: str, twiml: str, call_id: str, public_base: str) -> str:
        data: list[tuple[str, str]] = [
            ("To", to),
            ("From", self.settings.twilio_from_number or ""),
            ("Twiml", twiml),
            ("Timeout", str(self.settings.call_ring_timeout_seconds)),
            ("StatusCallback", f"{public_base}/twilio/status?call_id={call_id}"),
            ("StatusCallbackMethod", "POST"),
        ]
        for ev in ("initiated", "ringing", "answered", "completed"):
            data.append(("StatusCallbackEvent", ev))
        if self.settings.twilio_amd:
            data += [
                ("MachineDetection", "DetectMessageEnd"),
                ("AsyncAmd", "true"),
                ("AsyncAmdStatusCallback", f"{public_base}/twilio/amd?call_id={call_id}"),
                ("AsyncAmdStatusCallbackMethod", "POST"),
            ]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{self.base}/Calls.json", data=data, auth=self.auth)
        if resp.status_code >= 400:
            raise RuntimeError(f"Twilio create call failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["sid"]

    async def update_call_twiml(self, call_sid: str, twiml: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{self.base}/Calls/{call_sid}.json", data={"Twiml": twiml}, auth=self.auth)
        if resp.status_code >= 400:
            raise RuntimeError(f"Twilio update call failed ({resp.status_code}): {resp.text[:300]}")

    async def hangup(self, call_sid: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{self.base}/Calls/{call_sid}.json", data={"Status": "completed"}, auth=self.auth)


def relay_twiml(ws_url: str, call_id: str, greeting: str, action_url: str, tts_provider: str, voice: Optional[str]) -> str:
    voice_attr = f" voice={quoteattr(voice)}" if voice else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Connect action={quoteattr(action_url)}>"
        f"<ConversationRelay url={quoteattr(ws_url)} welcomeGreeting={quoteattr(greeting)} "
        f'welcomeGreetingInterruptible="speech" interruptible="speech" ttsProvider={quoteattr(tts_provider)}{voice_attr}>'
        f'<Parameter name="call_id" value={quoteattr(call_id)}/>'
        "</ConversationRelay></Connect></Response>"
    )


def say_and_hangup_twiml(message: str) -> str:
    from xml.sax.saxutils import escape

    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(message)}</Say><Hangup/></Response>'


def expected_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio request signature: HMAC-SHA1 over the full URL plus sorted POST params."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], signature: Optional[str]) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(expected_signature(auth_token, url, params), signature)
