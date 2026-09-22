from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass
class NormalizedJob:
    """Provider-neutral job record used by every ingestion path."""

    source: str
    title: str
    company: str
    location: str
    description: str
    url: str
    source_job_id: str | None = None
    application_url: str | None = None
    application_email: str | None = None
    work_setup: str | None = None
    salary: str | None = None
    date_posted: datetime | None = None
    employment_type: str | None = None
    seniority: str | None = None
    skills: list[str] = field(default_factory=list)
    raw_metadata: dict = field(default_factory=dict)
    company_domain: str | None = None
    requirements: str = ""
    country: str | None = None
    remote_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    experience_min: float | None = None
    experience_max: float | None = None
    category: str | None = None
    expires_at: datetime | None = None
    is_active: bool = True

    @property
    def fingerprint(self) -> str:
        base = "|".join([clean(self.company), clean(self.title), identity_location(self.location)])
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    @property
    def dedup_token(self) -> str:
        metadata = self.raw_metadata or {}
        canonical = canonicalize_url(metadata.get("canonical_url") or metadata.get("direct_apply_url") or self.application_url or self.url)
        source_identity = clean(str(self.source_job_id or "")) or canonical or description_signature(self.description)
        seed = "|".join((self.fingerprint, clean(self.source), source_identity))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        text = "|".join((clean(self.title), clean(self.description), clean(self.requirements)))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def posted_at(self) -> datetime | None:
        return self.date_posted


TIER_1_ROLE_TERMS = (
    "junior software engineer", "software engineer i", "associate software engineer",
    "junior software developer", "junior developer", "junior backend developer",
    "backend developer", "junior full stack developer", "full stack developer",
    "junior web developer", "application developer", "software engineer", "software developer",
    "backend engineer", "full stack engineer", "web developer",
)
TIER_2_ROLE_TERMS = (
    "junior devops engineer", "devops associate", "devops engineer", "cloud support engineer",
    "cloud operations engineer", "junior cloud engineer", "associate cloud engineer",
    "infrastructure engineer", "infrastructure support engineer", "platform support engineer",
    "systems engineer", "junior systems engineer", "linux support engineer", "noc engineer",
    "application support engineer", "production support engineer", "cloud support", "devops",
    "cloud engineer", "platform engineer", "site reliability", "sre", "linux engineer",
)
TIER_3_ROLE_TERMS = (
    "junior qa engineer", "qa automation engineer", "software qa engineer", "automation tester",
    "junior sdet", "test automation engineer", "quality assurance", "software tester",
    "qa engineer", "qa tester", "automation qa", "test engineer",
)
TIER_4_ROLE_TERMS = (
    "junior ai engineer", "ai application developer", "generative ai developer", "llm developer",
    "rag developer", "ai integration engineer", "ai full stack developer", "junior python ai developer",
    "ai/ml engineer", "machine learning engineer", "ai engineer", "rag", "llm",
)
PRIMARY_ROLE_TERMS = TIER_2_ROLE_TERMS
BROAD_ROLE_TERMS = tuple(dict.fromkeys(TIER_1_ROLE_TERMS + TIER_2_ROLE_TERMS + TIER_3_ROLE_TERMS + TIER_4_ROLE_TERMS + (
    "it support", "technical support", "help desk", "service desk", "desktop support",
    "application support", "software support", "technical operations", "it operations",
    "system administrator", "server administrator", "network administrator", "network engineer",
    "network operations", "database support", "database administrator", "cloud administrator",
    "support engineer", "implementation engineer", "technical support engineer", "technical analyst",
    "salesforce developer",
    "it analyst", "systems analyst", "business systems analyst", "application analyst",
    "technical implementation", "technical consultant", "associate engineer", "graduate engineer",
    "it trainee", "technology trainee", "cybersecurity", "soc analyst", "junior security",
    "security operations", "data analyst", "data engineer", "computer science graduate",
    "information technology graduate", "cloud support", "it associate", "technology associate",
    "graduate technology", "entry level it",
)))
EXCLUDED_ROLE_TERMS = (
    "accounting", "finance", "human resources", "hr", "sales", "marketing", "recruiter",
    "recruitment", "virtual assistant", "generic va", "real estate", "medical", "hospitality",
    "construction",
)
SENIOR_TITLE_RE = re.compile(r"\b(?:senior|sr\.?|lead|principal|staff|manager|director|head)\b", re.I)
EXPERIENCE_5_PLUS_RE = re.compile(r"(?:\b(?:requires?|minimum|at least|must have|with)\s+(?:at least\s+)?(?:[5-9]|10)\+?\s*years?\b|\b(?:[5-9]|10)\+?\s*years?\s+(?:of\s+)?experience\b)", re.I)
EXPERIENCE_3_TO_4_RE = re.compile(r"(?:\b(?:requires?|minimum|at least|must have|with)\s+(?:at least\s+)?[34]\+?\s*years?\b|\b[34]\+?\s*years?\s+(?:of\s+)?experience\b)", re.I)
ENTRY_SIGNALS = (
    "fresh graduate", "fresh grads welcome", "fresh grad", "new graduate", "graduate program",
    "entry level", "entry-level", "junior", "associate", "trainee", "engineer i", "level 1",
    "0-1 years", "0-2 years", "no experience required", "training provided",
)
JUNIOR = ENTRY_SIGNALS + ("1 year experience", "1-2 years", "2 years experience")
TECHNOLOGY_POINTS = {
    "aws": 9, "docker": 8, "terraform": 8, "github actions": 4, "ci/cd": 4, "linux": 6,
    "ubuntu": 2, "bash": 2, "nginx": 2, "kubernetes": 3, "python": 3, "java": 3,
    "c++": 2, "c#": 2, "php": 3, "javascript": 3, "typescript": 3, "sql": 3,
    "react": 3, "next.js": 3, "node.js": 3, "express.js": 3, "django": 3, "laravel": 3,
    "postgresql": 3, "mysql": 3, "mongodb": 3, "firebase": 2, "dynamodb": 2, "pgvector": 3,
    "rest api": 2, "jwt": 2, "otp authentication": 2, "prisma": 2, "socket.io": 2,
    "selenium": 3, "playwright": 3, "rag": 4, "llm": 4, "gemini": 3, "groq": 3,
    "embeddings": 3, "nlp": 3,
}
SKILLS = TECHNOLOGY_POINTS
PH_LOCATIONS = (
    "philippines", "metro manila", "makati", "taguig", "bgc", "pasig", "mandaluyong",
    "quezon city", "manila", "muntinlupa", "alabang", "pasay", "parañaque", "paranaque",
    "cavite", "bacoor", "imus", "dasmarinas", "laguna", "clark", "pampanga", "cebu", "davao",
)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def canonicalize_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urlencode(sorted((key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"source", "ref", "tracking"}))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))


