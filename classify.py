"""Classification rules for Marketplace chats.

Inputs: list of recent messages (text + sender), a "now" reference date, and a
Config (thresholds + phrase banks). Output: one of DELETE / ARCHIVE / LEAVE
plus a reasoning string.

Phrase banks and the age thresholds live in config.toml, so the language and
tuning are the user's to change. When in doubt, return LEAVE with flagged=True.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from dateutil import parser as dateparser

from config import Config


# ----- public types -----

@dataclass
class Decision:
    action: str          # "DELETE" | "ARCHIVE" | "LEAVE"
    flagged: bool        # True if ambiguous and user should review
    reasoning: str       # short human-readable explanation


# ----- helpers -----

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _any_phrase(text: str, phrases: Iterable[str]) -> Optional[str]:
    t = _norm(text)
    for p in phrases:
        if p in t:
            return p
    return None


_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_WEEKDAY_TIME_RE = re.compile(
    r"^(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+"
    r"(?P<h>\d{1,2}):(?P<m>\d{2})(?:\s*(?P<ap>am|pm))?\s*$",
    re.IGNORECASE,
)

_HHMM_ONLY_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})(?:\s*(?P<ap>am|pm))?\s*$", re.IGNORECASE)


def _resolve_weekday(name: str, *, now: datetime, h: int, m: int) -> datetime:
    """Return the most recent date in the past matching the named weekday.

    FB only shows a weekday name (no date) for messages within the last 7 days.
    """
    target_wd = _WEEKDAYS[name.lower()]
    days_back = (now.weekday() - target_wd) % 7
    if days_back == 0:
        # Same weekday-of-week as today. FB would show just HH:MM for *today*,
        # so a weekday match meaning "today" wouldn't happen — interpret as
        # 7 days ago instead.
        days_back = 7
    d = (now - timedelta(days=days_back)).replace(hour=h, minute=m, second=0, microsecond=0)
    return d


def parse_ts(text: Optional[str], *, now: datetime) -> Optional[datetime]:
    """Parse FB Messenger's aria-label timestamps.

    Observed forms (from the message aria-labels):
      "14:45"                    → today at HH:MM
      "9:31 PM"                  → today at H:MM AM/PM
      "Tuesday 17:51"            → most recent past Tuesday
      "4 May 2026, 14:45"        → explicit day-month-year
      "April 9, 2024 at 9:00 PM" → fallback
    """
    if not text:
        return None
    t = text.strip()
    low = t.lower()

    if "yesterday" in low:
        return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    if "today" in low:
        return now.replace(second=0, microsecond=0)

    # "Tuesday 17:51"
    m = _WEEKDAY_TIME_RE.match(t)
    if m:
        h, mi = int(m.group("h")), int(m.group("m"))
        ap = (m.group("ap") or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        try:
            return _resolve_weekday(m.group("wd"), now=now, h=h, m=mi)
        except Exception:
            pass

    # "HH:MM" — today
    m = _HHMM_ONLY_RE.match(t)
    if m:
        h, mi = int(m.group("h")), int(m.group("m"))
        ap = (m.group("ap") or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        return now.replace(hour=h, minute=mi, second=0, microsecond=0)

    # Otherwise dateutil — but only fall back to year-subtraction for clearly
    # absolute date strings that include a month name.
    try:
        d = dateparser.parse(t, default=now, fuzzy=True)
        # dateutil will happily parse "Marketplace" as today; require at least
        # a digit or a known month token before we trust it.
        if not re.search(r"\d", t):
            return None
        if d > now + timedelta(days=1):
            # Future date: only reasonable explanation is missing year. Don't
            # apply this hack unless the original text explicitly omitted the
            # year (i.e. has month text but no 4-digit year).
            if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", low) \
                    and not re.search(r"\b\d{4}\b", t):
                d = d.replace(year=d.year - 1)
            else:
                return None
        return d
    except Exception:
        return None


# ----- main classifier -----

def classify(
    *,
    messages: list[dict],
    last_msg_ts: Optional[datetime],
    cfg: Config,
    now: Optional[datetime] = None,
) -> Decision:
    """Apply rules to a thread.

    messages: oldest→newest, each {text, sender ('me'|'them'|'system'), ts_text}
    last_msg_ts: parsed timestamp of the most recent non-system message, if known
    cfg: thresholds and phrase banks (see config.py / config.toml)
    """
    now = now or datetime.now()

    # Strip system rows (date dividers).
    real = [m for m in messages if m.get("sender") in ("me", "them")]

    if not real:
        return Decision("LEAVE", True, "no readable messages — manual review")

    last = real[-1]
    last_text = last.get("text") or ""
    last_sender = last.get("sender")
    age_days: Optional[float] = None
    if last_msg_ts is not None:
        age_days = (now - last_msg_ts).total_seconds() / 86400.0

    # ----- DELETE: explicit "sold/taken" from counterparty -----
    sold_hit = _any_phrase(last_text, cfg.sold_phrases)
    if not sold_hit:
        # Also scan the last 3 messages for sold language (recent enough).
        for m in real[-3:]:
            if m.get("sender") == "them":
                h = _any_phrase(m.get("text") or "", cfg.sold_phrases)
                if h:
                    sold_hit = h
                    break
    if sold_hit:
        return Decision("DELETE", False, f"seller said it was sold/taken ('{sold_hit}')")

    # ----- ARCHIVE: closure language -----
    closure_hit = None
    for m in real[-4:]:
        h = _any_phrase(m.get("text") or "", cfg.closure_phrases)
        if h:
            closure_hit = h
            break
    if closure_hit:
        return Decision("ARCHIVE", False, f"closure language present ('{closure_hit}')")

    # From here on, age matters.
    if age_days is None:
        return Decision("LEAVE", True, "could not determine last-message date — manual review")

    # ----- LEAVE: recent activity (within recent_days) -----
    if age_days <= cfg.recent_days:
        # Coordination without closure → leave (ongoing logistics).
        coord_hit = None
        for m in real[-5:]:
            h = _any_phrase(m.get("text") or "", cfg.coordination_phrases)
            if h:
                coord_hit = h
                break
        if coord_hit:
            return Decision("LEAVE", False, f"recent coordination ongoing ('{coord_hit}')")
        return Decision("LEAVE", False, f"active within {age_days:.0f}d")

    # recent_days < age <= gray_zone_days — still in the gray zone; flag.
    if age_days <= cfg.gray_zone_days:
        return Decision("LEAVE", True, f"stale {age_days:.0f}d but under {cfg.gray_zone_days:.0f}d cutoff — manual review")

    # ----- DELETE: past the gray zone, my inquiry never answered -----
    if age_days > cfg.gray_zone_days:
        # Did I send the last message asking about availability/price?
        if last_sender == "me":
            inq = _any_phrase(last_text, cfg.inquiry_no_reply_phrases)
            if inq:
                return Decision(
                    "DELETE",
                    False,
                    f"my inquiry ('{inq}') went unanswered for {age_days:.0f}d",
                )
            # Even without an explicit inquiry phrase: if I sent last and it's
            # >30d, that's a dead-end on my side too.
            return Decision(
                "DELETE",
                False,
                f"I sent last message {age_days:.0f}d ago, no reply",
            )

        # One-message stub (single message in thread, >30d).
        if len(real) == 1:
            return Decision(
                "DELETE",
                False,
                f"one-message stub, {age_days:.0f}d old",
            )

        # Their last reply >30d ago — interpret as cold lead.
        if last_sender == "them" and len(real) <= 2:
            return Decision(
                "DELETE",
                False,
                f"only {len(real)} messages, last {age_days:.0f}d ago — dead lead",
            )

        # Older but with real back-and-forth — could be a completed deal
        # without explicit closure. Flag for review rather than auto-delete.
        return Decision(
            "LEAVE",
            True,
            f"{age_days:.0f}d old with {len(real)} msgs, no clear closure — manual review",
        )

    return Decision("LEAVE", True, "fell through — manual review")
