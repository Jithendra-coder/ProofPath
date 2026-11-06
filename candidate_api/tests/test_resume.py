import io
import zipfile

import pytest

from candidate_api.app.resume import (
    MAX_DOCX_UNCOMPRESSED_BYTES,
    ResumeProcessingError,
    extract_resume_text,
    suggest_resume_fields,
)


def docx(document_xml: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


def test_docx_text_and_heuristic_suggestions_are_editable_fields():
    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>Jordan Lee</w:t></w:r></w:p>
    <w:p><w:r><w:t>jordan@example.com</w:t></w:r></w:p>
    <w:p><w:r><w:t>Python FastAPI PostgreSQL Engineer with experience building useful systems and software products.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Education</w:t></w:r></w:p>
    <w:p><w:r><w:t>BSc Computer Science</w:t></w:r></w:p>
    </w:body></w:document>"""
    text, media_type = extract_resume_text("resume.docx", docx(xml))
    fields = suggest_resume_fields(text)
    assert media_type.endswith("wordprocessingml.document")
    assert fields["full_name"] == "Jordan Lee"
    assert fields["email"] == "jordan@example.com"
    assert fields["skills"] == ["PostgreSQL", "FastAPI", "Python"]
    assert fields["education"] == ["BSc Computer Science"]


@pytest.mark.parametrize(
    "filename, content",
    [("resume.txt", b"some readable text " * 10), ("resume.pdf", b"not a pdf " * 10), ("resume.docx", b"not a zip " * 10)],
)
def test_rejects_unsupported_or_malformed_files(filename, content):
    with pytest.raises(ResumeProcessingError):
        extract_resume_text(filename, content)


def test_rejects_docx_with_excessive_expanded_content():
    data = docx("<root>" + ("x" * (MAX_DOCX_UNCOMPRESSED_BYTES + 1)) + "</root>")
    with pytest.raises(ResumeProcessingError, match="expands"):
        extract_resume_text("resume.docx", data)

