"""
JobMatch AI — Pakistan-first resume→job matcher.

Run locally:   uvicorn main:app --reload
Deploy (Render): build `pip install -r requirements.txt`
                 start `uvicorn main:app --host 0.0.0.0 --port $PORT`
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import matching
import sources
from data_companies import ATS_BOARDS, PK_DIRECTORY

BASE_DIR = Path(__file__).parent
MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB
CONCURRENCY = 20

app = FastAPI(title="JobMatch AI", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


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
    for name in os.environ:
        if name.upper() in {k for s in sources.KEYED_SOURCES.values() for k in s["keys"]}:
            keys[name.upper()] = os.environ[name]
    return keys


def active_keyed_sources() -> tuple[list[str], dict[str, str]]:
    keys = load_api_keys()
    active = [name for name, spec in sources.KEYED_SOURCES.items()
              if all(k in keys for k in spec["keys"])]
    return active, keys


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
    }


@app.get("/api/health")
async def health():
    """Cheap liveness check — no external calls. Point your uptime pinger here."""
    return {"ok": True, "boards": len(BOARDS), "companies": len(DIRECTORY)}


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
                         " ".join(job.get("tags") or [])]).lower()
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

    boards = [b for b in BOARDS if b["pk"]] if scope == "pakistan" else BOARDS
    agg_names = sorted(sources.AGGREGATOR_FETCHERS)
    keyed_names, api_keys = active_keyed_sources()
    total_sources = len(boards) + len(agg_names) + len(keyed_names)

    async def stream():
        yield json.dumps({"type": "begin", "total_sources": total_sources,
                          "boards": len(boards),
                          "aggregators": len(agg_names),
                          "keyed": len(keyed_names)}) + "\n"

        collected: list[dict] = []
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

            tasks = [asyncio.create_task(guarded_board(b)) for b in boards]
            tasks += [asyncio.create_task(guarded_agg(n)) for n in agg_names]
            tasks += [asyncio.create_task(guarded_keyed(n)) for n in keyed_names]

            for finished in asyncio.as_completed(tasks):
                status, jobs = await finished
                if scope == "pakistan" and status["kind"] in ("aggregator", "keyed"):
                    jobs = [j for j in jobs if sources.looks_pakistan_friendly(j)]
                    status = {**status, "count": len(jobs)}
                collected.extend(jobs)
                yield json.dumps({"type": "source", "status": status}) + "\n"

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

        for j in unique:
            j.pop("description", None)  # keep the payload light

        yield json.dumps({"type": "done", "total_found": len(unique),
                          "jobs": unique[:400]}) + "\n"

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
