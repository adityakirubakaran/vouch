"""Live channels: Server-Sent Events for the review screen, WebSockets for calls,
and Twilio's voice webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from ..calls import WebSocketTransport
from ..events import CLOSE
from ..providers.twilio_client import valid_signature
from ..services import Services

log = logging.getLogger("vouch.realtime")
router = APIRouter()


def svc_from(app) -> Services:
    return app.state.services


@router.get("/api/stream")
async def stream(request: Request, application_id: Optional[str] = None) -> StreamingResponse:
    s = svc_from(request.app)
    sub = s.bus.subscribe(application_id)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=15)
                    if event.get("type") == CLOSE:
                        break  # server is shutting down
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            s.bus.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.websocket("/ws/phone/{call_id}")
async def browser_phone(ws: WebSocket, call_id: str) -> None:
    """The in-app phone: the browser does speech recognition and synthesis and
    exchanges ConversationRelay messages with the voice agent."""
    await ws.accept()
    s = svc_from(ws.app)
    await s.calls.answer_browser(call_id, WebSocketTransport(ws))


# ------------------------------------------------------------------- Twilio


async def _verified_form(request: Request) -> dict:
    s = svc_from(request.app)
    form = {k: str(v) for k, v in (await request.form()).items()}
    st = s.settings
    if st.validate_twilio_signatures and st.twilio_auth_token:
        base = (st.public_base_url or str(request.base_url)).rstrip("/")
        url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not valid_signature(st.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature")):
            raise HTTPException(403, "Invalid Twilio signature")
    return form


@router.post("/twilio/status")
async def twilio_status(request: Request, call_id: str) -> Response:
    form = await _verified_form(request)
    svc_from(request.app).calls.twilio_status(call_id, form.get("CallStatus", ""))
    return Response(status_code=204)


@router.post("/twilio/amd")
async def twilio_amd(request: Request, call_id: str) -> Response:
    form = await _verified_form(request)
    await svc_from(request.app).calls.twilio_amd(call_id, form.get("AnsweredBy", ""))
    return Response(status_code=204)


@router.post("/twilio/connect-action")
async def twilio_connect_action(request: Request, call_id: str) -> PlainTextResponse:
    await _verified_form(request)
    return PlainTextResponse('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>', media_type="text/xml")


@router.websocket("/twilio/relay")
async def twilio_relay(ws: WebSocket) -> None:
    await ws.accept()
    await svc_from(ws.app).calls.run_twilio_relay(WebSocketTransport(ws))
