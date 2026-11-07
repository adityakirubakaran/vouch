"""Bank agent: requests a bank reference by email and reads the reply."""

from __future__ import annotations

import re

from ..llm import LLM, fake_handler, text_of
from ..schemas import BankFindings, BankReference, CreditApplication

TOOL = "record_bank_reference"


def compose_request(seller: str, reply_to: str, application: CreditApplication) -> tuple[str, str]:
    bank: BankReference = application.bank  # type: ignore[assignment]
    applicant = application.applicant
    sig = application.signature
    acct = f" (account ending {bank.account_last4})" if bank.account_last4 else ""
    subject = f"Bank reference request: {applicant.legal_name}"
    greeting = f"Hello {bank.contact_name.split()[0]}," if bank.contact_name else "Hello,"
    body = f"""{greeting}

{applicant.legal_name}{acct} has applied for trade credit with {seller} and listed {bank.bank_name} \
as its bank. The application was signed by {sig.signer_name or 'an officer of the company'}\
{f', {sig.signer_title}' if sig.signer_title else ''}{f' on {sig.date}' if sig.date else ''}, \
authorizing us to contact its bank and trade references.

Could you share, in general terms:
1. How long the account has been open
2. Average balance range (for example, low five figures)
3. Any NSF or overdraft items in the last 12 months
4. Whether the customer has loans or lines with you, and if they are in good standing
5. Whether you consider the relationship satisfactory

A reply to this email is all we need. Thank you for your help.

{seller} Credit Team
{reply_to}
(Sent by Vouch, {seller}'s AI credit assistant)
"""
    return subject, body


async def read_reply(llm: LLM, applicant: str, body: str) -> BankFindings:
    return await llm.structured(
        role="smart",
        system=(
            f"You read bank reference replies for a credit team. The reply is about {applicant}. "
            "Record only what the bank stated; leave anything unstated null."
        ),
        content=[{"type": "text", "text": f"Bank reply:\n{body}\n\nCall {TOOL}."}],
        output=BankFindings,
        tool_name=TOOL,
        tool_description="Record what the bank said about the applicant's account.",
        max_tokens=800,
    )


@fake_handler(TOOL)
def _offline(system: str, content: list[dict]) -> dict:
    text = text_of(content)
    low = text.lower()
    since = re.search(r"since (\w+ \d{4}|\d{4})", text)
    balance = re.search(r"\b(low|mid|high) (four|five|six|seven) figures", low)
    nsf = 0 if re.search(r"no (nsf|overdraft)", low) else None
    has_loans = True if re.search(r"(line|loan)s? (with us )?that is in good standing|revolving line|line with us", low) else (False if "no loans" in low else None)
    good = True if "good standing" in low else None
    rating = "satisfactory" if "satisfactory" in low and "unsatisfactory" not in low else ("unsatisfactory" if "unsatisfactory" in low else "unknown")
    return BankFindings(
        account_open_since=since.group(1) if since else None,
        average_balance=balance.group(0) if balance else None,
        nsf_or_overdrafts_last_12mo=nsf,
        has_loans_or_lines=has_loans,
        loans_in_good_standing=good if has_loans else None,
        relationship_rating=rating,  # type: ignore[arg-type]
        summary=(
            f"Account open since {since.group(1) if since else 'an unstated date'}, "
            f"{balance.group(0) if balance else 'balance not stated'}, "
            f"{'no NSF items' if nsf == 0 else 'NSF history not stated'}, rated {rating}."
        ),
    ).model_dump()
