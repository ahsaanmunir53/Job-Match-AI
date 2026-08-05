"""
Company ATS boards — the source behind the aggregators.

LinkedIn and Indeed do not originate job postings; they syndicate them. The
posting is created in the company's applicant tracking system, and most of those
systems expose a PUBLIC, key-less JSON board that anyone may read — it is how the
company's own careers page renders.

Reading those boards gets you the same roles, usually sooner than the aggregator
re-post, with a direct apply link instead of a redirect. It also stays entirely
within terms, unlike driving a logged-in LinkedIn session.

  Greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  Lever       https://api.lever.co/v0/postings/{slug}?mode=json
  Ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}

Add or remove companies in COMPANIES below — one line each.
"""
from __future__ import annotations

import html
import re
from typing import Dict, List

import httpx

# Companies that hire remotely / internationally. Edit freely.
COMPANIES = [
    ("greenhouse", "gitlab"),
    ("greenhouse", "doximity"),
    ("greenhouse", "airbyte"),
    ("greenhouse", "cockroachlabs"),
    ("greenhouse", "grafanalabs"),
    ("lever", "postman"),
    ("lever", "netlify"),
    ("ashby", "ramp"),
    ("ashby", "linear"),

    # ══════════════ Pakistan ══════════════
    # Most Pakistani software houses run a standard ATS rather than a custom
    # careers page, which is the only reason their listings are reachable at all.
    # Verify a slug by opening the company's Careers link and reading the URL —
    # see the table in README.md. Dead slugs are skipped silently, so a wrong
    # guess costs nothing but a failed request.
    ("workable", "devsinc-17"),
    ("workable", "venturedive"),
    ("workable", "emumba"),
    ("workable", "tkxel"),
    ("workable", "confiz"),
    ("workable", "xavor"),
    ("workable", "gaditek"),
    ("workable", "cubix"),
    ("workable", "folio3"),
    ("workable", "conradlabs"),
    ("workable", "sastaticket"),
    ("workable", "bazaar-technologies"),
    ("workable", "postex"),
    ("workable", "abhi"),
    ("workable", "trukkr"),
    ("workable", "dubizzlelabs"),
    ("workable", "haball"),
    ("workable", "codeautomation"),
    ("recruitee", "arbisoft"),
    ("recruitee", "techlogix"),
    ("recruitee", "netsol"),
    ("smartrecruiters", "Systemsltd"),
    ("smartrecruiters", "Careem"),
    ("smartrecruiters", "ibex"),
    ("greenhouse", "educative"),
    ("greenhouse", "motive"),
    ("lever", "afiniti"),

    # ══════════════ Gulf / wider region (hire from Pakistan) ══════════════
    ("workable", "tabby"),
    ("smartrecruiters", "Talabat"),
    ("lever", "swvl"),
]

UA = {"User-Agent": "JobMatchAI/1.0 (personal job search tool)"}


def _strip(t: str, limit: int = 4000) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


async def _greenhouse(client, slug):
    r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                         headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for j in (r.json().get("jobs") or [])[:60]:
        loc = (j.get("location") or {}).get("name", "")
        out.append({
            "id": f"gh-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "company": slug.replace("-", " ").title(),
            "location": loc or "Not stated",
            "remote": "remote" in loc.lower(),
            "tags": [], "url": j.get("absolute_url", ""),
            "description": _strip(j.get("content", "")),
            "source": "Greenhouse", "posted": j.get("updated_at"),
        })
    return out


async def _lever(client, slug):
    r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for j in (r.json() or [])[:60]:
        cat = j.get("categories") or {}
        loc = cat.get("location", "") or ""
        out.append({
            "id": f"lv-{slug}-{j.get('id')}",
            "title": j.get("text", ""),
            "company": slug.replace("-", " ").title(),
            "location": loc or "Not stated",
            "remote": "remote" in loc.lower(),
            "tags": [x for x in [cat.get("team"), cat.get("commitment")] if x],
            "url": j.get("hostedUrl", ""),
            "description": _strip(j.get("descriptionPlain") or j.get("description", "")),
            "source": "Lever", "posted": None,
        })
    return out


