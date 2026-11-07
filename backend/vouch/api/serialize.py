"""Database rows to the JSON the web app consumes. The TypeScript mirror of these
shapes lives in frontend/src/api/types.ts."""

from __future__ import annotations

from typing import Any, Optional

from ..db import Application, Call, Email, Evidence, Reference, Step
from ..util import format_phone


def iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def call_out(c: Call) -> dict:
    return {
        "id": c.id,
        "attempt": c.attempt,
        "channel": c.channel,
        "dialed_number": format_phone(c.dialed_number),
        "status": c.status,
        "outcome": c.outcome,
        "transcript": c.transcript or [],
        "live_findings": c.live_findings,
        "result": c.result,
        "created_at": iso(c.created_at),
        "started_at": iso(c.started_at),
        "ended_at": iso(c.ended_at),
        "ring_deadline": iso(c.ring_deadline),
    }


def reference_out(r: Reference, step: Optional[Step]) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "position": r.position,
        "company": r.company,
        "contact_name": r.contact_name,
        "phone": r.phone,
        "phone_display": format_phone(r.phone),
        "email": r.email,
        "status": r.status,
        "flagged": r.flagged,
        "checks": r.checks,
        "findings": r.findings,
        "outcome": r.outcome,
        "summary": r.summary,
        "calls": [call_out(c) for c in r.calls],
        "step": step_out(step) if step else None,
    }


def step_out(s: Step) -> dict:
    return {
        "id": s.id,
        "kind": s.kind,
        "reference_id": s.reference_id,
        "status": s.status,
        "attempts": s.attempts,
        "call_attempts": (s.state or {}).get("call_attempts"),
        "wait_reason": s.wait_reason,
        "next_run_at": iso(s.next_run_at) if s.status == "pending" else None,
        "timeout_at": iso(s.timeout_at),
        "error": s.error,
        "created_at": iso(s.created_at),
        "updated_at": iso(s.updated_at),
    }


def evidence_out(e: Evidence) -> dict:
    return {
        "code": e.id.split(".", 1)[1],
        "kind": e.kind,
        "label": e.label,
        "detail": e.detail,
        "source_id": e.source_id,
    }


def email_out(e: Email) -> dict:
    return {
        "id": e.id,
        "reference_id": e.reference_id,
        "direction": e.direction,
        "to_addr": e.to_addr,
        "from_addr": e.from_addr,
        "subject": e.subject,
        "body": e.body,
        "delivery": e.delivery,
        "created_at": iso(e.created_at),
    }


def summary_out(a: Application) -> dict:
    decision = a.decision or {}
    refs = [r for r in a.references if r.kind == "trade"]
    return {
        "id": a.id,
        "created_at": iso(a.created_at),
        "updated_at": iso(a.updated_at),
        "status": a.status,
        "applicant_name": a.applicant_name,
        "requested_limit": a.requested_limit,
        "requested_terms": a.requested_terms,
        "decision": decision.get("decision"),
        "recommended_limit": decision.get("recommended_limit"),
        "approved_limit": (a.review or {}).get("limit"),
        "source_filename": a.source_filename,
        "sample_id": a.sample_id,
        "reference_count": len(refs),
        "flagged_references": sum(1 for r in refs if r.flagged),
        "completed_references": sum(1 for r in refs if r.status == "completed"),
    }


def detail_out(a: Application, steps: list[Step], evidence: list[Evidence], emails: list[Email], llm_mode: str, default_channel: str) -> dict:
    by_ref = {s.reference_id: s for s in steps if s.reference_id}
    bank_step = next((s for s in steps if s.kind == "bank"), None)
    refs = [reference_out(r, by_ref.get(r.id)) for r in a.references if r.kind == "trade"]
    bank_ref = next((r for r in a.references if r.kind == "bank"), None)
    return {
        **summary_out(a),
        "extracted": a.extracted,
        "applicant_checks": a.applicant_checks,
        "decision_detail": a.decision,
        "review": a.review,
        "sync": a.sync,
        "error": a.error,
        "references": refs,
        "bank": reference_out(bank_ref, bank_step) if bank_ref else None,
        "steps": [step_out(s) for s in steps],
        "evidence": [evidence_out(e) for e in evidence],
        "emails": [email_out(e) for e in emails],
        "document_url": f"/api/applications/{a.id}/document",
        "llm_mode": llm_mode,
        "call_channel": a.call_channel or default_channel,
    }
