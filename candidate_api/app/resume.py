import io
import re
import zipfile
from pathlib import PurePath
from xml.etree import ElementTree

from pypdf import PdfReader


MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RESUME_TEXT = 100_000
MAX_DOCX_UNCOMPRESSED_BYTES = 15 * 1024 * 1024

_SKILLS = (
    "Machine Learning", "Generative AI", "REST APIs", "CI/CD", "TypeScript",
    "JavaScript", "PostgreSQL", "Kubernetes", "LangGraph", "LangChain",
    "TensorFlow", "PyTorch", "FastAPI", "Django", "Flask", "React",
    "Next.js", "Node.js", "GraphQL", "Pandas", "NumPy", "Redis", "Docker",
    "Python", "Java", "C++", "C#", "Go", "Rust", "SQL", "AWS", "Azure",
    "GCP", "Git", "Linux", "HTML", "CSS",
)
_SECTION_NAMES = {
    "education": {"education", "academic background"},
    "experience": {"experience", "work experience", "employment", "professional experience"},
    "projects": {"projects", "selected projects", "personal projects"},
    "certifications": {"certifications", "certificates", "licenses and certifications"},
}
class ResumeProcessingError(ValueError):
    pass


def extract_resume_text(filename: str, content: bytes) -> tuple[str, str]:
    """Extract readable text from a bounded PDF or DOCX without executing its contents."""
    if not content or len(content) > MAX_RESUME_BYTES:
        raise ResumeProcessingError("Choose a non-empty resume file up to 5 MB.")
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".pdf":
        if not content.lstrip().startswith(b"%PDF-"):
            raise ResumeProcessingError("This file does not appear to be a valid PDF.")
        try:
            reader = PdfReader(io.BytesIO(content), strict=False)
            if reader.is_encrypted:
                raise ResumeProcessingError("Password-protected PDFs aren’t supported.")
            if len(reader.pages) > 50:
                raise ResumeProcessingError("Keep your resume to 50 pages or fewer.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ResumeProcessingError:
            raise
        except Exception as error:
            raise ResumeProcessingError("We couldn’t read this PDF. Try another copy or paste its text.") from error
        media_type = "application/pdf"
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as document:
                entries = document.infolist()
                if len(entries) > 1000 or sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise ResumeProcessingError("This DOCX expands beyond the supported document size.")
                xml = document.read("word/document.xml")
            root = ElementTree.fromstring(xml)
            text = "\n".join(
                "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t"))
                for paragraph in root.iter()
                if paragraph.tag.endswith("}p")
            )
        except ResumeProcessingError:
            raise
        except Exception as error:
            raise ResumeProcessingError("We couldn’t read this DOCX. Try another copy or paste its text.") from error
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        raise ResumeProcessingError("Upload a PDF or DOCX file, or paste your resume text.")
    text = normalize_resume_text(text)
    if len(text) > MAX_RESUME_TEXT:
        raise ResumeProcessingError("The extracted resume text is too long to review safely.")
    if len(text) < 40:
        raise ResumeProcessingError("We couldn’t find enough readable text. Try a text-based PDF or paste the resume text.")
    return text, media_type


def normalize_resume_text(text: str) -> str:
    text = text.replace("\x00", " ").replace("\f", "\n")
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def suggest_resume_fields(text: str) -> dict[str, object]:
    """Return editable suggestions only; this parser does not establish facts."""
    lines = normalize_resume_text(text).splitlines()
    name = next(
        (
            line[:120]
            for line in lines[:8]
            if len(line) <= 120 and "@" not in line and not _is_heading(line)
            and not re.search(r"\d{7,}", line)
        ),
        "",
    )
    email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    phone_match = re.search(r"(?:\+?\d[\d ().-]{6,}\d)", text)
    skills = [
        skill for skill in _SKILLS
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9])", text, re.I)
    ]
    sections = {key: [] for key in _SECTION_NAMES}
    current = None
    for line in lines:
        heading = _heading_key(line)
        if heading:
            current = heading
            continue
        if current and len(sections[current]) < 20:
            item = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", line).strip()
            if item and len(item) <= 500:
                sections[current].append(item)
    return {
        "full_name": name,
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0).strip() if phone_match else "",
        "target_roles": [],
        "skills": skills,
        **sections,
    }


def _heading_key(line: str) -> str | None:
    heading = re.sub(r"[^a-z ]", "", line.casefold()).strip()
    return next((key for key, values in _SECTION_NAMES.items() if heading in values), None)


def _is_heading(line: str) -> bool:
    return _heading_key(line) is not None
