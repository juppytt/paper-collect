from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_collect.text import (
    ExtractTextOptions,
    extract_texts,
    normalize_extracted_text,
    pdf_file_to_text,
)


class TextExtractionTests(unittest.TestCase):
    def test_pdf_file_to_text_invokes_pdftotext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.5\n")
            completed = subprocess.CompletedProcess(
                args=["pdftotext"],
                returncode=0,
                stdout="Title\r\n\r\nBody text\f",
                stderr="",
            )

            with mock.patch("paper_collect.text.subprocess.run", return_value=completed) as run:
                text = pdf_file_to_text(pdf, pdftotext_path="/usr/bin/pdftotext", timeout=10.0)

        run.assert_called_once()
        self.assertEqual(text, "Title\n\nBody text\n")

    def test_extract_texts_writes_text_file_and_updates_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "raw" / "pdf" / "ccs" / "2022" / "paper.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.5\n")
            conn = make_conn(pdf_path=str(pdf))
            completed = subprocess.CompletedProcess(
                args=["pdftotext"],
                returncode=0,
                stdout="A paper title\n\nThis is extracted body text.\n",
                stderr="",
            )
            options = ExtractTextOptions(output_dir=root / "raw", pdftotext_path="/usr/bin/pdftotext")

            with mock.patch("paper_collect.text.subprocess.run", return_value=completed):
                result = extract_texts(
                    conn,
                    venues={"ccs"},
                    year=None,
                    year_from=2020,
                    year_to=2022,
                    paper_ids=[],
                    dblp_keys=[],
                    title_contains=None,
                    options=options,
                )

            self.assertEqual(result.extracted_texts, 1)
            row = conn.execute("select text_path from papers where id = 1").fetchone()
            text_path = Path(row[0])
            self.assertTrue(text_path.exists())
            self.assertEqual(text_path.read_text(encoding="utf-8"), "A paper title\n\nThis is extracted body text.\n")
            self.assertEqual(text_path.parent, root / "raw" / "text" / "ccs" / "2022")

    def test_extract_texts_can_delete_pdf_after_successful_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "raw" / "pdf" / "ccs" / "2022" / "paper.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.5\n")
            conn = make_conn(pdf_path=str(pdf))
            completed = subprocess.CompletedProcess(
                args=["pdftotext"],
                returncode=0,
                stdout="A paper title\n\nThis is extracted body text.\n",
                stderr="",
            )
            options = ExtractTextOptions(
                output_dir=root / "raw",
                pdftotext_path="/usr/bin/pdftotext",
                delete_pdfs=True,
            )

            with mock.patch("paper_collect.text.subprocess.run", return_value=completed):
                result = extract_texts(
                    conn,
                    venues={"ccs"},
                    year=None,
                    year_from=2020,
                    year_to=2022,
                    paper_ids=[],
                    dblp_keys=[],
                    title_contains=None,
                    options=options,
                )

            self.assertEqual(result.extracted_texts, 1)
            self.assertEqual(result.deleted_pdfs, 1)
            self.assertFalse(pdf.exists())
            row = conn.execute("select text_path, pdf_path from papers where id = 1").fetchone()
            self.assertTrue(Path(row[0]).exists())
            self.assertIsNone(row[1])

    def test_extract_texts_can_delete_existing_pdf_when_text_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "raw" / "pdf" / "ccs" / "2022" / "paper.pdf"
            text = root / "raw" / "text" / "ccs" / "2022" / "paper.txt"
            pdf.parent.mkdir(parents=True)
            text.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.5\n")
            text.write_text("Existing text\n", encoding="utf-8")
            conn = make_conn(pdf_path=str(pdf), text_path=str(text))
            options = ExtractTextOptions(output_dir=root / "raw", delete_pdfs=True)

            with mock.patch("paper_collect.text.subprocess.run") as run:
                result = extract_texts(
                    conn,
                    venues={"ccs"},
                    year=None,
                    year_from=None,
                    year_to=None,
                    paper_ids=[],
                    dblp_keys=[],
                    title_contains=None,
                    options=options,
                )

            run.assert_not_called()
            self.assertEqual(result.extracted_texts, 0)
            self.assertEqual(result.deleted_pdfs, 1)
            self.assertEqual(result.skipped, 0)
            self.assertFalse(pdf.exists())
            row = conn.execute("select pdf_path from papers where id = 1").fetchone()
            self.assertIsNone(row[0])

    def test_extract_texts_dry_run_counts_existing_pdf_delete_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "raw" / "pdf" / "ccs" / "2022" / "paper.pdf"
            text = root / "raw" / "text" / "ccs" / "2022" / "paper.txt"
            pdf.parent.mkdir(parents=True)
            text.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.5\n")
            text.write_text("Existing text\n", encoding="utf-8")
            conn = make_conn(pdf_path=str(pdf), text_path=str(text))
            options = ExtractTextOptions(output_dir=root / "raw", dry_run=True, delete_pdfs=True)

            result = extract_texts(
                conn,
                venues={"ccs"},
                year=None,
                year_from=None,
                year_to=None,
                paper_ids=[],
                dblp_keys=[],
                title_contains=None,
                options=options,
            )

            self.assertEqual(result.extracted_texts, 0)
            self.assertEqual(result.deleted_pdfs, 1)
            self.assertTrue(pdf.exists())
            row = conn.execute("select pdf_path from papers where id = 1").fetchone()
            self.assertEqual(row[0], str(pdf))

    def test_extract_texts_skips_rows_without_pdf_path(self) -> None:
        conn = make_conn(pdf_path=None)
        result = extract_texts(
            conn,
            venues={"ccs"},
            year=None,
            year_from=None,
            year_to=None,
            paper_ids=[],
            dblp_keys=[],
            title_contains=None,
            options=ExtractTextOptions(output_dir=Path("data/raw")),
        )

        self.assertEqual(result.extracted_texts, 0)
        self.assertEqual(result.deleted_pdfs, 0)
        self.assertEqual(result.skipped, 1)

    def test_normalize_extracted_text_strips_page_breaks_and_trailing_spaces(self) -> None:
        self.assertEqual(normalize_extracted_text("a  \r\nb\f\n\n"), "a\nb\n")


def make_conn(*, pdf_path: str | None, text_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        create table papers (
            id integer primary key,
            dblp_key text not null,
            venue text not null,
            year integer not null,
            title text not null,
            doi text,
            ee_json text not null,
            abstract text,
            pdf_url text,
            pdf_path text,
            text_path text,
            updated_at text
        )
        """
    )
    conn.execute(
        """
        insert into papers (id, dblp_key, venue, year, title, doi, ee_json, pdf_path, text_path)
        values (1, 'conf/ccs/Example22', 'ccs', 2022, 'Example CCS Paper', '10.1145/example', '[]', ?, ?)
        """,
        (pdf_path, text_path),
    )
    return conn


if __name__ == "__main__":
    unittest.main()
