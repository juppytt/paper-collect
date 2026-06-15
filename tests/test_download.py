from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_collect.download import (
    DownloadOptions,
    FetchResponse,
    PaperPageParser,
    PaperRow,
    SPDownloader,
    csdl_pdf_url,
    extract_abstract_from_text,
    ndss_2016_current_pdf_url,
    pdf_output_path,
    process_paper,
    select_papers,
)


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

    def test_ndss_pdf_abstract_year_extracts_abstract_from_pdf_text(self) -> None:
        abstract = " ".join(["This paper studies cellular baseband firmware analysis."] * 12)
        paper = PaperRow(
            id=1,
            dblp_key="conf/ndss/Example22",
            venue="ndss",
            year=2022,
            title="Example NDSS Paper",
            doi=None,
            ee=("https://www.ndss-symposium.org/ndss-paper/example/",),
            abstract=None,
            pdf_url=None,
            pdf_path=None,
        )
        conn = sqlite3.connect(":memory:")
        options = DownloadOptions(targets=frozenset({"abstract", "pdf"}), output_dir=Path("data/raw"), dry_run=True)
        landing = FetchResponse(
            url="https://www.ndss-symposium.org/ndss-paper/example/",
            content_type="text/html; charset=UTF-8",
            body=b'<html><body><a href="/wp-content/uploads/2022-136-paper.pdf">Paper</a></body></html>',
        )
        pdf = FetchResponse(
            url="https://www.ndss-symposium.org/wp-content/uploads/2022-136-paper.pdf",
            content_type="application/pdf",
            body=b"%PDF-1.5",
        )

        with mock.patch("paper_collect.download.fetch_url", side_effect=[landing, pdf]):
            with mock.patch("paper_collect.download.extract_abstract_from_pdf_response", return_value=abstract):
                changed_abstract, changed_pdf = process_paper(conn, paper, options)

        self.assertTrue(changed_abstract)
        self.assertTrue(changed_pdf)

    def test_ndss_html_abstract_year_uses_page_abstract(self) -> None:
        abstract = " ".join(["This paper studies network security measurement."] * 12)
        paper = PaperRow(
            id=1,
            dblp_key="conf/ndss/Example17",
            venue="ndss",
            year=2017,
            title="Example NDSS Paper",
            doi=None,
            ee=("https://www.ndss-symposium.org/ndss2017/example/",),
            abstract=None,
            pdf_url=None,
            pdf_path=None,
        )
        conn = sqlite3.connect(":memory:")
        options = DownloadOptions(targets=frozenset({"abstract", "pdf"}), output_dir=Path("data/raw"), dry_run=True)
        landing = FetchResponse(
            url="https://www.ndss-symposium.org/ndss2017/example/",
            content_type="text/html; charset=UTF-8",
            body=f"""
            <html>
              <body>
                <div class="abstract">{abstract}</div>
                <a href="/wp-content/uploads/2017/example.pdf">Paper</a>
              </body>
            </html>
            """.encode(),
        )

        with mock.patch("paper_collect.download.fetch_url", return_value=landing):
            with mock.patch("paper_collect.download.extract_abstract_from_pdf_response") as extract_pdf:
                changed_abstract, changed_pdf = process_paper(conn, paper, options)

        extract_pdf.assert_not_called()
        self.assertTrue(changed_abstract)
        self.assertTrue(changed_pdf)

    def test_ndss_2016_policy_uses_current_pdf_host(self) -> None:
        old_url = (
            "http://wp.internetsociety.org/ndss/wp-content/uploads/sites/25/2017/09/"
            "simple-generic-attack-text-captchas.pdf"
        )
        current_url = (
            "https://www.ndss-symposium.org/wp-content/uploads/2017/09/"
            "simple-generic-attack-text-captchas.pdf"
        )
        self.assertEqual(ndss_2016_current_pdf_url(old_url), current_url)

        paper = PaperRow(
            id=1,
            dblp_key="conf/ndss/Example16",
            venue="ndss",
            year=2016,
            title="Example NDSS Paper",
            doi=None,
            ee=(old_url,),
            abstract=None,
            pdf_url=None,
            pdf_path=None,
        )
        conn = sqlite3.connect(":memory:")
        options = DownloadOptions(targets=frozenset({"abstract", "pdf"}), output_dir=Path("data/raw"), dry_run=True)
        abstract = " ".join(["This paper studies text captcha attacks."] * 12)
        pdf = FetchResponse(url=current_url, content_type="application/pdf", body=b"%PDF-1.5")

        with mock.patch("paper_collect.download.fetch_url", return_value=pdf) as fetch_url:
            with mock.patch("paper_collect.download.extract_abstract_from_pdf_response", return_value=abstract):
                changed_abstract, changed_pdf = process_paper(conn, paper, options)

        fetch_url.assert_called_once_with(current_url, timeout=30.0)
        self.assertTrue(changed_abstract)
        self.assertTrue(changed_pdf)

    def test_sp_downloader_uses_csdl_graphql(self) -> None:
        abstract = " ".join(["This paper studies protocol state machine extraction."] * 12)
        paper = PaperRow(
            id=1,
            dblp_key="conf/sp/Example22",
            venue="sp",
            year=2022,
            title="Automated Attack Synthesis by Extracting Finite State Machines from Protocol Specification Documents.",
            doi="10.1109/SP46214.2022.9833673",
            ee=("https://doi.org/10.1109/SP46214.2022.9833673",),
            abstract=None,
            pdf_url=None,
            pdf_path=None,
        )
        proceedings = {
            "data": {
                "proceedings": [
                    {"id": "1FlQurJZBuw", "acronym": "sp", "title": "SP 2022", "year": 2022},
                ]
            }
        }
        toc = {
            "data": {
                "articlesByProceeding": {
                    "articleResults": [
                        {
                            "id": "1FlQIbn9p7y",
                            "doi": "10.1109/SP46214.2022.9833673",
                            "title": paper.title.rstrip("."),
                            "year": 2022,
                        }
                    ]
                }
            }
        }
        detail = {
            "data": {
                "article": {
                    "id": "1FlQIbn9p7y",
                    "doi": "10.1109/SP46214.2022.9833673",
                    "title": paper.title.rstrip("."),
                    "abstract": f"<p>{abstract}</p>",
                    "hasPdf": True,
                }
            }
        }

        with mock.patch("paper_collect.download.post_graphql_json", side_effect=[proceedings, toc, detail]):
            artifacts = SPDownloader().collect(paper, need_abstract=True, need_pdf=True, timeout=30.0)

        self.assertEqual(artifacts.abstract, abstract)
        self.assertEqual(artifacts.pdf_url, csdl_pdf_url("1FlQIbn9p7y"))

    def test_extract_abstract_from_pdf_text(self) -> None:
        abstract = " ".join(["This paper studies anonymous communication systems."] * 12)
        text = f"""
        Clarion: Anonymous Communication

        Abstract--{abstract}

        I.

        I NTRODUCTION

        This section should not be included.
        """

        self.assertEqual(extract_abstract_from_text(text), abstract)


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
