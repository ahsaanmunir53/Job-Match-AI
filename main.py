"""
JobMatch AI — Pakistan-first resume→job matcher.

Run locally:   uvicorn main:app --reload
Deploy (Render): build `pip install -r requirements.txt`
                 start `uvicorn main:app --host 0.0.0.0 --port $PORT`
"""

from __future__ import annotations

import asyncio
import json
import time
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import matching
import sources
from data_companies import ATS_BOARDS, PK_DIRECTORY

BASE_DIR = Path(__file__).parent
MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB
# Free-tier instances get a fraction of a CPU, and every HTTPS fetch needs a
# TLS handshake — which is CPU work. Twenty at once starved the event loop, the
# health check timed out, and the platform restarted the instance mid-search.
CONCURRENCY = 5

# No search may run forever. Whatever arrived by the deadline is what ships.
SEARCH_DEADLINE = 40.0

# Hitting every board on every search is what caused the overload. Take a slice
# and rotate the starting point, so all boards still get covered over time.
BOARDS_PER_SEARCH = 25
_board_offset = 0

app = FastAPI(title="JobMatch AI", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
# Job payloads and the 637-row directory compress very well.
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ---------------------------------------------------------------------------
# Board registry (code seeds + optional data/custom_boards.json)
# ---------------------------------------------------------------------------

def load_boards() -> list[dict]:
    boards = [{"name": n, "ats": a, "slug": s, "pk": pk, "tags": tags}
              for (n, a, s, pk, tags) in ATS_BOARDS]
    custom_path = BASE_DIR / "data" / "custom_boards.json"
    if custom_path.exists():
        try:
            for item in json.loads(custom_path.read_text()):
                if item.get("ats") in sources.ATS_FETCHERS and item.get("slug"):
                    boards.append({"name": item.get("name") or item["slug"],
                                   "ats": item["ats"], "slug": item["slug"],
                                   "pk": bool(item.get("pk", True)),
                                   "tags": item.get("tags") or []})
        except Exception:
            pass  # a malformed custom file must never take the app down
    seen: set[tuple[str, str]] = set()
    unique = []
    for b in boards:
        key = (b["ats"], b["slug"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(b)
    return unique


BOARDS = load_boards()
DIRECTORY = [{"name": n, "sector": s, "city": c, "website": w}
             for (n, s, c, w) in PK_DIRECTORY]


# ---------------------------------------------------------------------------
# API keys — from data/api_keys.json first, then environment variables.
# A source stays off until every key it needs is present.
# ---------------------------------------------------------------------------

def load_api_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    key_path = BASE_DIR / "data" / "api_keys.json"
    if key_path.exists():
        try:
            for k, v in json.loads(key_path.read_text()).items():
                if isinstance(v, str) and v.strip() and not v.startswith("PASTE_"):
                    keys[k.upper()] = v.strip()
        except Exception:
            pass  # a malformed key file must never take the app down
    needed = {k for s in sources.KEYED_SOURCES.values() for k in s["keys"]}
    for name, value in os.environ.items():
        # Render creates blank variables for fields you leave empty — treat
        # those as absent, or the source switches on with no credentials.
        if name.upper() in needed and value.strip() and not value.startswith("PASTE_"):
            keys[name.upper()] = value.strip()
    return keys


def active_keyed_sources() -> tuple[list[str], dict[str, str]]:
    keys = load_api_keys()
    active = [name for name, spec in sources.KEYED_SOURCES.items()
              if all(k in keys for k in spec["keys"])]
    return active, keys


# ---------------------------------------------------------------------------
# Optional match explanations via Groq (off unless GROQ_API_KEY is set)
# ---------------------------------------------------------------------------

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Groq retired llama-3.1-8b-instant and llama-3.3-70b-versatile in June 2026.
# gpt-oss-20b is the current cheap, fast replacement. Override with GROQ_MODEL.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"


def get_secret(name: str) -> str | None:
    """Read a single secret from data/api_keys.json, then the environment."""
    path = BASE_DIR / "data" / "api_keys.json"
    if path.exists():
        try:
            value = json.loads(path.read_text()).get(name)
            if isinstance(value, str) and value.strip() and not value.startswith("PASTE_"):
                return value.strip()
        except Exception:
            pass
    value = os.environ.get(name, "")
    return value.strip() or None


# ---------------------------------------------------------------------------
# Basic pages + meta
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/meta")
async def meta():
    sectors = sorted({d["sector"] for d in DIRECTORY})
    cities = sorted({d["city"] for d in DIRECTORY if d["city"]})
    active, keys = active_keyed_sources()
    keyed = []
    for name, spec in sources.KEYED_SOURCES.items():
        keyed.append({
            "id": name, "label": spec["label"], "active": name in active,
            "keys": spec["keys"],
            "missing": [k for k in spec["keys"] if k not in keys],
            "coverage": spec["coverage"], "cost": spec["cost"],
            "signup": spec["signup"], "note": spec["note"],
        })
    return {
        "boards_total": len(BOARDS),
        "boards_pk": sum(1 for b in BOARDS if b["pk"]),
        "aggregators": sorted(sources.AGGREGATOR_FETCHERS),
        "directory_total": len(DIRECTORY),
        "sectors": sectors,
        "cities": cities,
        "keyed_sources": keyed,
        "keyed_active": len(active),
        "restricted_sites": sources.RESTRICTED_SITES,
        "explain_enabled": bool(get_secret("GROQ_API_KEY")),
    }


@app.get("/api/health")
async def health():
    """Cheap liveness check — no external calls. Point your uptime pinger here."""
    return {"ok": True, "boards": len(BOARDS), "companies": len(DIRECTORY)}


@app.post("/api/explain")
async def explain(payload: dict):
    """Short, honest read on how a resume lines up with one job."""
    resume = (payload.get("resume_text") or "").strip()
    job = payload.get("job") or {}
    if not resume:
        return JSONResponse({"error": "Load your resume first."}, status_code=400)

    # The explanation is built from the matcher's own data. A Groq key only
    # improves the wording — without one the feature still answers, instead of
    # showing an error where the answer should be.
    api_key = get_secret("GROQ_API_KEY")
    if not api_key:
        return {"explanation": matching.local_explanation(resume, job),
                "source": "local"}

    matched = ", ".join(job.get("matched_skills") or []) or "none detected"
    job_text = (
        f"Title: {job.get('title')}\n"
        f"Company: {job.get('company')}\n"
        f"Location: {job.get('location')}\n"
        f"Skills the matcher found in both: {matched}\n"
        f"Description: {(job.get('summary') or '')[:1200]}"
    )

    prompt = (
        "You are helping a job seeker in Pakistan decide whether to apply.\n\n"
        f"THEIR RESUME:\n{resume[:3500]}\n\n"
        f"THE JOB:\n{job_text}\n\n"
        "Write exactly three short bullets, no preamble, no heading:\n"
        "• Fit: the strongest specific overlap, naming real skills\n"
        "• Gap: what the job wants that the resume does not show. If nothing "
        "meaningful is missing, say so plainly instead of inventing a gap.\n"
        "• Move: one concrete thing to emphasise in the application\n\n"
        "Be direct and specific. No flattery, no filler. Under 90 words total."
    )

    body = {
        "model": os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }

    try:
        async with sources.make_client() as client:
            r = await client.post(GROQ_URL, json=body,
                                  headers={"Authorization": f"Bearer {api_key}",
                                           "Content-Type": "application/json"},
                                  timeout=sources.KEYED_TIMEOUT)
            if r.status_code == 401:
                return {"explanation": matching.local_explanation(resume, job),
                        "source": "local",
                        "note": "Groq rejected the key — showing the local read."}
            if r.status_code == 404:
                return {"explanation": matching.local_explanation(resume, job),
                        "source": "local",
                        "note": f"Model '{body['model']}' is not available — "
                                "showing the local read."}
            if r.status_code == 429:
                return {"explanation": matching.local_explanation(resume, job),
                        "source": "local",
                        "note": "Groq rate limit hit — showing the local read."}
            r.raise_for_status()
            data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        if not text:
            return {"explanation": matching.local_explanation(resume, job),
                    "source": "local",
                    "note": "Groq returned nothing — showing the local read."}
        return {"explanation": text, "source": "groq", "model": body["model"]}
    except Exception as e:
        # Never leave the panel blank. A local answer beats an error message.
        return {"explanation": matching.local_explanation(resume, job),
                "source": "local",
                "note": f"Couldn't reach Groq ({type(e).__name__}) — "
                        "showing the local read."}


@app.get("/api/directory")
async def directory(q: str = "", sector: str = "", city: str = ""):
    q_low = q.strip().lower()
    rows = DIRECTORY
    if sector:
        rows = [d for d in rows if d["sector"] == sector]
    if city:
        rows = [d for d in rows if d["city"].lower() == city.lower()]
    if q_low:
        rows = [d for d in rows if q_low in d["name"].lower()
                or q_low in d["sector"].lower() or q_low in d["city"].lower()]
    return {"count": len(rows), "companies": rows}


# ---------------------------------------------------------------------------
# Resume upload / paste
# ---------------------------------------------------------------------------

@app.post("/api/resume")
async def parse_resume(file: UploadFile | None = File(default=None),
                       text: str | None = Form(default=None)):
    if file is not None:
        raw = await file.read()
        if len(raw) > MAX_UPLOAD:
            return JSONResponse({"error": "File is over 5 MB. Upload a smaller "
                                          "file or paste the text instead."},
                                status_code=413)
        resume_text = matching.extract_text(file.filename or "", raw)
        source_name = file.filename or "upload"
    else:
        resume_text = (text or "").strip()
        source_name = "pasted text"

    resume_text = resume_text.strip()
    if len(resume_text) < 40:
        return JSONResponse({"error": "Couldn't read enough text from that "
                                      "resume. If it's a scanned PDF, paste the "
                                      "text instead."}, status_code=422)

    skills = matching.extract_skills(resume_text)
    return {"text": resume_text, "words": len(resume_text.split()),
            "skills": skills, "source_name": source_name}


# ---------------------------------------------------------------------------
# Search (NDJSON stream: source statuses as they finish, then scored jobs)
# ---------------------------------------------------------------------------

def _passes_filters(job: dict, *, keywords: list[str], city: str,
                    work_type: str, posted_days: int | None) -> bool:
    if keywords:
        blob = " ".join([job["title"], job["company"],
                         " ".join(job.get("tags") or []),
                         (job.get("description") or "")[:1200]]).lower()
        if not any(k in blob for k in keywords):
            return False

    loc = job["location"].lower()
    is_remote = bool(job.get("remote")) or "remote" in loc or \
        any(w in loc for w in ("worldwide", "anywhere", "global"))

    if city:
        if not (city in loc or is_remote):
            return False

    if work_type == "remote" and not is_remote:
        return False
    if work_type == "onsite" and is_remote:
        return False
    if work_type == "hybrid":
        blob = (job["title"] + " " + loc + " " +
                (job.get("description") or "")[:300]).lower()
        if "hybrid" not in blob:
            return False

    if posted_days:
        posted = job.get("posted_at")
        if posted:
            try:
                dt = datetime.fromisoformat(posted)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < datetime.now(timezone.utc) - timedelta(days=posted_days):
                    return False
            except ValueError:
                pass  # unparseable → keep, flagged client-side as "date n/a"
    return True


@app.post("/api/search")
async def search(payload: dict):
    resume_text: str = (payload.get("resume_text") or "").strip()
    keywords = [k.strip().lower() for k in
                (payload.get("keywords") or "").split(",") if k.strip()]
    scope = payload.get("scope") or "pakistan"
    city = (payload.get("city") or "").strip().lower()
    work_type = payload.get("work_type") or "any"
    posted_days = payload.get("posted_within_days") or None
    min_match = int(payload.get("min_match") or 0)

    global _board_offset
    pool = [b for b in BOARDS if b["pk"]] if scope == "pakistan" else BOARDS
    if len(pool) > BOARDS_PER_SEARCH:
        start = _board_offset % len(pool)
        boards = (pool + pool)[start:start + BOARDS_PER_SEARCH]
        _board_offset = start + BOARDS_PER_SEARCH
    else:
        boards = pool
    agg_names = sorted(sources.AGGREGATOR_FETCHERS)
    keyed_names, api_keys = active_keyed_sources()
    total_sources = len(boards) + len(agg_names) + len(keyed_names)

    async def stream():
        yield json.dumps({"type": "begin", "total_sources": total_sources,
                          "boards": len(boards),
                          "aggregators": len(agg_names),
                          "keyed": len(keyed_names)}) + "\n"

        collected: list[dict] = []
        early_seen: set[tuple[str, str]] = set()
        src_ok = src_empty = src_failed = 0
        first_errors: list[str] = []
        sem = asyncio.Semaphore(CONCURRENCY)

        async with sources.make_client() as client:

            async def guarded_board(b):
                async with sem:
                    return await sources.run_board(client, b)

            async def guarded_agg(name):
                async with sem:
                    return await sources.run_aggregator(
                        client, name, payload.get("keywords") or "")

            async def guarded_keyed(name):
                async with sem:
                    return await sources.run_keyed(
                        client, name, api_keys,
                        payload.get("keywords") or "", scope)

            # Aggregators first: few, reliable, and they put real results on
            # the page while the slower boards are still being tried.
            tasks = [asyncio.create_task(guarded_agg(n)) for n in agg_names]
            tasks += [asyncio.create_task(guarded_keyed(n)) for n in keyed_names]
            tasks += [asyncio.create_task(guarded_board(b)) for b in boards]

            deadline = time.monotonic() + SEARCH_DEADLINE
            for finished in asyncio.as_completed(tasks, timeout=SEARCH_DEADLINE):
                try:
                    status, jobs = await finished
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    break
                except Exception as exc:
                    src_failed += 1
                    if len(first_errors) < 5:
                        first_errors.append(f"unexpected: {type(exc).__name__}")
                    continue
                if time.monotonic() > deadline:
                    break
                if scope == "pakistan" and status["kind"] in ("aggregator", "keyed"):
                    jobs = [j for j in jobs if sources.looks_pakistan_friendly(j)]
                    status = {**status, "count": len(jobs)}
                if not status.get("ok"):
                    src_failed += 1
                    if status.get("error") and len(first_errors) < 5:
                        first_errors.append(f"{status.get('label')}: {status['error']}")
                elif status.get("count"):
                    src_ok += 1
                else:
                    src_empty += 1

                collected.extend(jobs)

                # Send this source's usable jobs straight away so the page
                # fills while the search runs. No match scores here on purpose:
                # score_jobs weighs terms across the whole result set, so a
                # per-source score would be a different — and wrong — number.
                # The ranked list arrives in the "done" message and replaces
                # these.
                early = []
                for j in jobs:
                    if not _passes_filters(j, keywords=keywords, city=city,
                                           work_type=work_type,
                                           posted_days=posted_days):
                        continue
                    key = (j["title"].lower(), j["company"].lower())
                    if key in early_seen:
                        continue
                    early_seen.add(key)
                    out = dict(j)     # never mutate the cached original
                    out["summary"] = (out.pop("description", "") or "")[:300]
                    early.append(out)

                yield json.dumps({"type": "source", "status": status,
                                  "jobs": early[:60]}) + "\n"

        for t in tasks:
            if not t.done():
                t.cancel()

        # Fold in boards this run skipped but which are still cached, so the
        # rotating slice adds to the picture instead of replacing it.
        carried, carried_boards = sources.cached_board_jobs(boards)
        if scope == "pakistan":
            carried = [j for j in carried if sources.looks_pakistan_friendly(j)]
        collected.extend(carried)

        # Filter
        filtered = [j for j in collected
                    if _passes_filters(j, keywords=keywords, city=city,
                                       work_type=work_type,
                                       posted_days=posted_days)]

        # Dedupe (prefer ATS entries over aggregator copies)
        filtered.sort(key=lambda j: 0 if j["source_kind"] == "ats" else 1)
        seen: set[tuple[str, str]] = set()
        unique: list[dict] = []
        for j in filtered:
            key = (j["title"].lower(), j["company"].lower())
            if key not in seen:
                seen.add(key)
                unique.append(j)

        # Score
        if resume_text:
            unique = matching.score_jobs(resume_text, unique)
            if min_match:
                unique = [j for j in unique if (j.get("match_score") or 0) >= min_match]
            unique.sort(key=lambda j: (-(j.get("match_score") or 0),
                                       j.get("posted_at") or ""), )
        else:
            unique.sort(key=lambda j: j.get("posted_at") or "", reverse=True)

        # Build the payload as fresh dicts. These job objects are the very
        # ones held in the source cache, so popping "description" here would
        # strip it from the cache too — and the next search inside the cache
        # window would filter against a description that no longer exists,
        # quietly returning fewer jobs each time.
        payload_jobs = []
        for j in unique[:400]:
            out = dict(j)
            out["summary"] = (out.pop("description", "") or "")[:600]
            payload_jobs.append(out)

        yield json.dumps({"type": "done", "total_found": len(unique),
                          "total_collected": len(collected),
                          "sources_ok": src_ok,
                          "sources_empty": src_empty,
                          "sources_failed": src_failed,
                          "boards_from_cache": carried_boards,
                          "sample_errors": first_errors,
                          "jobs": payload_jobs}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# Board verification (streams every board's live status)
# ---------------------------------------------------------------------------

@app.get("/api/sources/verify")
async def verify_sources():
    async def stream():
        yield json.dumps({"type": "begin", "total": len(BOARDS)}) + "\n"
        sem = asyncio.Semaphore(CONCURRENCY)
        async with sources.make_client() as client:
            async def guarded(b):
                async with sem:
                    return await sources.run_board(client, b)
            tasks = [asyncio.create_task(guarded(b)) for b in BOARDS]
            ok = 0
            for finished in asyncio.as_completed(tasks):
                status, _jobs = await finished
                ok += 1 if status["ok"] else 0
                yield json.dumps({"type": "source", "status": status}) + "\n"
        yield json.dumps({"type": "done", "alive": ok,
                          "total": len(BOARDS)}) + "\n"
    return StreamingResponse(stream(), media_type="application/x-ndjson")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
