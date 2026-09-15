from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import gemini
from .jobs import SKILLS
from .models import Job
from .profile import profile
from .resumes import prompt_resume_context, resume_evidence


@dataclass(frozen=True)
class CandidateProfile:
    name: str = ""
    location: str = ""
    phone: str = ""
    email: str = ""
    github: str = ""
    portfolio: str = ""
    education: str = ""


SKILL_LABELS = {
    "aws": "AWS",
    "docker": "Docker",
    "terraform": "Terraform",
    "linux": "Linux",
    "ubuntu": "Ubuntu",
    "ci/cd": "CI/CD",
    "github actions": "GitHub Actions",
    "kubernetes": "Kubernetes",
    "git": "Git",
    "bash": "Bash",
    "nginx": "Nginx",
    "python": "Python",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "javascript": "JavaScript",
    "node.js": "Node.js",
    "react": "React",
    "rag": "RAG",
    "gemini": "Gemini",
    "pgvector": "pgvector",
}
REQUIREMENT_TERMS = (
    ("aws", "AWS"),
    ("ecs", "Amazon ECS"),
    ("lambda", "AWS Lambda"),
    ("s3", "Amazon S3"),
    ("docker", "Docker"),
    ("kubernetes", "Kubernetes"),
    ("terraform", "Terraform"),
    ("infrastructure as code", "Infrastructure as Code"),
    ("github actions", "GitHub Actions"),
    ("ci/cd", "CI/CD"),
    ("linux", "Linux"),
    ("python", "Python"),
    ("postgresql", "PostgreSQL"),
    ("node.js", "Node.js"),
    ("react", "React"),
    ("rag", "RAG"),
    ("llm", "LLM integration"),
    ("technical support", "technical support"),
)
PLACEHOLDER_PATTERN = re.compile(
    r"(?:\[(?:your name|name|company(?: name)?|job title|current date|date|hiring manager|recruiter|[^\]]+)\]"
    r"|<[^>\n]+>|\{\{[^}\n]+\}\})",
    re.IGNORECASE,
)
PROHIBITED_CLAIMS = (
    r"\b\d+\+?\s+years?\b",
    r"\bcertified\b",
    r"\bcertification\b",
    r"\bsalary history\b",
    r"\bsecurity clearance\b",
    r"\bextensive experience\b",
    r"\bdeep expertise\b",
    r"\bseasoned professional\b",
    r"\bproven track record\b",
)
CORPORATE_PHRASES = (
    "it fits the direction i have been building",
    "i value careful implementation",
    "practical exposure",
    "technical foundation",
    "i would welcome the chance",
    "contribute carefully",
    "steady follow-through",
    "professional trajectory",
    "methodical troubleshooting",
    "leverage",
    "align with",
    "drawn to",
    "proven ability",
    "extensive experience",
)
TECHNICAL_FACT_TERMS = (
    "aws", "azure", "gcp", "google cloud", "docker", "terraform", "kubernetes",
    "jenkins", "ansible", "github actions", "gitlab ci", "linux", "ubuntu",
    "python", "java", "javascript", "node.js", "react", "django", "laravel",
    "postgresql", "mysql", "mongodb", "nginx", "bash", "selenium", "playwright",
    "rag", "pgvector", "gemini", "cloudwatch", "ecs", "lambda", "dynamodb",
)


@dataclass(frozen=True)
class CoverLetterGeneration:
    text: str
    method: str
    failure_reason: str | None = None
    similarity: float | None = None
    version: int = 1


def eligible_for_email(job: Job, minimum: int) -> tuple[bool, str]:
    if job.score < minimum:
        return False, "Score below auto-application threshold"
    if not job.application_email:
        return False, "No published application email"
    if job.warnings:
        return False, "Posting has seniority or experience warnings"
    return True, "Eligible: published destination, high score, no red flags"


