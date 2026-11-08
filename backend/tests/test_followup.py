"""Follow-up scheduling: callbacks at the time the reference asked for, and the
next business morning after two unanswered calls, in the reference's zone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vouch import followup
from vouch.db import Application, Reference, Step

from .conftest import wait_for_status
from .test_flow import submit_sample

PT = followup.reference_timezone("CA")
NY = followup.reference_timezone("NY")
# Saturday, Nov 7 2026, 7:40 PM Pacific
SAT_EVENING = datetime(2026, 11, 7, 19, 40, tzinfo=PT)
# Tuesday, Nov 10 2026, 11:15 AM Pacific
TUE_MORNING = datetime(2026, 11, 10, 11, 15, tzinfo=PT)


def at(text, now=TUE_MORNING):
    when = followup.parse_callback(text, now)
    return when.strftime("%a %H:%M") if when else None


def test_parses_how_people_ask_for_a_callback():
    assert at("tomorrow morning") == "Wed 10:00"
    assert at("Thursday morning") == "Thu 10:00"
    assert at("thursday after 2") == "Thu 14:00"
    assert at("tomorrow at 3:30pm") == "Wed 15:30"
    assert at("this afternoon") == "Tue 14:00"
    assert at("around noon") == "Tue 12:00"
    assert at("next week") == "Mon 10:00"
    assert at("in an hour") == "Tue 12:15"
    assert at("in 20 minutes") == "Tue 11:35"
    assert at("Tuesday") == "Tue 10:00" and followup.parse_callback("Tuesday", TUE_MORNING).date() > TUE_MORNING.date()  # next week's
    assert at("this morning") == "Wed 10:00"  # already past: tomorrow
    assert at("later") is None and at("some other time") is None and at(None) is None


def test_next_business_morning_skips_the_weekend():
    when = followup.next_business_morning(SAT_EVENING, 10)
    assert when.strftime("%a %b %d %H:%M") == "Mon Nov 09 10:00"
    assert followup.describe(when, SAT_EVENING) == "Mon, Nov 9 at 10:00 AM PST"


def test_plan_follow_up():
    now = SAT_EVENING.astimezone(timezone.utc)
    kw = dict(retry_seconds=30, honor_callback_times=True, next_day_after=2, hour=10)
    cb = followup.plan("callback_requested", 0, "tomorrow morning", now, PT, **kw)
    assert cb.when.strftime("%a %H:%M") == "Sun 10:00"
    assert cb.reason == "Asked for a callback tomorrow morning. Calling back tomorrow at 10:00 AM PST"
    assert abs(cb.after_seconds - (cb.when - SAT_EVENING).total_seconds()) < 1
    first = followup.plan("no_answer", 1, None, now, PT, **kw)
    assert first.when is None and first.after_seconds == 30 and first.reason == "No answer, trying again"
    second = followup.plan("no_answer", 2, None, now, PT, **kw)
    assert second.reason == "No answer, the second call without an answer. Trying again Mon, Nov 9 at 10:00 AM PST"
    vm = followup.plan("voicemail", 2, None, now, PT, **kw)
    assert vm.reason.startswith("Reached voicemail, the second call without an answer.")
    # Same moment, a New York reference: 10 AM their time.
    ny = followup.plan("no_answer", 2, None, now, NY, **kw)
    assert ny.when.strftime("%H:%M %Z") == "10:00 EST"
    assert followup.plan("callback_requested", 0, "gibberish", now, PT, **kw).when.strftime("%a") == "Mon"
    quick = followup.plan("callback_requested", 0, "Thursday", now, PT, **{**kw, "honor_callback_times": False})
    assert quick.when is None and quick.after_seconds == 30


async def test_callback_is_scheduled_and_call_now_pulls_it_forward(tmp_path):
    from vouch.services import build_services, start_services, stop_services

    from .conftest import make_settings

    svc = build_services(make_settings(tmp_path, honor_callback_times=True))
    await start_services(svc)
    try:
        app_id = submit_sample(svc, "acme")
        coastal_id = None
        deadline = datetime.now(timezone.utc) + timedelta(seconds=20)
        while datetime.now(timezone.utc) < deadline:
            with svc.db.session() as s:
                ref = s.query(Reference).filter_by(application_id=app_id, company="Coastal Flour Mills Inc.").first()
                if ref and ref.status == "callback_scheduled":
                    coastal_id = ref.id
                    step = s.query(Step).filter_by(reference_id=ref.id, kind="call").first()
                    wait_reason, next_run = step.wait_reason, step.next_run_at
                    summary = ref.summary
                    break
            import asyncio

            await asyncio.sleep(0.05)
        assert coastal_id, "Coastal never asked for a callback"
        assert wait_reason.startswith("Asked for a callback tomorrow morning. Calling back tomorrow at 10:00 AM")
        assert next_run - datetime.now(timezone.utc) > timedelta(hours=1)  # really waits until tomorrow
        assert "Calling back tomorrow at 10:00 AM" in summary
        with svc.db.session() as s:
            assert s.get(Application, app_id).status == "checking_references"  # the memo waits for Coastal

        assert svc.workflow.call_now(coastal_id, None)  # the demo's "Call now"
        await wait_for_status(svc, app_id, ("ready_for_review",), timeout=30)
        with svc.db.session() as s:
            ref = s.get(Reference, coastal_id)
            assert [c.outcome for c in ref.calls] == ["callback_requested", "completed"]
            assert ref.findings["high_credit"] == 20000
            assert s.get(Application, app_id).decision["recommended_limit"] == 25000
    finally:
        await stop_services(svc)
