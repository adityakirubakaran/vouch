"""Reference authenticity score: how likely is it that a trade reference is a real,
independent business and not someone the applicant set up?

The score is a logistic scorecard, the same model family credit bureaus use for
credit scores, so every point can be explained:

    log-odds = base + sum(weight of each finding)
    score    = 100 / (1 + e^-log-odds), shown from 1 to 99

Findings come from the application form, state records, the company's website,
domain age, phone intelligence (IPQualityScore's fraud model, carrier caller ID
and line type from Twilio, Google's business listings), and Vouch's own history
of numbers it has seen before. Findings against a reference are rule signals
(see verification.py); findings for it are corroborations, computed here.

Weights live in credit_policy.yaml. They are set by hand from how strongly each
finding points to a set-up reference. Once there are enough decided applications,
they can be refit with a logistic regression on the stored findings.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import asdict, dataclass
from typing import Optional

from ..providers.phone import SOURCE_LABEL
from ..util import FREE_EMAIL_DOMAINS, company_key, domain_of, format_phone, normalize_phone
from .verification import PartyInput, Signal, humanize_age

# Where each rule's evidence comes from, for the "why" list on the review screen.
RULE_SOURCE = {
    "REF_PHONE_IS_APPLICANT": "Application form",
    "REF_PHONE_SHARED": "Application form",
    "REF_ADDRESS_IS_APPLICANT": "Application form",
    "REF_NOT_REGISTERED": "State records",
    "REF_INACTIVE": "State records",
    "REF_NEWLY_REGISTERED": "State records",
    "REF_SHARES_OFFICER": "State records",
    "REF_NEW_DOMAIN": "Domain records",
    "REF_FREE_EMAIL": "Application form",
    "REF_PHONE_NOT_ON_WEBSITE": "Company website",
    "PHONE_SEEN_OTHER_COMPANY": "Vouch history",
    "PHONE_FLAGGED_BEFORE": "Vouch history",
}


@dataclass
class Factor:
    code: str
    label: str
    weight: float  # log-odds; negative counts against the reference
    source: str

    def to_dict(self) -> dict:
        return {**asdict(self), "weight": round(self.weight, 2), "impact": impact(self.weight)}


def impact(weight: float) -> str:
    """How much a finding moves the score, in words, for the review screen."""
    size = abs(weight)
    return "strong" if size >= 2 else "moderate" if size >= 0.8 else "slight"


# ------------------------------------------------------------------ names


def _squash(name: Optional[str]) -> str:
    return company_key(name).replace(" ", "")


def company_name_matches(candidate: Optional[str], company: Optional[str]) -> bool:
    """Does a caller ID or directory name refer to this company? Caller ID is cut
    to 15 characters ("COASTAL FLOUR M"), so a long enough prefix counts."""
    a, b = _squash(candidate), _squash(company)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 6 and long_.startswith(short):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


def person_name_matches(candidate: Optional[str], person: Optional[str]) -> bool:
    """Caller ID often lists people as LAST FIRST."""
    tokens = lambda s: set(re.findall(r"[a-z]+", (s or "").lower())) - {"mr", "ms", "mrs", "dr", "jr", "sr"}
    a, b = tokens(candidate), tokens(person)
    return bool(a) and len(a) >= 2 and a == b


# --------------------------------------------------------- phone signals


def phone_signals(ref: PartyInput, phone: Optional[dict], applicant_names: list[str], policy: dict) -> list[Signal]:
    """Findings against a reference from phone intelligence."""
    if not phone or not phone.get("checked"):
        return []
    cfg = policy["reference_score"]
    out: list[Signal] = []
    names = [(phone.get("caller_name"), "caller ID")] + [(x.get("name"), "a business listing") for x in phone.get("listings", [])]

    hit = next(((n, where) for n, where in names if n and any(company_name_matches(n, a) for a in applicant_names)), None)
    if hit:
        applicant = next(a for a in applicant_names if company_name_matches(hit[0], a))
        out.append(Signal("PHONE_LISTED_TO_APPLICANT", "critical",
                          f"{_capital(hit[1])} lists this number to {applicant}, the applicant, not to {ref.company}."))
    else:
        business_names = [n for n, _ in names[1:] if n]
        if phone.get("caller_type") == "BUSINESS" and phone.get("caller_name"):
            business_names.insert(0, phone["caller_name"])
        other = [n for n in business_names if not company_name_matches(n, ref.company)]
        if other and not any(company_name_matches(n, ref.company) for n in business_names):
            out.append(Signal("PHONE_LISTED_TO_OTHER", "warn", f"This number belongs to {_title(other[0])}, not {ref.company}."))

    if phone.get("line_type") == "nonFixedVoip":
        out.append(Signal("PHONE_VIRTUAL", "warn", "It is a virtual number of the kind anyone can create online, like Google Voice."))
    if phone.get("valid") is False or phone.get("active") is False:
        out.append(Signal("PHONE_INACTIVE", "warn", "The line appears to be invalid or disconnected."))
    score = phone.get("fraud_score")
    if score is not None and score >= cfg["high_fraud_score"]:
        out.append(Signal("PHONE_HIGH_FRAUD_SCORE", "critical", f"IPQualityScore rates this number {score}/100 for fraud risk."))
    elif score is not None and score >= cfg["elevated_fraud_score"]:
        out.append(Signal("PHONE_ELEVATED_FRAUD_SCORE", "warn", f"IPQualityScore rates this number {score}/100 for fraud risk."))
    if phone.get("recent_abuse"):
        out.append(Signal("PHONE_RECENT_ABUSE", "warn", "The number was recently reported for abuse."))
    return out


def history_signals(ref: PartyInput, sightings: list[dict]) -> list[Signal]:
    """Findings against a reference from numbers Vouch saw on earlier applications."""
    out: list[Signal] = []
    for s in sightings:
        if s["role"] == "applicant" and not company_name_matches(s["company"], ref.company):
            out.append(Signal("PHONE_SEEN_OTHER_COMPANY", "critical",
                              f"Vouch saw this number as {s['company']}'s own line on an earlier application ({_when(s['date'])})."))
            break
        if s["role"] == "reference" and not company_name_matches(s["company"], ref.company):
            out.append(Signal("PHONE_SEEN_OTHER_COMPANY", "critical",
                              f"Vouch saw this number listed for {s['company']} on an earlier application ({_when(s['date'])})."))
            break
    if any(s["role"] == "reference" and s.get("flagged") and company_name_matches(s["company"], ref.company) for s in sightings):
        out.append(Signal("PHONE_FLAGGED_BEFORE", "critical", "This reference was flagged as likely fake on an earlier application."))
    return out


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _title(name: str) -> str:
    return name.title() if name.isupper() else name


SERVICE_FOR = {"caller_id": "Twilio caller ID", "listing": "Google Places", "fraud": "IPQualityScore", "line": "Twilio line type"}


def phone_source(phone: dict, kind: str) -> str:
    """Name the service behind a phone finding. Sandbox answers stand in for the real
    services on the demo numbers, and say so."""
    sources = phone.get("sources") or []
    if "sandbox" in sources:
        return f"{SERVICE_FOR[kind]} (sandbox)"
    if kind == "caller_id":
        return {"twilio": "Twilio caller ID", "ipqs": "IPQualityScore"}.get(phone.get("caller_name_source") or "", "Caller ID")
    if kind == "listing":
        return "Google Places"
    if kind == "fraud":
        return "IPQualityScore"
    return "Twilio line type" if "twilio" in sources else "IPQualityScore"


def _when(date_iso: Optional[str]) -> str:
    from datetime import date

    try:
        d = date.fromisoformat(date_iso or "")
    except ValueError:
        return "an earlier date"
    return f"{d.strftime('%b')} {d.day}, {d.year}"


# --------------------------------------------------------- corroboration


def corroborations(ref: PartyInput, checks: dict, signals: list[Signal], officers: list[str],
                   sightings: list[dict], policy: dict) -> list[Factor]:
    """Findings for a reference: independent evidence that it is a real business."""
    cfg = policy["reference_score"]
    w = cfg["weights"]
    rules = {s.rule for s in signals}
    out: list[Factor] = []
    years = cfg["established_years"]

    reg = checks.get("registry") or {}
    if reg.get("found") and reg.get("age_days") is not None and reg["age_days"] >= years * 365 and "REF_INACTIVE" not in rules:
        out.append(Factor("REGISTERED_ESTABLISHED", f"Registered with the state for {humanize_age(reg['age_days'])}.", w["REGISTERED_ESTABLISHED"], "State records"))
    dom = checks.get("domain") or {}
    if dom.get("found") and dom.get("age_days") is not None and dom["age_days"] >= years * 365:
        out.append(Factor("DOMAIN_ESTABLISHED", f"Its website domain is {humanize_age(dom['age_days'])} old.", w["DOMAIN_ESTABLISHED"], "Domain records"))
    web = checks.get("website") or {}
    ref_phone = normalize_phone(ref.phone)
    if web.get("checked") and ref_phone and ref_phone in (web.get("phones") or []):
        out.append(Factor("PHONE_ON_WEBSITE", "The number is published on the company's own website.", w["PHONE_ON_WEBSITE"], "Company website"))
    email_domain = domain_of(ref.email)
    site_domain = domain_of(web.get("url")) or (dom.get("domain") if dom else None)
    if email_domain and email_domain not in FREE_EMAIL_DOMAINS and site_domain and email_domain == site_domain:
        out.append(Factor("EMAIL_ON_COMPANY_DOMAIN", "The contact's email is on the company's own domain.", w["EMAIL_ON_COMPANY_DOMAIN"], "Application form"))

    phone = checks.get("phone") or {}
    if phone.get("checked") and not ({"PHONE_LISTED_TO_APPLICANT", "PHONE_LISTED_TO_OTHER"} & rules):
        caller = phone.get("caller_name")
        officer = next((o for o in officers if caller and person_name_matches(caller, o)), None)
        if caller and company_name_matches(caller, ref.company):
            out.append(Factor("CALLER_ID_MATCHES", f"Carrier caller ID lists the number as {caller}.", w["CALLER_ID_MATCHES"], phone_source(phone, "caller_id")))
        elif officer:
            out.append(Factor("CALLER_ID_IS_OFFICER", f"Caller ID lists the number to {_title(officer)}, an officer of {ref.company} on the state record.",
                              w["CALLER_ID_IS_OFFICER"], phone_source(phone, "caller_id")))
        listing = next((x for x in phone.get("listings", []) if company_name_matches(x.get("name"), ref.company)), None)
        if listing:
            where = f" at {listing['address']}" if listing.get("address") else ""
            out.append(Factor("LISTED_TO_COMPANY", f"Google lists this number for {listing['name']}{where}.",
                              w["LISTED_TO_COMPANY"], phone_source(phone, "listing")))
        score = phone.get("fraud_score")
        if score is not None and score <= cfg["low_fraud_score"]:
            out.append(Factor("LOW_FRAUD_SCORE", f"IPQualityScore's fraud model rates this number {score}/100.", w["LOW_FRAUD_SCORE"],
                              phone_source(phone, "fraud")))
        if phone.get("line_type") in ("landline", "fixedVoip") and phone.get("caller_type") != "CONSUMER":
            kind = "landline" if phone["line_type"] == "landline" else "business VoIP line"
            out.append(Factor("BUSINESS_LINE", f"It is a {kind}{' with ' + phone['carrier'] if phone.get('carrier') else ''}.",
                              w["BUSINESS_LINE"], phone_source(phone, "line")))

    if any(s["role"] == "reference" and s.get("status") == "completed" and company_name_matches(s["company"], ref.company) for s in sightings):
        when = next(s["date"] for s in sightings if s.get("status") == "completed")
        out.append(Factor("VERIFIED_BEFORE", f"This number answered a reference call for {ref.company} on an earlier application ({_when(when)}).",
                          w["VERIFIED_BEFORE"], "Vouch history"))
    return out


# ------------------------------------------------------------------ score


def score_reference(ref: PartyInput, checks: dict, signals: list[Signal], officers: list[str],
                    sightings: list[dict], policy: dict) -> dict:
    cfg = policy["reference_score"]
    w = cfg["weights"]
    against = [
        Factor(s.rule, s.message, w.get(s.rule, -1.0 if s.severity == "critical" else -0.6),
               RULE_SOURCE.get(s.rule) or _phone_source(s.rule, checks))
        for s in signals if s.severity in ("warn", "critical")
    ]
    support = corroborations(ref, checks, signals, officers, sightings, policy)
    log_odds = cfg["base_log_odds"] + sum(f.weight for f in against + support)
    score = max(1, min(99, round(100 / (1 + math.exp(-log_odds)))))
    hard_stop = any(s.severity == "critical" for s in signals)
    flagged = hard_stop or score < cfg["flag_below"]
    if flagged:
        band = "likely_fake"
    elif score >= cfg["genuine_at"]:
        band = "likely_genuine"
    else:
        band = "unconfirmed"
    phone = checks.get("phone") or {}
    sources = sorted({f.source for f in against + support})
    return {
        "score": score,
        "band": band,
        "flagged": flagged,
        "hard_stop": hard_stop,
        "log_odds": round(log_odds, 2),
        "factors": [f.to_dict() for f in sorted(against + support, key=lambda f: -abs(f.weight))],
        "sources": sources,
        "phone_sources": [SOURCE_LABEL.get(s, s) for s in phone.get("sources", [])],
        "model": "Logistic scorecard (weights in credit_policy.yaml)",
    }


def _phone_source(rule: str, checks: dict) -> str:
    phone = checks.get("phone") or {}
    if rule in ("PHONE_HIGH_FRAUD_SCORE", "PHONE_ELEVATED_FRAUD_SCORE", "PHONE_RECENT_ABUSE", "PHONE_INACTIVE"):
        return phone_source(phone, "fraud")
    if rule == "PHONE_VIRTUAL":
        return phone_source(phone, "line")
    # Listed to the applicant / another business: caller ID and listings together.
    has_listing = bool(phone.get("listings"))
    has_caller = bool(phone.get("caller_name"))
    if has_listing and has_caller:
        return f"{phone_source(phone, 'caller_id')} + Google Places" if "sandbox" not in (phone.get("sources") or []) else "Caller ID + Google Places (sandbox)"
    return phone_source(phone, "listing" if has_listing else "caller_id")


def phone_summary(ref: PartyInput, phone: Optional[dict]) -> Optional[str]:
    """One sentence for the evidence list: what the phone lookup found."""
    if not phone or not phone.get("checked"):
        return None
    bits = []
    kind = {"landline": "a landline", "mobile": "a mobile line", "fixedVoip": "a business VoIP line",
            "nonFixedVoip": "a virtual number", "voip": "a VoIP line", "tollFree": "a toll-free number"}.get(phone.get("line_type") or "")
    num = format_phone(phone.get("number")) or ref.phone
    head = f"{num} is {kind}" if kind else f"{num} was looked up"
    if phone.get("carrier"):
        head += f" ({phone['carrier']})"
    bits.append(head)
    if phone.get("caller_name"):
        bits.append(f"caller ID {phone['caller_name']}")
    for x in phone.get("listings", [])[:1]:
        bits.append(f"listed for {x['name']}")
    if phone.get("fraud_score") is not None:
        bits.append(f"fraud score {phone['fraud_score']}/100")
    labels = ", ".join(SOURCE_LABEL.get(s, s) for s in phone.get("sources", []))
    return "; ".join(bits) + f" ({labels})."
