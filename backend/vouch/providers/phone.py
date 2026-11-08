"""Who does this phone number actually belong to?

A fake trade reference usually gives a number the applicant controls: their own
line, a friend's cell, or a virtual number made online for the occasion. Three
outside sources can expose that, and each is used when its key is configured:

  IPQualityScore   phone reputation from IPQS's fraud models: a 0-100 fraud score,
                   VoIP / prepaid flags, recent abuse reports, whether the line is
                   active, the carrier, and sometimes the owner's name
  Twilio Lookup    the carrier's caller ID name (CNAM, US numbers) and the line type:
                   landline, mobile, fixedVoip, or nonFixedVoip (Google Voice and
                   similar numbers anyone can create online)
  Google Places    which business, if any, publicly lists this number

The sources run in parallel and their answers are merged. Numbers in the demo
applications (555-01xx, reserved for fiction) always use built-in sandbox data,
so the demo behaves the same with or without keys.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import quote

import httpx

from ..config import Settings
from ..fixtures_data import sandbox
from ..util import normalize_phone

log = logging.getLogger("vouch.phone")

TIMEOUT = 8.0

SOURCE_LABEL = {
    "sandbox": "Sandbox data",
    "ipqs": "IPQualityScore",
    "twilio": "Twilio Lookup",
    "google": "Google Places",
}


@dataclass
class Listing:
    """A business directory entry that shows this phone number."""

    name: str
    address: Optional[str] = None
    status: Optional[str] = None  # OPERATIONAL, CLOSED_TEMPORARILY, CLOSED_PERMANENTLY
    source: str = "sandbox"


@dataclass
class PhoneIntel:
    number: Optional[str]  # E.164
    checked: bool = False
    sources: list[str] = field(default_factory=list)
    valid: Optional[bool] = None
    active: Optional[bool] = None
    line_type: Optional[str] = None  # landline | mobile | fixedVoip | nonFixedVoip | voip | tollFree | prepaid | unknown
    carrier: Optional[str] = None
    caller_name: Optional[str] = None  # carrier caller ID (CNAM), else the reputation owner name
    caller_name_source: Optional[str] = None
    caller_type: Optional[str] = None  # BUSINESS | CONSUMER
    fraud_score: Optional[int] = None  # IPQualityScore, 0-100
    recent_abuse: Optional[bool] = None
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.upper() in ("N/A", "NA", "UNKNOWN", "NULL") else text


def _ipqs_line_type(raw: Optional[str], voip: Optional[bool]) -> Optional[str]:
    t = (raw or "").strip().lower()
    if voip and t in ("", "voip", "n/a"):
        return "voip"
    return {
        "landline": "landline",
        "wireless": "mobile",
        "mobile": "mobile",
        "voip": "voip",
        "toll free": "tollFree",
        "prepaid": "prepaid",
    }.get(t, None if t in ("", "n/a") else "unknown")


def parse_ipqs(data: dict) -> PhoneIntel:
    intel = PhoneIntel(number=None, checked=True, sources=["ipqs"])
    if not data.get("success", False):
        intel.checked = False
        intel.errors.append(f"IPQualityScore: {data.get('message') or 'lookup failed'}")
        return intel
    intel.valid = data.get("valid")
    intel.active = data.get("active")
    intel.fraud_score = int(data["fraud_score"]) if data.get("fraud_score") is not None else None
    intel.recent_abuse = data.get("recent_abuse")
    intel.line_type = _ipqs_line_type(data.get("line_type"), data.get("VOIP"))
    intel.carrier = _clean(data.get("carrier"))
    name = _clean(data.get("name"))
    if name:
        intel.caller_name, intel.caller_name_source = name, "ipqs"
    return intel


def parse_twilio(data: dict) -> PhoneIntel:
    intel = PhoneIntel(number=data.get("phone_number"), checked=True, sources=["twilio"])
    intel.valid = data.get("valid")
    lti = data.get("line_type_intelligence") or {}
    if lti.get("type"):
        intel.line_type = lti["type"]
    intel.carrier = _clean(lti.get("carrier_name"))
    cn = data.get("caller_name") or {}
    name = _clean(cn.get("caller_name"))
    if name:
        intel.caller_name, intel.caller_name_source = name, "twilio"
    intel.caller_type = _clean(cn.get("caller_type"))
    return intel


def parse_places(data: dict, number: str) -> PhoneIntel:
    intel = PhoneIntel(number=number, checked=True, sources=["google"])
    for place in data.get("places") or []:
        phones = {normalize_phone(place.get("internationalPhoneNumber")), normalize_phone(place.get("nationalPhoneNumber"))}
        if number not in phones:
            continue  # text search also returns loose matches; keep exact phone matches only
        intel.listings.append(Listing(
            name=(place.get("displayName") or {}).get("text") or "Unnamed business",
            address=place.get("formattedAddress"),
            status=place.get("businessStatus"),
            source="google",
        ))
    return intel


def merge(number: str, parts: list[PhoneIntel]) -> PhoneIntel:
    """Combine sources. Twilio's carrier data wins for line type and caller ID;
    IPQualityScore supplies the fraud score; Google supplies listings."""
    out = PhoneIntel(number=number)
    by_source = {p.sources[0]: p for p in parts if p.sources}
    for p in parts:
        out.errors.extend(p.errors)
        if p.checked:
            out.checked = True
            out.sources.extend(s for s in p.sources if s not in out.sources)
        out.listings.extend(p.listings)
    order = [by_source.get("twilio"), by_source.get("ipqs"), by_source.get("google")]
    for p in order:
        if p is None or not p.checked:
            continue
        out.valid = out.valid if out.valid is not None else p.valid
        out.line_type = out.line_type or p.line_type
        out.carrier = out.carrier or p.carrier
        if not out.caller_name and p.caller_name:
            out.caller_name, out.caller_name_source = p.caller_name, p.caller_name_source
        out.caller_type = out.caller_type or p.caller_type
    ipqs = by_source.get("ipqs")
    if ipqs is not None and ipqs.checked:
        out.fraud_score, out.recent_abuse, out.active = ipqs.fraud_score, ipqs.recent_abuse, ipqs.active
    return out


def _from_fixture(number: str, row: dict) -> PhoneIntel:
    return PhoneIntel(
        number=number,
        checked=True,
        sources=["sandbox"],
        valid=row.get("valid", True),
        active=row.get("active", True),
        line_type=row.get("line_type"),
        carrier=row.get("carrier"),
        caller_name=row.get("caller_name"),
        caller_name_source="sandbox" if row.get("caller_name") else None,
        caller_type=row.get("caller_type"),
        fraud_score=row.get("fraud_score"),
        recent_abuse=row.get("recent_abuse", False),
        listings=[Listing(name=x["name"], address=x.get("address"), status=x.get("status", "OPERATIONAL")) for x in row.get("listings", [])],
    )


class PhoneProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def live_sources(self) -> list[str]:
        st = self.settings
        out = []
        if st.ipqs_api_key:
            out.append("ipqs")
        if st.twilio_lookup_configured:
            out.append("twilio")
        if st.google_maps_api_key:
            out.append("google")
        return out

    async def lookup(self, raw: Optional[str]) -> PhoneIntel:
        number = normalize_phone(raw)
        if not number:
            return PhoneIntel(number=None)
        for key, row in (sandbox().get("phones") or {}).items():
            if normalize_phone(key) == number:
                return _from_fixture(number, row)
        calls = {"ipqs": self._ipqs, "twilio": self._twilio, "google": self._places}
        sources = self.live_sources
        if not sources:
            return PhoneIntel(number=number)
        results = await asyncio.gather(*[calls[s](number) for s in sources], return_exceptions=True)
        parts: list[PhoneIntel] = []
        for source, res in zip(sources, results):
            if isinstance(res, Exception):
                log.warning("%s phone lookup failed: %s", source, res)
                parts.append(PhoneIntel(number=number, sources=[source], errors=[f"{SOURCE_LABEL[source]}: {res}"]))
            else:
                parts.append(res)
        return merge(number, parts)

    async def _ipqs(self, number: str) -> PhoneIntel:
        url = f"https://www.ipqualityscore.com/api/json/phone/{self.settings.ipqs_api_key}/{number.lstrip('+')}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url, params={"country[]": "US", "strictness": 0})
            r.raise_for_status()
            intel = parse_ipqs(r.json())
        intel.number = number
        return intel

    async def _twilio(self, number: str) -> PhoneIntel:
        st = self.settings
        url = f"https://lookups.twilio.com/v2/PhoneNumbers/{quote(number)}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url, params={"Fields": "line_type_intelligence,caller_name"},
                                 auth=(st.twilio_account_sid or "", st.twilio_auth_token or ""))
            r.raise_for_status()
            intel = parse_twilio(r.json())
        intel.number = number
        return intel

    async def _places(self, number: str) -> PhoneIntel:
        # Google's guidance for phone searches: country code, a space, then the number.
        query = f"+{number[1:-10]} {number[-10:]}" if number.startswith("+") and len(number) > 10 else number
        headers = {
            "X-Goog-Api-Key": self.settings.google_maps_api_key or "",
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
                                "places.internationalPhoneNumber,places.businessStatus",
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post("https://places.googleapis.com/v1/places:searchText", headers=headers,
                                  json={"textQuery": query, "regionCode": "US", "pageSize": 5})
            r.raise_for_status()
            return parse_places(r.json(), number)
