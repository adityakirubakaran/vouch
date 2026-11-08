"""Reference authenticity score: phone intelligence parsing, name matching, the
scorecard, and Vouch's memory of numbers across applications."""

from __future__ import annotations

import json

import httpx

from vouch.agents import scoring
from vouch.agents import verification as ver
from vouch.agents.verification import PartyInput
from vouch.db import Application, Evidence, Reference
from vouch.policy import load_policy
from vouch.providers.phone import PhoneProvider, merge, parse_ipqs, parse_places, parse_twilio

from .conftest import make_settings, wait_for_status
from .test_flow import submit_sample

POLICY = load_policy()


def party(company="Northwind Foods", phone="+14155551234", email="ar@northwind.com") -> PartyInput:
    return PartyInput("ref_x", company, phone, email, None, "1 Main St", "94105", "CA")


def checks(phone: dict | None = None, reg: dict | None = None, domain: dict | None = None, web: dict | None = None) -> dict:
    return {
        "registry": reg or {"found": False, "source": "sandbox", "officers": []},
        "domain": domain,
        "website": web or {"url": None, "checked": False, "phones": []},
        "phone": phone,
    }


APPLICANT = PartyInput("applicant", "Acme Bakery Supply LLC", "+15105550142", "ap@acmebakerysupply.example", None,
                       "1450 Industrial Way", "94607", "CA")


def score(ref: PartyInput, chk: dict, applicant_names=("Acme Bakery Supply LLC",), sightings=()):
    """Same steps as the workflow's verify step."""
    applicant_checks = {"registry": {"found": True, "officers": ["Maria Delgado"]}}
    sigs = ver.reference_signals(ref, chk, APPLICANT, applicant_checks, {"maria delgado"}, [], POLICY)
    sigs += scoring.phone_signals(ref, chk.get("phone"), list(applicant_names), POLICY)
    sigs += scoring.history_signals(ref, list(sightings))
    return scoring.score_reference(ref, chk, sigs, chk["registry"].get("officers", []), list(sightings), POLICY)


# ------------------------------------------------------------------ names


def test_name_matching_handles_caller_id_quirks():
    assert scoring.company_name_matches("COASTAL FLOUR M", "Coastal Flour Mills Inc.")  # cut at 15 characters
    assert scoring.company_name_matches("HARBOR PAPER CO", "Harbor Paper Co.")
    assert not scoring.company_name_matches("ACME BAKERY SUP", "Bayview Packaging LLC")
    assert not scoring.company_name_matches("BAY", "Bayview Packaging LLC")  # too short to trust a prefix
    assert scoring.person_name_matches("WHITFIELD DANA", "Dana Whitfield")
    assert not scoring.person_name_matches("DANA", "Dana Whitfield")


# ------------------------------------------------------------ providers


IPQS = {
    "success": True, "message": "Phone is valid.", "formatted": "+14155551234", "valid": True, "fraud_score": 88,
    "recent_abuse": True, "VOIP": True, "prepaid": False, "risky": True, "active": True, "name": "N/A",
    "carrier": "Bandwidth", "line_type": "VOIP", "country": "US",
}
TWILIO = {
    "phone_number": "+14155551234", "valid": True,
    "caller_name": {"caller_name": "NORTHWIND FOODS", "caller_type": "BUSINESS", "error_code": None},
    "line_type_intelligence": {"carrier_name": "Google (Grand Central) - SVR", "type": "nonFixedVoip", "error_code": None},
}
PLACES = {"places": [
    {"displayName": {"text": "Northwind Foods"}, "formattedAddress": "1 Main St, San Francisco, CA 94105",
     "nationalPhoneNumber": "(415) 555-1234", "internationalPhoneNumber": "+1 415-555-1234", "businessStatus": "OPERATIONAL"},
    {"displayName": {"text": "Somewhere Else"}, "nationalPhoneNumber": "(415) 555-9999"},  # loose text match, dropped
]}


