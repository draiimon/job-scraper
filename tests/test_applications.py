from types import SimpleNamespace

import pytest

from src.applications import (
    cover_letter,
    discord_cover_letter_chunks,
    generated_letter,
    valid_revision,
    write_package,
)


RESUME = """Mark Andrei R. Castillo
Bacoor City, Cavite, Philippines
+63 953 852 1829
andreicastillofficial@gmail.com
https://github.com/draiimon
https://www.draiimon.gt.tc/

EDUCATION
Bachelor of Science in Computer Science
Technological Institute of the Philippines

EXPERIENCE
Cloud DevOps Intern — Oaktree Innovations
• Worked with AWS cloud services including ECS and S3.
• Used Docker and GitHub Actions for deployment workflows.
• Worked with Terraform for Infrastructure as Code.

RESEARCH & PROJECTS
● Kubernetes Deployment Lab
Docker, Pods, Deployments, Services, Nginx Ingress, Linux / Ubuntu
"""

FULL_RESUME = RESUME + """
● School Web Portal & RAG Knowledge Assistant
Node.js + Express + PostgreSQL + pgvector + Gemini + Groq + Docker
● PanicSense PH
mBERT + Bi-GRU + LSTM + AWS + Terraform
"""


def job():
    return SimpleNamespace(
        title="Junior Cloud Engineer",
        company="Cloud PH",
        description="AWS Terraform Docker GitHub Actions Linux Kubernetes",
        url="https://example.com/job",
        score=90,
        raw_metadata={},
    )


def test_deterministic_letter_matches_brief_without_resume_bullet_dumping():
    letter = cover_letter(job(), RESUME)
    words = letter.split()

    assert 250 <= len(words) <= 350
    assert "Mark Andrei R. Castillo" in letter
    assert "andreicastillofficial@gmail.com" in letter
    assert "+63 953 852 1829" in letter
    assert "github.com/draiimon" in letter
    assert "Cloud PH" in letter and "Junior Cloud Engineer" in letter
    assert "[Your Name]" not in letter
    assert letter.count("Oaktree") == 1
    assert "AWS DOCKER" not in letter
    assert valid_revision(letter, job(), RESUME)


@pytest.mark.parametrize(
    ("title", "description", "required"),
    [
        (
            "Infrastructure Developer",
            "AWS Terraform Docker GitHub Actions Linux Kubernetes",
            ("Oaktree Innovations", "Kubernetes deployment lab"),
        ),
        (
            "Software Engineer",
            "Python JavaScript Node.js Express PostgreSQL REST APIs Docker",
            ("freelance full-stack and AI development", "school web portal"),
        ),
        (
            "IT Support Specialist",
            "Linux Ubuntu command line troubleshooting environment configuration technical support",
            ("Oaktree Innovations", "service-minded approach"),
        ),
        (
            "AI/ML Engineer",
            "AI machine learning NLP RAG Python mBERT LSTM Bi-GRU Gemini",
            ("PanicSense PH", "RAG knowledge assistant"),
        ),
    ],
)
def test_role_categories_select_relevant_verified_context(title, description, required):
    record = SimpleNamespace(
        title=title,
        company="Example Co",
        description=description,
        url="https://example.com/job",
        score=90,
        raw_metadata={},
    )
    letter = cover_letter(record, FULL_RESUME)
    assert 250 <= len(letter.split()) <= 350
    assert all(term.lower() in letter.lower() for term in required)
    assert "YOLOv8" not in letter
    assert "extensive experience" not in letter.lower()
    assert valid_revision(letter, record, FULL_RESUME)


def test_contact_header_date_and_four_body_paragraphs_are_present():
    letter = cover_letter(job(), FULL_RESUME)
    assert letter.startswith(
        "Mark Andrei R. Castillo\n"
        "Bacoor City, Cavite, Philippines\n"
        "+63 953 852 1829\n"
        "andreicastillofficial@gmail.com\n"
        "https://github.com/draiimon\n"
        "https://www.draiimon.gt.tc/\n"
    )
    from src.applications import _current_date

    assert _current_date() in letter
    body = letter.split("Dear Hiring Team,\n\n", 1)[1].split("\n\nThank you", 1)[0]
    assert len([part for part in body.split("\n\n") if part.strip()]) == 4


