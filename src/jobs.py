from __future__ import annotations
import hashlib, re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

@dataclass
class NormalizedJob:
    source: str; title: str; company: str; location: str; description: str; url: str
    source_job_id: str | None = None; application_url: str | None = None; application_email: str | None = None
    work_setup: str | None = None; salary: str | None = None; date_posted: datetime | None = None
    employment_type: str | None = None; seniority: str | None = None; skills: list[str] = field(default_factory=list); raw_metadata: dict = field(default_factory=dict)
    @property
    def fingerprint(self) -> str:
        # This is the broad identity bucket. Repository-level deduplication
        # adds source IDs, canonical URLs, and description similarity before
        # deciding whether two rows are truly the same vacancy.
        base = "|".join([clean(self.company), clean(self.title), identity_location(self.location)])
        return hashlib.sha256(base.encode()).hexdigest()

    @property
    def dedup_token(self) -> str:
        """In-source identity: keep distinct same-title vacancies intact."""
        metadata=self.raw_metadata or {}
        canonical=canonicalize_url(
            metadata.get("canonical_url") or metadata.get("direct_apply_url")
            or self.application_url or self.url
        )
        source_identity=clean(str(self.source_job_id or "")) or canonical or description_signature(self.description)
        seed="|".join((self.fingerprint, clean(self.source), source_identity))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

PRIMARY_ROLE_TERMS=("devops", "cloud", "infrastructure", "platform engineer", "systems engineer", "linux engineer", "noc", "site reliability", "sre", "cloud operations")
BROAD_ROLE_TERMS=(
    "it support", "technical support", "help desk", "service desk", "desktop support",
    "application support", "software support", "technical operations", "it operations",
    "it administrator", "system administrator", "server administrator", "network administrator",
    "network engineer", "network operations", "database support", "database administrator",
    "cloud administrator", "cloud support associate", "support engineer", "implementation engineer",
    "technical support engineer", "technical customer support", "salesforce developer",
    "software engineer", "software developer", "web developer", "backend developer",
    "frontend developer", "full stack", "python developer", "java developer", "php developer",
    "node.js developer", "quality assurance", "software tester", "software testing",
    "automation qa", "automation testing", "technical analyst", "it analyst", "systems analyst",
    "business systems analyst", "application analyst", "technical implementation",
    "technical consultant", "associate engineer", "graduate engineer", "it trainee",
    "technology trainee", "cybersecurity", "soc analyst", "junior security",
    "security operations", "data analyst", "data engineer", "machine learning",
    "computer science graduate", "information technology graduate", "cloud support", "it associate",
    "technology associate", "graduate technology", "entry level it",
)
EXCLUDED_ROLE_TERMS=("accounting", "finance", "human resources", "hr", "sales", "marketing", "recruiter", "recruitment", "virtual assistant", "generic va", "customer service", "real estate", "medical", "hospitality", "construction")
SENIOR_TITLE_RE=re.compile(r"\b(?:senior|sr\.?|lead|principal|staff|manager|director|head|architect)\b",re.I)
EXPERIENCE_5_PLUS_RE=re.compile(r"(?:\b(?:requires?|minimum|at least|must have|with)\s+(?:at least\s+)?(?:[5-9]|10)\+?\s*years?\b|\b(?:[5-9]|10)\+?\s*years?\s+(?:of\s+)?experience\b)",re.I)
EXPERIENCE_3_TO_4_RE=re.compile(r"(?:\b(?:requires?|minimum|at least|must have|with)\s+(?:at least\s+)?[34]\+?\s*years?\b|\b[34]\+?\s*years?\s+(?:of\s+)?experience\b)",re.I)
JUNIOR=("junior", "entry level", "entry-level", "associate", "graduate", "fresh graduate", "trainee", "0-1 years", "0-2 years", "1 year experience", "new graduate")
SKILLS={"aws":25,"docker":20,"terraform":20,"linux":15,"ubuntu":15,"ci/cd":15,"github actions":15,"kubernetes":10,"git":5,"bash":5,"nginx":5,"python":3}
PH_LOCATIONS=("philippines", "metro manila", "makati", "taguig", "bgc", "pasig", "mandaluyong", "quezon city", "manila", "muntinlupa", "alabang", "pasay", "parañaque", "paranaque", "cavite", "bacoor", "laguna", "clark", "pampanga", "cebu", "davao")
def clean(text: str) -> str: return re.sub(r"\s+", " ", text.lower()).strip()

def canonicalize_url(value: str | None) -> str:
    """Normalize tracking-only variation without inventing an apply URL."""
    if not value:
        return ""
    parsed=urlsplit(str(value).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query=urlencode(sorted(
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"source", "ref", "tracking"}
    ))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))

def description_signature(value: str) -> str:
    words=sorted(set(re.findall(r"[a-z0-9]{3,}", clean(value))))
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()

def identity_location(location: str) -> str:
    value=clean(location)
    for place in ('makati','taguig','bgc','pasig','mandaluyong','quezon city','manila','muntinlupa','alabang','pasay','parañaque','paranaque','cavite','bacoor','laguna','clark','pampanga','cebu','davao','philippines'):
        if place in value: return place
    return 'remote' if 'remote' in value else value