def description_signature(value: str) -> str:
    words = sorted(set(re.findall(r"[a-z0-9]{3,}", clean(value))))
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


def identity_location(location: str) -> str:
    value = clean(location)
    for place in PH_LOCATIONS:
        if place in value:
            return place
    return "remote" if "remote" in value else value


def _contains_phrase(text: str, phrase: str) -> bool:
    pattern = re.escape(phrase).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){pattern}(?!\w)", text, re.I))


def _excluded_title(title: str) -> bool:
    for term in EXCLUDED_ROLE_TERMS:
        if not _contains_phrase(title, term):
            continue
        if term == "hr" and any(value in title for value in ("software", "hardware", "technical")):
            continue
        return True
    return False


def _has_ph_location_evidence(job: NormalizedJob, place: str) -> bool:
    if any(value in place for value in PH_LOCATIONS) or re.search(r"\b(?:ph|phl)\b", place, re.I):
        return True
    if clean(job.country) in {"ph", "phl", "philippines"}:
        return True
    return bool((job.raw_metadata or {}).get("remote_ph_evidence"))


def is_ph_location(job: NormalizedJob) -> bool:
    place = clean(f"{job.location} {job.work_setup or ''} {job.remote_type or ''}")
    return _has_ph_location_evidence(job, place)


