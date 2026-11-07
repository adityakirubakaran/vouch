"""Extraction agent: scanned credit application in, structured CreditApplication out.

Claude reads the PDF natively (each page goes in as an image plus any text layer),
so handwriting and scans work without a separate OCR step. The output is forced
through a tool whose schema is the CreditApplication model.
"""

from __future__ import annotations

import base64
from typing import Optional

from ..fixtures_data import sample_for_bytes
from ..llm import LLM, LLMError, fake_handler
from ..schemas import CreditApplication
from ..util import normalize_phone

TOOL = "record_credit_application"

SYSTEM = """You are the intake clerk on {seller}'s credit team. You read commercial \
credit applications that customers fill out, often by hand and then scanned, and \
record exactly what they say.

Rules:
- Copy names, addresses, phone numbers, and emails exactly as written. Never guess a digit.
- If a field is blank, unreadable, or ambiguous, leave it null and add an entry to issues.
- requested_limit is a plain number in US dollars (e.g. 30000).
- Record only the last four digits of any bank account number.
- List trade references in the order they appear. Do not include the bank as a trade reference.
- signature.signed is true only if the authorization section is actually signed.
- signature.authorizes_reference_checks is true only if the form's authorization language \
lets the seller contact the applicant's bank and trade references and the form is signed.
"""

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def media_type_for(filename: str) -> Optional[str]:
    lower = filename.lower()
    for ext, mt in MEDIA_TYPES.items():
        if lower.endswith(ext):
            return mt
    return None


def build_content(data: bytes, media_type: str) -> list[dict]:
    encoded = base64.standard_b64encode(data).decode("ascii")
    if media_type == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
    return [block, {"type": "text", "text": f"Record this credit application by calling {TOOL}."}]


async def extract_application(llm: LLM, seller: str, data: bytes, media_type: str) -> CreditApplication:
    application = await llm.structured(
        role="smart",
        system=SYSTEM.format(seller=seller),
        content=build_content(data, media_type),
        output=CreditApplication,
        tool_name=TOOL,
        tool_description="Record every field of the commercial credit application.",
        max_tokens=4000,
    )
    return clean(application)


def clean(application: CreditApplication) -> CreditApplication:
    """Light post-processing that does not change meaning."""
    for ref in application.trade_references:
        ref.company = ref.company.strip()
        if ref.email:
            ref.email = ref.email.strip().lower()
    if application.applicant.email:
        application.applicant.email = application.applicant.email.strip().lower()
    issues = {i.field for i in application.issues}
    if application.applicant.phone and not normalize_phone(application.applicant.phone) and "applicant.phone" not in issues:
        application.issues.append({"field": "applicant.phone", "issue": "ambiguous", "note": "not a valid phone number"})  # type: ignore[arg-type]
    return CreditApplication.model_validate(application.model_dump())


@fake_handler(TOOL)
def _offline(system: str, content: list[dict]) -> dict:
    block = next((b for b in content if b.get("type") in ("document", "image")), None)
    if block is None:
        raise LLMError("no document provided")
    sample = sample_for_bytes(base64.b64decode(block["source"]["data"]))
    if sample is None:
        raise LLMError(
            "Offline mode can only read the bundled sample applications. "
            "Add ANTHROPIC_API_KEY to .env to read your own files."
        )
    return sample["application"]
