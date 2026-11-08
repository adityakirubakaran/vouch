"""Records agent: checks the applicant and every reference against public records
and applies fraud rules.

The rules are deterministic on purpose. A credit decision needs reasons you can
repeat to an applicant (and to a regulator), so a model never decides what is
flagged. Each rule produces a plain-English signal that the memo can cite.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import Optional

from ..providers.phone import PhoneProvider
from ..providers.records import DomainProvider, RegistryProvider, WebsiteProvider
from ..schemas import CreditApplication
from ..util import FREE_EMAIL_DOMAINS, address_key, company_key, domain_of, normalize_phone


@dataclass
class Signal:
    rule: str
    severity: str  # info | warn | critical
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PartyInput:
    key: str  # "applicant" or a reference id
    company: str
    phone: Optional[str]
    email: Optional[str]
    website: Optional[str]
    street: Optional[str]
    postal_code: Optional[str]
    state: Optional[str]


def humanize_age(days: Optional[int]) -> str:
    if days is None:
        return "Unknown"
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 60:
        return f"{days // 7} weeks"
    if days < 365:
        return f"{days // 30} months"
    return f"{days // 365} years"


def registered_label(record: dict) -> str:
    days = record.get("age_days")
    if not record.get("found"):
        return "Not found"
    if days is not None and days < 365:
        return f"{days} days ago"
    inc = record.get("incorporated_on")
    return inc[:4] if inc else "Yes"


def building_key(street: Optional[str], postal_code: Optional[str]) -> str:
    if not street:
        return ""
    base = re.split(r",|\b(?:unit|suite|ste|apt|#)\b", street, maxsplit=1, flags=re.IGNORECASE)[0]
    return address_key(base, postal_code)


async def check_party(
    party: PartyInput,
    registry: RegistryProvider,
    domains: DomainProvider,
    websites: WebsiteProvider,
    phones: Optional[PhoneProvider] = None,
) -> dict:
    """Public records for one party. With a phone provider (trade references), also
    look up who the listed phone number belongs to."""
    site_domain = domain_of(party.website) or domain_of(party.email)
    if site_domain in FREE_EMAIL_DOMAINS:
        site_domain = domain_of(party.website)

    async def no_phone():
        return None

    reg, web, phone = await asyncio.gather(
        registry.lookup(party.company, party.state),
        websites.phones_for(party.company, party.website or (f"https://{site_domain}" if site_domain else None)),
        phones.lookup(party.phone) if phones is not None else no_phone(),
    )
    # A reference that only gave a free email may still have a website we found by name.
    dom = await domains.lookup(site_domain or domain_of(web.url))
    return {
        "registry": reg.to_dict(),
        "domain": dom.to_dict() if dom else None,
        "website": web.to_dict(),
        "phone": phone.to_dict() if phone is not None else None,
    }


def reference_signals(
    ref: PartyInput,
    checks: dict,
    applicant: PartyInput,
    applicant_checks: dict,
    applicant_people: set[str],
    other_refs: list[PartyInput],
    policy: dict,
) -> list[Signal]:
    v = policy["verification"]
    out: list[Signal] = []
    ref_phone = normalize_phone(ref.phone)
    app_phone = normalize_phone(applicant.phone)

    if ref_phone and app_phone and ref_phone == app_phone:
        out.append(Signal("REF_PHONE_IS_APPLICANT", "critical", "Phone number is the applicant's own line."))
    for other in other_refs:
        if ref_phone and normalize_phone(other.phone) == ref_phone:
            out.append(Signal("REF_PHONE_SHARED", "critical", f"Shares a phone number with {other.company}."))
            break

    if building_key(ref.street, ref.postal_code) and building_key(ref.street, ref.postal_code) == building_key(applicant.street, applicant.postal_code):
        out.append(Signal("REF_ADDRESS_IS_APPLICANT", "critical", "Listed at the applicant's own address."))

    reg = checks["registry"]
    if not reg.get("found"):
        # The sandbox registry only knows the demo companies, so a miss there means
        # "not searched", not "not registered".
        if reg.get("source") != "sandbox":
            out.append(Signal("REF_NOT_REGISTERED", "warn", f"No business registration found ({reg.get('source')})."))
    else:
        status = (reg.get("status") or "").lower()
        if status and status not in ("active", "good standing", "registered"):
            out.append(Signal("REF_INACTIVE", "critical", f"Registry status is {reg.get('status')}."))
        age = reg.get("age_days")
        if age is not None and age < v["reference_min_age_days"]:
            out.append(Signal("REF_NEWLY_REGISTERED", "critical", f"Registered only {age} days ago."))
        shared = {o.lower() for o in reg.get("officers", [])} & applicant_people
        if shared:
            names = ", ".join(sorted(n.title() for n in shared))
            out.append(Signal("REF_SHARES_OFFICER", "critical", f"Shares an officer with the applicant ({names})."))

    dom = checks.get("domain")
    if dom and dom.get("found") and dom.get("age_days") is not None and dom["age_days"] < v["domain_min_age_days"]:
        out.append(Signal("REF_NEW_DOMAIN", "warn", f"Website domain is {humanize_age(dom['age_days'])} old."))

    email_domain = domain_of(ref.email)
    if email_domain in FREE_EMAIL_DOMAINS:
        out.append(Signal("REF_FREE_EMAIL", "warn", f"Uses a free email address ({email_domain})."))

    web = checks.get("website") or {}
    if web.get("checked") and web.get("phones") and ref_phone and ref_phone not in web["phones"]:
        out.append(Signal("REF_PHONE_NOT_ON_WEBSITE", "warn", "Phone number does not appear on the company's website."))
    return out


def is_flagged(signals: list[Signal], policy: dict) -> bool:
    if any(s.severity == "critical" for s in signals):
        return True
    return sum(1 for s in signals if s.severity == "warn") >= policy["verification"]["flag_on_warnings"]


def display_rows(ref: PartyInput, checks: dict, signals: list[Signal], factors: Optional[list[dict]] = None) -> list[dict]:
    """The three rows the review screen shows on each reference card."""
    rules = {s.rule for s in signals}
    found = {f["code"] for f in factors or []}
    reg = checks["registry"]
    reg_ok = reg.get("found") and not ({"REF_NEWLY_REGISTERED", "REF_INACTIVE"} & rules)

    phone_rows = [
        ("REF_PHONE_IS_APPLICANT", "Applicant's own line", False),
        ("PHONE_LISTED_TO_APPLICANT", "Listed to the applicant", False),
        ("REF_PHONE_SHARED", "Shared with another reference", False),
        ("PHONE_SEEN_OTHER_COMPANY", "Seen for another company", False),
        ("PHONE_LISTED_TO_OTHER", "Listed to another business", False),
        ("PHONE_VIRTUAL", "Virtual number", False),
        ("PHONE_HIGH_FRAUD_SCORE", "High fraud risk", False),
        ("REF_PHONE_NOT_ON_WEBSITE", "Not on its website", False),
        ("CALLER_ID_MATCHES", "Belongs to the company", True),
        ("LISTED_TO_COMPANY", "Belongs to the company", True),
        ("CALLER_ID_IS_OFFICER", "An officer's line", True),
        ("PHONE_ON_WEBSITE", "Matches its website", True),
    ]
    phone_value, phone_ok = next(((v, ok) for code, v, ok in phone_rows if code in rules or code in found), ("Not checked", None))
    if phone_ok is None and (checks.get("website") or {}).get("checked") and checks["website"].get("phones"):
        phone_value, phone_ok = "Matches its website", True

    dom = checks.get("domain") or {}
    if dom.get("found"):
        site_value = humanize_age(dom.get("age_days"))
        site_ok = "REF_NEW_DOMAIN" not in rules
    else:
        site_value, site_ok = "Unknown", None

    if not reg.get("found") and reg.get("source") == "sandbox":
        reg_row = {"label": "Registered", "value": "Not checked", "ok": None}
    else:
        reg_row = {"label": "Registered", "value": registered_label(reg), "ok": bool(reg_ok)}
    return [
        reg_row,
        {"label": "Phone number", "value": phone_value, "ok": phone_ok},
        {"label": "Website age", "value": site_value, "ok": site_ok},
    ]


def applicant_signals(application: CreditApplication, checks: dict, policy: dict) -> list[Signal]:
    out: list[Signal] = []
    reg = checks["registry"]
    if not reg.get("found"):
        out.append(Signal("APPLICANT_NOT_REGISTERED", "warn", "No business registration found for the applicant."))
    else:
        status = (reg.get("status") or "").lower()
        if status and status not in ("active", "good standing", "registered"):
            out.append(Signal("APPLICANT_INACTIVE", "critical", f"Applicant's registry status is {reg.get('status')}."))
    years = application.applicant.years_in_business
    if years is None and reg.get("age_days") is not None:
        years = reg["age_days"] / 365
    if years is not None and years < policy["applicant"]["min_years_in_business"]:
        out.append(Signal("APPLICANT_YOUNG", "warn", f"Applicant has been in business about {years:.1f} years."))
    if policy["applicant"]["require_signed_authorization"] and not (
        application.signature.signed and application.signature.authorizes_reference_checks
    ):
        out.append(Signal("AUTH_MISSING", "critical", "Application is not signed with authorization to check references."))
    ein = application.applicant.ein
    if ein and not re.fullmatch(r"\d{2}-?\d{7}", ein.strip()):
        out.append(Signal("EIN_FORMAT", "warn", f"EIN '{ein}' is not in the NN-NNNNNNN format."))
    return out


def applicant_people(application: CreditApplication, applicant_checks: dict) -> set[str]:
    people = {o.lower() for o in applicant_checks["registry"].get("officers", [])}
    if application.applicant.principal_name:
        people.add(application.applicant.principal_name.lower())
    if application.signature.signer_name:
        people.add(application.signature.signer_name.lower())
    return people


def same_company(a: str, b: str) -> bool:
    return company_key(a) == company_key(b)
