"""Public-record lookups used by the verification agent.

  RegistryProvider  business registration (sandbox fixtures or OpenCorporates)
  DomainProvider    domain registration date (sandbox fixtures or live RDAP)
  WebsiteProvider   phone numbers published on a company's website
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional

import httpx

from ..config import Settings
from ..fixtures_data import resolve_date, sandbox
from ..util import company_key, days_between, normalize_phone, parse_date

log = logging.getLogger("vouch.records")


@dataclass
class RegistryRecord:
    found: bool
    query: str
    legal_name: Optional[str] = None
    company_number: Optional[str] = None
    jurisdiction: Optional[str] = None
    status: Optional[str] = None
    incorporated_on: Optional[str] = None
    age_days: Optional[int] = None
    registered_address: Optional[str] = None
    officers: list[str] = field(default_factory=list)
    source: str = "sandbox"
    source_url: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DomainRecord:
    domain: str
    found: bool
    created_on: Optional[str] = None
    age_days: Optional[int] = None
    source: str = "sandbox"
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WebsiteRecord:
    url: Optional[str]
    checked: bool
    phones: list[str] = field(default_factory=list)
    source: str = "sandbox"
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _record_from_fixture(query: str, row: dict) -> RegistryRecord:
    inc: Optional[date] = resolve_date(row.get("incorporated"))
    return RegistryRecord(
        found=True,
        query=query,
        legal_name=row["name"],
        company_number=row.get("company_number"),
        jurisdiction=row.get("jurisdiction"),
        status=row.get("status"),
        incorporated_on=inc.isoformat() if inc else None,
        age_days=days_between(inc),
        registered_address=row.get("registered_address"),
        officers=list(row.get("officers", [])),
        source="sandbox",
    )


class RegistryProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def lookup(self, name: str, state: Optional[str] = None) -> RegistryRecord:
        key = company_key(name)
        for row in sandbox()["companies"]:
            if company_key(row["name"]) == key:
                return _record_from_fixture(name, row)
        if self.settings.registry_mode == "opencorporates" and self.settings.opencorporates_api_token:
            return await self._opencorporates(name, state)
        return RegistryRecord(found=False, query=name, source=self.settings.registry_mode)

    async def _opencorporates(self, name: str, state: Optional[str]) -> RegistryRecord:
        params = {"q": name, "api_token": self.settings.opencorporates_api_token, "per_page": "5"}
        if state:
            params["jurisdiction_code"] = f"us_{state.lower()}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.opencorporates.com/v0.4/companies/search", params=params)
                resp.raise_for_status()
            companies = resp.json().get("results", {}).get("companies", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("opencorporates lookup failed: %s", exc)
            return RegistryRecord(found=False, query=name, source="opencorporates", error=str(exc))
        key = company_key(name)
        best = None
        for item in companies:
            c = item.get("company", {})
            if company_key(c.get("name")) == key:
                best = c
                break
        if best is None and companies:
            best = companies[0].get("company")
        if not best:
            return RegistryRecord(found=False, query=name, source="opencorporates")
        inc = parse_date(best.get("incorporation_date"))
        return RegistryRecord(
            found=company_key(best.get("name")) == key,
            query=name,
            legal_name=best.get("name"),
            company_number=best.get("company_number"),
            jurisdiction=best.get("jurisdiction_code"),
            status=best.get("current_status"),
            incorporated_on=inc.isoformat() if inc else None,
            age_days=days_between(inc),
            registered_address=best.get("registered_address_in_full"),
            source="opencorporates",
            source_url=best.get("opencorporates_url"),
        )


class DomainProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def lookup(self, domain: Optional[str]) -> Optional[DomainRecord]:
        if not domain:
            return None
        fixture = sandbox()["domains"].get(domain)
        if fixture is not None:
            created = resolve_date(fixture.get("created"))
            return DomainRecord(domain=domain, found=True, created_on=created.isoformat() if created else None, age_days=days_between(created))
        if domain.endswith(".example") or not self.settings.live_domain_checks:
            return DomainRecord(domain=domain, found=False, source="sandbox")
        return await self._rdap(domain)

    async def _rdap(self, domain: str) -> DomainRecord:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                resp = await client.get(f"https://rdap.org/domain/{domain}")
            if resp.status_code == 404:
                return DomainRecord(domain=domain, found=False, source="rdap")
            resp.raise_for_status()
            events = resp.json().get("events", [])
            created = next((parse_date(e.get("eventDate")) for e in events if e.get("eventAction") == "registration"), None)
            return DomainRecord(domain=domain, found=True, created_on=created.isoformat() if created else None, age_days=days_between(created), source="rdap")
        except Exception as exc:  # noqa: BLE001
            log.warning("rdap lookup failed for %s: %s", domain, exc)
            return DomainRecord(domain=domain, found=False, source="rdap", error=str(exc))


_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")


class WebsiteProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def phones_for(self, company: str, website: Optional[str]) -> WebsiteRecord:
        key = company_key(company)
        for name, row in sandbox()["websites"].items():
            if company_key(name) == key:
                return WebsiteRecord(url=row.get("url"), checked=True, phones=[p for p in (normalize_phone(x) for x in row.get("phones", [])) if p])
        if not website or website.endswith(".example") or not self.settings.live_domain_checks:
            return WebsiteRecord(url=website, checked=False)
        url = website if website.startswith("http") else f"https://{website}"
        phones: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": "VouchCreditDesk/0.1"}) as client:
                for path in ("", "/contact", "/contact-us"):
                    try:
                        resp = await client.get(url.rstrip("/") + path)
                    except httpx.HTTPError:
                        continue
                    if resp.status_code == 200:
                        for match in _PHONE_RE.findall(resp.text):
                            p = normalize_phone(match)
                            if p:
                                phones.add(p)
            return WebsiteRecord(url=url, checked=True, phones=sorted(phones), source="live")
        except Exception as exc:  # noqa: BLE001
            return WebsiteRecord(url=url, checked=False, source="live", error=str(exc))