def test_parsers_and_merge():
    ipqs, tw, gp = parse_ipqs(IPQS), parse_twilio(TWILIO), parse_places(PLACES, "+14155551234")
    assert ipqs.fraud_score == 88 and ipqs.line_type == "voip" and ipqs.caller_name is None  # "N/A" is not a name
    assert tw.line_type == "nonFixedVoip" and tw.caller_name == "NORTHWIND FOODS"
    assert [x.name for x in gp.listings] == ["Northwind Foods"]
    merged = merge("+14155551234", [ipqs, tw, gp])
    # Twilio's carrier data wins for line type and caller ID; IPQS supplies the fraud score.
    assert merged.line_type == "nonFixedVoip" and merged.caller_name_source == "twilio"
    assert merged.fraud_score == 88 and merged.recent_abuse is True
    assert set(merged.sources) == {"ipqs", "twilio", "google"}
    assert parse_ipqs({"success": False, "message": "Invalid key"}).errors == ["IPQualityScore: Invalid key"]


async def test_live_lookup_calls_each_configured_service(tmp_path, monkeypatch):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen[host] = request
        body = {"www.ipqualityscore.com": IPQS, "lookups.twilio.com": TWILIO, "places.googleapis.com": PLACES}[host]
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr("vouch.providers.phone.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    provider = PhoneProvider(make_settings(tmp_path, ipqs_api_key="ipqs-key", google_maps_api_key="g-key",
                                           twilio_account_sid="AC1", twilio_auth_token="tok"))
    assert provider.live_sources == ["ipqs", "twilio", "google"]
    intel = await provider.lookup("(415) 555-1234")
    assert intel.checked and intel.number == "+14155551234"
    assert seen["www.ipqualityscore.com"].url.path == "/api/json/phone/ipqs-key/14155551234"
    assert seen["lookups.twilio.com"].url.params["Fields"] == "line_type_intelligence,caller_name"
    google = seen["places.googleapis.com"]
    assert json.loads(google.content)["textQuery"] == "+1 4155551234"
    assert "places.nationalPhoneNumber" in google.headers["X-Goog-FieldMask"]