@pytest.mark.parametrize(
    "bad",
    [
        "A" * 400 + " [Your Name]",
        "A" * 400 + " I have 5 years of experience.",
        "A" * 400 + " I am AWS certified.",
        "A" * 400 + "\n• AWS\n• Docker",
        "A" * 400 + " Really!!",
    ],
)
def test_quality_validator_rejects_common_bad_output(bad):
    assert not valid_revision(bad)


def test_quality_validator_rejects_copied_bullets_and_invented_employer():
    copied = cover_letter(job(), RESUME).replace(
        "During my Cloud DevOps internship",
        "Worked with AWS cloud services including ECS and S3. During my Cloud DevOps internship",
    )
    assert not valid_revision(copied, job(), RESUME)

    invented = cover_letter(job(), RESUME).replace("Oaktree Innovations", "Google")
    assert not valid_revision(invented, job(), RESUME)


def test_quality_validator_rejects_duplicate_experience_and_missing_identity():
    duplicate = "A" * 300 + "\n\nOaktree experience.\n\nOaktree experience."
    assert not valid_revision(duplicate)

    incomplete = cover_letter(job(), RESUME).replace("andreicastillofficial@gmail.com", "")
    assert not valid_revision(incomplete, job(), RESUME)


def test_write_package_creates_named_professional_pdf(tmp_path):
    package = write_package(job(), root=tmp_path, letter=cover_letter(job(), RESUME))
    pdfs = list(package.glob("Mark_Andrei_Castillo_Cover_Letter_Cloud_Ph_*.pdf"))

    assert len(pdfs) == 1
    import pymupdf

    document = pymupdf.open(pdfs[0])
    extracted = "\n".join(page.get_text() for page in document)
    document.close()
    assert "Cloud PH" in extracted
    assert "Oaktree Innovations" in extracted
    txts = list(package.glob("Mark_Andrei_Castillo_Cover_Letter_Cloud_Ph_*.txt"))
    assert len(txts) == 1
    assert txts[0].read_text(encoding="utf-8") == cover_letter(job(), RESUME)


def test_copy_ready_letter_keeps_urls_and_paragraphs_without_discord_artifacts():
    letter = cover_letter(job(), RESUME)
    assert "andreicastillofficial@gmail.com" in letter
    assert "https://github.com/draiimon" in letter
    assert "https://www.draiimon.gt.tc/" in letter
    assert "\\@" not in letter and "\\." not in letter
    assert "[image]" not in letter.lower() and "svg" not in letter.lower()
    assert "Dear Hiring Team,\n\n" in letter
    assert "\n\nThank you for your time and consideration.\n\nSincerely,\n" in letter


@pytest.mark.asyncio
async def test_bad_ai_revision_falls_back_to_deterministic_letter(monkeypatch):
    class FakeGemini:
        async def revise(self, prompt):
            return "I am a certified expert with 10 years of experience."

    monkeypatch.setattr("src.applications.gemini", lambda: FakeGemini())
    letter, mode = await generated_letter(job(), use_ai=True, regenerate=True, resume_text=RESUME)

    assert mode == "TEMPLATE"
    assert "[Your Name]" not in letter
    assert "Oaktree Innovations" in letter


def test_discord_preview_removes_editor_artifacts_and_preserves_paragraphs():
    raw = (
        "Mark Andrei R. Castillo\n\n"
        "Useful paragraph one.\n\n"
        "svg\n(image)\n![preview](https://cdn.discordapp.com/avatars/1.png)\n\n"
        "Useful paragraph two."
    )
    chunks = discord_cover_letter_chunks(raw)
    combined = "\n\n".join(chunks)
    assert "svg" not in combined.lower()
    assert "(image)" not in combined.lower()
    assert "discordapp.com/avatars" not in combined.lower()
    assert "Useful paragraph one.\n\nUseful paragraph two." in combined
    assert all(len(chunk) <= 3900 for chunk in chunks)