def extract_experience_years(text: str) -> tuple[float | None, float | None]:
    value = clean(text)
    ranges = re.findall(r"\b(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*years?\b", value)
    if ranges:
        return float(ranges[0][0]), float(ranges[0][1])
    plus = re.search(r"\b(\d+(?:\.\d+)?)\s*\+\s*years?\b", value)
    if plus:
        return float(plus.group(1)), None
    single = re.search(r"\b(?:at least|minimum|requires?|with)\s*(\d+(?:\.\d+)?)\s*years?\b", value)
    if single:
        return float(single.group(1)), None
    return None, None


def detect_remote_type(text: str, location: str = "") -> str | None:
    value = clean(f"{text} {location}")
    if "hybrid" in value:
        return "HYBRID"
    if any(term in value for term in ("remote", "work from home", "wfh")):
        return "REMOTE"
    if any(term in value for term in ("onsite", "on-site", "office-based")):
        return "ONSITE"
    return None


def enrich_canonical(job: NormalizedJob) -> NormalizedJob:
    """Fill deterministic canonical fields that providers often omit."""
    combined = f"{job.title} {job.description} {job.requirements}"
    if not job.remote_type:
        job.remote_type = detect_remote_type(combined, job.location)
    if not job.work_setup:
        job.work_setup = job.remote_type
    parsed_min, parsed_max = extract_experience_years(combined)
    if job.experience_min is None:
        job.experience_min = parsed_min
    if job.experience_max is None:
        job.experience_max = parsed_max
    title = clean(job.title)
    if not job.seniority:
        if SENIOR_TITLE_RE.search(title) or ("architect" in title and not any(_contains_phrase(title, signal) for signal in ENTRY_SIGNALS)):
            job.seniority = "SENIOR"
        elif any(_contains_phrase(title, signal) for signal in ENTRY_SIGNALS):
            job.seniority = "ENTRY_LEVEL"
    if not job.category:
        if any(_contains_phrase(title, term) for term in TIER_1_ROLE_TERMS):
            job.category = "SOFTWARE_ENGINEERING"
        elif any(_contains_phrase(title, term) for term in TIER_2_ROLE_TERMS):
            job.category = "CLOUD_OPERATIONS_DEVOPS"
        elif any(_contains_phrase(title, term) for term in TIER_3_ROLE_TERMS):
            job.category = "QA_AUTOMATION"
        elif any(_contains_phrase(title, term) for term in TIER_4_ROLE_TERMS):
            job.category = "AI_ENGINEERING"
        elif any(_contains_phrase(title, term) for term in BROAD_ROLE_TERMS):
            job.category = "TECHNOLOGY"
    if not job.country and _has_ph_location_evidence(job, clean(job.location)):
        job.country = "Philippines"
    return job


def recency_label(job: NormalizedJob, now: datetime | None = None) -> str:
    if not job.date_posted:
        return "OLDER"
    reference = now or datetime.now(timezone.utc)
    posted = job.date_posted.replace(tzinfo=timezone.utc) if job.date_posted.tzinfo is None else job.date_posted
    age_hours = max(0.0, (reference - posted).total_seconds() / 3600)
    if age_hours < 1:
        return "JUST POSTED"
    if age_hours < 6:
        return "< 6 HOURS"
    if age_hours < 24:
        return "TODAY"
    if age_hours < 72:
        return "1-3 DAYS"
    if age_hours < 168:
        return "4-7 DAYS"
    return "OLDER"


def freshness(job: NormalizedJob, now: datetime | None = None) -> tuple[int, str | None, bool]:
    if not job.date_posted:
        return -20, "Posted date unavailable", False
    reference = now or datetime.now(timezone.utc)
    posted = job.date_posted
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    age = max(0, (reference - posted).total_seconds() / 86400)
    if age <= 1:
        return 10, "Posted within 1 day", True
    if age <= 7:
        return 7, "Posted within 7 days", True
    if age <= 14:
        return 4, "Posted 8-14 days ago", True
    if age <= 30:
        return 2, "Posted 15-30 days ago", True
    if age <= 60:
        return -5, "Posted 31-60 days ago", True
    if age <= 90:
        return -10, "Posted 61-90 days ago", True
    return -100, "Stale posting (over 90 days)", False


def is_active_listing(job: NormalizedJob) -> bool:
    if job.is_active is False or (job.raw_metadata or {}).get("is_active") is False:
        return False
    return (job.application_url or job.url).startswith(("https://", "http://"))


