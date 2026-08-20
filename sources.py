"""
Job source fetchers. Everything runs SERVER-SIDE through httpx — this is the
fix for the old "all ATS blocked" problem, which was the browser refusing
cross-origin calls to Greenhouse/Lever/etc. (CORS). The backend has no such
restriction.

Design rules:
- Every fetch has a hard timeout and returns a status object instead of raising.
- A browser-like User-Agent is sent (several boards 403 the default client UA).
- Results are cached in-memory for CACHE_TTL seconds per source.
- Board slugs are seeds: /api/sources/verify probes them live and the app
  remembers which ones actually resolve.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 JobMatchAI/2.0")

HEADERS = {"User-Agent": UA, "Accept": "application/json, text/xml, */*"}
# Shorter than before: one slow board must not hold up the whole search.
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
# Keyed aggregator APIs are much slower than ATS boards — JSearch alone
# averages ~8.5s — so they get their own, far more patient budget.
KEYED_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# Longer: restarts wipe the cache, so what survives should last.
CACHE_TTL = 30 * 60  # seconds

_CACHE: dict[str, tuple[float, list[dict], dict]] = {}

# Every Workable board lives on the same host, apply.workable.com, and two
# thirds of our registry is Workable. Firing them off in parallel earned a wall
# of HTTP 429s — the host rate-limits per IP, not per board. So requests to a
# provider are gated per host, and the busiest one is served strictly one at a
# time with a gap between calls.
_HOST_GATES: dict[str, asyncio.Semaphore] = {}
_HOST_LAST: dict[str, float] = {}

HOST_CONCURRENCY = {"workable": 1, "bamboohr": 1, "greenhouse": 3, "lever": 3}
HOST_MIN_GAP = {"workable": 1.1, "bamboohr": 0.4}

# A provider that answers 429 is telling us the gap is too small. Widen it for
# the rest of this process rather than guessing a fixed number that is either
# too slow on a good day or too fast on a busy one.
_HOST_PENALTY: dict[str, float] = {}
PENALTY_STEP = 0.6
PENALTY_CAP = 3.0

# Boards that 404 or answer with HTML are not coming back within the hour, and
# every search spent a quarter of its budget re-discovering that. Remember them
# and give the slots to boards that work. Re-checked after DEAD_TTL.
_DEAD: dict[str, float] = {}
DEAD_TTL = 6 * 3600


def live_boards(boards: list[dict]) -> tuple[list[dict], int]:
    """Drop boards known to be dead, and say how many were dropped."""
    now = time.time()
    keep = [b for b in boards
            if _DEAD.get(f"{b['ats']}:{b['slug']}", 0) <= now]
    return keep, len(boards) - len(keep)


def mark_dead(key: str):
    _DEAD[key] = time.time() + DEAD_TTL


async def _host_gate(ats: str):
    """Hold a slot for this provider, spacing calls to the touchy ones."""
    if ats not in _HOST_GATES:
        _HOST_GATES[ats] = asyncio.Semaphore(HOST_CONCURRENCY.get(ats, 4))
    await _HOST_GATES[ats].acquire()
    gap = HOST_MIN_GAP.get(ats, 0.0) + _HOST_PENALTY.get(ats, 0.0)
    if gap:
        wait = gap - (time.monotonic() - _HOST_LAST.get(ats, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
    _HOST_LAST[ats] = time.monotonic()


def _host_release(ats: str):
    gate = _HOST_GATES.get(ats)
    if gate:
        gate.release()

PK_CITY_WORDS = ("pakistan", "karachi", "lahore", "islamabad", "rawalpindi",
                 "faisalabad", "multan", "peshawar", "hyderabad", "sialkot",
                 "gujranwala", "quetta")
OPEN_LOCATION_WORDS = ("worldwide", "anywhere", "global", "remote", "asia",
                       "apac", "emea", "international", "utc", "flexible")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_date(value) -> str | None:
    """Best-effort → ISO-8601 string, else None."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        s = str(value).strip()
        if s.isdigit():
            return parse_date(int(s))
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
        try:  # RFC 2822 (RSS)
            return parsedate_to_datetime(s).isoformat()
        except Exception:
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d %b %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(s[:len(fmt) + 6], fmt)\
                    .replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
    except Exception:
        return None
    return None


