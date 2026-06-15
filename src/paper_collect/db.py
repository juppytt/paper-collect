from __future__ import annotations

import json
import sqlite3
from importlib.resources import files
from pathlib import Path
from typing import Iterable

from .dblp import DEFAULT_MAX_YEAR, DEFAULT_VENUES, DblpRecord, scan_dblp


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema = files("paper_collect").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()


def upsert_papers(conn: sqlite3.Connection, records: Iterable[DblpRecord]) -> int:
    rows = [record_to_row(record) for record in records]
    if not rows:
        return 0
    conn.executemany(
        """
        insert into papers (
            dblp_key,
            venue,
            year,
            title,
            authors_json,
            booktitle,
            pages,
            crossref,
            dblp_url,
            doi,
            ee_json,
            source
        )
        values (
            :dblp_key,
            :venue,
            :year,
            :title,
            :authors_json,
            :booktitle,
            :pages,
            :crossref,
            :dblp_url,
            :doi,
            :ee_json,
            'dblp'
        )
        on conflict(dblp_key) do update set
            venue = excluded.venue,
            year = excluded.year,
            title = excluded.title,
            authors_json = excluded.authors_json,
            booktitle = excluded.booktitle,
            pages = excluded.pages,
            crossref = excluded.crossref,
            dblp_url = excluded.dblp_url,
            doi = excluded.doi,
            ee_json = excluded.ee_json,
            updated_at = current_timestamp
        """,
        rows,
    )
    return len(rows)


def import_dblp(
    xml_path: Path,
    db_path: Path,
    venues: set[str] | None = None,
    max_year: int = DEFAULT_MAX_YEAR,
    batch_size: int = 1000,
    stop_after_matches: int | None = None,
    min_year: int | None = None,
) -> dict[str, object]:
    venues = set(venues or DEFAULT_VENUES)
    inserted = 0
    batch: list[DblpRecord] = []

    with connect(db_path) as conn:
        init_db(conn)

        def on_record(record: DblpRecord) -> None:
            nonlocal inserted, batch
            batch.append(record)
            if len(batch) >= batch_size:
                inserted += upsert_papers(conn, batch)
                conn.commit()
                batch = []

        stats = scan_dblp(
            xml_path,
            venues=venues,
            max_year=max_year,
            on_record=on_record,
            stop_after_matches=stop_after_matches,
            min_year=min_year,
        )
        inserted += upsert_papers(conn, batch)
        conn.commit()

    return {
        "db": str(db_path),
        "inserted_or_updated": inserted,
        "matched": stats["matched"],
        "venues": sorted(venues),
        "min_year": min_year,
        "max_year": max_year,
        "stopped_early": stats["stopped_early"],
    }


def record_to_row(record: DblpRecord) -> dict[str, object]:
    return {
        "dblp_key": record.dblp_key,
        "venue": record.venue,
        "year": record.year,
        "title": record.title,
        "authors_json": json.dumps(record.authors, ensure_ascii=False),
        "booktitle": record.booktitle,
        "pages": record.pages,
        "crossref": record.crossref,
        "dblp_url": record.dblp_url,
        "doi": record.doi,
        "ee_json": json.dumps(record.ee, ensure_ascii=False),
    }
