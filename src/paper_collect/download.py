from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .downloaders.ccs import CCSDownloader, acm_pdf_url
from .downloaders.common import (
    CollectedArtifacts,
    DownloadOptions,
    FetchError,
    FetchResponse,
    PaperPageParser,
    PaperRow,
    VenueDownloader,
    candidate_urls,
    clean_text,
    dedupe,
    extract_abstract_from_pdf_response,
    extract_abstract_from_text,
    fetch_url,
    is_pdf_url,
    post_graphql_json,
    slugify,
)
from .downloaders.generic import GenericDownloader
from .downloaders.ndss import NDSSDownloader, ndss_2016_current_pdf_url
from .downloaders.security import SecurityDownloader
from .downloaders.sp import SPDownloader, csdl_pdf_url


@dataclass(frozen=True)
class DownloadResult:
    selected: int
    updated_abstracts: int
    downloaded_pdfs: int
    skipped: int
    failed: int

    def to_dict(self) -> dict[str, int]:
        return {
            "selected": self.selected,
            "updated_abstracts": self.updated_abstracts,
            "downloaded_pdfs": self.downloaded_pdfs,
            "skipped": self.skipped,
            "failed": self.failed,
        }


GENERIC_DOWNLOADER = GenericDownloader()
VENUE_DOWNLOADERS: dict[str, VenueDownloader] = {
    "ccs": CCSDownloader(),
    "ndss": NDSSDownloader(),
    "security": SecurityDownloader(),
    "sp": SPDownloader(),
}


def downloader_for(venue: str) -> VenueDownloader:
    return VENUE_DOWNLOADERS.get(venue, GENERIC_DOWNLOADER)


def download_papers(
    conn: sqlite3.Connection,
    *,
    venues: set[str] | None,
    year: int | None,
    year_from: int | None,
    year_to: int | None,
    paper_ids: Sequence[int],
    dblp_keys: Sequence[str],
    title_contains: str | None,
    options: DownloadOptions,
) -> DownloadResult:
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
    updated_abstracts = 0
    downloaded_pdfs = 0
    skipped = 0
    failed = 0

    try:
        for index, paper in enumerate(rows, start=1):
            try:
                changed_abstract, changed_pdf = process_paper(conn, paper, options)
                updated_abstracts += int(changed_abstract)
                downloaded_pdfs += int(changed_pdf)
                if not changed_abstract and not changed_pdf:
                    skipped += 1
                if options.sleep_seconds > 0 and index < len(rows):
                    time.sleep(options.sleep_seconds)
            except FetchError as exc:
                failed += 1
                print(f"failed paper_id={paper.id} venue={paper.venue} year={paper.year}: {exc}")
    finally:
        close_downloaders()

    return DownloadResult(
        selected=len(rows),
        updated_abstracts=updated_abstracts,
        downloaded_pdfs=downloaded_pdfs,
        skipped=skipped,
        failed=failed,
    )


def process_paper(conn: sqlite3.Connection, paper: PaperRow, options: DownloadOptions) -> tuple[bool, bool]:
    need_abstract = "abstract" in options.targets and (options.force or not paper.abstract)
    need_pdf = "pdf" in options.targets and (options.force or not paper.pdf_path)
    if not need_abstract and not need_pdf:
        return False, False

    artifacts = downloader_for(paper.venue).collect(
        paper,
        need_abstract=need_abstract,
        need_pdf=need_pdf,
        timeout=options.timeout,
        options=options,
    )
    abstract = artifacts.abstract
    pdf_url = artifacts.pdf_url
    pdf_response = artifacts.pdf_response
    local_pdf_path = artifacts.local_pdf_path

    changed_abstract = False
    changed_pdf = False
    updates: dict[str, object] = {}
    if need_abstract and abstract and abstract != paper.abstract:
        changed_abstract = True
        updates["abstract"] = abstract

    if need_pdf and pdf_url:
        pdf_path = pdf_output_path(options.output_dir, paper, pdf_url)
        if not options.dry_run:
            if local_pdf_path is not None:
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(local_pdf_path), pdf_path)
                shutil.rmtree(local_pdf_path.parent, ignore_errors=True)
                pdf_response = None
            elif pdf_response is not None and pdf_response.is_pdf:
                pass
            elif is_pdf_url(pdf_url):
                pdf_response = fetch_url(pdf_url, timeout=options.timeout)
            else:
                pdf_response = fetch_url(pdf_url, timeout=options.timeout)
            if local_pdf_path is None and (pdf_response is None or not pdf_response.is_pdf):
                raise FetchError(f"PDF URL did not return a PDF: {pdf_url}")
            if pdf_response is not None:
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                pdf_path.write_bytes(pdf_response.body)
        changed_pdf = True
        updates["pdf_url"] = pdf_url
        updates["pdf_path"] = str(pdf_path)

    if updates and not options.dry_run:
        set_clause = ", ".join(f"{key} = :{key}" for key in updates)
        updates["id"] = paper.id
        conn.execute(
            f"update papers set {set_clause}, updated_at = current_timestamp where id = :id",
            updates,
        )
        conn.commit()

    return changed_abstract, changed_pdf


