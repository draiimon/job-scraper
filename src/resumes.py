from __future__ import annotations

import re

MAX_RESUME_BYTES = 10 * 1024 * 1024


def extract_resume_text(file_data: bytes, filename: str) -> str:
    """Extract searchable text from a PDF resume before it enters storage."""
    if len(file_data) > MAX_RESUME_BYTES:
        raise ValueError("Resume PDF is larger than the 10 MB limit.")
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF resumes are supported.")
    try:
        import pymupdf

        document = pymupdf.open(stream=file_data, filetype="pdf")
        text = "\n".join(page.get_text() for page in document)
        document.close()
    except Exception as exc:
        raise ValueError("The uploaded file is not a readable PDF.") from exc
    cleaned = "\n".join(line.replace("\u200b", "").strip() for line in text.splitlines() if line.strip())
    if len(cleaned) < 80:
        raise ValueError("The PDF does not contain enough readable resume text.")
    return cleaned


def resume_evidence(job_text: str, resume_text: str | None) -> list[str]:
    """Return short, relevant resume snippets for deterministic letters and AI review."""
    if not resume_text:
        return []
    job_lower = job_text.lower()
    resume_lines = [re.sub(r"\s+", " ", line.replace("\u200b", "")).strip() for line in resume_text.splitlines() if line.strip()]
    matched_terms = [
        term
        for term in (
            "aws",
            "ecs",
            "lambda",
            "s3",
            "dynamodb",
            "docker",
            "kubernetes",
            "terraform",
            "github actions",
            "ci/cd",
            "linux",
            "python",
            "postgresql",
            "mysql",
            "mongodb",
            "rag",
            "gemini",
            "ai",
            "nginx",
            "javascript",
            "react",
            "node.js",
            "rest api",
            "selenium",
            "playwright",
        )
        if term in job_lower and term in resume_text.lower()
    ]
    if not matched_terms:
        return []
    blocks: list[str] = []
    section = ""
    header = ""
    project_parts: list[str] = []
    for line in resume_lines + ["RESEARCH & PROJECTS END"]:
        upper = line.upper()
        if upper == "EXPERIENCE":
            section = "experience"
            continue
        if upper == "RESEARCH & PROJECTS":
            section = "projects"
            header = ""
            project_parts = []
            continue
        if upper == "RESEARCH & PROJECTS END":
            if section == "projects" and header and project_parts:
                blocks.append(f"{header}: {' '.join(project_parts)}")
            break
        if section == "experience":
            if line.startswith("•"):
                if header:
                    blocks.append(f"{header}: {line.lstrip('• ').strip()}")
            elif "—" in line or " - " in line:
                header = line
        elif section == "projects":
            if line.startswith("●"):
                if header and project_parts:
                    blocks.append(f"{header}: {' '.join(project_parts)}")
                header = line.lstrip("● ").strip()
                project_parts = []
            elif header and not upper.startswith(("LEADERSHIP", "REFERENCES")):
                project_parts.append(line)
    evidence: list[str] = []
    for block in blocks:
        lowered = block.lower()
        if "@" in block or "http://" in lowered or "https://" in lowered:
            continue
        if any(term in lowered for term in matched_terms):
            evidence.append(block[:420].strip())
        if len(evidence) >= 4:
            break
    return evidence


def prompt_resume_context(resume_text: str | None) -> str:
    """Minimize unnecessary personal-data exposure when sending context to an AI editor."""
    if not resume_text:
        return "No resume has been uploaded. Use only the deterministic draft."
    redacted = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email omitted]", resume_text)
    redacted = re.sub(r"\+?\d[\d\s().-]{7,}\d", "[phone omitted]", redacted)
    return redacted[:12000]