from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
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
    return re.sub(r"^https?://", "", value.strip(), flags=re.IGNORECASE).rstrip("/ ")


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
    return CandidateProfile(
        name=name,
        location=location,
        phone=email_match and _first(contact, "phone") or (phone_match.group(0).strip() if phone_match else ""),
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


def _experience_paragraph(job_text: str, resume_text: str | None) -> str:
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
        support_sentence = (
            "It also strengthened the support habits that matter when a local or cloud environment behaves unexpectedly: "
            "checking the configuration, isolating the problem, and communicating the next step clearly."
            if category == "support"
            else
            "I also gained practice with monitoring and troubleshooting, which helped me connect configuration changes "
            "to the behavior of the application being delivered."
        )
        return (
            f"During my Cloud DevOps internship at Oaktree Innovations, I worked with {tools} "
            "while supporting deployment workflows, environment configuration, and infrastructure tasks. "
            "That experience gave me practical exposure to Infrastructure as Code, CI/CD, troubleshooting, "
            f"and the day-to-day discipline of delivering applications into cloud environments. {support_sentence}"
        )
    if ("school web portal" in source or "rag-based" in source or "rag knowledge" in source) and category in {"software", "ai", "general"}:
        technologies = [
            label
            for term, label in (("node.js", "Node.js"), ("postgresql", "PostgreSQL"), ("pgvector", "pgvector"), ("gemini", "Gemini"))
            if term in source
        ][:4]
        tools = _join(technologies) if technologies else "full-stack and AI integration tools"
        return (
            f"Through freelance full-stack and AI development work on a school web portal, I built student-information, "
            f"appointment, and administrative features alongside a RAG knowledge assistant using {tools}. "
            "The work involved connecting application flow, databases, searchable content, and deployment concerns. "
            "It gave me practical experience translating requirements into a usable application while keeping the "
            "technical pieces understandable and connected."
        )
    if resume_text:
        skills = [
            SKILL_LABELS[skill]
            for skill in SKILL_LABELS
            if skill in source and (not requirements or SKILL_LABELS[skill] in requirements)
        ][:4]
        if skills:
            return (
                f"My resume reflects hands-on work with {_join(skills)} in development, deployment, and "
                "technical project settings. That background has strengthened my ability to learn unfamiliar "
                "systems, troubleshoot methodically, and contribute without overstating the scope of my experience."
            )
    return (
        "My background is built around hands-on technical work and a practical approach to learning new systems. "
        "I focus on understanding how the pieces fit together, documenting what I learn, and solving problems "
        "carefully so that the result is useful to the team and maintainable after handoff."
    )


def _project_paragraph(job_text: str, resume_text: str | None) -> str:
    source = (resume_text or "").lower()
    category = _role_category(job_text)
    if not resume_text:
        return (
            "I would bring the same approach to this role by connecting technical implementation with reliable "
            "day-to-day execution, clear communication, and continued development of my engineering foundation. "
            "That means testing changes carefully, asking focused questions when requirements are unclear, and leaving "
            "the surrounding work easier for the next person to understand."
        )
    if _contains(job_text, "kubernetes", "infrastructure", "platform", "devops", "cloud") and "kubernetes" in source:
        return (
            "A Kubernetes deployment lab further developed this foundation through containerized workloads, "
            "Deployments, Services, and Nginx Ingress in a Linux environment. It helped me understand how "
            "applications are packaged, exposed, and troubleshot across the parts of a deployment system. "
            "That project-based practice complements my internship exposure to cloud delivery without presenting "
            "the lab as production employment."
        )
    if category == "ai" and _contains(source, "panicsense", "mbert", "bi-gru", "lstm"):
        models = [
            label
            for term, label in (("mbert", "mBERT"), ("bi-gru", "Bi-GRU"), ("lstm", "LSTM"))
            if term in source
        ]
        model_text = _join(models) if models else "language and sequence models"
        return (
            f"In the PanicSense PH project, I worked with {model_text} as part of research-oriented language and "
            "sentiment work. That project taught me to examine data and model behavior carefully, connect an AI "
            "component to a larger application goal, and explain technical results clearly rather than treating a "
            "model as a complete product by itself."
        )
    if category == "ai" and ("school web portal" in source or "rag" in source):
        return (
            "The RAG knowledge assistant in the school portal work is also relevant because it combined a searchable "
            "knowledge base with an application flow and an LLM integration. Working through retrieval, database "
            "behavior, and the user-facing result helped me see how AI features need product context, testing, and "
            "clear explanations to be useful."
        )
    if category == "software" and "school web portal" in source:
        return (
            "The school web portal work is especially relevant because it required more than writing isolated "
            "features: I worked through application flow, database behavior, AI integration, and deployment setup "
            "as connected parts of one usable product. That project strengthened my habit of tracing a feature from "
            "its user need through its data and implementation details."
        )
    if category == "support" and "oaktree" in source:
        return (
            "My Kubernetes and Linux project work also gave me a practical environment for troubleshooting services, "
            "checking how components are exposed, and following a problem through configuration and deployment. "
            "Together with the internship experience, it supports a careful, service-minded approach to technical "
            "issues without overstating the scope of my professional background."
        )
    if _contains(job_text, "ai", "machine learning", "nlp", "rag") and _contains(source, "panicsense", "rag", "mbert"):
        return (
            "My AI and NLP project work also taught me to separate a model or service from the surrounding product "
            "decisions. Working with research-oriented language and sentiment systems strengthened my ability to "
            "evaluate inputs, connect technical components, and explain results clearly. It also reinforced the "
            "importance of validating outputs instead of assuming that a technically interesting result is ready to use."
        )
    return (
        "Across my technical projects, I have learned to turn broad requirements into smaller implementation "
        "steps, test the result, and keep the surrounding workflow understandable. That project-based practice "
        "would help me contribute thoughtfully while continuing to grow in the role, especially when the work crosses "
        "development, configuration, and communication."
    )


def _current_date() -> str:
    now=datetime.now(ZoneInfo("Asia/Manila"))
    return f"{now.strftime('%B')} {now.day}, {now.year}"


def cover_letter(job: Job, resume_text: str | None = None) -> str:
    """Build a factual, role-specific letter without asking an AI to invent the facts."""
    title = str(job.title).strip()
    company = str(job.company).strip() or "the company"
    description = f"{title} {company} {job.description or ''}"
    requirements = _requirements(description)
    profile_data = _candidate_profile(resume_text)
    requirement_text = _join(requirements) if requirements else "the role's technical responsibilities"
    category = _role_category(description)
    direction = {
        "infrastructure": "cloud infrastructure, automation, and reliable application delivery",
        "software": "application development, connected data flows, and maintainable software",
        "support": "technical support, environment configuration, and methodical troubleshooting",
        "ai": "AI-enabled applications, data-aware development, and practical model integration",
        "general": "the technical responsibilities and practical problem solving this role requires",
    }[category]
    opening = (
        f"The {title} opportunity at {company} stood out to me because it aligns with the direction I have been "
        f"building through {direction}. The posting's focus on {requirement_text} is relevant to my hands-on background, "
        "and I am drawn to work where dependable implementation, careful troubleshooting, and clear communication "
        "matter as much as knowing a particular tool."
    )
    experience = _experience_paragraph(description, resume_text)
    project = _project_paragraph(description, resume_text)
    closing = (
        f"I would welcome the opportunity to discuss how my technical foundation and hands-on experience could "
        f"support {company}'s team. I would also value the chance to continue developing through the work and "
        "contribute with the same care I bring to technical projects."
    )

    header = [
        line
        for line in (
            profile_data.name,
            profile_data.location,
            profile_data.phone,
            profile_data.email,
            profile_data.github,
            profile_data.portfolio,
            _current_date(),
            "Hiring Team",
            company,
            "",
            "Dear Hiring Team,",
        )
        if line
    ]
    return "\n".join(header + ["", opening, "", experience, "", project, "", closing, "", "Thank you for your time and consideration.", "", "Sincerely,", profile_data.name])


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


def valid_revision(letter: str, job: Job | None = None, resume_text: str | None = None) -> bool:
    """Reject fabricated, placeholder-filled, duplicated, or visibly low-quality revisions."""
    if not isinstance(letter, str) or not 300 <= len(letter) <= 5000:
        return False
    lowered = letter.lower()
    if PLACEHOLDER_PATTERN.search(letter) or any(re.search(pattern, lowered) for pattern in PROHIBITED_CLAIMS):
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
    if not 240 <= len(words) <= 400:
        return False
    profile_data = _candidate_profile(resume_text)
    for required in (profile_data.name, profile_data.phone, profile_data.email, profile_data.github, profile_data.portfolio):
        if required and required.lower() not in lowered:
            return False
    if resume_text and any(
        bullet and len(bullet.split()) >= 5 and bullet in _normalized(letter)
        for bullet in _resume_bullets(resume_text)
    ):
        return False
    if _has_unsupported_employer(letter, job, resume_text):
        return False
    if not re.search(r"(?im)^dear hiring team,\s*$", letter):
        return False
    return len(paragraphs) >= 4


async def revised_cover_letter(job: Job, resume_text: str | None = None) -> str:
    draft = cover_letter(job, resume_text)
    requirements = _requirements(f"{job.title} {job.description or ''}")
    prompt = (
        "You are the final editor for a technology job application. Rewrite the draft for natural, modern "
        "professional English, but do not change its facts. Return only the finished letter.\n\n"
        "Rules:\n"
        "- Preserve the candidate header, contact details, current date, exact job title, and company.\n"
        "- Keep four concise body paragraphs plus a professional closing, approximately 250-350 words.\n"
        "- Use only facts in the draft or the redacted canonical resume context.\n"
        "- Do not add years of experience, certifications, metrics, employers, clients, recruiters, or production ownership.\n"
        "- Do not use placeholders, resume-bullet dumping, generic AI openings, or exaggerated senior language.\n"
        f"- Prioritize these actual job requirements when relevant: {', '.join(requirements) or 'the posted responsibilities'}.\n\n"
        f"Role: {job.title}\nCompany: {job.company}\nJob description:\n{job.description[:5000]}\n\n"
        f"Deterministic draft:\n{draft}\n\n"
        f"Canonical resume context:\n{prompt_resume_context(resume_text)}"
    )
    revision = await gemini().revise(prompt)
    return revision if revision and valid_revision(revision, job, resume_text) else draft


async def generated_letter(
    job: Job,
    use_ai: bool = True,
    regenerate: bool = False,
    resume_text: str | None = None,
) -> tuple[str, str]:
    """Cache the verified final letter on the job; Gemini is an optional editor."""
    cached = (job.raw_metadata or {}).get("cover_letter")
    if cached and not regenerate:
        return cached, (job.raw_metadata or {}).get("cover_letter_mode", "TEMPLATE")
    draft = cover_letter(job, resume_text)
    final = await revised_cover_letter(job, resume_text) if use_ai else draft
    mode = "GEMINI" if use_ai and final != draft else "TEMPLATE"
    return final, mode


def _write_pdf(letter: str, path: Path) -> None:
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
    document.save(path)
    document.close()


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
    letter: str | None = None,
    resume_bytes: bytes | None = None,
    resume_filename: str | None = None,
) -> Path:
    company_slug = re.sub(r"[^a-z0-9]+", "-", job.company.lower()).strip("-") or "company"
    company_filename = company_slug[:1].upper() + company_slug[1:]
    role = re.sub(r"[^a-z0-9]+", "-", job.title.lower()).strip("-") or "role"
    path = root / company_slug / role
    path.mkdir(parents=True, exist_ok=True)
    final_letter = letter or cover_letter(job)
    (path / "description.txt").write_text(job.description, encoding="utf-8")
    (path / "cover-letter.md").write_text(final_letter, encoding="utf-8")
    (path / "job.json").write_text(
        json.dumps({"job_url": job.url, "title": job.title, "company": job.company, "score": job.score}, indent=2),
        encoding="utf-8",
    )
    pdf_name = f"Mark_Andrei_Castillo_Cover_Letter_{company_filename}_{datetime.now(ZoneInfo('Asia/Manila')).date().isoformat()}.pdf"
    _write_pdf(final_letter, path / pdf_name)
    if resume_bytes:
        (path / (resume_filename or "resume.pdf")).write_bytes(resume_bytes)
    return path