def select_papers(
    conn: sqlite3.Connection,
    *,
    venues: set[str] | None,
    year: int | None,
    year_from: int | None,
    year_to: int | None,
    paper_ids: Sequence[int],
    dblp_keys: Sequence[str],
    title_contains: str | None,
    limit: int | None,
) -> list[PaperRow]:
    clauses: list[str] = []
    params: dict[str, object] = {}

    if venues:
        placeholders = []
        for index, venue in enumerate(sorted(venues)):
            key = f"venue_{index}"
            placeholders.append(f":{key}")
            params[key] = venue
        clauses.append(f"venue in ({', '.join(placeholders)})")
    if year is not None:
        clauses.append("year = :year")
        params["year"] = year
    if year_from is not None:
        clauses.append("year >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        clauses.append("year <= :year_to")
        params["year_to"] = year_to
    if paper_ids:
        placeholders = []
        for index, paper_id in enumerate(paper_ids):
            key = f"paper_id_{index}"
            placeholders.append(f":{key}")
            params[key] = paper_id
        clauses.append(f"id in ({', '.join(placeholders)})")
    if dblp_keys:
        placeholders = []
        for index, dblp_key in enumerate(dblp_keys):
            key = f"dblp_key_{index}"
            placeholders.append(f":{key}")
            params[key] = dblp_key
        clauses.append(f"dblp_key in ({', '.join(placeholders)})")
    if title_contains:
        clauses.append("lower(title) like :title_contains")
        params["title_contains"] = f"%{title_contains.lower()}%"

    where = " where " + " and ".join(clauses) if clauses else ""
    sql = f"""
        select id, dblp_key, venue, year, title, doi, ee_json, abstract, pdf_url, pdf_path, text_path
        from papers
        {where}
        order by venue, year desc, title
    """
    if limit is not None:
        sql += " limit :limit"
        params["limit"] = limit

    rows = conn.execute(sql, params).fetchall()
    return [row_to_paper(row) for row in rows]


def row_to_paper(row: sqlite3.Row | tuple[object, ...]) -> PaperRow:
    return PaperRow(
        id=int(row[0]),
        dblp_key=str(row[1]),
        venue=str(row[2]),
        year=int(row[3]),
        title=str(row[4]),
        doi=str(row[5]) if row[5] else None,
        ee=tuple(json.loads(str(row[6] or "[]"))),
        abstract=str(row[7]) if row[7] else None,
        pdf_url=str(row[8]) if row[8] else None,
        pdf_path=str(row[9]) if row[9] else None,
        text_path=str(row[10]) if len(row) > 10 and row[10] else None,
    )


def pdf_output_path(output_dir: Path, paper: PaperRow, pdf_url: str) -> Path:
    suffix = Path(urllib.parse.urlparse(pdf_url).path).suffix.lower()
    if suffix != ".pdf":
        suffix = ".pdf"
    key_hash = hashlib.sha1(paper.dblp_key.encode("utf-8")).hexdigest()[:10]
    slug = slugify(paper.title)[:80] or "paper"
    filename = f"{paper.year}-{paper.id}-{slug}-{key_hash}{suffix}"
    return output_dir / "pdf" / paper.venue / str(paper.year) / filename


def close_downloaders() -> None:
    seen: set[int] = set()
    for downloader in [*VENUE_DOWNLOADERS.values(), GENERIC_DOWNLOADER]:
        if id(downloader) in seen:
            continue
        seen.add(id(downloader))
        close = getattr(downloader, "close", None)
        if callable(close):
            close()
