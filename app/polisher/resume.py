"""Resume loader — extracts text from PDF or plain text resume files."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_resume(path: Path) -> str:
    """Load resume text from a PDF or text file.

    Args:
        path: Path to the resume file (.pdf, .txt, .md)

    Returns:
        Extracted text content.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file type is unsupported.
    """
    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(path)
    elif suffix in (".txt", ".md", ".text"):
        return path.read_text(encoding="utf-8")
    else:
        raise ValueError(f"Unsupported resume format: {suffix} (use .pdf, .txt, or .md)")


def _extract_pdf(path: Path) -> str:
    """Extract text from a PDF using PyMuPDF (fitz)."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError("pymupdf is required for PDF parsing: pip install pymupdf")

    doc = fitz.open(str(path))
    pages = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            pages.append(text.strip())
    doc.close()

    full_text = "\n\n".join(pages)
    logger.info("Extracted %d chars from %d pages of %s", len(full_text), len(pages), path.name)
    return full_text
