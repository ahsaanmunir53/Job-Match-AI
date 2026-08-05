"""
Resume ↔ job matching.

WHY TF-IDF AND NOT SENTENCE EMBEDDINGS
--------------------------------------
The obvious choice looks like sentence-transformers. Three reasons this uses
TF-IDF instead, and they are worth being able to defend:

  1. Weight. sentence-transformers pulls in torch — roughly 500MB+ resident.
     Free hosting gives 512MB. It would OOM before serving a request.

  2. Interpretability. Cosine over TF-IDF lets you point at the exact terms that
     drove the score. An embedding gives you 0.82 and no explanation. For a tool
     whose whole job is "why does this job fit me", that matters more than a
     marginally better number.

  3. Fidelity to the real process. Applicant tracking systems screen on keyword
     overlap. Modelling the thing that actually filters you is more useful than
     modelling semantic similarity the recruiter never computes.

The score is deliberately two-part: a lexical similarity term, and an explicit
skill-overlap term computed against a curated taxonomy. The second is what
produces the "you have / you're missing" breakdown.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ── skill taxonomy ────────────────────────────────────────────────────────
# Canonical name -> patterns that mean the same thing. Written by hand because
# an automatic extractor produces noise like "experience" and "team".
SKILLS: Dict[str, List[str]] = {
    # languages
    "Python": [r"\bpython\b"],
    "JavaScript": [r"\bjavascript\b", r"\bjs\b(?!on)"],
    "TypeScript": [r"\btypescript\b", r"\bts\b"],
    "Java": [r"\bjava\b(?!script)"],
    "Go": [r"\bgolang\b", r"\bgo\b(?= developer| engineer| lang)"],
    "Ruby": [r"\bruby\b", r"\brails\b"],
    "C#": [r"\bc#\b", r"\bdotnet\b", r"\b\.net\b"],
    "PHP": [r"\bphp\b", r"\blaravel\b"],
    "SQL": [r"\bsql\b", r"\bpostgres", r"\bmysql\b"],
    "Bash": [r"\bbash\b", r"\bshell script"],
    # frontend
    "React": [r"\breact\b", r"\breact\.?js\b"],
    "Next.js": [r"\bnext\.?js\b"],
    "Vue": [r"\bvue\b", r"\bvue\.?js\b"],
    "Angular": [r"\bangular\b"],
    "Tailwind": [r"\btailwind\b"],
    # backend / frameworks
    "Django": [r"\bdjango\b"],
    "FastAPI": [r"\bfastapi\b"],
    "Flask": [r"\bflask\b"],
    "Node.js": [r"\bnode\.?js\b", r"\bexpress\.?js\b"],
    "NestJS": [r"\bnestjs\b"],
    "GraphQL": [r"\bgraphql\b"],
    "REST APIs": [r"\brest\b", r"\brestful\b", r"\bapi design\b"],
    "Microservices": [r"\bmicroservices?\b"],
    # cloud / infra
    "AWS": [r"\baws\b", r"\bamazon web services\b"],
    "Azure": [r"\bazure\b"],
    "GCP": [r"\bgcp\b", r"\bgoogle cloud\b"],
    "Kubernetes": [r"\bkubernetes\b", r"\bk8s\b", r"\beks\b", r"\baks\b", r"\bgke\b"],
    "Docker": [r"\bdocker\b", r"\bcontaineri[sz]"],
    "Terraform": [r"\bterraform\b", r"\biac\b", r"\binfrastructure as code\b"],
    "Helm": [r"\bhelm\b"],
    "CI/CD": [r"\bci/?cd\b", r"\bcontinuous (integration|deployment|delivery)\b",
              r"\bgithub actions\b", r"\bgitlab ci\b", r"\bjenkins\b"],
    "Linux": [r"\blinux\b", r"\bunix\b"],
    "Monitoring": [r"\bprometheus\b", r"\bgrafana\b", r"\bdatadog\b",
                   r"\bobservability\b", r"\bmonitoring\b"],
    "SRE": [r"\bsre\b", r"\bsite reliability\b", r"\bslo\b", r"\bsla\b"],
    # data / ML
    "Machine Learning": [r"\bmachine learning\b", r"\bml\b(?!ops)", r"\bscikit"],
    "Deep Learning": [r"\bdeep learning\b", r"\btensorflow\b", r"\bpytorch\b", r"\bkeras\b"],
    "LLM / GenAI": [r"\bllm\b", r"\bgenerative ai\b", r"\bgen ?ai\b", r"\bopenai\b",
                    r"\banthropic\b", r"\bclaude\b", r"\bgpt\b", r"\bprompt engineering\b"],
    "RAG": [r"\brag\b", r"\bretrieval augmented\b", r"\bvector (db|database|search)\b",
            r"\bembeddings?\b"],
    "MLOps": [r"\bmlops\b", r"\bmodel serving\b", r"\bmodel deployment\b"],
    "Data Engineering": [r"\betl\b", r"\bdata pipeline\b", r"\bairflow\b", r"\bspark\b"],
    "Pandas": [r"\bpandas\b", r"\bnumpy\b"],
    # databases / infra services
    "PostgreSQL": [r"\bpostgres(ql)?\b"],
    "MongoDB": [r"\bmongo(db)?\b"],
    "Redis": [r"\bredis\b"],
    "Kafka": [r"\bkafka\b", r"\bevent streaming\b"],
    "Elasticsearch": [r"\belasticsearch\b", r"\bopensearch\b"],
    # ways of working
    "Agile": [r"\bagile\b", r"\bscrum\b", r"\bkanban\b"],
    "Git": [r"\bgit\b", r"\bversion control\b"],
    "Testing": [r"\bunit test", r"\bpytest\b", r"\bjest\b", r"\btdd\b", r"\btest automation\b"],
    "Security": [r"\bsecurity\b", r"\biam\b", r"\boauth\b", r"\bpenetration test"],
}

_COMPILED = {name: [re.compile(p, re.I) for p in pats] for name, pats in SKILLS.items()}


def extract_skills(text: str) -> List[str]:
    """Which canonical skills appear in this text."""
    if not text:
        return []
    return sorted(
        name for name, pats in _COMPILED.items()
        if any(p.search(text) for p in pats)
    )


def _clean(t: str) -> str:
    t = re.sub(r"<[^>]+>", " ", t or "")      # feeds return HTML descriptions
    t = re.sub(r"[^a-zA-Z0-9+#./\s-]", " ", t)
    return re.sub(r"\s+", " ", t).lower().strip()


def score_jobs(resume: str, jobs: List[Dict]) -> List[Dict]:
    """
    Score every job against the resume.

    final = 0.45 * lexical similarity + 0.55 * skill coverage

    Skill coverage is weighted higher because a job asking for 8 skills you have
    7 of is a better match than one that merely uses similar prose.
    """
    if not jobs:
        return []

    resume_clean = _clean(resume)
    resume_skills = set(extract_skills(resume))

    docs = [resume_clean] + [_clean(f"{j.get('title','')} {j.get('description','')}") for j in jobs]

    try:
        vec = TfidfVectorizer(
            stop_words="english", ngram_range=(1, 2),
            min_df=1, max_features=20000, sublinear_tf=True,
        )
        matrix = vec.fit_transform(docs)
        lexical = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
    except ValueError:
        lexical = [0.0] * len(jobs)           # empty vocabulary

    out = []
    for i, job in enumerate(jobs):
        text = f"{job.get('title','')} {job.get('description','')}"
        job_skills = set(extract_skills(text))

        matched = sorted(job_skills & resume_skills)
        missing = sorted(job_skills - resume_skills)
        extra = sorted(resume_skills - job_skills)

        coverage = len(matched) / len(job_skills) if job_skills else 0.0
        lex = float(lexical[i])

        # lexical cosine on short docs rarely exceeds ~0.35, so rescale it into
        # a range where it can actually move the final number
        lex_scaled = min(1.0, lex * 2.6)
        final = 0.45 * lex_scaled + 0.55 * coverage

        out.append({
            **job,
            "score": round(final * 100, 1),
            "lexical": round(lex, 4),
            "coverage": round(coverage * 100, 1),
            "matched_skills": matched,
            "missing_skills": missing,
            "extra_skills": extra[:8],
            "verdict": (
                "strong" if final >= 0.6 else
                "good" if final >= 0.4 else
                "partial" if final >= 0.22 else "weak"
            ),
        })

    out.sort(key=lambda j: j["score"], reverse=True)
    return out


def resume_summary(resume: str) -> Dict:
    skills = extract_skills(resume)
    words = len(_clean(resume).split())
    return {
        "skills_found": skills,
        "skill_count": len(skills),
        "word_count": words,
        "warning": (
            "Resume looks short — paste the full text for better matching."
            if words < 120 else None
        ),
    }
