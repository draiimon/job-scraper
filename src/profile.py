from __future__ import annotations
import json
from pathlib import Path
from .config import settings
from .jobs import SKILLS

def profile() -> dict:
    path=Path(settings().profile_path)
    if path.exists(): return json.loads(path.read_text(encoding='utf-8'))
    return {'skills':[], 'projects':[], 'summary':''}

def relevant_facts(description: str, resume_text: str | None = None) -> tuple[list[str], list[dict]]:
    data=profile(); text=description.lower()
    if resume_text:
        resume_lower=resume_text.lower()
        skills=[skill.upper() if skill != 'ci/cd' else 'CI/CD' for skill in SKILLS if skill in text and skill in resume_lower]
        return skills[:8],[]
    skills=[x for x in data.get('skills',[]) if x.lower() in text]
    projects=[x for x in data.get('projects',[]) if any(k.lower() in text for k in x.get('skills',[]))]
    return skills[:8], projects[:3]
