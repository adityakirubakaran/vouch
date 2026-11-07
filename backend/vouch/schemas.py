"""Typed shapes that move between agents.

These double as the JSON schemas Claude must fill through forced tool calls, so
field descriptions are written as instructions to the model.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Address(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = Field(None, description="Two-letter US state code when available.")
    postal_code: Optional[str] = None


class Party(BaseModel):
    legal_name: str = Field(description="Legal business name exactly as written.")
    dba: Optional[str] = Field(None, description="Doing-business-as name, if different.")
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    address: Address = Field(default_factory=Address)


class Applicant(Party):
    entity_type: Optional[str] = Field(None, description="LLC, corporation, partnership, sole proprietor...")
    state_of_incorporation: Optional[str] = None
    ein: Optional[str] = Field(None, description="Federal EIN, formatted NN-NNNNNNN if legible.")
    years_in_business: Optional[float] = None
    annual_revenue: Optional[float] = Field(None, description="Annual revenue in USD if stated.")
    principal_name: Optional[str] = None
    principal_title: Optional[str] = None


class TradeReference(BaseModel):
    company: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Address = Field(default_factory=Address)


class BankReference(BaseModel):
    bank_name: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    account_last4: Optional[str] = Field(None, description="Only the last four digits, never the full number.")


class Signature(BaseModel):
    signed: bool = Field(description="True if the authorization section carries a signature.")
    signer_name: Optional[str] = None
    signer_title: Optional[str] = None
    date: Optional[str] = Field(None, description="Date as written, ISO format if unambiguous.")
    authorizes_reference_checks: bool = Field(
        description="True if the applicant authorized the seller to contact its bank and trade references."
    )


class ExtractionIssue(BaseModel):
    field: str = Field(description="Dotted path of the field, e.g. trade_references.1.phone")
    issue: Literal["illegible", "missing", "ambiguous"]
    note: Optional[str] = None


class CreditApplication(BaseModel):
    """Everything Vouch needs from a commercial credit application."""

    applicant: Applicant
    requested_limit: Optional[float] = Field(None, description="Requested credit limit in USD.")
    requested_terms: Optional[str] = Field(None, description="Requested payment terms, e.g. 'Net 30'.")
    bank: Optional[BankReference] = None
    trade_references: List[TradeReference] = Field(default_factory=list)
    signature: Signature
    issues: List[ExtractionIssue] = Field(
        default_factory=list, description="Fields that were missing, illegible, or ambiguous on the form."
    )


# ---------------------------------------------------------------------------
# Reference call findings


class ReferenceFindings(BaseModel):
    """What a trade reference said about the applicant. Leave a field null if it was not stated."""

    customer_since: Optional[str] = Field(None, description="Year or date the applicant became a customer.")
    terms: Optional[str] = Field(None, description="Terms the reference extends, e.g. 'Net 30'.")
    high_credit: Optional[float] = Field(None, description="Highest balance or credit line extended, USD.")
    current_balance: Optional[float] = Field(None, description="Current balance owed, USD.")
    past_due_amount: Optional[float] = Field(None, description="Amount past due in USD. 0 if they said nothing is past due.")
    avg_days_to_pay: Optional[float] = Field(None, description="Average days from invoice to payment.")
    pays_within_terms: Optional[bool] = None
    would_extend_more_credit: Optional[bool] = None
    comments: Optional[str] = Field(None, description="Anything else material the reference volunteered.")


CallOutcome = Literal[
    "completed",  # reference answered the questions
    "callback_requested",  # asked us to call back later
    "declined",  # will not give a reference by phone
    "wrong_number",
    "voicemail",
    "no_answer",
    "failed",
]


class CallResult(BaseModel):
    outcome: CallOutcome
    findings: ReferenceFindings = Field(default_factory=ReferenceFindings)
    callback_at: Optional[str] = Field(None, description="When they asked to be called back, as they said it.")
    alternate_contact: Optional[str] = Field(None, description="Email or person they asked us to contact instead.")
    summary: str = Field(description="One or two plain sentences on how the call went.")


# ---------------------------------------------------------------------------
# Bank reference


class BankFindings(BaseModel):
    account_open_since: Optional[str] = Field(None, description="When the account was opened, as stated.")
    average_balance: Optional[str] = Field(None, description="Average balance as stated, e.g. 'low five figures'.")
    nsf_or_overdrafts_last_12mo: Optional[int] = Field(None, description="Count of NSF or overdraft events in the last year.")
    has_loans_or_lines: Optional[bool] = None
    loans_in_good_standing: Optional[bool] = None
    relationship_rating: Optional[Literal["satisfactory", "unsatisfactory", "unknown"]] = None
    summary: str = Field(description="One plain sentence summarizing the bank's answer.")


# ---------------------------------------------------------------------------
# Credit memo narrative written by Claude (numbers come from the policy engine)


class MemoPoint(BaseModel):
    text: str = Field(description="One sentence. Use only numbers present in the facts you were given.")
    evidence_ids: List[str] = Field(description="IDs of the evidence items that support this sentence.")


class MemoNarrative(BaseModel):
    headline: str = Field(description="One sentence stating the recommendation and the main reason.")
    points: List[MemoPoint] = Field(description="Three to five supporting points, strongest first.")
    risks: List[MemoPoint] = Field(default_factory=list, description="Risks or open questions, if any.")
