from __future__ import annotations
import re
import json
import re
from pathlib import Path
from .models import Job
from .profile import relevant_facts
from .ai import gemini

def eligible_for_email(job: Job, minimum: int) -> tuple[bool,str]:
    if job.score < minimum: return False,'Score below auto-application threshold'
    if not job.application_email: return False,'No published application email'
    if job.warnings: return False,'Posting has seniority or experience warnings'
    return True,'Eligible: published destination, high score, no red flags'
def cover_letter(job: Job) -> str:
    skills,projects=relevant_facts(job.title+' '+job.description)
    project_text='; '.join(f"{x['name']}: {x['description']}" for x in projects[:2])
    return f'''Dear Hiring Team,

I am applying for the {job.title} role at {job.company}. I am interested in building and supporting reliable cloud and infrastructure systems, and this entry-level opportunity aligns with my hands-on project work.

My relevant skills include {', '.join(skills) if skills else 'cloud infrastructure, Docker, Terraform, Linux, and CI/CD'}. {('Relevant work includes '+project_text+'.') if project_text else ''}

I would welcome the opportunity to discuss how my practical learning and project experience can support your team. Thank you for your consideration.

Sincerely,
[Your Name]
'''
async def revised_cover_letter(job: Job) -> str:
    draft=cover_letter(job)
    prompt=("Revise this cover letter only for clarity and natural professional tone. Preserve every factual claim; do not add facts, metrics, certifications, or experience. Return only the letter.\n"
            f"Role: {job.title}\nCompany: {job.company}\nRelevant requirements: {job.description[:3000]}\nDraft:\n{draft}")
    revision=await gemini().revise(prompt)
    return revision if revision and valid_revision(revision) else draft
async def generated_letter(job: Job, use_ai: bool=True, regenerate: bool=False) -> tuple[str,str]:
    """Cache the verified final letter on the job; Gemini is an optional editor."""
    cached=(job.raw_metadata or {}).get('cover_letter')
    if cached and not regenerate: return cached,(job.raw_metadata or {}).get('cover_letter_mode','TEMPLATE')
    draft=cover_letter(job)
    final=await revised_cover_letter(job) if use_ai else draft
    mode='GEMINI' if use_ai and final!=draft else 'TEMPLATE'
    return final,mode
def valid_revision(letter: str) -> bool:
    """Reject common fabricated-credential claims; deterministic draft is always safe fallback."""
    lowered=letter.lower()
    prohibited=(r'\b\d+\+?\s+years?\b',r'\bcertified\b',r'\bcertification\b',r'\bsalary history\b',r'\bsecurity clearance\b')
    return 300 <= len(letter) <= 5000 and not any(re.search(pattern,lowered) for pattern in prohibited)
def write_package(job: Job, root: Path=Path('data/applications'), letter: str | None=None) -> Path:
    company=re.sub(r'[^a-z0-9]+','-',job.company.lower()).strip('-'); role=re.sub(r'[^a-z0-9]+','-',job.title.lower()).strip('-')
    path=root/company/role; path.mkdir(parents=True,exist_ok=True)
    (path/'description.txt').write_text(job.description,encoding='utf-8')
    (path/'cover-letter.md').write_text(letter or cover_letter(job),encoding='utf-8')
    (path/'job.json').write_text(json.dumps({'job_url':job.url,'title':job.title,'company':job.company,'score':job.score},indent=2),encoding='utf-8')
    return path
