"""Small normalizers shared by agents and providers."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
    "proton.me", "protonmail.com", "live.com", "msn.com", "ymail.com", "mail.com", "gmx.com",
}

_SUFFIXES = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|ltd|limited|lp|llp|pllc|group)\b\.?",
    re.IGNORECASE,
)


def normalize_phone(raw: Optional[str], default_country: str = "1") -> Optional[str]:
    """Return E.164 (+15105550142) for US/Canada-style numbers, else digits with +."""
    if not raw:
        return None
    raw = raw.strip()
    ext_split = re.split(r"\s*(?:ext\.?|x|#)\s*\d+$", raw, flags=re.IGNORECASE)
    digits = re.sub(r"\D", "", ext_split[0])
    if raw.startswith("+") and len(digits) >= 8:
        return "+" + digits
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith(default_country):
        return "+" + digits
    if len(digits) >= 8:
        return "+" + digits
    return None


def format_phone(e164: Optional[str]) -> Optional[str]:
    if not e164:
        return None
    d = re.sub(r"\D", "", e164)
    if len(d) == 11 and d.startswith("1"):
        return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
    return e164


def domain_of(email_or_url: Optional[str]) -> Optional[str]:
    if not email_or_url:
        return None
    value = email_or_url.strip().lower()
    if "@" in value and "/" not in value:
        value = value.split("@", 1)[1]
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/", 1)[0].split(":", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    return value or None


def company_key(name: Optional[str]) -> str:
    """Comparable company name: lowercase, no legal suffixes or punctuation."""
    if not name:
        return ""
    value = _SUFFIXES.sub(" ", name.lower())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def address_key(street: Optional[str], postal_code: Optional[str] = None) -> str:
    if not street:
        return ""
    value = street.lower()
    replacements = {r"\bstreet\b": "st", r"\bavenue\b": "ave", r"\bsuite\b": "ste", r"\broad\b": "rd", r"\bboulevard\b": "blvd"}
    for pattern, repl in replacements.items():
        value = re.sub(pattern, repl, value)
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return f"{value}|{(postal_code or '').strip()[:5]}"


def days_between(earlier: Optional[date], later: Optional[date] = None) -> Optional[int]:
    if earlier is None:
        return None
    later = later or datetime.now(timezone.utc).date()
    return (later - earlier).days


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def money(amount: Optional[float]) -> str:
    if amount is None:
        return "unknown"
    return f"${amount:,.0f}"
