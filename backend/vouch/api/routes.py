"""REST API for the review app."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..db import Application, Call, Email, EventRow, Evidence, Reference, Step
from ..fixtures_data import sample_by_id, sample_path, samples
from ..policy import load_policy
from ..services import Services
from . import serialize as out

router = APIRouter(prefix="/api")


def svc(request: Request) -> Services:
    return request.app.state.services


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


@router.get("/config")
async def config(request: Request) -> dict:
    s = svc(request)
    st = s.settings
    return {
        "seller_name": st.seller_name,
        "llm": {"mode": s.llm.mode, "model_smart": st.model_smart, "model_fast": st.model_fast},
        "calls": {
            "default_channel": s.calls.resolve_channel(None),
            "configured_channel": st.call_channel,
            "twilio_configured": st.twilio_configured,
            "public_base_url": st.public_base_url,
            "demo_reference_phone": bool(st.demo_reference_phone),
            "max_attempts": st.call_max_attempts,
            "retry_seconds": st.call_retry_seconds,
        },
        "email": {"mode": st.email_mode, "simulate_bank_reply_seconds": st.simulate_bank_reply_seconds},
        "registry": {"mode": st.registry_mode, "opencorporates": bool(st.opencorporates_api_token), "live_domain_checks": st.live_domain_checks},
        "accounting": s.accounting.status(),
    }


@router.get("/policy")
async def policy() -> dict:
    return load_policy()


@router.get("/samples")
async def list_samples() -> list[dict]:
    return [
        {"id": x["id"], "title": x["title"], "description": x["description"], "available": sample_path(x).exists(),
         "url": f"/api/samples/{x['id']}/document"}
        for x in samples()
    ]


@router.get("/samples/{sample_id}/document")
async def sample_document(sample_id: str) -> FileResponse:
    sample = sample_by_id(sample_id)
    if sample is None or not sample_path(sample).exists():
        raise HTTPException(404, "Unknown sample")
    return FileResponse(sample_path(sample), media_type="application/pdf")


# ---------------------------------------------------------------- applications


@router.get("/applications")
async def list_applications(request: Request) -> list[dict]:
    s = svc(request)
    with s.db.session() as db:
        apps = db.scalars(select(Application).order_by(Application.created_at.desc())).all()
        return [out.summary_out(a) for a in apps]


class FromSample(BaseModel):
    sample_id: str


@router.post("/applications", status_code=201)
async def upload_application(request: Request, file: UploadFile = File(...)) -> dict:
    s = svc(request)
    data = await file.read()
    if not data:
        raise HTTPException(400, "The file is empty")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Keep applications under 25 MB")
    try:
        app_id = s.workflow.create_application(file.filename or "application.pdf", data)  # type: ignore[union-attr]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": app_id}


@router.post("/applications/from-sample", status_code=201)
async def application_from_sample(request: Request, body: FromSample) -> dict:
    s = svc(request)
    sample = sample_by_id(body.sample_id)
    if sample is None or not sample_path(sample).exists():
        raise HTTPException(404, "Unknown sample")
    path = sample_path(sample)
    app_id = s.workflow.create_application(path.name, path.read_bytes(), sample_id=sample["id"])  # type: ignore[union-attr]
    return {"id": app_id}


def load_detail(s: Services, app_id: str) -> dict:
    with s.db.session() as db:
        app = db.get(Application, app_id)
        if app is None:
            raise HTTPException(404, "Unknown application")
        steps = db.scalars(select(Step).where(Step.application_id == app_id).order_by(Step.created_at)).all()
        evidence = db.scalars(select(Evidence).where(Evidence.application_id == app_id).order_by(Evidence.created_at)).all()
        emails = db.scalars(select(Email).where(Email.application_id == app_id).order_by(Email.created_at)).all()
        return out.detail_out(app, list(steps), list(evidence), list(emails), s.llm.mode)


@router.get("/applications/{app_id}")
async def get_application(request: Request, app_id: str) -> dict:
    return load_detail(svc(request), app_id)


@router.get("/applications/{app_id}/document")
async def get_document(request: Request, app_id: str) -> FileResponse:
    s = svc(request)
    with s.db.session() as db:
        app = db.get(Application, app_id)
        if app is None:
            raise HTTPException(404, "Unknown application")
        path = Path(app.source_path)
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, headers={"Content-Disposition": f'inline; filename="{path.name}"'})


@router.get("/applications/{app_id}/events")
async def get_events(request: Request, app_id: str) -> list[dict]:
    s = svc(request)
    with s.db.session() as db:
        rows = db.scalars(select(EventRow).where(EventRow.application_id == app_id).order_by(EventRow.id)).all()
        return [{"id": r.id, "ts": out.iso(r.ts), "type": r.type, "message": r.message, "data": r.data} for r in rows]


class ApproveBody(BaseModel):
    limit: Optional[float] = None
    terms: Optional[str] = None
    note: Optional[str] = None


@router.post("/applications/{app_id}/approve")
async def approve(request: Request, app_id: str, body: ApproveBody) -> dict:
    s = svc(request)
    try:
        s.workflow.approve(app_id, body.limit, body.terms, body.note)  # type: ignore[union-attr]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return load_detail(s, app_id)


class DeclineBody(BaseModel):
    reason: Optional[str] = None


@router.post("/applications/{app_id}/decline")
async def decline(request: Request, app_id: str, body: DeclineBody) -> dict:
    s = svc(request)
    try:
        s.workflow.decline(app_id, body.reason)  # type: ignore[union-attr]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return load_detail(s, app_id)


class InfoBody(BaseModel):
    message: Optional[str] = None


@router.post("/applications/{app_id}/request-info")
async def request_info(request: Request, app_id: str, body: InfoBody) -> dict:
    s = svc(request)
    try:
        await s.workflow.request_info(app_id, body.message)  # type: ignore[union-attr]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return load_detail(s, app_id)


@router.post("/applications/{app_id}/sync")
async def retry_sync(request: Request, app_id: str) -> dict:
    s = svc(request)
    try:
        s.workflow.retry_sync(app_id)  # type: ignore[union-attr]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return load_detail(s, app_id)


class ChannelBody(BaseModel):
    channel: str


@router.post("/applications/{app_id}/call-channel")
async def set_call_channel(request: Request, app_id: str, body: ChannelBody) -> dict:
    if body.channel not in ("simulated", "browser", "twilio"):
        raise HTTPException(400, "channel must be simulated, browser, or twilio")
    s = svc(request)
    s.workflow.set_default_channel(app_id, body.channel)  # type: ignore[union-attr]
    return {"ok": True}


@router.post("/applications/{app_id}/simulate-bank-reply")
async def simulate_bank_reply(request: Request, app_id: str) -> dict:
    s = svc(request)
    try:
        email_id = s.workflow.simulate_bank_reply(app_id)  # type: ignore[union-attr]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"email_id": email_id}


class InboundEmail(BaseModel):
    application_id: str
    from_addr: Optional[str] = None
    subject: str = "Bank reference"
    text: str


@router.post("/email/inbound")
async def inbound_email(request: Request, body: InboundEmail) -> dict:
    """Generic inbound-email webhook (Postmark, SendGrid Inbound Parse, or a script)."""
    s = svc(request)
    email_id = s.mailer.receive(body.application_id, body.from_addr, body.subject, body.text, delivery="webhook")
    return {"email_id": email_id}


# ------------------------------------------------------------------ references


class CallNowBody(BaseModel):
    channel: Optional[str] = None


@router.post("/references/{ref_id}/call-now")
async def call_now(request: Request, ref_id: str, body: CallNowBody) -> dict:
    s = svc(request)
    if body.channel and body.channel not in ("simulated", "browser", "twilio"):
        raise HTTPException(400, "channel must be simulated, browser, or twilio")
    ok = s.workflow.call_now(ref_id, body.channel)  # type: ignore[union-attr]
    if not ok:
        raise HTTPException(409, "This reference is not waiting for a call")
    return {"ok": True}


@router.get("/calls/ringing")
async def ringing_calls(request: Request) -> list[dict]:
    s = svc(request)
    with s.db.session() as db:
        calls = db.scalars(select(Call).where(Call.status == "ringing", Call.channel == "browser").order_by(Call.created_at)).all()
        result = []
        for c in calls:
            ref = db.get(Reference, c.reference_id)
            app = db.get(Application, c.application_id)
            result.append({
                "id": c.id,
                "application_id": c.application_id,
                "reference_company": ref.company if ref else None,
                "contact_name": ref.contact_name if ref else None,
                "applicant": app.applicant_name if app else None,
                "seller": s.settings.seller_name,
                "dialed_number": out.call_out(c)["dialed_number"],
                "ring_deadline": out.iso(c.ring_deadline),
            })
        return result


@router.get("/calls/{call_id}")
async def get_call(request: Request, call_id: str) -> dict:
    s = svc(request)
    with s.db.session() as db:
        c = db.get(Call, call_id)
        if c is None:
            raise HTTPException(404, "Unknown call")
        return out.call_out(c)


# ------------------------------------------------------------------- QuickBooks


@router.get("/integrations/quickbooks/connect")
async def quickbooks_connect(request: Request):
    s = svc(request)
    acct = s.accounting
    if acct.mode != "quickbooks":
        raise HTTPException(400, "Set ACCOUNTING_MODE=quickbooks and QuickBooks client credentials in .env first")
    return RedirectResponse(acct.authorize_url())  # type: ignore[attr-defined]


@router.get("/integrations/quickbooks/callback")
async def quickbooks_callback(request: Request, code: str, realmId: str, state: str):  # noqa: N803 - Intuit's name
    s = svc(request)
    try:
        await s.accounting.complete_oauth(code, realmId, state)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"QuickBooks connection failed: {exc}") from exc
    return RedirectResponse("/settings?quickbooks=connected")