def _contains_phrase(text: str, phrase: str) -> bool:
    pattern=re.escape(phrase).replace(r"\ ",r"\s+")
    return bool(re.search(rf"(?<!\w){pattern}(?!\w)",text,re.I))

def _excluded_title(title: str) -> bool:
    # Word boundaries keep technical titles such as "Salesforce Developer"
    # from being misclassified as sales roles.
    for term in EXCLUDED_ROLE_TERMS:
        if not _contains_phrase(title,term):
            continue
        if term=='customer service' and any(x in title for x in ('technical','it ','software','application','engineer','support')):
            continue
        return True
    return False

def _has_ph_location_evidence(job: NormalizedJob, place: str) -> bool:
    if any(value in place for value in PH_LOCATIONS):
        return True
    if re.search(r"\b(?:ph|phl)\b",place,re.I):
        return True
    return bool((job.raw_metadata or {}).get('remote_ph_evidence'))

def is_ph_location(job: NormalizedJob) -> bool:
    place=clean(f"{job.location} {job.work_setup or ''}")
    ph_evidence=_has_ph_location_evidence(job,place)
    # A bare worldwide/US-only Remote result is never made PH-compatible just
    # because it was discovered by a Philippines search.  A provider must
    # return explicit PH evidence (or the adapter must have found it in the
    # listing itself) before remote work qualifies.
    return ph_evidence
def freshness(job: NormalizedJob, now: datetime | None=None) -> tuple[int,str|None,bool]:
    """Return score adjustment, human note, and whether a listing is in-window.

    The source timestamp is authoritative. Discovery time must never make an
    old listing look new, and the 90-day window is intentionally inclusive.
    """
    if not job.date_posted: return -20,'Posted date unavailable',False
    reference=now or datetime.now(timezone.utc)
    posted=job.date_posted
    if reference.tzinfo is None: reference=reference.replace(tzinfo=timezone.utc)
    if posted.tzinfo is None: posted=posted.replace(tzinfo=timezone.utc)
    age=max(0,(reference-posted).total_seconds()/86400)
    if age<=1: return 15,'Posted within 1 day',True
    if age<=7: return 8,'Posted within 7 days',True
    if age<=14: return 3,'Posted 8–14 days ago',True
    if age<=30: return 0,'Posted 15–30 days ago',True
    if age<=60: return -5,'Posted 31–60 days ago',True
    if age<=90: return -10,'Posted 61–90 days ago',True
    return -100,'Stale posting (over 90 days)',False
def is_active_listing(job: NormalizedJob) -> bool:
    # Public ATS listings returned by their current board endpoints are active;
    # malformed/non-HTTP links are never allowed through.
    return (job.application_url or job.url).startswith(('https://','http://'))
def evaluate(job: NormalizedJob, now: datetime | None=None) -> tuple[int,list[str],list[str],bool]:
    text=clean(f"{job.title} {job.description}"); title=clean(job.title); score=0; reasons=[]; warnings=[]
    excluded=_excluded_title(title)
    primary_in_title=any(x in title for x in PRIMARY_ROLE_TERMS)
    broad=any(x in title for x in BROAD_ROLE_TERMS)
    technical_context=any(x in title for x in ('engineer','developer','support','analyst','administrator','technician','trainee','graduate'))
    primary=primary_in_title or (technical_context and any(x in text for x in PRIMARY_ROLE_TERMS))
    relevant=(primary or broad) and not excluded
    if primary:
        score+=35; reasons.append("Relevant cloud/DevOps/infrastructure role")
    elif broad:
        score+=45; reasons.append("Relevant entry-level technology role")
    for skill, points in SKILLS.items():
        if skill in text: score+=points; reasons.append(skill.upper() if skill != "ci/cd" else "CI/CD")
    if any(x in text for x in JUNIOR): score+=20; reasons.append("Entry-level indicator")
    # Seniority is a title/requirement judgment.  Do not reject a junior role
    # merely because its description mentions a senior teammate or manager.
    senior_role=bool(SENIOR_TITLE_RE.search(title))
    if senior_role: score-=100; warnings.append("Senior-level role")
    requires_5_plus=bool(EXPERIENCE_5_PLUS_RE.search(text))
    requires_3_to_4=bool(EXPERIENCE_3_TO_4_RE.search(text))
    if requires_5_plus: score-=70; warnings.append("Requires 5+ years")
    elif requires_3_to_4: score-=40; warnings.append("Requires 3-4 years")
    if excluded:
        score-=100; warnings.append("Role is outside the computer/IT search scope")
    elif not relevant: score-=50; warnings.append("Role is outside technology disciplines")
    freshness_points,freshness_reason,fresh=freshness(job,now)
    score+=freshness_points
    if freshness_reason:
        (reasons if freshness_points>=0 else warnings).append(freshness_reason)
    experience_mismatch=requires_5_plus
    return max(0,min(100,score)), reasons, warnings, relevant and not senior_role and not experience_mismatch and score >= 0

def extract_skills(job: NormalizedJob) -> list[str]:
    text=clean(f"{job.title} {job.description}")
    return [x for x in SKILLS if x in text]
