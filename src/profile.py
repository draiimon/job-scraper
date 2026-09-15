from __future__ import annotations
import json
from pathlib import Path
from .config import settings
from .jobs import SKILLS


PUBLIC_PROFILE_PATH = Path("config/candidate_public_profile.json")


def _profile_file(path: Path) -> dict:
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value,dict) else {}


def profile() -> dict:
    path=Path(settings().profile_path)
    # Public links are safe to ship with this single-user app; private resume
    # facts remain in the database or ignored profile file. Merge rather than
    # replace so an absent local file never drops the canonical public URLs.
    public=_profile_file(PUBLIC_PROFILE_PATH)
    private=_profile_file(path)
    merged={**public,**private}
    public_contact=public.get('contact') if isinstance(public.get('contact'),dict) else {}
    private_contact=private.get('contact') if isinstance(private.get('contact'),dict) else {}
    if public_contact or private_contact:
        merged['contact']={**public_contact,**private_contact}
    return merged or {'skills':[], 'projects':[], 'summary':''}

def relevant_facts(description: str, resume_text: str | None = None) -> tuple[list[str], list[dict]]:
    data=profile(); text=description.lower()
    if resume_text:
        resume_lower=resume_text.lower()
        skills=[skill.upper() if skill != 'ci/cd' else 'CI/CD' for skill in SKILLS if skill in text and skill in resume_lower]
        return skills[:8],[]
    skills=[x for x in data.get('skills',[]) if x.lower() in text]
    projects=[x for x in data.get('projects',[]) if any(k.lower() in text for k in x.get('skills',[]))]
    return skills[:8], projects[:3]
