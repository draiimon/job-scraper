from __future__ import annotations
import hashlib, re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

@dataclass
class NormalizedJob:
    source: str; title: str; company: str; location: str; description: str; url: str
    source_job_id: str | None = None; application_url: str | None = None; application_email: str | None = None
    work_setup: str | None = None; salary: str | None = None; date_posted: datetime | None = None
    employment_type: str | None = None; seniority: str | None = None; skills: list[str] = field(default_factory=list); raw_metadata: dict = field(default_factory=dict)
    @property
    def fingerprint(self) -> str:
        # Source identifiers differ across ATS, alerts, and aggregators; identity must not.
        base = "|".join([clean(self.company), clean(self.title), identity_location(self.location)])
        return hashlib.sha256(base.encode()).hexdigest()

PRIMARY_ROLE_TERMS=("devops", "cloud", "infrastructure", "platform engineer", "systems engineer", "linux engineer", "noc", "site reliability", "cloud operations")
BROAD_ROLE_TERMS=("it support", "technical support", "help desk", "service desk", "desktop support", "application support", "software support", "technical operations", "it operations", "it administrator", "system administrator", "network administrator", "network engineer", "database support", "database administrator", "software engineer", "software developer", "web developer", "backend developer", "frontend developer", "full stack", "quality assurance", "software testing", "automation testing", "technical analyst", "it analyst", "systems analyst", "business systems analyst", "application analyst", "technical implementation", "technical consultant", "associate engineer", "graduate engineer", "it trainee", "technology trainee", "cybersecurity", "soc analyst", "security operations", "data analyst", "data engineer", "machine learning", "computer science graduate", "information technology graduate")
NEGATIVE=("senior", " sr", "lead", "principal", "staff engineer", "manager", "director", "head of", "architect")
JUNIOR=("junior", "entry level", "entry-level", "associate", "graduate", "fresh graduate", "trainee", "0-1 years", "0-2 years", "1 year experience", "new graduate")
SKILLS={"aws":25,"docker":20,"terraform":20,"linux":15,"ubuntu":15,"ci/cd":15,"github actions":15,"kubernetes":10,"git":5,"bash":5,"nginx":5,"python":3}
PH_LOCATIONS=("philippines", "metro manila", "makati", "taguig", "bgc", "pasig", "quezon city", "manila", "alabang", "cavite", "bacoor", "remote")
def clean(text: str) -> str: return re.sub(r"\s+", " ", text.lower()).strip()
def identity_location(location: str) -> str:
    value=clean(location)
    for place in ('makati','taguig','bgc','pasig','quezon city','manila','alabang','cavite','bacoor','philippines'):
        if place in value: return place
    return 'remote' if 'remote' in value else value
def is_ph_location(job: NormalizedJob) -> bool:
    place=clean(f"{job.location} {job.work_setup or ''}")
    return any(x in place for x in PH_LOCATIONS) and not ("remote" in place and "philippines" not in place and "ph" not in place)
def evaluate(job: NormalizedJob, now: datetime | None=None) -> tuple[int,list[str],list[str],bool]:
    text=clean(f"{job.title} {job.description}"); title=clean(job.title); score=0; reasons=[]; warnings=[]
    primary_in_title=any(x in title for x in PRIMARY_ROLE_TERMS)
    broad=any(x in title for x in BROAD_ROLE_TERMS)
    technical_context=any(x in title for x in ('engineer','developer','support','analyst','administrator','technician','trainee','graduate'))
    primary=primary_in_title or (technical_context and any(x in text for x in PRIMARY_ROLE_TERMS))
    relevant=primary or broad
    if primary:
        score+=35; reasons.append("Relevant cloud/DevOps/infrastructure role")
    elif broad:
        score+=45; reasons.append("Relevant entry-level technology role")
    for skill, points in SKILLS.items():
        if skill in text: score+=points; reasons.append(skill.upper() if skill != "ci/cd" else "CI/CD")
    if any(x in text for x in JUNIOR): score+=20; reasons.append("Entry-level indicator")
    senior_role=any(x in title for x in NEGATIVE) or bool(re.search(r'\b(?:senior|sr\.?|lead|principal|staff)\s+(?:\w+\s+){0,2}(?:engineer|developer|administrator|analyst)\b',text))
    if senior_role: score-=100; warnings.append("Senior-level role")
    if re.search(r"\b(?:5|6|7|8|9|10)\+?\s*years?", text): score-=70; warnings.append("Requires 5+ years")
    elif re.search(r"\b[34]\+?\s*years?", text): score-=40; warnings.append("Requires 3-4 years")
    if not relevant: score-=50; warnings.append("Role is outside technology disciplines")
    current=now or datetime.now(timezone.utc)
    if job.date_posted:
        age=(current-job.date_posted).total_seconds()/86400
        if age<=1: score+=15; reasons.append("Posted within 24 hours")
        elif age<=3: score+=10; reasons.append("Posted within 3 days")
    return max(0,min(100,score)), reasons, warnings, relevant and not senior_role and score >= 0

def extract_skills(job: NormalizedJob) -> list[str]:
    text=clean(f"{job.title} {job.description}")
    return [x for x in SKILLS if x in text]
