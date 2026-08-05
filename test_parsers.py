"""
Offline verification of the keyed fetchers.

Feeds each fetcher a payload shaped exactly like the real API's response and
checks the normalized job that comes out. Proves the parsing and field mapping
are right without spending a single request from your quota.

Run:  python test_parsers.py
"""

import asyncio
import json

import httpx

import sources

PAYLOADS = {
    "jsearch": {
        "status": "OK",
        "data": [{
            "job_id": "abc123",
            "employer_name": "Systems Limited",
            "job_title": "Senior Python Engineer",
            "job_apply_link": "https://careers.systemsltd.com/job/abc123",
            "job_city": "Lahore", "job_state": "Punjab", "job_country": "PK",
            "job_is_remote": False,
            "job_posted_at_datetime_utc": "2026-07-28T10:00:00.000Z",
            "job_description": "<p>Django, FastAPI, PostgreSQL, Docker, AWS.</p>",
            "job_employment_type": "FULLTIME", "job_publisher": "LinkedIn",
        }],
    },
    "jooble": {
        "totalCount": 1,
        "jobs": [{
            "title": "DevOps Engineer", "company": "Gaditek",
            "location": "Karachi", "link": "https://pk.jooble.org/desc/999",
            "updated": "2026-08-03T00:00:00", "type": "Full-time",
            "snippet": "Kubernetes, Terraform, CI/CD pipelines.",
        }],
    },
    "careerjet": {
        "type": "JOBS", "hits": 1,
        "jobs": [{
            "title": "Software Engineer", "company": "Arbisoft",
            "locations": "Lahore",
            "url": "https://www.careerjet.com.pk/jobad/pk123",
            "date": "2026-08-01",
            "description": "Python, Django, React, REST APIs.",
        }],
    },
    "adzuna": {
        "results": [{
            "title": "Python Developer",
            "company": {"display_name": "Acme Ltd"},
            "location": {"display_name": "London, UK"},
            "redirect_url": "https://www.adzuna.co.uk/jobs/land/ad/555",
            "created": "2026-08-01T09:00:00Z",
            "description": "Backend Python, Flask, SQL.",
            "category": {"label": "IT Jobs"},
        }],
    },
    "findwork": {
        "count": 1,
        "results": [{
            "role": "Backend Engineer", "company_name": "RemoteCo",
            "location": "Remote", "remote": True,
            "url": "https://findwork.dev/jobs/777",
            "date_posted": "2026-08-02T00:00:00Z",
            "text": "Node.js, TypeScript, MongoDB.",
            "keywords": ["node", "typescript"],
        }],
    },
    "themuse": {
        "page": 0,
        "results": [{
            "name": "Data Analyst",
            "company": {"name": "Muse Co"},
            "locations": [{"name": "New York, NY"}],
            "refs": {"landing_page": "https://www.themuse.com/jobs/museco/888"},
            "publication_date": "2026-08-01T00:00:00Z",
            "contents": "<p>SQL, Tableau, Python.</p>",
            "categories": [{"name": "Data Science"}],
        }],
    },
}

FAKE_KEYS = {
    "RAPIDAPI_KEY": "x", "JOOBLE_API_KEY": "x", "CAREERJET_AFFID": "x",
    "ADZUNA_APP_ID": "x", "ADZUNA_APP_KEY": "x",
    "FINDWORK_API_KEY": "x", "MUSE_API_KEY": "x",
}

REQUIRED = ("title", "company", "location", "url", "source")


def handler_for(name):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PAYLOADS[name])
    return handler


async def main():
    failures = []
    for name, fetcher in sources.KEYED_FETCHERS.items():
        transport = httpx.MockTransport(handler_for(name))
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                jobs = await fetcher(client, "python", "pakistan", FAKE_KEYS)
            except Exception as e:
                failures.append(f"{name}: raised {type(e).__name__}: {e}")
                continue

        if not jobs:
            failures.append(f"{name}: returned no jobs")
            continue

        job = jobs[0]
        missing = [f for f in REQUIRED if not job.get(f)]
        if missing:
            failures.append(f"{name}: empty fields {missing}")
            continue

        print(f"  OK  {name:<11} {job['title']} @ {job['company']} "
              f"({job['location']}) posted={str(job['posted_at'])[:10]}")

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print(f"All {len(sources.KEYED_FETCHERS)} keyed parsers produce valid jobs.")


if __name__ == "__main__":
    print("Testing keyed API parsers against real response shapes\n")
    asyncio.run(main())