def _strip_html(text: str, limit: int = 1800) -> str:
    if not text:
        return ""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " ")
                .replace("&#39;", "'").replace("&quot;", '"'))
    return " ".join(text.split())[:limit]


def make_job(*, title, company, location, url, source, source_kind,
             remote=None, posted_at=None, description="", tags=None) -> dict:
    return {
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip() or "Not specified",
        "url": url or "",
        "source": source,
        "source_kind": source_kind,
        "remote": remote,
        "posted_at": posted_at,
        "description": description or "",
        "tags": tags or [],
    }


def looks_pakistan_friendly(job: dict) -> bool:
    """True if a job is in Pakistan, mentions it, or is open-location remote."""
    blob = " ".join([job.get("location", ""), job.get("title", ""),
                     " ".join(job.get("tags") or []),
                     (job.get("description") or "")[:400]]).lower()
    if any(w in blob for w in PK_CITY_WORDS):
        return True
    loc = job.get("location", "").lower()
    if job.get("remote") and (not loc or loc in ("not specified", "remote")):
        return True
    return any(w in loc for w in OPEN_LOCATION_WORDS)


async def _get_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# ATS fetchers — each returns list[dict] of normalized jobs
# ---------------------------------------------------------------------------

async def fetch_greenhouse(client, slug, company):
    data = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(make_job(
            title=j.get("title"), company=company,
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url"),
            source=f"greenhouse:{slug}", source_kind="ats",
            posted_at=parse_date(j.get("updated_at") or j.get("first_published")),
            description=_strip_html(j.get("content", "")),
        ))
    return jobs


