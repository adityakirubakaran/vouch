"""End-to-end runs of the whole workflow in offline mode (scripted Claude,
simulated references, simulated bank)."""

from __future__ import annotations

from sqlalchemy import select

from vouch.db import Application, Call, Email, Evidence, Reference, Step
from vouch.fixtures_data import sample_by_id, sample_path

from .conftest import wait_for_status


def submit_sample(svc, sample_id: str) -> str:
    sample = sample_by_id(sample_id)
    path = sample_path(sample)
    return svc.workflow.create_application(path.name, path.read_bytes(), sample_id=sample_id)


async def test_acme_flags_fake_reference_and_recommends_reduced_limit(services):
    app_id = submit_sample(services, "acme")
    await wait_for_status(services, app_id, ("ready_for_review", "failed"), timeout=30)

    with services.db.session() as s:
        app = s.get(Application, app_id)
        assert app.status == "ready_for_review", app.error
        refs = {r.company: r for r in app.references if r.kind == "trade"}
        bayview = refs["Bayview Packaging LLC"]
        assert bayview.flagged
        rules = {sig["rule"] for sig in bayview.checks["signals"]}
        assert {"REF_PHONE_IS_APPLICANT", "REF_NEWLY_REGISTERED", "REF_SHARES_OFFICER", "REF_FREE_EMAIL"} <= rules
        assert bayview.calls == []  # flagged references are never called

        harbor = refs["Harbor Paper Co."]
        assert harbor.status == "completed"
        assert [c.outcome for c in harbor.calls] == ["voicemail", "completed"]  # first attempt reached voicemail
        vm = harbor.calls[0]
        assert vm.result["summary"] == "Reached voicemail and left a message."
        assert [t.get("voicemail", False) for t in vm.transcript if t["role"] == "reference"] == [True]
        assert vm.transcript[-1].get("voicemail_message") and "We'll try you again later" in vm.transcript[-1]["text"]
        assert len([t for t in vm.transcript if t["role"] == "agent"]) == 2  # greeting and the message, no questions
        assert harbor.findings["high_credit"] == 40000
        assert harbor.findings["avg_days_to_pay"] == 28

        coastal = refs["Coastal Flour Mills Inc."]
        assert coastal.findings["high_credit"] == 20000

        bank = next(r for r in app.references if r.kind == "bank")
        assert bank.status == "completed"
        assert bank.findings["nsf_or_overdrafts_last_12mo"] == 0

        d = app.decision
        assert d["decision"] == "approve_reduced"
        assert d["recommended_limit"] == 25000
        assert d["terms"] == "Net 30"
        assert d["verified_references"] == 2
        assert d["flagged_references"] == 1
        codes = {e.id.split(".", 1)[1] for e in s.scalars(select(Evidence).where(Evidence.application_id == app_id))}
        for point in d["narrative"]["points"] + d["narrative"]["risks"]:
            assert set(point["evidence_ids"]) <= codes

    services.workflow.approve(app_id, None, None, "Looks good")
    await wait_for_status(services, app_id, ("synced",), timeout=10)
    with services.db.session() as s:
        app = s.get(Application, app_id)
        assert app.sync["ok"] and app.sync["provider"] == "ledger"
        assert app.sync["payload"]["credit_limit"] == 25000


async def test_ridgeline_callback_and_declined_reference(services):
    app_id = submit_sample(services, "ridgeline")
    await wait_for_status(services, app_id, ("ready_for_review", "failed"), timeout=30)
    with services.db.session() as s:
        app = s.get(Application, app_id)
        refs = {r.company: r for r in app.references if r.kind == "trade"}
        golden = refs["Golden Gate Restaurant Supply"]
        assert [c.outcome for c in golden.calls] == ["callback_requested", "completed"]
        assert golden.findings["high_credit"] == 25000
        pacific = refs["Pacific Coffee Roasters"]
        assert pacific.status == "declined"
        assert "credit@pacificroasters.example" in pacific.summary
        assert not any(r.flagged for r in refs.values())
        d = app.decision
        # Sierra 18k + Golden Gate 25k -> avg 21.5k * 0.85 = 18.3k -> 15k after rounding, capped at the 15k requested
        assert d["decision"] == "approve"
        assert d["recommended_limit"] == 15000