def _first(mapping: dict, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _display_url(value: str) -> str:
    """Keep contact links readable in a letter header without hiding their destination."""
    value=value.strip()
    if value and not re.match(r"^https?://",value,re.IGNORECASE):
        value=f"https://{value}"
    return value.rstrip("/ ") + ("/" if value.rstrip("/ ").endswith("draiimon.gt.tc") else "")


def _format_phone(value: str) -> str:
    """Present a Philippine mobile number read from the resume in a copyable form."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("639") and len(digits) == 12:
        return f"+63 {digits[2:5]} {digits[5:8]} {digits[8:12]}"
    if digits.startswith("09") and len(digits) == 11:
        return f"+63 {digits[1:4]} {digits[4:7]} {digits[7:11]}"
    return value.strip()


def _format_location(value: str) -> str:
    """Keep a partial Philippine resume location complete without changing its city."""
    normalized = re.sub(r"\s+", " ", value or "").strip(" ,")
    if normalized and "philippines" not in normalized.lower() and any(
        place in normalized.lower() for place in ("cavite", "manila", "taguig", "makati", "bacoor")
    ):
        return f"{normalized}, Philippines"
    return normalized


def _candidate_profile(resume_text: str | None) -> CandidateProfile:
    """Extract identity/contact facts from the active resume or private profile."""
    data = profile()
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    text = resume_text or ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]

    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"\+?\d[\d\s().-]{7,}\d", text)
    urls = re.findall(r"https?://[^\s<>()]+", text)
    github = next((url.rstrip(".,)") for url in urls if "github.com" in url.lower()), "")
    portfolio = next(
        (url.rstrip(".,)") for url in urls if "github.com" not in url.lower() and "linkedin.com" not in url.lower()),
        "",
    )
    name = _first(data, "name", "full_name") or _first(contact, "name", "full_name")
    if not name and lines:
        candidate = lines[0]
        if "@" not in candidate and not re.match(r"^\+?\d", candidate) and len(candidate) <= 80:
            name = candidate
    location = _first(data, "location", "city") or _first(contact, "location", "city")
    if not location:
        location = next(
            (line for line in lines[:12] if "philippines" in line.lower() or "cavite" in line.lower()),
            "",
        )
    education = _first(data, "education") or _first(contact, "education")
    raw_phone = _first(contact, "phone") or (phone_match.group(0).strip() if phone_match else "")
    return CandidateProfile(
        name=name,
        location=_format_location(location),
        phone=_format_phone(raw_phone),
        email=_first(contact, "email") or (email_match.group(0) if email_match else ""),
        github=_display_url(_first(contact, "github") or github),
        portfolio=_display_url(_first(contact, "portfolio", "website") or portfolio),
        education=education,
    )


def _requirements(description: str) -> list[str]:
    lowered = description.lower()
    result: list[str] = []
    for term, label in REQUIREMENT_TERMS:
        if term in lowered and label not in result:
            result.append(label)
        if len(result) == 5:
            break
    if not result:
        result = [
            SKILL_LABELS[skill]
            for skill in SKILLS
            if skill in lowered and skill in SKILL_LABELS
        ][:3]
    return result


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _contains(text: str, *terms: str) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _role_category(job_text: str) -> str:
    lowered = job_text.lower()
    if _contains(lowered, "machine learning", "ai/ml", "artificial intelligence", "nlp", "llm", "rag"):
        return "ai"
    if _contains(lowered, "it support", "technical support", "help desk", "service desk", "systems support"):
        return "support"
    if _contains(lowered, "devops", "infrastructure", "platform", "cloud", "site reliability", "sre"):
        return "infrastructure"
    if _contains(lowered, "software", "developer", "backend", "frontend", "full stack", "web", "api"):
        return "software"
    return "general"


def _experience_paragraph(job_text: str, resume_text: str | None, variant: int = 0) -> str:
    source = (resume_text or "").lower()
    requirements = _requirements(job_text)
    category = _role_category(job_text)
    if "oaktree" in source and category in {"infrastructure", "support"}:
        technologies = [
            label
            for term, label in (
                ("aws", "AWS"),
                ("docker", "Docker"),
                ("github actions", "GitHub Actions"),
                ("terraform", "Terraform"),
                ("linux", "Linux"),
            )
            if term in source and (not requirements or label in requirements or label in ("AWS", "Docker", "Terraform"))
        ][:4]
        tools = _join(technologies) if technologies else "cloud and deployment tools"
        versions = (
            f"During my Cloud DevOps internship at Oaktree Innovations, I worked with {tools}. "
            "I helped with deployment tasks, environment setup, and infrastructure work. "
            "I also used Infrastructure as Code and CI/CD in tasks assigned to me. When an environment had an issue, "
            "I checked the settings, looked for the cause, and shared the next step clearly.",
            f"At Oaktree Innovations, I used {tools} during my Cloud DevOps internship. "
            "My work included deployment workflows, configuration, and cloud infrastructure tasks. "
            "This gave me hands-on practice with Infrastructure as Code, CI/CD, and troubleshooting. "
            "I learned to check the setup first and explain what I found in a simple way.",
            f"My internship at Oaktree Innovations involved {tools}. "
            "I supported deployment work and helped set up environments for cloud tasks. "
            "I used Infrastructure as Code and CI/CD tools while learning how changes affect an application. "
            "The work also taught me to review configuration carefully when something does not work.",
        )
        return versions[variant % len(versions)]
    if ("school web portal" in source or "rag-based" in source or "rag knowledge" in source) and category in {"software", "ai", "general"}:
        technologies = [
            label
            for term, label in (("node.js", "Node.js"), ("postgresql", "PostgreSQL"), ("pgvector", "pgvector"), ("gemini", "Gemini"))
            if term in source
        ][:4]
        tools = _join(technologies) if technologies else "full-stack and AI integration tools"
        versions = (
            f"In freelance full-stack and AI work, I helped build a school web portal with student, appointment, "
            f"and admin features. It included a RAG knowledge assistant using {tools}. "
            "I worked with the application flow, database, searchable content, and deployment setup.",
            f"My freelance project work included a school web portal and a RAG knowledge assistant built with {tools}. "
            "I worked on student and admin features, database tasks, and application flow. "
            "It taught me to connect a user need with the code and data behind it.",
            f"For a school web portal project, I worked on full-stack and AI features using {tools}. "
            "The project had student, appointment, and admin functions plus a RAG knowledge assistant. "
            "I learned how the interface, database, and deployment setup need to work together.",
        )
        return versions[variant % len(versions)]
    if resume_text:
        skills = [
            SKILL_LABELS[skill]
            for skill in SKILL_LABELS
            if skill in source and (not requirements or SKILL_LABELS[skill] in requirements)
        ][:4]
        if skills:
            return (
                f"My resume includes hands-on work with {_join(skills)} in development and deployment projects. "
                "I am still early in my career, but I learn new systems quickly, test my work, and ask questions "
                "when I need help."
            )
    return (
        "My background comes from hands-on technical work and projects. I try to understand how each part works, "
        "test changes, and keep clear notes. I am comfortable learning new tools when the role needs them."
    )


def _project_paragraph(job_text: str, resume_text: str | None, variant: int = 0) -> str:
    source = (resume_text or "").lower()
    category = _role_category(job_text)
    if not resume_text:
        return (
            "I can bring the same approach to this role: learn the task, test the change, and communicate clearly. "
            "When requirements are unclear, I ask questions early and keep the work easy for the next person to review."
        )
    if _contains(job_text, "kubernetes", "infrastructure", "platform", "devops", "cloud") and "kubernetes" in source:
        versions = (
            "I also built a Kubernetes deployment lab using containerized workloads, Deployments, Services, "
            "and Nginx Ingress in Linux. It helped me understand how an application is packaged, exposed, and checked "
            "during deployment. This was a project, not production work, but it supports what I learned in my internship.",
            "For a Kubernetes deployment lab, I used containers, Deployments, Services, and Nginx Ingress on Linux. "
            "I used it to see how an application moves from a container to a running service. It was project work, "
            "but it gave me useful practice for cloud and deployment tasks.",
            "My Kubernetes lab used Linux, containers, Deployments, Services, and Nginx Ingress. "
            "It gave me a simple way to practice deploying an application and checking how its services are exposed. "
            "The lab was not production employment, but it is related to the cloud work I did during my internship.",
        )
        return versions[variant % len(versions)]
    if category == "ai" and _contains(source, "panicsense", "mbert", "bi-gru", "lstm"):
        models = [
            label
            for term, label in (("mbert", "mBERT"), ("bi-gru", "Bi-GRU"), ("lstm", "LSTM"))
            if term in source
        ]
        model_text = _join(models) if models else "language and sequence models"
        return (
            f"In the PanicSense PH project, I worked with {model_text} for language and sentiment tasks. "
            "The project taught me to check data and model output carefully, then connect the AI part to the rest "
            "of an application."
        )
    if category == "ai" and ("school web portal" in source or "rag" in source):
        return (
            "The RAG knowledge assistant in the school portal combined a searchable knowledge base with an application "
            "flow and an LLM feature. I learned that AI output needs testing, useful context, and clear explanations."
        )
    if category == "software" and "school web portal" in source:
        return (
            "The school web portal project included application flow, database work, AI integration, and deployment "
            "setup. It gave me practice following a feature from the user need to the code and data behind it."
        )
    if category == "support" and "oaktree" in source:
        return (
            "My Kubernetes and Linux project work also gave me practice checking services, configuration, and deployment. "
            "Together with my internship, it helps me approach support issues step by step without claiming more "
            "experience than I have."
        )
    if _contains(job_text, "ai", "machine learning", "nlp", "rag") and _contains(source, "panicsense", "rag", "mbert"):
        return (
            "My AI and NLP projects taught me to check inputs and outputs instead of trusting a model automatically. "
            "I learned to connect the AI part with the rest of the application and explain the result clearly."
        )
    return (
        "My technical projects taught me to break a task into smaller steps, test the result, and keep the setup clear. "
        "I can use that habit while I continue learning in this role."
    )


def _current_date() -> str:
    now=datetime.now(ZoneInfo("Asia/Manila"))
    return f"{now.strftime('%B')} {now.day}, {now.year}"


def cover_letter(job: Job, resume_text: str | None = None, variant: int = 0) -> str:
    """Build a factual, role-specific letter without asking an AI to invent the facts."""
    title = str(job.title).strip()
    company = str(job.company).strip() or "the company"
    description = f"{title} {company} {job.description or ''}"
    requirements = _requirements(description)
    profile_data = _candidate_profile(resume_text)
    requirement_text = _join(requirements) if requirements else "the role's technical responsibilities"
    category = _role_category(description)
    direction = {
        "infrastructure": "cloud and infrastructure skills",
        "software": "software development skills",
        "support": "technical support and troubleshooting skills",
        "ai": "AI and application development skills",
        "general": "technical skills",
    }[category]
    openings = (
        f"I am applying for the {title} role at {company}. This role matches the {direction} I have been learning "
        f"and using. The job mentions {requirement_text}, which are related to my internship and project work. "
        "I am looking for a junior role where I can learn from the team and help with real tasks.",
        f"I am interested in the {title} position at {company}. I have been building my {direction} through an "
        f"internship and personal projects. The role asks for {requirement_text}, and I would like to use those skills "
        "while learning from experienced teammates.",
        f"I would like to apply for the {title} role at {company}. The work is connected to the {direction} I have "
        f"been studying and practicing. I saw {requirement_text} in the posting, and these are areas where I have "
        "hands-on experience from my internship and projects.",
    )
    closings = (
        f"I would be happy to discuss how my internship and project experience can help the {company} team. "
        "I am ready to learn, ask questions, and do the work carefully. I know I still have more to learn, so I "
        "check my work, take feedback seriously, and keep notes so I can improve.",
        f"Thank you for considering my application. I would be glad to talk about how I can support the {company} "
        "team while growing in this role. I am comfortable starting with clear tasks, checking my work, and asking "
        "for help when I need it. I use feedback to improve my next task.",
        f"I hope to have the chance to speak with the {company} team. I can bring a willingness to learn, clear "
        "communication, and steady effort on the tasks given to me. I will review my work, ask questions early, and "
        "keep notes so I do not make the same mistake again.",
    )
    opening = openings[variant % len(openings)]
    experience = _experience_paragraph(description, resume_text, variant)
    project = _project_paragraph(description, resume_text, variant)
    closing = closings[variant % len(closings)]

    contact = [
        line
        for line in (
            profile_data.name,
            profile_data.location,
            profile_data.phone,
            profile_data.email,
            profile_data.github,
            profile_data.portfolio,
        )
        if line
    ]
    return "\n".join(
        contact
        + [
            "",
            _current_date(),
            "",
            "Hiring Team",
            company,
            "",
            "Dear Hiring Team,",
            "",
            opening,
            "",
            experience,
            "",
            project,
            "",
            closing,
            "",
            "Thank you for your time and consideration.",
            "",
            "Sincerely,",
            "",
            profile_data.name,
        ]
    )


def _duplicate_text(items: list[str]) -> bool:
    normalized = [re.sub(r"[^a-z0-9]+", " ", item.lower()).strip() for item in items if item.strip()]
    return len(normalized) != len(set(normalized))


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _resume_bullets(resume_text: str | None) -> list[str]:
    if not resume_text:
        return []
    return [
        _normalized(line.lstrip("•●▪◦*- ").strip())
        for line in resume_text.splitlines()
        if re.match(r"^\s*[•●▪◦*-]\s+", line)
    ]


def _has_all_caps_skill_dump(letter: str) -> bool:
    skills = r"(?:AWS|DOCKER|TERRAFORM|KUBERNETES|PYTHON|LINUX|CI/CD|GITHUB ACTIONS|POSTGRESQL|JAVASCRIPT)"
    return bool(re.search(rf"\b{skills}\b(?:\s*[,/&-]?\s*\b{skills}\b)+", letter))


def _has_unsupported_employer(letter: str, job: Job, resume_text: str | None) -> bool:
    known = {_normalized(str(job.company))}
    for line in (resume_text or "").splitlines():
        if "—" in line:
            known.add(_normalized(line.split("—", 1)[-1]))
        elif " - " in line:
            known.add(_normalized(line.split(" - ", 1)[-1]))
    for match in re.finditer(r"\bat\s+([A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*){0,3})", letter):
        employer = _normalized(match.group(1).rstrip(".,"))
        if employer and not any(employer == item or employer.startswith(item + " ") or item.startswith(employer + " ") for item in known if item):
            return True
    return False


def _has_unsupported_candidate_technology_claim(letter: str, resume_text: str | None) -> bool:
    """Reject a revision that turns an employer requirement into applicant experience.

    This is deliberately conservative: it checks only sentences that claim
    first-person hands-on use, while still allowing the opening to state that
    the *job* mentions a tool the candidate has not used.
    """
    source = (resume_text or "").lower()
    if not source:
        return False
    claim_pattern = re.compile(
        r"\b(?:i|my internship|my project|my work|we)\b[^.]{0,180}"
        r"\b(?:used|worked with|built with|developed with|experience with|proficient in)\b[^.]*",
        re.IGNORECASE,
    )
    for sentence in claim_pattern.findall(letter):
        lowered = sentence.lower()
        for term in TECHNICAL_FACT_TERMS:
            if term in lowered and term not in source:
                return True
    return False


def valid_revision(letter: str, job: Job | None = None, resume_text: str | None = None) -> bool:
    """Reject fabricated, placeholder-filled, duplicated, or visibly low-quality revisions."""
    if not isinstance(letter, str) or not 220 <= len(letter) <= 5000:
        return False
    lowered = letter.lower()
    if PLACEHOLDER_PATTERN.search(letter) or any(re.search(pattern, lowered) for pattern in PROHIBITED_CLAIMS):
        return False
    if any(phrase in lowered for phrase in CORPORATE_PHRASES):
        return False
    if "\\@" in letter or "\\." in letter or re.search(r"\[[^\]]+\]\(https?://", letter):
        return False
    if re.search(r"(?:[.!?]){2,}", letter):
        return False
    if re.search(r"(?m)^\s*(?:[•●▪◦*-]|\d+[.)])\s+", letter):
        return False
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", letter) if part.strip()]
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", letter) if part.strip()]
    if _duplicate_text(paragraphs) or _duplicate_text(sentences):
        return False
    if _has_all_caps_skill_dump(letter):
        return False
    if job is None:
        return True
    if job.title.lower() not in lowered or job.company.lower() not in lowered or _current_date().lower() not in lowered:
        return False
    words = re.findall(r"\b[\w’'-]+\b", letter)
    # A direct entry-level letter can be concise. Enforce enough detail to be
    # useful without forcing filler/corporate wording just to reach 250 words.
    if not 200 <= len(words) <= 400:
        return False
    profile_data = _candidate_profile(resume_text)
    for required in (
        profile_data.name,
        profile_data.location,
        profile_data.phone,
        profile_data.email,
        profile_data.github,
        profile_data.portfolio,
    ):
        if required and required.lower() not in lowered:
            return False
    if resume_text and any(
        bullet and len(bullet.split()) >= 5 and bullet in _normalized(letter)
        for bullet in _resume_bullets(resume_text)
    ):
        return False
    if _has_unsupported_employer(letter, job, resume_text):
        return False
    if _has_unsupported_candidate_technology_claim(letter, resume_text):
        return False
    if not re.search(r"(?im)^dear hiring team,\s*$", letter):
        return False
    return len(paragraphs) >= 4


def _letter_body_for_similarity(letter: str) -> str:
    """Ignore identity/date boilerplate when deciding whether a version changed."""
    body = letter.split("Dear Hiring Team,", 1)[-1]
    return _normalized(body)


def letter_similarity(first: str, second: str) -> float:
    return SequenceMatcher(None, _letter_body_for_similarity(first), _letter_body_for_similarity(second)).ratio()


def meaningfully_different(previous: str | None, candidate: str, threshold: float = 0.86) -> bool:
    return not previous or letter_similarity(previous, candidate) < threshold


def letter_body_hash(letter: str) -> str:
    import hashlib
    return hashlib.sha256(_letter_body_for_similarity(letter).encode("utf-8")).hexdigest()


def cover_letter_metadata(metadata: dict | None, result: CoverLetterGeneration) -> dict:
    """Make one generated version the canonical stored text and retain compact history."""
    updated = dict(metadata or {})
    previous = str(updated.get("cover_letter") or "")
    history = [item for item in (updated.get("cover_letter_versions") or []) if isinstance(item, dict)]
    if previous and previous != result.text:
        history.append(
            {
                "version": int(updated.get("cover_letter_version", 1) or 1),
                "method": updated.get(
                    "cover_letter_generation_method", updated.get("cover_letter_mode", "UNKNOWN")
                ),
                "body_hash": letter_body_hash(previous),
            }
        )
    updated.update(
        {
            "cover_letter": result.text,
            "cover_letter_mode": result.method,
            "cover_letter_generation_method": result.method,
            "cover_letter_failure_reason": result.failure_reason,
            "cover_letter_similarity": result.similarity,
            "cover_letter_version": result.version,
            "cover_letter_body_hash": letter_body_hash(result.text),
            "cover_letter_versions": history[-5:],
        }
    )
    return updated


async def _ai_revision(
    job: Job,
    draft: str,
    resume_text: str | None,
    previous_text: str | None,
    version: int,
    attempt: int,
) -> CoverLetterGeneration:
    requirements = _requirements(f"{job.title} {job.description or ''}")
    prompt = (
        "You edit a truthful technology cover letter. Return only the finished letter.\n\n"
        "Use simple, direct, neutral professional English. The writer is an early-career Filipino applicant. "
        "Use short sentences and common words. Do not use corporate or AI-sounding phrases such as: "
        "'technical foundation', 'practical exposure', 'I would welcome', 'steady follow-through', 'align with', "
        "'leverage', or 'proven ability'.\n"
        "Keep the exact header, contact details, current date, role, company, and truthful facts. "
        "Do not add experience, years, certifications, metrics, employers, or facts not in the draft/resume. "
        "Keep four body paragraphs and a normal closing.\n"
        f"This is regeneration version {version}, attempt {attempt}. Change the writing, sentence order, transitions, "
        "and emphasis from the previous version. A date-only change is not acceptable.\n"
        f"Use these job requirements only when relevant: {', '.join(requirements) or 'the listed responsibilities'}.\n\n"
        f"Role: {job.title}\nCompany: {job.company}\nJob description:\n{job.description[:5000]}\n\n"
        f"Draft to improve:\n{draft}\n\n"
        f"Previous version to avoid repeating:\n{previous_text or '(none)'}\n\n"
        f"Canonical resume context:\n{prompt_resume_context(resume_text)}"
    )
    provider = gemini()
    if hasattr(provider, "revise_with_status"):
        outcome = await provider.revise_with_status(prompt)
        revision, failure_reason = outcome.text, outcome.failure_reason
    else:  # Keeps test doubles and older compatible integrations usable.
        revision, failure_reason = await provider.revise(prompt), None
    if not revision:
        return CoverLetterGeneration(draft, "DETERMINISTIC_FALLBACK", failure_reason or "AI_UNAVAILABLE", version=version)
    if not valid_revision(revision, job, resume_text):
        return CoverLetterGeneration(draft, "DETERMINISTIC_FALLBACK", "VALIDATION_FAILURE", version=version)
    if not meaningfully_different(draft, revision):
        return CoverLetterGeneration(draft, "DETERMINISTIC_FALLBACK", "AI_NO_MEANINGFUL_CHANGE", version=version)
    similarity = letter_similarity(previous_text, revision) if previous_text else None
    if not meaningfully_different(previous_text, revision):
        return CoverLetterGeneration(draft, "DETERMINISTIC_FALLBACK", "AI_TOO_SIMILAR", similarity, version)
    return CoverLetterGeneration(revision, "AI_REVISED", None, similarity, version)


async def generate_cover_letter(
    job: Job,
    use_ai: bool = True,
    regenerate: bool = False,
    resume_text: str | None = None,
) -> CoverLetterGeneration:
    """Create one truthful canonical version and disclose whether AI actually ran."""
    metadata = job.raw_metadata or {}
    cached = metadata.get("cover_letter")
    if cached and not regenerate:
        return CoverLetterGeneration(
            cached,
            metadata.get("cover_letter_generation_method", metadata.get("cover_letter_mode", "DETERMINISTIC_ONLY")),
            metadata.get("cover_letter_failure_reason"),
            metadata.get("cover_letter_similarity"),
            int(metadata.get("cover_letter_version", 1)),
        )

    previous = str(cached) if cached else None
    # Legacy saved letters had no version. Treat one as version 1 so its first
    # regenerate is clearly version 2 instead of silently reusing version 1.
    prior_version = int(metadata.get("cover_letter_version", 1 if cached else 0) or (1 if cached else 0))
    version = prior_version + 1 if regenerate or not cached else max(1, prior_version)
    historical_hashes = {
        str(item.get("body_hash"))
        for item in (metadata.get("cover_letter_versions") or [])
        if isinstance(item, dict) and item.get("body_hash")
    }
    if previous:
        historical_hashes.add(letter_body_hash(previous))
    last_failure: str | None = None

    # A regenerate intentionally gets a new prompt/version and bypasses the
    # same-prompt Gemini cache. At most two AI revisions are attempted.
    if use_ai:
        for attempt in range(1, 3):
            draft = cover_letter(job, resume_text, variant=version + attempt - 1)
            result = await _ai_revision(job, draft, resume_text, previous, version, attempt)
            if result.method == "AI_REVISED" and letter_body_hash(result.text) not in historical_hashes:
                return result
            last_failure = result.failure_reason or "AI_REPEATED_PRIOR_VERSION"

    # AI is optional. Pick a deterministic variant that is genuinely different
    # from the active version; never present it as an AI rewrite.
    for offset in range(3):
        draft = cover_letter(job, resume_text, variant=version + offset)
        if meaningfully_different(previous, draft) and letter_body_hash(draft) not in historical_hashes:
            return CoverLetterGeneration(
                draft,
                "DETERMINISTIC_FALLBACK" if use_ai else "DETERMINISTIC_ONLY",
                last_failure if use_ai else None,
                letter_similarity(previous, draft) if previous else None,
                version,
            )
    # All known variants have already been used. Keep the active canonical
    # version instead of silently moving the user back to an older draft.
    if previous:
        return CoverLetterGeneration(
            previous,
            "DETERMINISTIC_FALLBACK" if use_ai else "DETERMINISTIC_ONLY",
            "NO_MEANINGFUL_VARIATION",
            1.0,
            prior_version,
        )
    return CoverLetterGeneration(
        cover_letter(job, resume_text, variant=version),
        "DETERMINISTIC_FALLBACK" if use_ai else "DETERMINISTIC_ONLY",
        last_failure,
        None,
        version,
    )


async def revised_cover_letter(job: Job, resume_text: str | None = None) -> str:
    """Compatibility helper for legacy callers; Discord uses generate_cover_letter."""
    return (await generate_cover_letter(job, use_ai=True, regenerate=True, resume_text=resume_text)).text


async def generated_letter(
    job: Job,
    use_ai: bool = True,
    regenerate: bool = False,
    resume_text: str | None = None,
) -> tuple[str, str]:
    """Backward-compatible tuple API for existing callers/tests."""
    result = await generate_cover_letter(job, use_ai, regenerate, resume_text)
    return result.text, result.method


def cover_letter_pdf_bytes(letter: str) -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    x, y, width, bottom = 72, 72, 468, 760
    for paragraph in letter.splitlines():
        if not paragraph.strip():
            y += 9
            continue
        for line in textwrap.wrap(paragraph, width=88, break_long_words=False, break_on_hyphens=False) or [""]:
            if y > bottom:
                page = document.new_page()
                y = 72
            page.insert_text((x, y), line, fontname="helv", fontsize=10.5, color=(0, 0, 0))
            y += 15
        y += 3
    data=document.tobytes()
    document.close()
    return data


def _write_pdf(letter: str, path: Path) -> None:
    path.write_bytes(cover_letter_pdf_bytes(letter))


def cover_letter_filename(job: Job, extension: str) -> str:
    company=re.sub(r"[^a-z0-9]+", "_", str(job.company).lower()).strip("_").title() or "Company"
    date=datetime.now(ZoneInfo('Asia/Manila')).date().isoformat()
    return f"Mark_Andrei_Castillo_Cover_Letter_{company}_{date}.{extension}"


def cover_letter_text_bytes(letter: str) -> bytes:
    """The download is exactly the final validated letter, without Discord markup."""
    return letter.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def discord_cover_letter_chunks(letter: str, limit: int = 3900) -> list[str]:
    """Sanitize editor artifacts and split only at paragraph boundaries where possible."""
    clean: list[str] = []
    for line in letter.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if (
            not stripped
            or lowered in {"svg", "(image)", "[image]"}
            or stripped.startswith("![")
            or lowered.startswith(("debug:", "trace:", "data:image/", "http://cdn.discordapp.com/", "https://cdn.discordapp.com/"))
            or "discordapp.com/avatars/" in lowered
            or re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s]*)?$", lowered)
        ):
            if not stripped:
                clean.append("")
            continue
        clean.append(line)
    text = "\n".join(clean).strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            split_at = paragraph.rfind(" ", 0, limit + 1)
            split_at = split_at if split_at > 0 else limit
            chunks.append(paragraph[:split_at].rstrip())
            paragraph = paragraph[split_at:].lstrip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def write_package(
    job: Job,
    root: Path = Path("data/applications"),
    letter: str = "",
    resume_bytes: bytes | None = None,
    resume_filename: str | None = None,
) -> Path:
    company_slug = re.sub(r"[^a-z0-9]+", "-", job.company.lower()).strip("-") or "company"
    company_filename = company_slug[:1].upper() + company_slug[1:]
    role = re.sub(r"[^a-z0-9]+", "-", job.title.lower()).strip("-") or "role"
    path = root / company_slug / role
    path.mkdir(parents=True, exist_ok=True)
    if not letter:
        raise ValueError("write_package requires the persisted canonical cover-letter text")
    final_letter = letter
    (path / "description.txt").write_text(job.description, encoding="utf-8")
    (path / "cover-letter.md").write_text(final_letter, encoding="utf-8")
    (path / "job.json").write_text(
        json.dumps({"job_url": job.url, "title": job.title, "company": job.company, "score": job.score}, indent=2),
        encoding="utf-8",
    )
    pdf_name = cover_letter_filename(job, "pdf")
    txt_name = cover_letter_filename(job, "txt")
    (path / txt_name).write_bytes(cover_letter_text_bytes(final_letter))
    _write_pdf(final_letter, path / pdf_name)
    if resume_bytes:
        (path / (resume_filename or "resume.pdf")).write_bytes(resume_bytes)
    return path