async def _ashby(client, slug):
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false",
                         headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for j in (r.json().get("jobs") or [])[:60]:
        loc = j.get("location", "") or ""
        out.append({
            "id": f"as-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "company": j.get("companyName") or slug.title(),
            "location": loc or "Not stated",
            "remote": bool(j.get("isRemote")) or "remote" in loc.lower(),
            "tags": [j.get("department")] if j.get("department") else [],
            "url": j.get("jobUrl", ""),
            "description": _strip(j.get("descriptionPlain") or ""),
            "source": "Ashby", "posted": j.get("publishedAt"),
        })
    return out


async def _workable(client, slug):
    """Workable powers many Pakistani software houses (e.g. Devsinc)."""
    r = await client.get(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
        headers=UA, timeout=15)
    r.raise_for_status()
    data = r.json()
    company = data.get("name") or slug.split("-")[0].title()
    out = []
    for j in (data.get("jobs") or [])[:60]:
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append({
            "id": f"wk-{slug}-{j.get('shortcode')}",
            "title": j.get("title", ""),
            "company": company,
            "location": loc or "Not stated",
            "remote": bool(j.get("telecommuting")) or "remote" in loc.lower(),
            "tags": [j.get("department")] if j.get("department") else [],
            "url": j.get("url") or j.get("application_url", ""),
            "description": _strip(j.get("description", "")),
            "source": "Workable", "posted": j.get("published_on") or j.get("created_at"),
        })
    return out


async def _smartrecruiters(client, slug):
    r = await client.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
        headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for j in (r.json().get("content") or [])[:60]:
        loc_o = j.get("location") or {}
        loc = ", ".join(x for x in [loc_o.get("city"), loc_o.get("country")] if x)
        out.append({
            "id": f"sr-{slug}-{j.get('id')}",
            "title": j.get("name", ""),
            "company": (j.get("company") or {}).get("name") or slug.title(),
            "location": loc or "Not stated",
            "remote": bool(loc_o.get("remote")) or "remote" in loc.lower(),
            "tags": [(j.get("department") or {}).get("label")] if j.get("department") else [],
            "url": (j.get("ref") or "").replace("api.smartrecruiters.com/v1/companies",
                                                "jobs.smartrecruiters.com") or
                   f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            "description": _strip(str(j.get("jobAd", ""))),
            "source": "SmartRecruiters", "posted": j.get("releasedDate"),
        })
    return out


async def _recruitee(client, slug):
    r = await client.get(f"https://{slug}.recruitee.com/api/offers/", headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for j in (r.json().get("offers") or [])[:60]:
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append({
            "id": f"rc-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "company": j.get("company_name") or slug.title(),
            "location": loc or "Not stated",
            "remote": "remote" in (j.get("remote") or loc or "").lower(),
            "tags": [j.get("department")] if j.get("department") else [],
            "url": j.get("careers_url") or j.get("url", ""),
            "description": _strip(j.get("description", "")),
            "source": "Recruitee", "posted": j.get("published_at"),
        })
    return out


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
            "workable": _workable, "smartrecruiters": _smartrecruiters,
            "recruitee": _recruitee}


async def fetch_ats(report: bool = False):
    """
    Pull every configured company board concurrently.

    A wrong slug is expected — company career URLs change and some of these are
    educated guesses. A failure is therefore silent by design: it costs one dead
    request and never breaks the search. Pass report=True (or call /api/sources)
    to see exactly which boards answered, so bad slugs can be corrected.
    """
    import asyncio

    sem = asyncio.Semaphore(12)          # be polite; 39 boards at once is rude

    async def one(client, kind, slug):
        async with sem:
            try:
                jobs = await FETCHERS[kind](client, slug)
                return {"platform": kind, "slug": slug, "ok": True,
                        "count": len(jobs), "jobs": jobs}
            except httpx.HTTPStatusError as e:
                return {"platform": kind, "slug": slug, "ok": False,
                        "count": 0, "jobs": [], "error": f"HTTP {e.response.status_code}"}
            except Exception as e:
                return {"platform": kind, "slug": slug, "ok": False,
                        "count": 0, "jobs": [], "error": type(e).__name__}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            *[one(client, k, sl) for k, sl in COMPANIES if k in FETCHERS])

    jobs = []
    for r in results:
        jobs.extend(j for j in r["jobs"] if j.get("title") and j.get("url"))

    if report:
        return jobs, [{k: v for k, v in r.items() if k != "jobs"} for r in results]
    return jobs
