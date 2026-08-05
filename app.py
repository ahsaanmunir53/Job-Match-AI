"""
JobMatch AI — find jobs that fit your resume, and draft the application.

What it automates: finding, filtering, ranking, and drafting.
What it deliberately does not automate: pressing submit.

That line is not timidity. Auto-submitting through LinkedIn or Indeed breaches
their User Agreement and puts your real account at risk, and mass generic
applications convert badly anyway. Everything up to the submit button is the
part worth saving time on.
"""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, UploadFile, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from extract import extract as extract_resume
from matcher import extract_skills, resume_summary, score_jobs
from resume_parser import ResumeParseError, parse_resume
from geo import COUNTRIES, REGIONS, apply_filters
from sources import fetch_jobs

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="JobMatch AI", docs_url="/api/docs", redoc_url=None)


class MatchIn(BaseModel):
    resume: str = Field(..., min_length=80, max_length=25000)
    query: Optional[str] = ""
    country: Optional[str] = ""
    city: Optional[str] = ""
    work_type: Optional[str] = "any"
    max_age_days: int = Field(0, ge=0, le=365)   # 0 = no limit
    limit: int = Field(30, ge=1, le=100)
    min_score: float = Field(0, ge=0, le=100)


class DraftIn(BaseModel):
    resume: str = Field(..., min_length=80, max_length=25000)
    job_title: str
    company: str
    job_description: str = ""
    matched_skills: List[str] = []
    missing_skills: List[str] = []
    tone: str = "professional"


DRAFT_SYSTEM = """You write short, specific job applications. You are writing AS the candidate.

Rules:
- 140-190 words. Anything longer does not get read.
- Open with something concrete about THIS role or company — never "I am writing to apply for".
- Name 2-3 specific things from their resume that map to what the job asks for. Use real details, never invent experience they did not list.
- If there is a notable gap, address it once, briefly, as something being closed — do not dwell or apologise.
- No buzzwords: passionate, dynamic, synergy, leverage, cutting-edge, fast-paced.
- No em dashes.
- End with one clear line about next steps.

Return ONLY the letter body. No subject line, no "Dear Hiring Manager", no sign-off block."""


def _explain_empty(d: dict) -> str:
    """Say why the result set is empty. An empty page with no reason is useless."""
    if d.get("after_keyword") == 0 and d.get("total_jobs"):
        return "No jobs matched that keyword. Try a broader term, or clear it."
    if d.get("after_work_type") == 0:
        return "No jobs matched that work type. Try 'Any'."
    if d.get("city"):
        return (f"Nothing found in {d['city']}. Free job feeds carry very few city-level "
                f"listings outside the US and EU — try clearing the city and using the country.")
    if d.get("country"):
        return (f"No listings based in {d['country']}, and none open worldwide after the other "
                f"filters. For roles physically in {d['country']}, local boards like Rozee.pk "
                f"and LinkedIn are still where those live. Try country 'Worldwide' to see remote "
                f"roles you can take from anywhere.")
    return "Nothing matched. Try widening the filters."


@app.get("/api/sources")
async def sources_status():
    """
    Which company boards answered and which did not.

    Use this to fix slugs: a board reporting HTTP 404 has the wrong slug in
    ats.py. Open that company's Careers page, read the URL, correct the entry.
    """
    from ats import COMPANIES, fetch_ats
    jobs, report = await fetch_ats(report=True)
    ok = [r for r in report if r["ok"]]
    return {
        "configured": len(COMPANIES),
        "responding": len(ok),
        "total_jobs": len(jobs),
        "working": sorted([f"{r['platform']}/{r['slug']} ({r['count']})" for r in ok]),
        "failing": sorted([f"{r['platform']}/{r['slug']} — {r.get('error')}"
                           for r in report if not r["ok"]]),
        "hint": "A failing board usually means the slug changed. Check the company's careers URL.",
    }


@app.get("/api/places")
def places():
    """Countries and regions the filter understands, for the dropdowns."""
    return {"regions": list(REGIONS.keys()), "countries": sorted(COUNTRIES.keys())}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "ai_enabled": bool(os.environ.get("GROQ_API_KEY", "").strip()),
        "sources": ["Arbeitnow", "RemoteOK", "Remotive", "Himalayas"],
    }


