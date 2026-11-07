"""Loaders for the bundled demo data (samples, sandbox registry, personas)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .config import REPO_ROOT
from .util import company_key

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def resolve_date(value: Any) -> Optional[date]:
    """Fixture dates are ISO strings or {"days_ago": N} relative to today."""
    if value is None:
        return None
    if isinstance(value, dict) and "days_ago" in value:
        return datetime.now(timezone.utc).date() - timedelta(days=int(value["days_ago"]))
    return date.fromisoformat(str(value))


@lru_cache
def samples() -> list[dict]:
    return _load("samples.json")["samples"]


def sample_by_id(sample_id: str) -> Optional[dict]:
    return next((s for s in samples() if s["id"] == sample_id), None)


def sample_path(sample: dict) -> Path:
    return REPO_ROOT / sample["file"]


@lru_cache
def _sample_hashes() -> dict[str, str]:
    out: dict[str, str] = {}
    for s in samples():
        p = sample_path(s)
        if p.exists():
            out[hashlib.sha256(p.read_bytes()).hexdigest()] = s["id"]
    return out


def sample_for_bytes(data: bytes) -> Optional[dict]:
    sample_id = _sample_hashes().get(hashlib.sha256(data).hexdigest())
    return sample_by_id(sample_id) if sample_id else None


@lru_cache
def sandbox() -> dict:
    return _load("sandbox.json")


@lru_cache
def personas() -> dict:
    return _load("personas.json")


def persona_for(company: str) -> Optional[dict]:
    key = company_key(company)
    refs = personas()["references"]
    if key in refs:
        return refs[key]
    for k, v in refs.items():
        if k in key or key in k:
            return v
    return None


def bank_reply_for(bank_name: str) -> Optional[str]:
    key = company_key(bank_name)
    for k, v in personas()["banks"].items():
        if company_key(k) == key:
            return v
    return None
