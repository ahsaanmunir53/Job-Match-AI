# JobMatch AI — Pakistan-first job radar

Upload a resume → get live jobs from real company hiring systems and remote job
feeds, scored against your skills, plus a searchable directory of **637
Pakistani employers** across 34 sectors (663 unique companies counting the live
boards).

Single FastAPI service, static frontend, free-tier friendly (no scikit-learn,
no database, in-memory caching).

---

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# open http://127.0.0.1:8000
```

## Deploy on Render (free tier)

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

Nothing else needed. First search after a cold start is the slowest; results
are cached for 15 minutes per source after that.

---

## What was broken before, and the actual fixes

**1. “All ATS is blocked.”**
The old build called Greenhouse/Lever/Ashby/Workable **directly from the
browser**. Browsers block cross-origin API calls (CORS), so every single ATS
request died in devtools as “blocked” — nothing was wrong with the ATS, the
architecture was wrong. Now **every fetch goes through the backend** (`sources.py`),
which browsers don't restrict. Each source also gets a browser-like User-Agent
(several boards 403 the default Python client), a hard 9-second timeout, and
its own error handling, so one bad source can never block the rest. The live
**signal board** in the UI shows every source's result as it lands: `✓ 12 jobs`,
`○ 0 open roles`, or `✕ no public board found` — visible truth instead of a
silent hang.

**2. White City input.**
That was Chrome **autofill** painting its own white background over the dark
field. Fixed in `styles.css` with the inset box-shadow override on
`input:-webkit-autofill` plus `color-scheme: dark` so native widgets (select
menus, scrollbars) render dark too.

**3. White “review or edit the text” box.**
That was an **unstyled native button**. The stylesheet now themes every
control — buttons, inputs, selects, textareas, range sliders, file inputs —
so nothing can fall back to white browser defaults.

---

## Where jobs come from

| Layer | What it is |
|---|---|
| **87 Pakistan-linked company boards** | Companies with offices in Pakistan or a record of hiring Pakistani talent, pulled live from Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR and Breezy public APIs. |
| **20 global remote-friendly boards** | GitLab, Canonical, Zapier, Deel, Supabase, etc. — used in Worldwide scope. |
| **6 keyless job feeds** | Remotive, Arbeitnow, Jobicy, Himalayas, RemoteOK, WeWorkRemotely. In Pakistan scope, only jobs that are open-location remote or mention Pakistan pass through. |
| **637-company PK directory** | Searchable by sector/city; every row has a careers-search link that always works, plus the official website where known. |
| **6 optional keyed APIs** | JSearch, Jooble, Careerjet, Adzuna, Findwork, The Muse — off until you add a free key. See below. |
| **Quick links** | One-click searches on Rozee, Indeed PK, LinkedIn, Mustakbil, Bayt and Glassdoor built from your keywords. |

---

## Why LinkedIn, Rozee and Indeed are link-only

**These sites do not permit third-party apps to pull their listings.** Not a
technical limitation — a policy one:

| Site | Situation |
|---|---|
| **LinkedIn** | Jobs API restricted to approved Talent Solutions partners. Scraping prohibited by their terms. |
| **Indeed** | The Publisher API was retired and closed to new signups. Scraping prohibited by their terms. |
| **Rozee.pk** | No public API. Data access is arranged privately with employers and partners. |
| **Mustakbil** | No public API published. |
| **Bayt** | No open public API; partner-only. |
| **Glassdoor** | Partner-only API, closed to general signups. |

Scraping them anyway would mean IP bans, possible legal exposure, and code that
breaks the moment they change their HTML. So the app deep-links into their own
search with your keywords pre-filled. That's permitted, instant, and it never
silently breaks.

**The workaround that is legitimate:** Google for Jobs indexes most of those
same postings, and **JSearch** reads Google for Jobs through a properly licensed
API. Add that one key and you effectively get Rozee, Indeed and LinkedIn
listings through the front door. It's the single highest-value key on the list.

---

## Turning on the keyed sources

Copy `data/api_keys.example.json` → `data/api_keys.json`, paste your keys, delete
the lines you're not using, restart the server. A source stays off until every
key it needs is filled, and unfilled `PASTE_HERE` lines are ignored. Environment
variables work too if you prefer them (same names) — useful on Render, where you
set them under **Environment**.

| Source | Coverage | Cost | Where to get the key |
|---|---|---|---|
| **JSearch** ⭐ | Pakistan + worldwide | Free tier on RapidAPI | [rapidapi.com/…/jsearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) |
| **Jooble** | Pakistan + 60 countries | Free key on request | [jooble.org/api/about](https://jooble.org/api/about) |
| **Careerjet** | Pakistan + worldwide | Free affiliate ID | [careerjet.com/partners/api](https://www.careerjet.com/partners/api/) |
| **Adzuna** | Worldwide — *no Pakistan index* | Free, instant | [developer.adzuna.com](https://developer.adzuna.com/) |
| **Findwork.dev** | Remote tech worldwide | Free key | [findwork.dev/developers](https://findwork.dev/developers/) |
| **The Muse** | Worldwide, US-heavy | Free key | [themuse.com/developers](https://www.themuse.com/developers/api/v2) |

For Pakistan specifically, the order that matters is **JSearch → Jooble →
Careerjet**. Adzuna has no Pakistani index, so it only helps the Worldwide
toggle.

Keyed sources are query-driven, so set **Role or keywords** before searching —
they return far better results with a term than without one. A bad or expired
key shows `✕ key rejected — check it` on the source board and costs you nothing;
the rest of the search carries on.

Optional extras: `ADZUNA_COUNTRY` (default `gb`), `CAREERJET_LOCALE` (default
`en_PK`).

> **Never commit `data/api_keys.json` to GitHub.** Add it to `.gitignore`. If a
> key does get pushed, revoke and reissue it — the same lesson as the Monal
> `.env` incident.

### About board slugs (read this once)

ATS slugs in `data_companies.py` are **best-effort seeds**. Companies rename
boards or switch ATS providers all the time, so the app treats the list as
claims to verify, not facts: press **“Check sources”** in the header and the
app probes every board live, marks what resolves, and caches the result.
A dead slug shows `✕ no public board found` and costs you nothing. Run it
once after deploying to see your true live-board count.

### Add your own boards — no code

Create `data/custom_boards.json`:

```json
[
  {"name": "Some Company", "ats": "greenhouse", "slug": "somecompany", "pk": true},
  {"name": "Another One", "ats": "lever", "slug": "anotherone", "pk": false}
]
```

Supported `ats` values: `greenhouse`, `lever`, `ashby`, `workable`,
`smartrecruiters`, `recruitee`, `bamboohr`, `breezy`. Finding a company's
slug: open their careers page and look at the URL —
`boards.greenhouse.io/acme` → greenhouse/`acme`,
`jobs.lever.co/acme` → lever/`acme`, `apply.workable.com/acme` → workable/`acme`,
`acme.recruitee.com` → recruitee/`acme`.

---

## Testing your keys

```bash
python check_keys.py                     # uses "python developer"
python check_keys.py "devops engineer"   # your own query
```

One line per source: whether the key works, job count, response time, and a
sample listing. Costs one request per configured source — don't loop it on the
JSearch free tier.

```bash
python test_parsers.py
```

Verifies every keyed fetcher parses its API's response shape correctly. Uses
mocked responses, so it spends nothing and works offline.

## Match scoring

`score = 0.55 × scaled TF-IDF cosine (resume ↔ job text) + 0.45 × skill overlap`

Skills come from a 140-entry vocabulary with aliases (`k8s` → Kubernetes,
`js` → JavaScript), matched on word boundaries. Bands: **Strong ≥ 70**,
Good 50–69, Fair 30–49, Weak < 30. Without a resume loaded, jobs sort by date
instead of score.

## Files

```
main.py            FastAPI app — streaming search, resume, directory, verify
sources.py         All fetchers (8 ATS + 6 feeds), caching, error isolation
matching.py        Resume parsing (PDF/DOCX/TXT), skills, TF-IDF scoring
data_companies.py  Board registry + 637-company PK directory
static/            index.html, styles.css, app.js
```