@app.get("/api/jobs")
async def jobs(q: str = "", country: str = "", city: str = "", work_type: str = "any", refresh: bool = False):
    """Raw feed, no scoring — useful for browsing before pasting a resume."""
    data = await fetch_jobs(force=refresh)
    found, _ = apply_filters(data["jobs"], q, country, city, work_type)
    return {
        "total_available": len(data["jobs"]),
        "matching_filter": len(found),
        "cached": data.get("cached", False),
        "per_source": data.get("per_source"),
        "jobs": found[:60],
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Pull the text out of an uploaded PDF / DOCX / TXT resume."""
    try:
        data = await file.read()
        result = extract_resume(file.filename or "", data)
    except ValueError as e:
        # These messages are written to be shown to the user as-is.
        return JSONResponse({"error": "extraction_failed", "detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"error": "extraction_failed",
             "detail": f"Could not read that file ({str(e)[:90]}). Try pasting the text."},
            status_code=400)

    result["skills"] = resume_summary(result["text"])
    return result


@app.post("/api/analyze")
async def analyze(body: dict):
    """Skills detected in a resume, before any matching."""
    resume = str(body.get("resume", ""))
    if len(resume) < 80:
        return JSONResponse(
            {"error": "too_short", "detail": "Paste at least a few lines of your resume."},
            status_code=400)
    return resume_summary(resume)


@app.post("/api/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    """Accept a PDF, DOCX or TXT resume and return its text plus detected skills."""
    try:
        data = await file.read()
        text, kind = parse_resume(file.filename or "", data)
    except ResumeParseError as exc:
        return JSONResponse({"error": "parse_failed", "detail": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse(
            {"error": "parse_failed", "detail": "That file could not be read."},
            status_code=400)

    return {"filename": file.filename, "type": kind, "text": text, **resume_summary(text)}


@app.post("/api/match")
async def match(body: MatchIn):
    data = await fetch_jobs()
    pool, diag = apply_filters(data["jobs"], body.query, body.country or "", body.city or "", body.work_type or "any")

    if body.max_age_days:
        before = len(pool)
        pool = [j for j in pool if j.get("age_days", 999) <= body.max_age_days]
        diag["after_age"] = len(pool)
        diag["removed_by_age"] = before - len(pool)

    if not pool:
        return {
            "jobs": [], "analyzed": len(data["jobs"]),
            "resume": resume_summary(body.resume),
            "diagnostics": diag,
            "note": _explain_empty(diag),
        }

    scored = score_jobs(body.resume, pool)
    if body.min_score:
        scored = [j for j in scored if j["score"] >= body.min_score]

    return {
        "fallback": data.get("fallback", False),
        "diagnostics": diag,
        "analyzed": len(pool),
        "returned": len(scored[:body.limit]),
        "resume": resume_summary(body.resume),
        "sources": data.get("per_source"),
        "jobs": scored[:body.limit],
    }


@app.post("/api/draft")
async def draft(body: DraftIn):
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return JSONResponse(
            {"error": "no_api_key",
             "detail": "Set GROQ_API_KEY to generate drafts. Matching works without it."},
            status_code=503)

    user = (
        f"JOB TITLE: {body.job_title}\n"
        f"COMPANY: {body.company}\n"
        f"THEY ASK FOR: {', '.join(body.matched_skills + body.missing_skills) or 'see description'}\n"
        f"CANDIDATE ALREADY HAS: {', '.join(body.matched_skills) or 'see resume'}\n"
        f"GAPS: {', '.join(body.missing_skills) or 'none significant'}\n\n"
        f"JOB DESCRIPTION:\n{body.job_description[:2500]}\n\n"
        f"CANDIDATE RESUME:\n{body.resume[:4000]}"
    )

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": os.environ.get("JOBMATCH_MODEL", "openai/gpt-oss-120b"),
                    "messages": [
                        {"role": "system", "content": DRAFT_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.6,
                    "max_tokens": 500,
                },
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as e:
        return JSONResponse(
            {"error": "assistant_error",
             "detail": f"Groq returned {e.response.status_code}. Check the API key."},
            status_code=503)
    except Exception as e:
        return JSONResponse({"error": "assistant_error", "detail": str(e)[:150]}, status_code=503)

    text = re.sub(r"—", "-", text)          # the prompt bans em dashes; enforce it
    return {"draft": text, "word_count": len(text.split())}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
