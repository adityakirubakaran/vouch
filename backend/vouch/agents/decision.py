"""Memo agent: applies the credit policy, then has Claude explain it.

Split of responsibilities:
  * evaluate() is plain Python. It decides approve / reduce / request info /
    review and computes the limit, so the same file always gets the same answer.
  * write_narrative() asks Claude to explain that result in a few sentences,
    citing evidence IDs. Citations to unknown IDs are dropped, and if Claude is
    unavailable a template narrative is used.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from ..llm import LLM, fake_handler, text_of
from ..schemas import BankFindings, CreditApplication, MemoNarrative, MemoPoint, ReferenceFindings
from ..util import money


@dataclass
class RefInput:
    id: str
    company: str
    flagged: bool
    outcome: Optional[str]
    findings: Optional[ReferenceFindings]
    signals: list[dict]
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Fact:
    id: str  # evidence id the fact rests on (or "policy")
    text: str


def terms_days(terms: Optional[str]) -> Optional[int]:
    m = re.search(r"(\d+)", terms or "")
    return int(m.group(1)) if m else None


def evaluate(
    policy: dict,
    application: CreditApplication,
    applicant_signals: list[dict],
    refs: list[RefInput],
    bank: Optional[BankFindings],
    bank_evidence: Optional[str],
    app_evidence: str,
    applicant_registry_evidence: Optional[str],
) -> dict:
    lim = policy["limits"]
    terms = policy.get("terms_offered", "Net 30")
    requested = application.requested_limit
    facts: list[Fact] = []
    risks: list[Fact] = []
    math_lines: list[str] = []
    missing: list[str] = []

    facts.append(Fact(app_evidence, f"Requested {money(requested)} on {application.requested_terms or terms}."))
    if applicant_registry_evidence:
        facts.append(Fact(applicant_registry_evidence, "Applicant registration checked with the state."))

    critical = [s for s in applicant_signals if s["severity"] == "critical"]
    for s in applicant_signals:
        (risks if s["severity"] != "info" else facts).append(Fact(applicant_registry_evidence or app_evidence, s["message"]))

    completed = [r for r in refs if not r.flagged and r.outcome == "completed" and r.findings and r.findings.high_credit]
    flagged = [r for r in refs if r.flagged]
    unreachable = [r for r in refs if not r.flagged and r.outcome not in ("completed",)]

    for r in completed:
        f = r.findings
        assert f is not None
        bits = [f"{r.company} extends {money(f.high_credit)} high credit"]
        if f.terms:
            bits.append(f"on {f.terms}")
        if f.avg_days_to_pay is not None:
            bits.append(f"and is paid in about {f.avg_days_to_pay:.0f} days")
        if f.past_due_amount:
            bits.append(f"with {money(f.past_due_amount)} past due")
        facts.append(Fact(r.evidence_ids[0] if r.evidence_ids else "policy", " ".join(bits) + "."))
    for r in flagged:
        why = " ".join(s["message"] for s in r.signals if s["severity"] in ("critical", "warn")) or "It failed verification."
        risks.append(Fact(r.evidence_ids[0] if r.evidence_ids else "policy", f"{r.company} was excluded and not called. {why}"))
    for r in unreachable:
        risks.append(Fact(r.evidence_ids[0] if r.evidence_ids else "policy", f"{r.company} did not give a phone reference ({r.outcome or 'not reached'})."))

    if bank:
        facts.append(Fact(bank_evidence or "policy", bank.summary))
    else:
        risks.append(Fact("policy", "The bank has not replied yet."))

    decision = "approve"
    limit: Optional[float] = None
    reason = ""

    # Hard stops first.
    if any(s["rule"] == "APPLICANT_INACTIVE" for s in critical):
        decision, reason = "decline", "The applicant is not an active registered business."
    elif any(s["rule"] == "AUTH_MISSING" for s in critical):
        decision, reason = "request_info", "The application needs a signed authorization to check references."
        missing.append("A signed authorization to contact the bank and trade references")
    elif len(completed) < lim["min_completed_references"]:
        need = lim["min_completed_references"] - len(completed)
        decision, reason = "request_info", f"Only {len(completed)} trade reference{'s' if len(completed) != 1 else ''} could be verified; policy needs {lim['min_completed_references']}."
        missing.append(f"{need} more trade reference{'s' if need != 1 else ''} we can verify by phone")
    else:
        avg_high = sum(r.findings.high_credit for r in completed) / len(completed)  # type: ignore[union-attr,misc]
        base = avg_high * lim["fraction_of_avg_high_credit"]
        math_lines.append(f"Average high credit from {len(completed)} verified references: {money(avg_high)}")
        math_lines.append(f"Policy factor of {lim['fraction_of_avg_high_credit']:.0%}: {money(base)}")

        offered_days = terms_days(terms) or 30
        days = [r.findings.avg_days_to_pay for r in completed if r.findings and r.findings.avg_days_to_pay is not None]
        if days:
            avg_days = sum(days) / len(days)
            if avg_days > offered_days + lim["slow_pay_grace_days"]:
                base *= lim["slow_pay_fraction"]
                math_lines.append(
                    f"Pays in {avg_days:.0f} days on average, more than {lim['slow_pay_grace_days']} days past {terms}, "
                    f"so the limit is cut to {lim['slow_pay_fraction']:.0%}: {money(base)}"
                )
                risks.append(Fact("policy", f"References are paid in {avg_days:.0f} days on average, slower than {terms}."))
            else:
                math_lines.append(f"Pays in {avg_days:.0f} days on average, within {terms} plus {lim['slow_pay_grace_days']} days of grace, so no reduction")

        past_due = sum((r.findings.past_due_amount or 0) for r in completed if r.findings)
        step = lim["round_down_to"]
        limit = math.floor(base / step) * step
        math_lines.append(f"Rounded down to the nearest {money(step)}: {money(limit)}")
        if lim.get("cap_at_requested") and requested and limit > requested:
            limit = requested
            math_lines.append(f"Capped at the {money(requested)} requested")

        if limit <= 0:
            decision, reason = "request_info", "Verified references do not support any limit under policy."
            limit = None
        elif past_due > 0.1 * avg_high:
            decision, reason = "manual_review", f"References report {money(past_due)} past due."
        elif bank and ((bank.nsf_or_overdrafts_last_12mo or 0) > policy["bank"]["max_nsf_last_12mo"] or bank.relationship_rating == "unsatisfactory"):
            decision, reason = "manual_review", "The bank reported problems with the account."
        elif limit > lim["senior_review_above"]:
            decision, reason = "manual_review", f"Limits above {money(lim['senior_review_above'])} need a senior credit manager."
        elif requested and limit < requested:
            decision, reason = "approve_reduced", f"Verified references support {money(limit)}, below the {money(requested)} requested."
        else:
            decision, reason = "approve", f"Verified references support the full {money(limit)} requested."

    return {
        "decision": decision,
        "reason": reason,
        "recommended_limit": limit,
        "requested_limit": requested,
        "terms": terms,
        "math": math_lines,
        "missing": missing,
        "verified_references": len(completed),
        "flagged_references": len(flagged),
        "facts": [f.__dict__ for f in facts],
        "risks": [f.__dict__ for f in risks],
    }


NARRATIVE_TOOL = "write_credit_memo"

SYSTEM = """You write the summary section of credit memos for {seller}'s credit team. \
The decision and every number are already computed by the credit policy; do not change \
them or introduce new numbers. Write plainly, the way an experienced credit analyst writes \
to a busy manager. Each sentence must cite the evidence IDs it relies on, using only IDs \
from the facts provided."""


