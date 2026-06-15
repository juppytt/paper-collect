from __future__ import annotations

import json
import sqlite3
import tempfile
from unittest import mock
import unittest
from pathlib import Path

from paper_collect.download import DownloadOptions, PaperPageParser, PaperRow, pdf_output_path, process_paper, select_papers


class DownloadTests(unittest.TestCase):
    def test_select_papers_filters_manifest_rows(self) -> None:
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
                pdf_path text
            )
            """
        )
        conn.executemany(
            """
            insert into papers (id, dblp_key, venue, year, title, doi, ee_json)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "conf/ndss/A", "ndss", 2022, "Clarion", None, json.dumps(["https://example/a"])),
                (2, "conf/ccs/B", "ccs", 2021, "Other", None, json.dumps([])),
            ],
        )

        rows = select_papers(
            conn,
            venues={"ndss"},
            year=None,
            year_from=2020,
            year_to=2022,
            paper_ids=[],
            dblp_keys=[],
            title_contains="clar",
            limit=None,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].dblp_key, "conf/ndss/A")

    def test_paper_page_parser_extracts_abstract_and_pdf_link(self) -> None:
        abstract = " ".join(["This paper studies metadata hiding communication systems."] * 12)
        parser = PaperPageParser(
            f"""
            <html>
              <head><meta name="citation_abstract" content="{abstract}"></head>
              <body><a href="/wp-content/uploads/2022-141-paper.pdf">Paper</a></body>
            </html>
            """,
            base_url="https://www.ndss-symposium.org/ndss-paper/example/",
        )

        self.assertEqual(parser.abstract, abstract)
        self.assertEqual(
            parser.best_pdf_url,
            "https://www.ndss-symposium.org/wp-content/uploads/2022-141-paper.pdf",
        )

    def test_paper_page_parser_extracts_usenix_description_block(self) -> None:
        abstract = " ".join(["Facial liveness verification is widely used for authentication."] * 12)
        parser = PaperPageParser(
            f"""
            <html>
              <body>
                <div class="field field-name-field-paper-description field-type-text-long">
                  <div><p>{abstract}</p></div>
                </div>
                <a href="https://www.usenix.org/system/files/sec22-example.pdf">Paper PDF</a>
              </body>
            </html>
            """,
            base_url="https://www.usenix.org/conference/usenixsecurity22/presentation/example",
        )

        self.assertEqual(parser.abstract, abstract)
        self.assertEqual(parser.best_pdf_url, "https://www.usenix.org/system/files/sec22-example.pdf")

    def test_pdf_output_path_is_stable_and_grouped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper = select_papers_from_fixture()[0]
            path = pdf_output_path(Path(tmp), paper, "https://example.com/paper")

        self.assertIn("/pdf/sp/2022/", str(path))
        self.assertTrue(path.name.endswith(".pdf"))

    def test_abstract_target_skips_direct_pdf_url(self) -> None:
        paper = PaperRow(
            id=1,
            dblp_key="conf/uss/Direct10",
            venue="security",
            year=2010,
            title="Direct PDF Only",
            doi=None,
            ee=("https://example.com/direct.pdf",),
            abstract=None,
            pdf_url=None,
            pdf_path=None,
        )
        conn = sqlite3.connect(":memory:")
        options = DownloadOptions(targets=frozenset({"abstract"}), output_dir=Path("data/raw"))

        with mock.patch("paper_collect.download.fetch_url") as fetch_url:
            changed_abstract, changed_pdf = process_paper(conn, paper, options)

        fetch_url.assert_not_called()
        self.assertFalse(changed_abstract)
        self.assertFalse(changed_pdf)


def select_papers_from_fixture():
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
            pdf_path text
        )
        """
    )
    conn.execute(
        """
        insert into papers (id, dblp_key, venue, year, title, doi, ee_json)
        values (7, 'conf/sp/Example22', 'sp', 2022, 'An Example Paper: With Punctuation!', null, '[]')
        """
    )
    return select_papers(
        conn,
        venues=None,
        year=None,
        year_from=None,
        year_to=None,
        paper_ids=[],
        dblp_keys=[],
        title_contains=None,
        limit=None,
    )


if __name__ == "__main__":
    unittest.main()