async def test_one_failing_service_does_not_sink_the_lookup(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lookups.twilio.com":
            return httpx.Response(401, json={"message": "Authenticate"})
        return httpx.Response(200, json=IPQS)

    real = httpx.AsyncClient
    monkeypatch.setattr("vouch.providers.phone.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    provider = PhoneProvider(make_settings(tmp_path, ipqs_api_key="k", twilio_account_sid="AC1", twilio_auth_token="bad"))
    intel = await provider.lookup("+14155551234")
    assert intel.checked and intel.fraud_score == 88
    assert any(e.startswith("Twilio Lookup") for e in intel.errors)


async def test_no_keys_means_not_checked(tmp_path):
    intel = await PhoneProvider(make_settings(tmp_path)).lookup("+14155551234")
    assert not intel.checked and intel.sources == []


# ---------------------------------------------------------------- scores


def test_no_evidence_is_unconfirmed_not_genuine():
    out = score(party(), checks())
    assert out["band"] == "unconfirmed" and not out["flagged"] and out["factors"] == []


def test_corroborated_reference_scores_high():
    phone = {"checked": True, "sources": ["twilio", "google"], "line_type": "landline", "caller_name": "NORTHWIND FOODS",
             "caller_name_source": "twilio", "caller_type": "BUSINESS", "listings": [{"name": "Northwind Foods", "source": "google"}]}
    reg = {"found": True, "source": "opencorporates", "status": "Active", "age_days": 4000, "officers": []}
    out = score(party(), checks(phone, reg))
    assert out["band"] == "likely_genuine" and out["score"] >= 90
    codes = {f["code"] for f in out["factors"]}
    assert {"CALLER_ID_MATCHES", "LISTED_TO_COMPANY", "REGISTERED_ESTABLISHED", "BUSINESS_LINE"} <= codes
    assert next(f for f in out["factors"] if f["code"] == "CALLER_ID_MATCHES")["source"] == "Twilio caller ID"


def test_virtual_number_alone_lowers_but_does_not_flag():
    phone = {"checked": True, "sources": ["twilio"], "line_type": "nonFixedVoip", "listings": []}
    out = score(party(), checks(phone))
    assert out["band"] == "unconfirmed" and out["score"] < 69


def test_soft_findings_add_up_to_a_flag():
    phone = {"checked": True, "sources": ["twilio"], "line_type": "nonFixedVoip", "listings": []}
    out = score(party(email="northwind.orders@gmail.com"), checks(phone))
    assert out["flagged"] and not out["hard_stop"] and out["band"] == "likely_fake"


def test_number_listed_to_another_business_counts_against():
    phone = {"checked": True, "sources": ["google"], "listings": [{"name": "Joe's Auto Body", "source": "google"}]}
    out = score(party(), checks(phone))
    assert any(f["code"] == "PHONE_LISTED_TO_OTHER" for f in out["factors"])
    assert out["score"] < 69


def test_high_fraud_score_is_a_hard_stop():
    phone = {"checked": True, "sources": ["ipqs"], "fraud_score": 92, "listings": []}
    reg = {"found": True, "source": "opencorporates", "status": "Active", "age_days": 4000, "officers": []}
    out = score(party(), checks(phone, reg))
    assert out["flagged"] and out["hard_stop"]


def test_number_listed_to_the_applicant_is_a_hard_stop():
    phone = {"checked": True, "sources": ["twilio"], "caller_name": "ACME BAKERY SUP", "caller_name_source": "twilio",
             "caller_type": "BUSINESS", "listings": []}
    out = score(party(), checks(phone))
    assert out["flagged"]
    assert "Acme Bakery Supply LLC, the applicant" in next(f["label"] for f in out["factors"] if f["code"] == "PHONE_LISTED_TO_APPLICANT")


def test_owner_cell_listed_to_an_officer_is_fine():
    """A small company's reference line is often the owner's cell. Caller ID shows a
    person, which is not a mismatch when that person is on the state record."""
    phone = {"checked": True, "sources": ["twilio"], "line_type": "mobile", "caller_name": "WHITFIELD DANA",
             "caller_name_source": "twilio", "caller_type": "CONSUMER", "listings": []}
    reg = {"found": True, "source": "opencorporates", "status": "Active", "age_days": 4000, "officers": ["Dana Whitfield"]}
    out = score(party("Pacific Coffee Roasters"), checks(phone, reg))
    officer = next(f for f in out["factors"] if f["code"] == "CALLER_ID_IS_OFFICER")
    assert "Dana Whitfield" in officer["label"]
    assert not any(f["weight"] < 0 for f in out["factors"])


# --------------------------------------------------------------- history


async def test_vouch_remembers_numbers_across_applications(services):
    first = submit_sample(services, "acme")
    await wait_for_status(services, first, ("ready_for_review",), timeout=30)

    # A different company listing Acme's own line is caught from history alone.
    sightings = services.workflow._phone_history("app_other", "(510) 555-0142")
    sigs = scoring.history_signals(party("Lakeside Cartons", phone="+15105550142"), sightings)
    assert [s.rule for s in sigs][:1] == ["PHONE_SEEN_OTHER_COMPANY"] and sigs[0].severity == "critical"

    second = submit_sample(services, "acme")
    await wait_for_status(services, second, ("ready_for_review",), timeout=30)
    with services.db.session() as s:
        refs = {r.company: r for r in s.get(Application, second).references if r.kind == "trade"}
        harbor = {f["code"] for f in refs["Harbor Paper Co."].checks["score"]["factors"]}
        bayview = {f["code"] for f in refs["Bayview Packaging LLC"].checks["score"]["factors"]}
    assert "VERIFIED_BEFORE" in harbor  # answered a reference call last time
    assert "PHONE_FLAGGED_BEFORE" in bayview


async def test_samples_score_as_expected(services):
    app_id = submit_sample(services, "acme")
    await wait_for_status(services, app_id, ("ready_for_review",), timeout=30)
    with services.db.session() as s:
        refs = {r.company: r for r in s.query(Reference).filter_by(application_id=app_id, kind="trade")}
        evidence = {e.id.split(".", 1)[1]: e.detail for e in s.query(Evidence).filter_by(application_id=app_id)}
        harbor, bayview = refs["Harbor Paper Co."].checks["score"], refs["Bayview Packaging LLC"].checks["score"]
        app = s.get(Application, app_id)
        risks = " ".join(r["text"] for r in app.decision["risks"])
    assert harbor["score"] >= 95 and harbor["band"] == "likely_genuine"
    assert bayview["score"] <= 5 and bayview["flagged"]
    assert {"REF_PHONE_IS_APPLICANT", "PHONE_LISTED_TO_APPLICANT"} <= {f["code"] for f in bayview["factors"]}
    assert "authenticity 1/100" in risks
    assert "caller ID HARBOR PAPER CO" in evidence["phone-ref-1"]
    assert evidence["fraud-ref-3"].startswith("Bayview Packaging LLC scored 1/100")