async def write_narrative(llm: LLM, seller: str, applicant: str, result: dict) -> MemoNarrative:
    payload = {
        "applicant": applicant,
        "decision": result["decision"],
        "reason": result["reason"],
        "recommended_limit": result["recommended_limit"],
        "requested_limit": result["requested_limit"],
        "terms": result["terms"],
        "limit_math": result["math"],
        "facts": result["facts"],
        "risks": result["risks"],
        "missing": result["missing"],
    }
    narrative = await llm.structured(
        role="smart",
        system=SYSTEM.format(seller=seller),
        content=[{"type": "text", "text": f"Policy result and facts:\n{json.dumps(payload, indent=2)}\n\nCall {NARRATIVE_TOOL}."}],
        output=MemoNarrative,
        tool_name=NARRATIVE_TOOL,
        tool_description="Write the credit memo headline, supporting points, and risks.",
        max_tokens=1500,
    )
    return sanitize(narrative, result)


def known_ids(result: dict) -> set[str]:
    return {f["id"] for f in result["facts"] + result["risks"]}


def sanitize(narrative: MemoNarrative, result: dict) -> MemoNarrative:
    ids = known_ids(result)

    def clean(points: list[MemoPoint]) -> list[MemoPoint]:
        out = []
        for p in points:
            p.evidence_ids = [e for e in p.evidence_ids if e in ids and e != "policy"]
            out.append(p)
        return out

    narrative.points = clean(narrative.points)
    narrative.risks = clean(narrative.risks)
    return narrative


def template_narrative(applicant: str, result: dict) -> MemoNarrative:
    headlines = {
        "approve": f"Approve {applicant} for {money(result['recommended_limit'])} on {result['terms']}.",
        "approve_reduced": f"Approve {applicant} for {money(result['recommended_limit'])} on {result['terms']}, below the {money(result['requested_limit'])} requested.",
        "request_info": f"Ask {applicant} for more information before deciding.",
        "manual_review": f"Send {applicant} to a credit manager for review.",
        "decline": f"Decline {applicant}'s application for terms.",
    }
    points = [MemoPoint(text=f["text"], evidence_ids=[f["id"]] if f["id"] != "policy" else []) for f in result["facts"][1:5]]
    risks = [MemoPoint(text=f["text"], evidence_ids=[f["id"]] if f["id"] != "policy" else []) for f in result["risks"]]
    headline = headlines[result["decision"]]
    if result["decision"] in ("request_info", "manual_review", "decline"):
        headline = f"{headline} {result['reason']}"
    return MemoNarrative(headline=headline, points=points, risks=risks)


@fake_handler(NARRATIVE_TOOL)
def _offline(system: str, content: list[dict]) -> dict:
    raw = text_of(content).split("\n", 1)[1].rsplit("\n\nCall", 1)[0]
    payload = json.loads(raw)
    result = {**payload, "math": payload["limit_math"]}
    return template_narrative(payload["applicant"], result).model_dump()