async def test_restart_resumes_waiting_steps(tmp_path):
    """Kill the engine mid-flight; a new process picks up where it left off."""
    from .conftest import make_settings
    from vouch.services import build_services, start_services, stop_services

    settings = make_settings(tmp_path, simulate_bank_reply_seconds=0)  # bank never replies on its own
    first = build_services(settings)
    await start_services(first)
    app_id = submit_sample(first, "acme")
    await wait_for_status(first, app_id, ("checking_references",), timeout=15)
    await stop_services(first)

    second = build_services(settings)  # same database
    await start_services(second)
    try:
        with second.db.session() as s:
            bank_step = s.scalars(select(Step).where(Step.application_id == app_id, Step.kind == "bank")).one()
            assert bank_step.status == "waiting"
        second.workflow.simulate_bank_reply(app_id)
        await wait_for_status(second, app_id, ("ready_for_review",), timeout=30)
    finally:
        await stop_services(second)


async def test_request_info_when_references_cannot_be_verified(tmp_path):
    from .conftest import make_settings
    from vouch.services import build_services, start_services, stop_services

    settings = make_settings(tmp_path, call_max_attempts=1)  # Harbor never answers attempt 1
    svc = build_services(settings)
    await start_services(svc)
    try:
        app_id = submit_sample(svc, "acme")
        await wait_for_status(svc, app_id, ("ready_for_review",), timeout=30)
        with svc.db.session() as s:
            app = s.get(Application, app_id)
            assert app.decision["decision"] == "request_info"
            assert app.decision["missing"]
        await svc.workflow.request_info(app_id, None)
        with svc.db.session() as s:
            assert s.get(Application, app_id).status == "info_requested"
            outbound = s.scalars(select(Email).where(Email.application_id == app_id, Email.direction == "outbound")).all()
            assert any("one more thing" in e.subject for e in outbound)
    finally:
        await stop_services(svc)


async def test_calling_again_after_the_memo_reopens_the_decision(services):
    app_id = submit_sample(services, "ridgeline")
    await wait_for_status(services, app_id, ("ready_for_review",), timeout=30)
    with services.db.session() as s:
        app = s.get(Application, app_id)
        first = app.decision["generated_at"]
        pacific = next(r for r in app.references if r.company == "Pacific Coffee Roasters")
        assert pacific.status == "declined"
    assert services.workflow.call_now(pacific.id, "simulated")
    await wait_for_status(services, app_id, ("deciding", "ready_for_review"), timeout=10)
    import asyncio

    for _ in range(200):
        with services.db.session() as s:
            app = s.get(Application, app_id)
            if app.status == "ready_for_review" and app.decision["generated_at"] != first:
                break
        await asyncio.sleep(0.05)
    with services.db.session() as s:
        app = s.get(Application, app_id)
        assert app.decision["generated_at"] != first
        assert len(next(r for r in app.references if r.company == "Pacific Coffee Roasters").calls) == 2


async def test_claude_error_mid_call_is_retried_without_hanging_up(services):
    """An overloaded API in the middle of a reply is retried once; the reference
    never notices and the call still completes."""
    real = services.llm.voice_turn
    state = {"n": 0}

    async def flaky_turn(**kw):
        state["n"] += 1
        if state["n"] == 3:
            async for ev in real(**kw):
                yield ev
                raise RuntimeError("overloaded_error")
        async for ev in real(**kw):
            yield ev

    services.llm.voice_turn = flaky_turn  # type: ignore[method-assign]
    app_id = submit_sample(services, "acme")
    await wait_for_status(services, app_id, ("ready_for_review", "failed"), timeout=30)
    with services.db.session() as s:
        app = s.get(Application, app_id)
        assert app.status == "ready_for_review", app.error
        assert state["n"] >= 3
        outcomes = [c.outcome for r in app.references for c in r.calls]
        assert "failed" not in outcomes
        assert app.decision["recommended_limit"] == 25000


async def test_claude_down_mid_call_ends_politely_and_calls_back(services):
    """If the retry fails too, the agent apologizes, hangs up, and the reference is
    called again later instead of being recorded as complete."""
    real = services.llm.voice_turn
    state = {"n": 0}

    async def broken_turn(**kw):
        state["n"] += 1
        if state["n"] in (3, 4):
            raise RuntimeError("overloaded_error")
        async for ev in real(**kw):
            yield ev

    services.llm.voice_turn = broken_turn  # type: ignore[method-assign]
    app_id = submit_sample(services, "acme")
    await wait_for_status(services, app_id, ("ready_for_review", "failed"), timeout=30)
    with services.db.session() as s:
        app = s.get(Application, app_id)
        assert app.status == "ready_for_review", app.error
        failed = [c for r in app.references for c in r.calls if c.outcome == "failed"]
        assert len(failed) == 1
        assert "trouble on my end" in failed[0].transcript[-1]["text"]
        ref = s.get(Reference, failed[0].reference_id)
        assert ref.status == "completed" and ref.calls[-1].outcome == "completed"  # the callback worked
        assert app.decision["recommended_limit"] == 25000
