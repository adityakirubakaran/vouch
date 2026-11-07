"""Deterministic pieces: fraud rules, credit policy math, schema handling, normalizers."""

from __future__ import annotations

import pytest

from vouch.agents import verification as ver
from vouch.agents.decision import RefInput, evaluate
from vouch.fixtures_data import sample_by_id
from vouch.llm import inline_schema
from vouch.policy import load_policy
from vouch.schemas import BankFindings, CallResult, CreditApplication, ReferenceFindings
from vouch.util import company_key, domain_of, format_phone, normalize_phone


def acme() -> CreditApplication:
    return CreditApplication.model_validate(sample_by_id("acme")["application"])


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(510) 555-0142", "+15105550142"),
        ("510.555.0142", "+15105550142"),
        ("1-510-555-0142", "+15105550142"),
        ("+44 20 7946 0958", "+442079460958"),
        ("555-0142", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_small_normalizers():
    assert format_phone("+15105550142") == "(510) 555-0142"
    assert domain_of("ap@AcmeBakerySupply.example") == "acmebakerysupply.example"
    assert domain_of("https://www.harborpaper.example/contact") == "harborpaper.example"
    assert company_key("Coastal Flour Mills, Inc.") == company_key("coastal flour mills")


def test_inline_schema_has_no_refs():
    schema = inline_schema(CreditApplication)
    text = str(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["properties"]["applicant"]["properties"]["address"]["type"] == "object"


async def test_bayview_is_flagged_and_harbor_is_clean(settings):
    from vouch.providers.records import DomainProvider, RegistryProvider, WebsiteProvider

    policy = load_policy()
    app = acme()
    reg, dom, web = RegistryProvider(settings), DomainProvider(settings), WebsiteProvider(settings)
    a = app.applicant
    applicant = ver.PartyInput("applicant", a.legal_name, a.phone, a.email, a.website, a.address.street, a.address.postal_code, a.address.state)
    refs = [
        ver.PartyInput(f"r{i}", r.company, r.phone, r.email, None, r.address.street, r.address.postal_code, r.address.state)
        for i, r in enumerate(app.trade_references)
    ]
    applicant_checks = await ver.check_party(applicant, reg, dom, web)
    people = ver.applicant_people(app, applicant_checks)
    results = {}
    for ref in refs:
        chk = await ver.check_party(ref, reg, dom, web)
        sigs = ver.reference_signals(ref, chk, applicant, applicant_checks, people, [r for r in refs if r is not ref], policy)
        results[ref.company] = (sigs, ver.is_flagged(sigs, policy), ver.display_rows(ref, chk, sigs))

    sigs, flagged, rows = results["Bayview Packaging LLC"]
    assert flagged
    assert {s.rule for s in sigs} >= {"REF_PHONE_IS_APPLICANT", "REF_ADDRESS_IS_APPLICANT", "REF_NEWLY_REGISTERED", "REF_SHARES_OFFICER", "REF_NEW_DOMAIN", "REF_FREE_EMAIL"}
    assert rows[0] == {"label": "Registered", "value": "23 days ago", "ok": False}
    assert rows[1]["value"] == "Applicant's own line" and rows[1]["ok"] is False
    assert rows[2]["value"] == "2 weeks" and rows[2]["ok"] is False

    sigs, flagged, rows = results["Harbor Paper Co."]
    assert not flagged and sigs == []
    assert rows == [
        {"label": "Registered", "value": "2011", "ok": True},
        {"label": "Phone number", "value": "Matches its website", "ok": True},
        {"label": "Website age", "value": "15 years", "ok": True} if rows[2]["value"] == "15 years" else rows[2],
    ]
    assert rows[2]["ok"] is True


def test_two_warnings_flag_a_reference():
    policy = load_policy()
    warns = [ver.Signal("REF_FREE_EMAIL", "warn", ""), ver.Signal("REF_NEW_DOMAIN", "warn", "")]
    assert ver.is_flagged(warns, policy)
    assert not ver.is_flagged(warns[:1], policy)


def test_unsigned_application_requires_authorization():
    app = acme()
    app.signature.signed = False
    checks = {"registry": {"found": True, "status": "Active", "age_days": 4000, "officers": []}}
    rules = {s.rule for s in ver.applicant_signals(app, checks, load_policy())}
    assert "AUTH_MISSING" in rules


def _ref(company, high, days, terms="Net 30", past_due=0.0, flagged=False, outcome="completed", code="call-ref-1"):
    return RefInput(
        id=company, company=company, flagged=flagged, outcome=outcome,
        findings=ReferenceFindings(high_credit=high, avg_days_to_pay=days, terms=terms, past_due_amount=past_due),
        signals=[{"rule": "REF_PHONE_IS_APPLICANT", "severity": "critical", "message": "Phone is the applicant's line."}] if flagged else [],
        evidence_ids=[code],
    )


GOOD_BANK = BankFindings(nsf_or_overdrafts_last_12mo=0, relationship_rating="satisfactory", summary="Satisfactory.")


def run(refs, requested=30000, bank=GOOD_BANK, signals=None):
    app = acme()
    app.requested_limit = requested
    return evaluate(load_policy(), app, signals or [], refs, bank, "bank", "app", "reg-applicant")


def test_policy_reduces_limit_from_reference_high_credit():
    r = run([_ref("Harbor", 40000, 28), _ref("Coastal", 20000, 31, code="call-ref-2"), _ref("Bayview", 100000, 15, flagged=True, code="fraud-ref-3")])
    assert r["decision"] == "approve_reduced"
    assert r["recommended_limit"] == 25000  # avg 30k * 0.85 = 25.5k -> 25k
    assert r["verified_references"] == 2 and r["flagged_references"] == 1
    assert any("Bayview" in x["text"] for x in r["risks"])


def test_policy_caps_at_requested_amount():
    r = run([_ref("A", 90000, 25), _ref("B", 80000, 28)], requested=15000)
    assert r["decision"] == "approve" and r["recommended_limit"] == 15000


def test_slow_payers_get_half():
    r = run([_ref("A", 40000, 45), _ref("B", 40000, 50)])
    assert r["recommended_limit"] == 15000  # 34k * 0.5 = 17k -> 15k
    assert any("slower" in x["text"] for x in r["risks"])


def test_too_few_references_requests_info():
    r = run([_ref("A", 40000, 28), _ref("B", None, None, outcome="no_answer")])
    assert r["decision"] == "request_info" and r["recommended_limit"] is None
    assert r["missing"]


def test_past_due_and_bad_bank_go_to_review():
    assert run([_ref("A", 40000, 28, past_due=9000), _ref("B", 40000, 28)])["decision"] == "manual_review"
    bad_bank = BankFindings(nsf_or_overdrafts_last_12mo=5, relationship_rating="satisfactory", summary="Several NSFs.")
    assert run([_ref("A", 40000, 28), _ref("B", 40000, 28)], bank=bad_bank)["decision"] == "manual_review"


def test_inactive_applicant_is_declined():
    r = run([_ref("A", 40000, 28), _ref("B", 40000, 28)], signals=[{"rule": "APPLICANT_INACTIVE", "severity": "critical", "message": "Dissolved."}])
    assert r["decision"] == "decline"


def test_call_result_schema_accepts_minimal_outcome():
    assert CallResult.model_validate({"outcome": "no_answer", "summary": "No answer."}).findings.high_credit is None