def _role_points(title: str, text: str) -> tuple[int, str | None, bool]:
    for terms, points, label in (
        (TIER_1_ROLE_TERMS, 20, "Tier 1 software role"),
        (TIER_2_ROLE_TERMS, 20, "Tier 2 cloud/operations role"),
        (TIER_3_ROLE_TERMS, 16, "Tier 3 QA/automation role"),
        (TIER_4_ROLE_TERMS, 16, "Tier 4 AI role"),
    ):
        if any(_contains_phrase(title, term) for term in terms):
            return points, label, True
    if any(_contains_phrase(title, term) for term in BROAD_ROLE_TERMS):
        return 25, "Related technology role", True
    technical_context = any(term in title for term in ("engineer", "developer", "support", "analyst", "administrator", "technician", "trainee", "graduate"))
    if technical_context and any(term in text for term in BROAD_ROLE_TERMS):
        return 8, "Technical role with relevant description", True
    return 0, None, False


def evaluate(job: NormalizedJob, now: datetime | None = None) -> tuple[int, list[str], list[str], bool]:
    text = clean(f"{job.title} {job.description} {job.requirements}")
    title = clean(job.title)
    score = 0
    reasons: list[str] = []
    warnings: list[str] = []
    excluded = _excluded_title(title)
    role_points, role_reason, relevant = _role_points(title, text)
    score += role_points
    if role_reason:
        reasons.append(role_reason)
    matched_tech = [skill for skill in TECHNOLOGY_POINTS if _contains_phrase(text, skill)]
    score += min(25, sum(TECHNOLOGY_POINTS[skill] for skill in matched_tech))
    reasons.extend(skill.upper() if skill != "ci/cd" else "CI/CD" for skill in matched_tech[:8])
    title_entry = any(_contains_phrase(title, signal) for signal in ENTRY_SIGNALS)
    description_entry = any(_contains_phrase(text, signal) for signal in ENTRY_SIGNALS)
    entry_points = (20 if title_entry else 0) + (10 if description_entry else 0)
    score += entry_points
    if entry_points:
        reasons.append("Entry-level indicator")
    if is_ph_location(job):
        score += 10
        reasons.append("Philippines-compatible location")
        if detect_remote_type(text, job.location) == "REMOTE":
            reasons.append("Remote Philippines")
    experience_min, _experience_max = extract_experience_years(text)
    if experience_min is not None and experience_min >= 5:
        score -= 50
        warnings.append("Requires 5+ years")
    elif experience_min is not None and experience_min >= 3:
        score -= 20
        warnings.append("Requires 3-4 years")
    elif EXPERIENCE_5_PLUS_RE.search(text):
        score -= 50
        warnings.append("Requires 5+ years")
    elif EXPERIENCE_3_TO_4_RE.search(text):
        score -= 20
        warnings.append("Requires 3-4 years")
    senior_role = bool(SENIOR_TITLE_RE.search(title)) or ("architect" in title and not any(_contains_phrase(title, signal) for signal in ENTRY_SIGNALS))
    if senior_role:
        score -= 60
        warnings.append("Senior-level role")
    if excluded:
        score -= 70
        warnings.append("Role is outside the computer/IT search scope")
        relevant = False
    elif not relevant:
        score -= 30
        warnings.append("Role is outside technology disciplines")
    if any(term in text for term in ("computer science", "information technology", "bachelor", "bs degree", "college graduate")):
        score += 5
        reasons.append("Education-compatible")
    freshness_points, freshness_reason, fresh = freshness(job, now)
    score += freshness_points
    if freshness_reason:
        (reasons if freshness_points >= 0 else warnings).append(freshness_reason)
    profile_eligible = relevant and not senior_role and not bool(EXPERIENCE_5_PLUS_RE.search(text)) and score >= 0
    return max(0, min(100, score)), reasons, warnings, profile_eligible and fresh


def extract_skills(job: NormalizedJob) -> list[str]:
    text = clean(f"{job.title} {job.description} {job.requirements}")
    return [skill for skill in TECHNOLOGY_POINTS if _contains_phrase(text, skill)]
