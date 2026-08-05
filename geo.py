"""
Location and work-type filtering.

THE BUG THIS FILE EXISTS TO FIX
------------------------------
The first version kept one flat list per region and asked "is the preference in
this list, and does the job location contain any term from it". Selecting
Pakistan therefore expanded to every Asian term and matched jobs in India and
the UAE. A country must match that country. Only an explicit region name may
expand.

WHAT "OPEN TO ME" MEANS
-----------------------
A posting that says "Remote - Worldwide" is genuinely available to someone in
Lahore, so it matches every country. A posting that says "Remote - US only" is
not, even though it says remote. That distinction is the whole point of the
filter, so it is handled explicitly rather than by substring luck.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# ── countries: canonical name -> strings that appear in job location fields ──
COUNTRIES: Dict[str, List[str]] = {
    "Pakistan": ["pakistan", "lahore", "karachi", "islamabad", "rawalpindi", "faisalabad", "peshawar"],
    "India": ["india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "chennai", "noida", "gurgaon"],
    "United Arab Emirates": ["united arab emirates", "uae", "dubai", "abu dhabi", "sharjah"],
    "Saudi Arabia": ["saudi", "riyadh", "jeddah"],
    "Singapore": ["singapore"],
    "United Kingdom": ["united kingdom", "uk", "england", "london", "manchester", "edinburgh", "bristol"],
    "Germany": ["germany", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne"],
    "Netherlands": ["netherlands", "amsterdam", "rotterdam", "utrecht"],
    "Poland": ["poland", "warsaw", "krakow", "kraków", "wroclaw"],
    "Spain": ["spain", "madrid", "barcelona", "valencia"],
    "Portugal": ["portugal", "lisbon", "lisboa", "porto"],
    "France": ["france", "paris", "lyon"],
    "Ireland": ["ireland", "dublin"],
    "Sweden": ["sweden", "stockholm"],
    "Switzerland": ["switzerland", "zurich", "geneva"],
    "United States": ["united states", "usa", " us ", "u.s.", "new york", "san francisco",
                      "seattle", "austin", "boston", "chicago", "los angeles", "denver"],
    "Canada": ["canada", "toronto", "vancouver", "montreal", "ottawa"],
    "Australia": ["australia", "sydney", "melbourne", "brisbane"],
    "Turkey": ["turkey", "türkiye", "istanbul", "ankara"],
    "Egypt": ["egypt", "cairo"],
    "Nigeria": ["nigeria", "lagos", "abuja"],
    "Kenya": ["kenya", "nairobi"],
    "South Africa": ["south africa", "cape town", "johannesburg"],
    "Brazil": ["brazil", "brasil", "sao paulo", "são paulo"],
    "Japan": ["japan", "tokyo"],
    "Malaysia": ["malaysia", "kuala lumpur"],
    "Indonesia": ["indonesia", "jakarta"],
    "Philippines": ["philippines", "manila"],
    "Bangladesh": ["bangladesh", "dhaka"],
}

# ── regions: the ONLY things allowed to expand to many countries ──
REGIONS: Dict[str, List[str]] = {
    "Worldwide": [],                       # special-cased below
    "Europe": ["United Kingdom", "Germany", "Netherlands", "Poland", "Spain", "Portugal",
               "France", "Ireland", "Sweden", "Switzerland"],
    "Asia": ["Pakistan", "India", "Singapore", "Japan", "Malaysia", "Indonesia",
             "Philippines", "Bangladesh"],
    "Middle East": ["United Arab Emirates", "Saudi Arabia", "Turkey", "Egypt"],
    "North America": ["United States", "Canada"],
    "Africa": ["Nigeria", "Kenya", "South Africa", "Egypt"],
    "South Asia": ["Pakistan", "India", "Bangladesh"],
}

# Phrases meaning "anyone, anywhere" — these are open to every country.
GLOBAL_TERMS = [
    "worldwide", "anywhere", "global", "any location", "fully remote",
    "remote - global", "location independent", "work from anywhere",
]

# Phrases meaning "remote, but only from here" — must NOT count as worldwide.
RESTRICTED = re.compile(
    r"remote\s*[-–(,]?\s*(us|usa|united states|uk|eu|europe|emea|canada|apac|latam|india)\b"
    r"|\b(us|usa|uk|eu|canada|india)[\s-]+only\b"
    r"|must be (located|based) in", re.I)


def _text(job: Dict) -> str:
    return " ".join(str(x) for x in [
        job.get("location", ""), job.get("title", ""),
        " ".join(map(str, job.get("tags", []) or [])),
    ]).lower()


def is_global(job: Dict) -> bool:
    """Open to any country — not merely 'remote'."""
    t = _text(job)
    if RESTRICTED.search(t):
        return False
    return any(g in t for g in GLOBAL_TERMS)


def country_of(job: Dict) -> str | None:
    t = _text(job)
    for name, aliases in COUNTRIES.items():
        if any(a in t for a in aliases):
            return name
    return None


def matches_location(job: Dict, country: str = "", city: str = "",
                     include_global: bool = True) -> bool:
    """
    country may be a country name OR a region name. Only region names expand.
    city is a plain substring test against the location text.
    """
    t = _text(job)

    if city:
        if city.strip().lower() not in t:
            return False

    if not country:
        return True

    c = country.strip()

    # "Worldwide" means: only show postings open to everyone
    if c.lower() == "worldwide":
        return is_global(job)

    # region -> any of its member countries
    region = next((r for r in REGIONS if r.lower() == c.lower()), None)
    if region:
        members = REGIONS[region]
        hit = any(any(a in t for a in COUNTRIES[m]) for m in members)
        return hit or (include_global and is_global(job))

    # plain country -> that country's own aliases only
    canon = next((k for k in COUNTRIES if k.lower() == c.lower()), None)
    aliases = COUNTRIES.get(canon, [c.lower()])
    hit = any(a in t for a in aliases)
    return hit or (include_global and is_global(job))


# ── work type ─────────────────────────────────────────────────────────────
HYBRID = re.compile(r"\bhybrid\b|\d\s*days? (a week )?(in|at) (the )?office|partially remote", re.I)
ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|office[- ]based|no remote", re.I)


def work_type_of(job: Dict) -> str:
    blob = f"{_text(job)} {str(job.get('description',''))[:600]}"
    if HYBRID.search(blob):
        return "hybrid"
    if job.get("remote") or "remote" in _text(job):
        return "remote"
    if ONSITE.search(blob):
        return "onsite"
    return "unspecified"


def matches_work_type(job: Dict, want: str) -> bool:
    if not want or want == "any":
        return True
    wt = work_type_of(job)
    if want == "remote":
        return wt == "remote"
    if want == "hybrid":
        return wt == "hybrid"
    if want == "onsite":
        return wt in ("onsite", "unspecified")
    return True


def apply_filters(jobs: List[Dict], query: str = "", country: str = "", city: str = "",
                  work_type: str = "any") -> Tuple[List[Dict], Dict]:
    """
    Returns (filtered, diagnostics). Diagnostics tell the UI *why* a result set
    is small, which is far more useful than showing an empty page.
    """
    total = len(jobs)

    after_q = jobs
    q = (query or "").strip().lower()
    if q:
        terms = [t for t in q.split() if t]
        after_q = [
            j for j in jobs
            if any(t in f"{j.get('title','')} {j.get('company','')} "
                   f"{' '.join(map(str, j.get('tags',[]) or []))} "
                   f"{str(j.get('description',''))[:900]}".lower() for t in terms)
        ]

    after_wt = [j for j in after_q if matches_work_type(j, work_type)]

    # local = physically in that country; global = open to everyone
    local, glob = [], []
    for j in after_wt:
        if country or city:
            if matches_location(j, country, city, include_global=False):
                local.append(j)
            elif not city and country and is_global(j):
                glob.append(j)
        else:
            local.append(j)

    diag = {
        "total_jobs": total,
        "after_keyword": len(after_q),
        "after_work_type": len(after_wt),
        "local_matches": len(local),
        "global_open": len(glob),
        "country": country,
        "city": city,
    }
    return local + glob, diag
