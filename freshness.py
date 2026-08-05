"""
Posting age and freshness.

WHY THIS IS NOT JUST "PARSE THE DATE"
------------------------------------
Every source dates its postings differently: ISO 8601 with a timezone, ISO
without one, RFC 2822 from RSS, and RemoteOK sends a unix epoch as a string.
Handled in one place so the rest of the app can assume a datetime.

ABOUT "VALID UNTIL"
-------------------
Almost no job feed publishes an expiry date — it is simply not in the data, and
inventing one would be a lie dressed as a feature. What the data does support is
an age, and age is a good proxy: a listing older than about six weeks is usually
filled or abandoned. So the UI shows how old a posting is and how likely it is to
still be open, and says which of those is measured and which is inferred.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Optional

# Age bands, in days. Chosen from how job boards behave rather than arbitrarily:
# most ATS postings are filled within 30 days, and boards commonly auto-expire
# listings at 60.
FRESH_DAYS = 7
RECENT_DAYS = 21
AGING_DAYS = 45


def parse_posted(value) -> Optional[datetime]:
    """Best-effort parse of whatever a source calls a date."""
    if value is None or value == "":
        return None

    # unix epoch — RemoteOK sends this, sometimes as a string
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            ts = float(value)
            if ts > 1e11:            # milliseconds
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    if not isinstance(value, str):
        return None
    s = value.strip()

    # ISO 8601, with or without Z / offset
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass

    # RFC 2822, as used in RSS
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def humanise(dt: Optional[datetime]) -> str:
    if dt is None:
        return "date not given"
    days = (datetime.now(timezone.utc) - dt).days
    if days < 0:
        return "just posted"
    if days == 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    if days < 7:
        return f"posted {days} days ago"
    if days < 14:
        return "posted last week"
    if days < 31:
        return f"posted {days // 7} weeks ago"
    if days < 60:
        return "posted about a month ago"
    return f"posted {days // 30} months ago"


def freshness(dt: Optional[datetime]) -> Dict:
    """
    Age is measured. 'Likely still open' is INFERRED from age, because feeds do
    not publish expiry. The label says so.
    """
    if dt is None:
        return {"band": "unknown", "days": None, "label": "date not given",
                "likely_open": None,
                "note": "This source does not publish a posting date."}

    days = max(0, (datetime.now(timezone.utc) - dt).days)

    if days <= FRESH_DAYS:
        band, likely = "fresh", True
        note = "Recently posted — worth applying now."
    elif days <= RECENT_DAYS:
        band, likely = "recent", True
        note = "Still within the window most roles stay open."
    elif days <= AGING_DAYS:
        band, likely = "aging", True
        note = "Getting old. Many roles are filled by this point — apply soon or move on."
    else:
        band, likely = "stale", False
        note = "Likely filled or withdrawn. Boards often leave old listings up."

    est_close = dt + timedelta(days=60)
    return {
        "band": band,
        "days": days,
        "label": humanise(dt),
        "posted_iso": dt.date().isoformat(),
        "likely_open": likely,
        "estimated_close": est_close.date().isoformat(),
        "note": note,
    }


def enrich(job: Dict) -> Dict:
    """Attach age info to a job. Called once per job after normalisation."""
    dt = parse_posted(job.get("posted"))
    job["age"] = freshness(dt)
    job["age_days"] = job["age"]["days"] if job["age"]["days"] is not None else 999
    return job
