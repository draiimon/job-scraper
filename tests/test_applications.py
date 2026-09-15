from types import SimpleNamespace

import pytest

from src.applications import (
    cover_letter,
    cover_letter_pdf_bytes,
    cover_letter_text_bytes,
    discord_cover_letter_chunks,
    generate_cover_letter,
    generated_letter,
    letter_body_hash,
    letter_similarity,
    valid_revision,
    write_package,
)
from src.ai import RevisionOutcome


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


def infrastructure_job(raw_metadata=None):
    """The production regression fixture from the Morgan McKinley report."""
    return SimpleNamespace(
        title="Infrastructure Developer",
        company="Morgan McKinley",
        description="AWS Terraform Docker GitHub Actions Linux Kubernetes Infrastructure as Code CI/CD Python",
        url="https://example.com/infrastructure-developer",
        score=90,
        raw_metadata=raw_metadata or {},
    )


def test_deterministic_letter_matches_brief_without_resume_bullet_dumping():
    letter = cover_letter(job(), RESUME)
    words = letter.split()

    assert 200 <= len(words) <= 350
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
            ("freelance full-stack and AI work", "school web portal"),
        ),
        (
            "IT Support Specialist",
            "Linux Ubuntu command line troubleshooting environment configuration technical support",
            ("Oaktree Innovations", "deployment tasks"),
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
    assert 200 <= len(letter.split()) <= 350
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


def test_public_profile_keeps_the_canonical_github_link_when_the_pdf_omits_it():
    resume_without_github = RESUME.replace("https://github.com/draiimon\n", "")

    letter = cover_letter(job(), resume_without_github)

    assert "https://github.com/draiimon" in letter
    assert "\\@" not in letter and "\\." not in letter


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


def test_quality_validator_rejects_an_invented_first_person_technology_claim():
    fabricated = cover_letter(job(), RESUME) + "\n\nI used Azure for production infrastructure."

    assert not valid_revision(fabricated, job(), RESUME)


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


def test_revision_guard_rejects_corporate_language_and_escaped_contact_details():
    record = infrastructure_job()
    letter = cover_letter(record, FULL_RESUME, variant=1)

    assert valid_revision(letter, record, FULL_RESUME)
    assert "\\@" not in letter and "\\." not in letter
    assert "[https://" not in letter

    corporate = letter.replace(
        "I am interested in the",
        "I would welcome the chance to apply for the",
    )
    escaped_email = letter.replace(
        "andreicastillofficial@gmail.com",
        "andreicastillofficial\\@gmail.com",
    )
    markdown_url = letter.replace(
        "https://github.com/draiimon",
        "[GitHub](https://github.com/draiimon)",
    )

    assert not valid_revision(corporate, record, FULL_RESUME)
    assert not valid_revision(escaped_email, record, FULL_RESUME)
    assert not valid_revision(markdown_url, record, FULL_RESUME)


@pytest.mark.asyncio
async def test_ai_revision_calls_provider_and_returns_ai_revised(monkeypatch):
    record = infrastructure_job()
    ai_text = cover_letter(record, FULL_RESUME, variant=2)

    class SuccessfulProvider:
        def __init__(self):
            self.prompts = []

        async def revise_with_status(self, prompt):
            self.prompts.append(prompt)
            return RevisionOutcome(ai_text)

    provider = SuccessfulProvider()
    monkeypatch.setattr("src.applications.gemini", lambda: provider)

    result = await generate_cover_letter(record, use_ai=True, resume_text=FULL_RESUME)

    assert provider.prompts
    assert "Previous version to avoid repeating:" in provider.prompts[0]
    assert result.method == "AI_REVISED"
    assert result.failure_reason is None
    assert result.text == ai_text
    assert result.version == 1


@pytest.mark.asyncio
async def test_ai_unavailable_is_truthfully_labeled_deterministic_fallback(monkeypatch):
    record = infrastructure_job()

    class UnavailableProvider:
        def __init__(self):
            self.calls = 0

        async def revise_with_status(self, _prompt):
            self.calls += 1
            return RevisionOutcome(None, "RATE_LIMIT")

    provider = UnavailableProvider()
    monkeypatch.setattr("src.applications.gemini", lambda: provider)

    result = await generate_cover_letter(record, use_ai=True, resume_text=FULL_RESUME)

    assert provider.calls == 2  # bounded retry, never an unbounded retry storm
    assert result.method == "DETERMINISTIC_FALLBACK"
    assert result.failure_reason == "RATE_LIMIT"
    assert valid_revision(result.text, record, FULL_RESUME)


@pytest.mark.asyncio
async def test_regenerate_creates_meaningfully_different_version_beyond_date():
    record = infrastructure_job()

    version_a = await generate_cover_letter(record, use_ai=False, resume_text=FULL_RESUME)
    record.raw_metadata = {
        "cover_letter": version_a.text,
        "cover_letter_generation_method": version_a.method,
        "cover_letter_version": version_a.version,
        "cover_letter_versions": [{"body_hash": letter_body_hash(version_a.text)}],
    }

    version_b = await generate_cover_letter(
        record,
        use_ai=False,
        regenerate=True,
        resume_text=FULL_RESUME,
    )

    assert version_a.version == 1
    assert version_b.version == 2
    assert version_b.method == "DETERMINISTIC_ONLY"
    assert version_b.text != version_a.text
    assert letter_body_hash(version_b.text) != letter_body_hash(version_a.text)
    assert letter_similarity(version_a.text, version_b.text) < 0.86
    assert valid_revision(version_a.text, record, FULL_RESUME)
    assert valid_revision(version_b.text, record, FULL_RESUME)


def test_txt_pdf_and_package_use_the_exact_explicit_canonical_letter(tmp_path):
    record = infrastructure_job()
    canonical = cover_letter(record, FULL_RESUME, variant=2)
    package = write_package(record, root=tmp_path, letter=canonical)

    import pymupdf

    pdf_path = next(package.glob("*.pdf"))
    document = pymupdf.open(pdf_path)
    pdf_text = "\n".join(page.get_text() for page in document)
    document.close()
    txt_path = next(package.glob("Mark_Andrei_Castillo_Cover_Letter_*.txt"))

    normalize = lambda value: " ".join(value.split())
    assert cover_letter_text_bytes(canonical) == canonical.encode("utf-8")
    assert txt_path.read_bytes() == cover_letter_text_bytes(canonical)
    assert (package / "cover-letter.md").read_text(encoding="utf-8") == canonical
    assert normalize(pdf_text) == normalize(canonical)
    assert normalize(
        "\n".join(
            page.get_text()
            for page in pymupdf.open(stream=cover_letter_pdf_bytes(canonical), filetype="pdf")
        )
    ) == normalize(canonical)

    with pytest.raises(ValueError, match="canonical cover-letter"):
        write_package(record, root=tmp_path / "requires-letter")


@pytest.mark.asyncio
async def test_bad_ai_revision_falls_back_to_deterministic_letter(monkeypatch):
    class FakeGemini:
        async def revise(self, prompt):
            return "I am a certified expert with 10 years of experience."

    monkeypatch.setattr("src.applications.gemini", lambda: FakeGemini())
    letter, mode = await generated_letter(job(), use_ai=True, regenerate=True, resume_text=RESUME)

    assert mode == "DETERMINISTIC_FALLBACK"
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
