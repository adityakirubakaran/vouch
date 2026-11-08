"""When to call a reference again.

A good credit analyst does not redial every thirty seconds. If the reference
asks to be called back ("tomorrow morning", "Thursday after 2"), they call back
then. If nobody picks up twice, they try again the next business morning. Both
happen in the reference's own time zone, taken from the state on its address.

parse_callback() understands the way people say this on the phone. Anything it
cannot read falls back to the next business morning, which is what an analyst
would do too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

# Main time zone per US state (multi-zone states use where most people live).
STATE_TZ = {
    **dict.fromkeys(["CT", "DE", "DC", "FL", "GA", "IN", "KY", "ME", "MD", "MA", "MI", "NH", "NJ", "NY", "NC", "OH", "PA",
                     "RI", "SC", "VT", "VA", "WV"], "America/New_York"),
    **dict.fromkeys(["AL", "AR", "IL", "IA", "KS", "LA", "MN", "MS", "MO", "NE", "ND", "OK", "SD", "TN", "TX", "WI"], "America/Chicago"),
    **dict.fromkeys(["CO", "ID", "MT", "NM", "UT", "WY"], "America/Denver"),
    "AZ": "America/Phoenix",
    **dict.fromkeys(["CA", "NV", "OR", "WA"], "America/Los_Angeles"),
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PARTS_OF_DAY = {"afternoon": 14, "morning": 10, "lunch": 13, "noon": 12, "end of day": 16, "evening": 16, "tonight": 16}


def reference_timezone(state: Optional[str], default: str = "America/Los_Angeles") -> tzinfo:
    name = STATE_TZ.get((state or "").strip().upper(), default)
    try:
        return ZoneInfo(name) if ZoneInfo else timezone(timedelta(hours=-8), "PT")
    except Exception:  # noqa: BLE001 - no tz database on this machine
        return timezone(timedelta(hours=-8), "PT")


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_business_morning(now: datetime, hour: int = 10) -> datetime:
    return datetime.combine(next_business_day(now.date()), time(hour), tzinfo=now.tzinfo)


def _clock(text: str) -> Optional[tuple[int, int]]:
    """'at 3', '3pm', '2:30', 'noon' -> (hour, minute) on a 24-hour clock."""
    if re.search(r"\bnoon\b", text):
        return 12, 0
    m = re.search(r"\b(?:at|after|around|by|before)?\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d/])", text)
    if not m or not (m.group(3) or re.search(r"\b(at|after|around|by|before)\s*\d", text) or m.group(2)):
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    suffix = (m.group(3) or "").replace(".", "")
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif not suffix and hour < 8:
        hour += 12  # "at 3" on a business call means 3 PM
    return hour, minute


def parse_callback(text: Optional[str], now: datetime, default_hour: int = 10) -> Optional[datetime]:
    """When a reference asked to be called back, as a time in their zone. None if
    the words give no usable time ("later", "some other time")."""
    if not text:
        return None
    t = text.lower()
    rel = re.search(r"\bin (an?|\d+|half an?) ?(minute|min|hour|hr)s?\b", t)
    if rel:
        n = 0.5 if rel.group(1).startswith("half") else 1 if rel.group(1) in ("a", "an") else int(rel.group(1))
        return now + (timedelta(hours=n) if rel.group(2).startswith(("hour", "hr")) else timedelta(minutes=n))

    day: Optional[date] = None
    if "tomorrow" in t:
        day = now.date() + timedelta(days=1)
    elif "next week" in t:
        day = now.date() + timedelta(days=7 - now.weekday())  # next Monday
    elif "today" in t or "this afternoon" in t or "this evening" in t or "tonight" in t:
        day = now.date()
    else:
        for i, name in enumerate(WEEKDAYS):
            if re.search(rf"\b{name}\b", t):
                ahead = (i - now.weekday()) % 7 or 7
                day = now.date() + timedelta(days=ahead)
                break

    clock = _clock(t)
    if clock is None:
        part = next((h for words, h in PARTS_OF_DAY.items() if re.search(rf"\b{words}\b", t)), None)
        clock = (part, 0) if part is not None else None
    if day is None and clock is None:
        return None
    if day is None:  # a time with no day: today if it is still ahead, else tomorrow
        day = now.date()
    hour, minute = clock if clock else (default_hour, 0)
    when = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
    if when <= now:
        when = when + timedelta(days=1) if day == now.date() else when
    if when <= now:
        when = now + timedelta(hours=1)
    return when


def describe(when: datetime, now: datetime) -> str:
    """'tomorrow at 10:00 AM PST', 'today at 3:30 PM PST', 'Mon, Nov 9 at 10:00 AM PST'."""
    clock = when.strftime("%I:%M %p").lstrip("0")
    zone = when.strftime("%Z")
    if when.date() == now.date():
        day = "today"
    elif when.date() == now.date() + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{when.strftime('%a, %b')} {when.day}"
    return f"{day} at {clock}{' ' + zone if zone else ''}"


@dataclass
class FollowUp:
    after_seconds: float
    when: Optional[datetime]  # in the reference's zone; None for a quick retry
    reason: str  # shown on the review screen while the step waits


def plan(outcome: str, misses: int, callback_at: Optional[str], now_utc: datetime, tz: tzinfo,
         retry_seconds: float, honor_callback_times: bool, next_day_after: int, hour: int) -> FollowUp:
    """Decide the next attempt after a call that did not finish the reference.

    misses counts calls nobody answered (no answer, voicemail, failed); a callback
    request is a conversation, not a miss.
    """
    now = now_utc.astimezone(tz)
    if outcome == "callback_requested":
        asked = f" {callback_at}" if callback_at else ""
        if not honor_callback_times:
            return FollowUp(retry_seconds, None, f"Asked for a callback{asked}; calling back now")
        when = parse_callback(callback_at, now, hour) or next_business_morning(now, hour)
        return FollowUp(max(retry_seconds, (when - now).total_seconds()), when, f"Asked for a callback{asked}. Calling back {describe(when, now)}")
    label = {"no_answer": "No answer", "voicemail": "Left a voicemail", "failed": "Call failed"}.get(outcome, outcome)
    if outcome in ("no_answer", "voicemail") and next_day_after > 0 and misses >= next_day_after:
        when = next_business_morning(now, hour)
        times = "twice" if misses == 2 else f"{misses} times"
        return FollowUp(max(retry_seconds, (when - now).total_seconds()), when,
                        f"{label} {times}. Trying again {describe(when, now)}")
    return FollowUp(retry_seconds, None, f"{label}, trying again")