async def fetch_lever(client, slug, company):
    data = await _get_json(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        jobs.append(make_job(
            title=j.get("text"), company=company,
            location=cats.get("location", ""),
            url=j.get("hostedUrl"),
            source=f"lever:{slug}", source_kind="ats",
            remote="remote" in (cats.get("location") or "").lower() or None,
            posted_at=parse_date(j.get("createdAt")),
            description=(j.get("descriptionPlain") or "")[:1800],
            tags=[t for t in [cats.get("team"), cats.get("commitment")] if t],
        ))
    return jobs


async def fetch_ashby(client, slug, company):
    data = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or ""
        secondary = [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        jobs.append(make_job(
            title=j.get("title"), company=company,
            location=", ".join([x for x in [loc, *secondary] if x]),
            url=j.get("jobUrl") or j.get("applyUrl"),
            source=f"ashby:{slug}", source_kind="ats",
            remote=j.get("isRemote"),
            posted_at=parse_date(j.get("publishedAt")),
            description=_strip_html(j.get("descriptionHtml") or ""),
            tags=[j.get("department") or "", j.get("team") or ""],
        ))
    return jobs


async def fetch_workable(client, slug, company):
    url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    r = await client.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                          json={"query": "", "location": [], "department": [],
                                "worktype": [], "remote": []},
                          timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("results", []):
        locobj = j.get("location") or {}
        city = locobj.get("city") or ""
        country = locobj.get("country") or ""
        jobs.append(make_job(
            title=j.get("title"), company=company,
            location=", ".join([x for x in [city, country] if x]),
            url=f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
            source=f"workable:{slug}", source_kind="ats",
            remote=bool(j.get("remote")),
            posted_at=parse_date(j.get("published_on") or j.get("created_at")),
            description="", tags=[j.get("department") or ""],
        ))
    return jobs


async def fetch_smartrecruiters(client, slug, company):
    data = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    jobs = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        city = loc.get("city") or ""
        country = (loc.get("country") or "").upper()
        jobs.append(make_job(
            title=j.get("name"), company=company,
            location=", ".join([x for x in [city, country] if x]),
            url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            source=f"smartrecruiters:{slug}", source_kind="ats",
            remote=bool(loc.get("remote")),
            posted_at=parse_date(j.get("releasedDate")),
            tags=[(j.get("department") or {}).get("label", "")],
        ))
    return jobs


async def fetch_recruitee(client, slug, company):
    data = await _get_json(client, f"https://{slug}.recruitee.com/api/offers/")
    jobs = []
    for j in data.get("offers", []):
        jobs.append(make_job(
            title=j.get("title"), company=company,
            location=j.get("location") or ", ".join(
                [x for x in [j.get("city"), j.get("country")] if x]),
            url=j.get("careers_url"),
            source=f"recruitee:{slug}", source_kind="ats",
            remote=(j.get("remote") in (True, "fully", "hybrid")) or None,
            posted_at=parse_date(j.get("published_at") or j.get("created_at")),
            description=_strip_html(j.get("description") or ""),
            tags=[j.get("department") or ""],
        ))
    return jobs


async def fetch_bamboohr(client, slug, company):
    data = await _get_json(client, f"https://{slug}.bamboohr.com/careers/list")
    jobs = []
    for j in (data.get("result") or []):
        loc = j.get("location") or {}
        loc_str = ", ".join([x for x in [loc.get("city"), loc.get("state")] if x])
        if j.get("isRemote"):
            loc_str = (loc_str + " (Remote)").strip()
        jobs.append(make_job(
            title=j.get("jobOpeningName"), company=company,
            location=loc_str,
            url=f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
            source=f"bamboohr:{slug}", source_kind="ats",
            remote=bool(j.get("isRemote")) or None,
            tags=[j.get("departmentLabel") or ""],
        ))
    return jobs


async def fetch_breezy(client, slug, company):
    data = await _get_json(client, f"https://{slug}.breezy.hr/json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("location") or {}
        country = (loc.get("country") or {})
        country_name = country.get("name") if isinstance(country, dict) else str(country or "")
        jobs.append(make_job(
            title=j.get("name"), company=company,
            location=", ".join([x for x in [loc.get("city"), country_name] if x]),
            url=j.get("url"),
            source=f"breezy:{slug}", source_kind="ats",
            posted_at=parse_date(j.get("published_date") or j.get("creation_date")),
            tags=[j.get("department") or "", j.get("type", {}).get("name", "")
                  if isinstance(j.get("type"), dict) else ""],
        ))
    return jobs


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "bamboohr": fetch_bamboohr,
    "breezy": fetch_breezy,
}


# ---------------------------------------------------------------------------
# Aggregators
# ---------------------------------------------------------------------------

async def fetch_remotive(client, keywords=""):
    url = "https://remotive.com/api/remote-jobs?limit=200"
    if keywords:
        url += f"&search={httpx.QueryParams({'q': keywords})['q']}"
    data = await _get_json(client, url)
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(make_job(
            title=j.get("title"), company=j.get("company_name"),
            location=j.get("candidate_required_location") or "Remote",
            url=j.get("url"), source="remotive", source_kind="aggregator",
            remote=True, posted_at=parse_date(j.get("publication_date")),
            description=_strip_html(j.get("description") or ""),
            tags=(j.get("tags") or [])[:8] + [j.get("job_type") or ""],
        ))
    return jobs


async def fetch_arbeitnow(client, keywords=""):
    data = await _get_json(client, "https://www.arbeitnow.com/api/job-board-api")
    jobs = []
    for j in data.get("data", []):
        jobs.append(make_job(
            title=j.get("title"), company=j.get("company_name"),
            location=j.get("location") or ("Remote" if j.get("remote") else ""),
            url=j.get("url"), source="arbeitnow", source_kind="aggregator",
            remote=bool(j.get("remote")),
            posted_at=parse_date(j.get("created_at")),
            description=_strip_html(j.get("description") or ""),
            tags=(j.get("tags") or [])[:8],
        ))
    return jobs


async def fetch_jobicy(client, keywords=""):
    url = "https://jobicy.com/api/v2/remote-jobs?count=100"
    data = await _get_json(client, url)
    jobs = []
    for j in data.get("jobs", []):
        industry = j.get("jobIndustry")
        if isinstance(industry, list):
            industry = ", ".join(industry)
        jobs.append(make_job(
            title=j.get("jobTitle"), company=j.get("companyName"),
            location=j.get("jobGeo") or "Remote",
            url=j.get("url"), source="jobicy", source_kind="aggregator",
            remote=True, posted_at=parse_date(j.get("pubDate")),
            description=_strip_html(j.get("jobExcerpt") or ""),
            tags=[industry or "", (j.get("jobType") or [""])[0]
                  if isinstance(j.get("jobType"), list) else (j.get("jobType") or "")],
        ))
    return jobs


async def fetch_himalayas(client, keywords=""):
    data = await _get_json(client, "https://himalayas.app/jobs/api?limit=100")
    jobs = []
    for j in data.get("jobs", []):
        restr = j.get("locationRestrictions") or []
        location = ", ".join(restr) if restr else "Worldwide"
        jobs.append(make_job(
            title=j.get("title"), company=j.get("companyName"),
            location=location,
            url=j.get("applicationLink") or j.get("guid"),
            source="himalayas", source_kind="aggregator",
            remote=True, posted_at=parse_date(j.get("pubDate")),
            description=_strip_html(j.get("excerpt") or j.get("description") or ""),
            tags=(j.get("categories") or [])[:8],
        ))
    return jobs


async def fetch_remoteok(client, keywords=""):
    data = await _get_json(client, "https://remoteok.com/api")
    jobs = []
    for j in (data[1:] if isinstance(data, list) and data else []):
        if not isinstance(j, dict) or not j.get("position"):
            continue
        jobs.append(make_job(
            title=j.get("position"), company=j.get("company"),
            location=j.get("location") or "Remote",
            url=j.get("url") or (f"https://remoteok.com{j.get('slug','')}" if j.get("slug") else ""),
            source="remoteok", source_kind="aggregator",
            remote=True, posted_at=parse_date(j.get("date") or j.get("epoch")),
            description=_strip_html(j.get("description") or ""),
            tags=(j.get("tags") or [])[:8],
        ))
    return jobs


async def fetch_weworkremotely(client, keywords=""):
    r = await client.get("https://weworkremotely.com/remote-jobs.rss",
                         headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    jobs = []
    for item in root.iter("item"):
        raw_title = (item.findtext("title") or "").strip()
        company, _, title = raw_title.partition(":")
        if not title:
            title, company = raw_title, ""
        region = item.findtext("region") or ""
        jobs.append(make_job(
            title=title.strip(), company=company.strip(),
            location=region or "Remote",
            url=(item.findtext("link") or "").strip(),
            source="weworkremotely", source_kind="aggregator",
            remote=True, posted_at=parse_date(item.findtext("pubDate")),
            description=_strip_html(item.findtext("description") or "", 900),
        ))
    return jobs


AGGREGATOR_FETCHERS = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "jobicy": fetch_jobicy,
    "himalayas": fetch_himalayas,
    "remoteok": fetch_remoteok,
    "weworkremotely": fetch_weworkremotely,
}


# ---------------------------------------------------------------------------
# Cached, status-wrapped execution
# ---------------------------------------------------------------------------

def cached_board_jobs(boards: list[dict]) -> tuple[list[dict], int]:
    """Jobs already cached for boards this search will not fetch.

    Searches rotate through a slice of the board list to keep the instance
    alive. Without this the results would swing between runs — twenty jobs,
    then three, then twenty. Reading the rest from cache means the picture
    only grows as more boards get covered.
    """
    wanted = {f"{b['ats']}:{b['slug']}" for b in boards}
    jobs: list[dict] = []
    n = 0
    now = time.time()
    for key, (expires, cached_jobs, _status) in _CACHE.items():
        if key in wanted or expires <= now:
            continue
        if ":" in key and key.split(":", 1)[0] in ATS_FETCHERS:
            jobs.extend(cached_jobs)
            n += 1
    return jobs, n


async def run_board(client: httpx.AsyncClient, board: dict) -> tuple[dict, list[dict]]:
    """board = {name, ats, slug} → (status, jobs). Never raises."""
    key = f"{board['ats']}:{board['slug']}"
    cached = _CACHE.get(key)
    if cached and cached[0] > time.time():
        return cached[2], cached[1]

    status = {"id": key, "label": board["name"], "kind": "ats",
              "ats": board["ats"], "ok": False, "count": 0, "error": None}
    ats = board["ats"]
    fetcher = ATS_FETCHERS[ats]
    jobs = []
    try:
        # One retry on 429: a rate limit is a "come back shortly", not a
        # dead board, and treating it as failure loses two thirds of the
        # registry on every search.
        for attempt in (1, 2):
            await _host_gate(ats)
            try:
                jobs = await fetcher(client, board["slug"], board["name"])
                status["ok"] = True
                status["count"] = len(jobs)
                status["error"] = None
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code == 429:
                    # Widen this provider's gap for every later request.
                    _HOST_PENALTY[ats] = min(
                        PENALTY_CAP, _HOST_PENALTY.get(ats, 0.0) + PENALTY_STEP)
                    if attempt == 1:
                        status["error"] = "rate limited, retrying"
                        await asyncio.sleep(1.5 + _HOST_PENALTY[ats])
                        continue
                if code in (404, 410):
                    status["error"] = "no public board found"
                    mark_dead(key)
                elif code == 429:
                    status["error"] = "rate limited by the provider"
                else:
                    status["error"] = f"HTTP {code}"
                break
            finally:
                _host_release(ats)
    except ValueError:
        # json() on a non-JSON body. The endpoint answered with HTML, which
        # means this is not a public board of that type — permanently.
        status["error"] = "not a public board (no JSON returned)"
        mark_dead(key)
    except httpx.TimeoutException:
        status["error"] = "timed out"
    except Exception as e:
        status["error"] = type(e).__name__

    _CACHE[key] = (time.time() + CACHE_TTL, jobs, status)
    return status, jobs


async def run_aggregator(client: httpx.AsyncClient, name: str,
                         keywords: str = "") -> tuple[dict, list[dict]]:
    key = f"agg:{name}"
    cached = _CACHE.get(key)
    if cached and cached[0] > time.time():
        return cached[2], cached[1]

    status = {"id": key, "label": name, "kind": "aggregator",
              "ok": False, "count": 0, "error": None}
    try:
        jobs = await AGGREGATOR_FETCHERS[name](client, keywords)
        status["ok"] = True
        status["count"] = len(jobs)
    except httpx.HTTPStatusError as e:
        status["error"] = f"HTTP {e.response.status_code}"
        jobs = []
    except httpx.TimeoutException:
        status["error"] = "timed out"
    except Exception as e:
        status["error"] = type(e).__name__

    _CACHE[key] = (time.time() + CACHE_TTL, jobs, status)
    return status, jobs


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(http2=False)


# ---------------------------------------------------------------------------
# Sites that do NOT permit third-party automation
#
# These are deliberately link-only. LinkedIn, Indeed, Rozee, Mustakbil, Bayt
# and Glassdoor either have no public jobs API, restrict theirs to approved
# partners, or explicitly forbid scraping in their terms of service. Building
# a scraper against them risks IP bans and legal exposure, and it would break
# the first time they change their markup. So the app links you straight into
# their own search instead, which is allowed and never breaks.
#
# The good news: Google for Jobs indexes most of those same postings, and
# JSearch (below) reads Google for Jobs through a proper licensed API. That is
# the legitimate route to LinkedIn/Rozee/Indeed listings.
# ---------------------------------------------------------------------------

RESTRICTED_SITES = [
    {"name": "LinkedIn",
     "why": "Jobs API is restricted to approved Talent Solutions partners. "
            "Scraping is prohibited by their terms.",
     "route": "Deep link, or reach the same postings via JSearch."},
    {"name": "Indeed",
     "why": "The old Publisher API was retired and closed to new signups. "
            "Scraping is prohibited by their terms.",
     "route": "Deep link, or reach the same postings via JSearch."},
    {"name": "Rozee.pk",
     "why": "No public API. Data access is arranged privately with employers "
            "and partners.",
     "route": "Deep link, or reach the same postings via JSearch / Careerjet."},
    {"name": "Mustakbil",
     "why": "No public API published.",
     "route": "Deep link."},
    {"name": "Bayt",
     "why": "No open public API; access is partner-only.",
     "route": "Deep link."},
    {"name": "Glassdoor",
     "why": "Partner-only API, closed to general signups.",
     "route": "Deep link."},
]


# ---------------------------------------------------------------------------
# Keyed sources — off until you add a key. Free tiers exist for all of them.
# Each entry documents exactly where the key comes from and what it covers.
# ---------------------------------------------------------------------------

KEYED_SOURCES = {
    "jsearch": {
        "label": "JSearch (Google for Jobs)",
        "keys": ["RAPIDAPI_KEY"],
        "coverage": "Pakistan + worldwide",
        "cost": "Free tier on RapidAPI (~200 requests/month)",
        "signup": "https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch",
        "note": "The one to add first. It reads Google for Jobs, which indexes "
                "Rozee, Indeed and LinkedIn postings — so this is the legal "
                "route to the listings those sites won't expose directly.",
    },
    "jooble": {
        "label": "Jooble",
        "keys": ["JOOBLE_API_KEY"],
        "coverage": "Pakistan + 60 countries",
        "cost": "Free key, request by email from their API page",
        "signup": "https://jooble.org/api/about",
        "note": "Runs jooble.org/pk, so genuine Pakistani coverage. Key usually "
                "arrives within a day.",
    },
    "careerjet": {
        "label": "Careerjet",
        "keys": ["CAREERJET_AFFID"],
        "coverage": "Pakistan + worldwide",
        "cost": "Free affiliate ID",
        "signup": "https://www.careerjet.com/partners/api/",
        "note": "Runs careerjet.com.pk. Set CAREERJET_LOCALE to change locale "
                "(defaults to en_PK).",
    },
    "adzuna": {
        "label": "Adzuna",
        "keys": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"],
        "coverage": "Worldwide — no Pakistan index",
        "cost": "Free tier, instant signup",
        "signup": "https://developer.adzuna.com/",
        "note": "Useful for the Worldwide toggle and remote roles, not for PK "
                "listings. Set ADZUNA_COUNTRY (default gb).",
    },
    "findwork": {
        "label": "Findwork.dev",
        "keys": ["FINDWORK_API_KEY"],
        "coverage": "Remote + tech worldwide",
        "cost": "Free key on signup",
        "signup": "https://findwork.dev/developers/",
        "note": "Tech-heavy remote board, strong for engineering roles.",
    },
    "themuse": {
        "label": "The Muse",
        "keys": ["MUSE_API_KEY"],
        "coverage": "Worldwide, US-heavy",
        "cost": "Free key",
        "signup": "https://www.themuse.com/developers/api/v2",
        "note": "Higher rate limits with a key; mostly US and remote roles.",
    },
}


async def fetch_jsearch(client, keywords, scope, keys):
    query = keywords or ("jobs in Pakistan" if scope == "pakistan" else "remote jobs")
    params = {"query": query, "page": "1", "num_pages": "1", "date_posted": "all"}
    if scope == "pakistan":
        params["country"] = "pk"
    headers = {**HEADERS, "X-RapidAPI-Key": keys["RAPIDAPI_KEY"],
               "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}

    # v5 of the API serves search from /search-v2; older plans still use
    # /search. Try the current path first, fall back if it isn't there.
    payload = None
    for path in ("/search-v2", "/search"):
        r = await client.get(f"https://jsearch.p.rapidapi.com{path}",
                             params=params, headers=headers, timeout=KEYED_TIMEOUT)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        payload = r.json()
        break
    if payload is None:
        raise httpx.HTTPStatusError("no JSearch search endpoint responded",
                                    request=r.request, response=r)

    # /search returns data as a list; /search-v2 nests it under data.jobs
    # alongside a pagination cursor. Accept either.
    raw = payload.get("data")
    if isinstance(raw, dict):
        raw = raw.get("jobs") or raw.get("results") or []
    if not isinstance(raw, list):
        raw = []

    jobs = []
    for j in raw:
        if not isinstance(j, dict):
            continue
        loc = ", ".join(x for x in [j.get("job_city"), j.get("job_state"),
                                    j.get("job_country")] if x)
        jobs.append(make_job(
            title=j.get("job_title"), company=j.get("employer_name"),
            location=loc or ("Remote" if j.get("job_is_remote") else ""),
            url=j.get("job_apply_link") or j.get("job_google_link"),
            source="jsearch", source_kind="keyed",
            remote=bool(j.get("job_is_remote")),
            posted_at=parse_date(j.get("job_posted_at_datetime_utc")
                                 or j.get("job_posted_at_timestamp")),
            description=_strip_html(j.get("job_description") or ""),
            tags=[j.get("job_employment_type") or "", j.get("job_publisher") or ""],
        ))
    return jobs


async def fetch_jooble(client, keywords, scope, keys):
    body = {"keywords": keywords or "",
            "location": "Pakistan" if scope == "pakistan" else ""}
    r = await client.post(f"https://jooble.org/api/{keys['JOOBLE_API_KEY']}",
                          json=body, headers={**HEADERS, "Content-Type": "application/json"},
                          timeout=KEYED_TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in (r.json().get("jobs") or []):
        jobs.append(make_job(
            title=j.get("title"), company=j.get("company"),
            location=j.get("location"), url=j.get("link"),
            source="jooble", source_kind="keyed",
            posted_at=parse_date(j.get("updated")),
            description=_strip_html(j.get("snippet") or ""),
            tags=[j.get("type") or ""],
        ))
    return jobs


async def fetch_careerjet(client, keywords, scope, keys):
    import os
    params = {
        "affid": keys["CAREERJET_AFFID"],
        "keywords": keywords or "",
        "location": "Pakistan" if scope == "pakistan" else "",
        "locale_code": os.environ.get("CAREERJET_LOCALE", "en_PK"),
        "pagesize": "50",
        "user_ip": os.environ.get("CAREERJET_USER_IP", "127.0.0.1"),
        "user_agent": UA,
    }
    # Careerjet's public API is documented over http; https sometimes refuses
    # the connection outright. Try secure first, fall back so it still works.
    last_error = None
    for scheme in ("https", "http"):
        try:
            r = await client.get(f"{scheme}://public.api.careerjet.net/search",
                                 params=params, headers=HEADERS,
                                 timeout=KEYED_TIMEOUT)
            r.raise_for_status()
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_error = e
    else:
        raise last_error

    data = r.json()
    jobs = []
    for j in (data.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        jobs.append(make_job(
            title=j.get("title"), company=j.get("company"),
            location=j.get("locations"), url=j.get("url"),
            source="careerjet", source_kind="keyed",
            posted_at=parse_date(j.get("date")),
            description=_strip_html(j.get("description") or ""),
        ))
    return jobs


async def fetch_adzuna(client, keywords, scope, keys):
    import os
    country = os.environ.get("ADZUNA_COUNTRY", "gb").lower()
    params = {"app_id": keys["ADZUNA_APP_ID"], "app_key": keys["ADZUNA_APP_KEY"],
              "results_per_page": "50", "content-type": "application/json"}
    if keywords:
        params["what"] = keywords
    r = await client.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                         params=params, headers=HEADERS, timeout=KEYED_TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in (r.json().get("results") or []):
        jobs.append(make_job(
            title=j.get("title"),
            company=(j.get("company") or {}).get("display_name"),
            location=(j.get("location") or {}).get("display_name"),
            url=j.get("redirect_url"), source="adzuna", source_kind="keyed",
            posted_at=parse_date(j.get("created")),
            description=_strip_html(j.get("description") or ""),
            tags=[(j.get("category") or {}).get("label", "")],
        ))
    return jobs


async def fetch_findwork(client, keywords, scope, keys):
    params = {}
    if keywords:
        params["search"] = keywords
    r = await client.get("https://findwork.dev/api/jobs/", params=params,
                         headers={**HEADERS,
                                  "Authorization": f"Token {keys['FINDWORK_API_KEY']}"},
                         timeout=KEYED_TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in (r.json().get("results") or []):
        jobs.append(make_job(
            title=j.get("role"), company=j.get("company_name"),
            location=j.get("location") or ("Remote" if j.get("remote") else ""),
            url=j.get("url"), source="findwork", source_kind="keyed",
            remote=bool(j.get("remote")),
            posted_at=parse_date(j.get("date_posted")),
            description=_strip_html(j.get("text") or ""),
            tags=(j.get("keywords") or [])[:8],
        ))
    return jobs


async def fetch_themuse(client, keywords, scope, keys):
    params = {"page": "0", "api_key": keys["MUSE_API_KEY"]}
    r = await client.get("https://www.themuse.com/api/public/jobs", params=params,
                         headers=HEADERS, timeout=KEYED_TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in (r.json().get("results") or []):
        locations = ", ".join(l.get("name", "") for l in (j.get("locations") or []))
        jobs.append(make_job(
            title=j.get("name"), company=(j.get("company") or {}).get("name"),
            location=locations,
            url=(j.get("refs") or {}).get("landing_page"),
            source="themuse", source_kind="keyed",
            posted_at=parse_date(j.get("publication_date")),
            description=_strip_html(j.get("contents") or ""),
            tags=[c.get("name", "") for c in (j.get("categories") or [])][:5],
        ))
    return jobs


KEYED_FETCHERS = {
    "jsearch": fetch_jsearch,
    "jooble": fetch_jooble,
    "careerjet": fetch_careerjet,
    "adzuna": fetch_adzuna,
    "findwork": fetch_findwork,
    "themuse": fetch_themuse,
}


async def run_keyed(client: httpx.AsyncClient, name: str, keys: dict,
                    keywords: str = "", scope: str = "pakistan"
                    ) -> tuple[dict, list[dict]]:
    """Same contract as run_board / run_aggregator. Never raises."""
    cache_key = f"keyed:{name}:{scope}:{keywords.lower()}"
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[2], cached[1]

    label = KEYED_SOURCES[name]["label"]
    status = {"id": cache_key, "label": label, "kind": "keyed",
              "ok": False, "count": 0, "error": None}
    try:
        jobs = await KEYED_FETCHERS[name](client, keywords, scope, keys)
        status["ok"] = True
        status["count"] = len(jobs)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        status["error"] = ("key rejected (401) — wrong or expired key" if code == 401
                           else "forbidden (403) — check the key and that you're "
                                "subscribed to the plan" if code == 403
                           else "rate limit reached" if code == 429
                           else f"HTTP {code}")
        jobs = []
    except httpx.TimeoutException:
        status["error"] = "timed out"
        jobs = []
    except Exception as e:
        status["error"] = type(e).__name__
        jobs = []

    _CACHE[cache_key] = (time.time() + CACHE_TTL, jobs, status)
    return status, jobs
