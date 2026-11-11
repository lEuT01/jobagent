"""Read a CV (PDF, docx, txt, md) into plain text."""

from __future__ import annotations

from pathlib import Path


def read_cv(path: str | Path) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        try:
            import docx  # python-docx, optional
        except ImportError as e:
            raise RuntimeError("pip install python-docx to read .docx CVs") from e
        d = docx.Document(str(p))
        return "\n".join(par.text for par in d.paragraphs)
    return p.read_text(encoding="utf-8", errors="ignore")
