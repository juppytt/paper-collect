from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_collect.db import import_dblp
from paper_collect.dblp import collect_sample_records, normalize_venues, summarize_dblp


SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<dblp>
<inproceedings key="conf/sp/Alpha22">
<author>Alice Alpha</author>
<author>Bob Beta</author>
<title>Security Paper.</title>
<pages>1-12</pages>
<year>2022</year>
<booktitle>SP</booktitle>
<ee>https://doi.org/10.1109/SP00000.2022.00001</ee>
<crossref>conf/sp/2022</crossref>
<url>db/conf/sp/sp2022.html#Alpha22</url>
</inproceedings>
<inproceedings key="conf/sp/Workshop22">
<author>Workshop Author</author>
<title>Workshop Paper.</title>
<year>2022</year>
<booktitle>SP Workshops</booktitle>
<crossref>conf/sp/2022w</crossref>
</inproceedings>
<inproceedings key="conf/uss/Gamma23">
<author>Gamma Author</author>
<title>Future Paper.</title>
<year>2023</year>
<booktitle>USENIX Security</booktitle>
<crossref>conf/uss/2023</crossref>
</inproceedings>
<inproceedings key="conf/ccs/Delta21">
<author>Delta Author</author>
<title>CCS Paper.</title>
<year>2021</year>
<booktitle>CCS</booktitle>
<crossref>conf/ccs/2021</crossref>
</inproceedings>
</dblp>
"""

SAMPLE_WITH_OLDER_XML = SAMPLE_XML.replace(
    b"</dblp>",
    b"""<inproceedings key="conf/ccs/Older12">
<author>Older Author</author>
<title>Older CCS Paper.</title>
<year>2012</year>
<booktitle>CCS</booktitle>
<crossref>conf/ccs/2012</crossref>
</inproceedings>
</dblp>""",
)


class DblpTests(unittest.TestCase):
    def write_sample(self, root: Path, content: bytes = SAMPLE_XML) -> Path:
        path = root / "dblp.xml.gz"
        with gzip.open(path, "wb") as output:
            output.write(content)
        return path

    def test_summary_filters_main_conference_crossrefs_and_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = self.write_sample(Path(tmp))
            summary = summarize_dblp(xml_path, venues=normalize_venues(["sp", "ccs", "security"]), max_year=2022)

        self.assertEqual(summary["counts"], {"ccs": 1, "security": 0, "sp": 1})
        self.assertEqual(summary["by_year"]["sp"], {2022: 1})
        self.assertEqual(summary["by_year"]["ccs"], {2021: 1})

    def test_min_year_filters_summary_sample_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_path = self.write_sample(root, SAMPLE_WITH_OLDER_XML)
            venues = normalize_venues(["ccs"])
            summary = summarize_dblp(xml_path, venues=venues, min_year=2013, max_year=2022)
            records = collect_sample_records(xml_path, venues=venues, min_year=2013, max_year=2022, limit=10)
            db_path = root / "papers.sqlite"
            result = import_dblp(xml_path, db_path, venues=venues, min_year=2013, max_year=2022)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("select year, title from papers order by year").fetchall()

        self.assertEqual(summary["year_filter"], {"min_year": 2013, "max_year": 2022})
        self.assertEqual(summary["counts"], {"ccs": 1})
        self.assertEqual([record.year for record in records], [2021])
        self.assertEqual(result["min_year"], 2013)
        self.assertEqual(rows, [(2021, "CCS Paper.")])

    def test_collect_sample_records_normalizes_url_and_doi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = self.write_sample(Path(tmp))
            records = collect_sample_records(xml_path, venues=normalize_venues(["sp"]), max_year=2022, limit=1)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.dblp_url, "https://dblp.org/db/conf/sp/sp2022.html#Alpha22")
        self.assertEqual(record.doi, "10.1109/SP00000.2022.00001")
        self.assertEqual(record.authors, ("Alice Alpha", "Bob Beta"))

    def test_import_dblp_writes_sqlite_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_path = self.write_sample(root)
            db_path = root / "papers.sqlite"
            result = import_dblp(xml_path, db_path, venues=normalize_venues(["sp", "ccs"]), max_year=2022)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "select venue, year, title, authors_json from papers order by venue, year"
                ).fetchall()

        self.assertEqual(result["inserted_or_updated"], 2)
        self.assertEqual(rows[0][:3], ("ccs", 2021, "CCS Paper."))
        self.assertEqual(json.loads(rows[1][3]), ["Alice Alpha", "Bob Beta"])


if __name__ == "__main__":
    unittest.main()
