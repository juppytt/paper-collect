from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .download import PaperRow, select_papers, slugify


@dataclass(frozen=True)
class ExtractTextOptions:
    output_dir: Path
    force: bool = False
    dry_run: bool = False
    limit: int | None = None
    timeout: float = 120.0
    pdftotext_path: str | None = None
    min_chars: int = 1


@dataclass(frozen=True)
class ExtractTextResult:
    selected: int
    extracted_texts: int
    skipped: int
    failed: int

    def to_dict(self) -> dict[str, int]:
        return {
            "selected": self.selected,
            "extracted_texts": self.extracted_texts,
            "skipped": self.skipped,
            "failed": self.failed,
        }


def extract_texts(
    conn: sqlite3.Connection,
    *,
    venues: set[str] | None,
    year: int | None,
    year_from: int | None,
    year_to: int | None,
    paper_ids: Sequence[int],
    dblp_keys: Sequence[str],
    title_contains: str | None,
    options: ExtractTextOptions,
) -> ExtractTextResult:
    rows = select_papers(
        conn,
        venues=venues,
        year=year,
        year_from=year_from,
        year_to=year_to,
        paper_ids=paper_ids,
        dblp_keys=dblp_keys,
        title_contains=title_contains,
        limit=options.limit,
    )
    extracted_texts = 0
    skipped = 0
    failed = 0

    for paper in rows:
        try:
            changed = process_text(conn, paper, options)
            if changed:
                extracted_texts += 1
            else:
                skipped += 1
        except TextExtractionError as exc:
            failed += 1
            print(f"failed paper_id={paper.id} venue={paper.venue} year={paper.year}: {exc}")

    return ExtractTextResult(
        selected=len(rows),
        extracted_texts=extracted_texts,
        skipped=skipped,
        failed=failed,
    )


def process_text(conn: sqlite3.Connection, paper: PaperRow, options: ExtractTextOptions) -> bool:
    if not paper.pdf_path:
        return False
    if paper.text_path and not options.force:
        return False

    pdf_path = Path(paper.pdf_path)
    if not pdf_path.exists():
        raise TextExtractionError(f"PDF path does not exist: {pdf_path}")

    text_path = text_output_path(options.output_dir, paper, pdf_path)
    if options.dry_run:
        return True

    text = pdf_file_to_text(pdf_path, pdftotext_path=options.pdftotext_path, timeout=options.timeout)
    if len(text.strip()) < options.min_chars:
        raise TextExtractionError(f"extracted text below minimum length: {pdf_path}")

    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")
    conn.execute(
        "update papers set text_path = :text_path, updated_at = current_timestamp where id = :id",
        {"id": paper.id, "text_path": str(text_path)},
    )
    conn.commit()
    return True


def pdf_file_to_text(pdf_path: Path, *, pdftotext_path: str | None = None, timeout: float = 120.0) -> str:
    pdftotext = pdftotext_path or shutil.which("pdftotext")
    if pdftotext is None:
        raise TextExtractionError("pdftotext is required for PDF text extraction")

    completed = subprocess.run(
        [pdftotext, "-enc", "UTF-8", "-nopgbrk", str(pdf_path), "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        stderr = clean_process_text(completed.stderr)
        raise TextExtractionError(f"pdftotext failed: {stderr or 'unknown error'}")
    return normalize_extracted_text(completed.stdout)


def text_output_path(output_dir: Path, paper: PaperRow, pdf_path: Path) -> Path:
    stem = pdf_path.stem or slugify(paper.title)[:80] or "paper"
    return output_dir / "text" / paper.venue / str(paper.year) / f"{stem}.txt"


def normalize_extracted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    normalized = "\n".join(lines).strip()
    return f"{normalized}\n" if normalized else ""


def clean_process_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class TextExtractionError(RuntimeError):
    pass
