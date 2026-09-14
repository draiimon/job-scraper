from __future__ import annotations
import re
import json
from pathlib import Path
from .models import Job
from .jobs import SKILLS
from .profile import relevant_facts
from .ai import gemini
from .resumes import prompt_resume_context, resume_evidence

def eligible_for_email(job: Job, minimum: int) -> tuple[bool,str]:
    if job.score < minimum: return False,'Score below auto-application threshold'
    if not job.application_email: return False,'No published application email'
    if job.warnings: return False,'Posting has seniority or experience warnings'
    return True,'Eligible: published destination, high score, no red flags'
def cover_letter(job: Job, resume_text: str | None = None) -> str:
    text=(job.title+' '+job.description).lower()
    skills,projects=relevant_facts(text, resume_text)
    detected=[skill.upper() if skill != 'ci/cd' else 'CI/CD' for skill in SKILLS if skill in text]
    evidence=resume_evidence(text, resume_text)
    if resume_text and skills:
        skills_sentence=f"My resume shows hands-on work with {', '.join(skills)}."
    elif detected:
        skills_sentence=f"The role highlights technologies such as {', '.join(detected)}; my application is based on hands-on project work."
    else:
        skills_sentence='My application is based on hands-on project work with cloud infrastructure, Docker, Terraform, Linux, and CI/CD.'
    project_text='; '.join(f"{x['name']}: {x['description']}" for x in projects[:2])
    evidence_text=' '.join(evidence[:3])
    if evidence_text:
        project_text=(project_text+'; ' if project_text else '')+evidence_text
    return f'''Dear Hiring Team,

I am applying for the {job.title} role at {job.company}. I am interested in building and supporting reliable cloud and infrastructure systems, and this entry-level opportunity aligns with my hands-on project work.

{skills_sentence} {('Relevant work includes '+project_text+'.') if project_text else ''}

I would welcome the opportunity to discuss how my practical learning and project experience can support your team. Thank you for your consideration.

Sincerely,
[Your Name]
'''
async def revised_cover_letter(job: Job, resume_text: str | None = None) -> str:
    draft=cover_letter(job, resume_text)
    prompt=("Revise this cover letter only for clarity and natural professional tone. Preserve every factual claim; do not add facts, metrics, certifications, or experience. Return only the letter.\n"
            f"Role: {job.title}\nCompany: {job.company}\nRelevant requirements: {job.description[:3000]}\nDraft:\n{draft}")
    prompt += f"\nResume source of truth:\n{prompt_resume_context(resume_text)}"
    revision=await gemini().revise(prompt)
    return revision if revision and valid_revision(revision) else draft
async def generated_letter(job: Job, use_ai: bool=True, regenerate: bool=False, resume_text: str | None = None) -> tuple[str,str]:
    """Cache the verified final letter on the job; Gemini is an optional editor."""
    cached=(job.raw_metadata or {}).get('cover_letter')
    if cached and not regenerate: return cached,(job.raw_metadata or {}).get('cover_letter_mode','TEMPLATE')
    draft=cover_letter(job, resume_text)
    final=await revised_cover_letter(job, resume_text) if use_ai else draft
    mode='GEMINI' if use_ai and final!=draft else 'TEMPLATE'
    return final,mode
def valid_revision(letter: str) -> bool:
    """Reject common fabricated-credential claims; deterministic draft is always safe fallback."""
    lowered=letter.lower()
    prohibited=(r'\b\d+\+?\s+years?\b',r'\bcertified\b',r'\bcertification\b',r'\bsalary history\b',r'\bsecurity clearance\b')
    return 300 <= len(letter) <= 5000 and not any(re.search(pattern,lowered) for pattern in prohibited)
def write_package(job: Job, root: Path=Path('data/applications'), letter: str | None=None, resume_bytes: bytes | None=None, resume_filename: str | None=None) -> Path:
    company=re.sub(r'[^a-z0-9]+','-',job.company.lower()).strip('-'); role=re.sub(r'[^a-z0-9]+','-',job.title.lower()).strip('-')
    path=root/company/role; path.mkdir(parents=True,exist_ok=True)
    (path/'description.txt').write_text(job.description,encoding='utf-8')
    (path/'cover-letter.md').write_text(letter or cover_letter(job),encoding='utf-8')
    (path/'job.json').write_text(json.dumps({'job_url':job.url,'title':job.title,'company':job.company,'score':job.score},indent=2),encoding='utf-8')
    if resume_bytes:
        (path/(resume_filename or 'resume.pdf')).write_bytes(resume_bytes)
    return path
