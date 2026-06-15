from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Sequence


USER_AGENT = "paper-collect/0.1 (+https://github.com/local/paper-collect)"
PDF_CONTENT_TYPES = ("application/pdf", "application/x-pdf")
PDF_EXT_RE = re.compile(r"\.pdf(?:[?#].*)?$", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")
ABSTRACT_CONTAINER_HINTS = ("abstract", "paper-description")


@dataclass(frozen=True)
class PaperRow:
    id: int
    dblp_key: str
    venue: str
    year: int
    title: str
    doi: str | None
    ee: tuple[str, ...]
    abstract: str | None
    pdf_url: str | None
    pdf_path: str | None


@dataclass(frozen=True)
class DownloadOptions:
    targets: frozenset[str]
    output_dir: Path
    force: bool = False
    dry_run: bool = False
    limit: int | None = None
    timeout: float = 30.0
    sleep_seconds: float = 0.0


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


@dataclass(frozen=True)
class FetchResponse:
    url: str
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        charset = parse_charset(self.content_type) or "utf-8"
        return self.body.decode(charset, errors="replace")

    @property
    def is_pdf(self) -> bool:
        normalized = self.content_type.split(";", 1)[0].strip().lower()
        return normalized in PDF_CONTENT_TYPES or self.body.startswith(b"%PDF-")


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

    abstract = paper.abstract
    pdf_url = paper.pdf_url
    landing_pages: list[tuple[str, FetchResponse]] = []

    for url in candidate_urls(paper):
        if is_pdf_url(url):
            if need_pdf:
                pdf_url = pdf_url or url
            continue
        if need_abstract or (need_pdf and not pdf_url):
            response = fetch_url(url, timeout=options.timeout)
            if response.is_pdf:
                if need_pdf:
                    pdf_url = pdf_url or response.url
                continue
            landing_pages.append((url, response))
            page = PaperPageParser(response.text, base_url=response.url)
            if need_abstract and not abstract:
                abstract = page.abstract
            if need_pdf and not pdf_url:
                pdf_url = page.best_pdf_url
            if (not need_abstract or abstract) and (not need_pdf or pdf_url):
                break

    changed_abstract = False
    changed_pdf = False
    updates: dict[str, object] = {}
    if need_abstract and abstract and abstract != paper.abstract:
        changed_abstract = True
        updates["abstract"] = abstract

    if need_pdf and pdf_url:
        pdf_path = pdf_output_path(options.output_dir, paper, pdf_url)
        if not options.dry_run:
            if is_pdf_url(pdf_url):
                pdf_response = fetch_url(pdf_url, timeout=options.timeout)
            else:
                pdf_response = find_pdf_response(pdf_url, landing_pages, timeout=options.timeout)
            if not pdf_response.is_pdf:
                raise FetchError(f"PDF URL did not return a PDF: {pdf_url}")
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
        select id, dblp_key, venue, year, title, doi, ee_json, abstract, pdf_url, pdf_path
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
    )


def candidate_urls(paper: PaperRow) -> list[str]:
    urls: list[str] = []
    for url in paper.ee:
        if url.startswith("http://") or url.startswith("https://"):
            urls.append(url)
    if paper.doi:
        urls.append(f"https://doi.org/{paper.doi}")
    return dedupe(urls)


def find_pdf_response(
    pdf_url: str,
    landing_pages: Iterable[tuple[str, FetchResponse]],
    *,
    timeout: float,
) -> FetchResponse:
    for page_url, response in landing_pages:
        if response.url == pdf_url or page_url == pdf_url:
            return response
    return fetch_url(pdf_url, timeout=timeout)


def fetch_url(url: str, *, timeout: float) -> FetchResponse:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("content-type", "")
            return FetchResponse(url=response.geturl(), content_type=content_type, body=body)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"could not fetch {url}: {exc.reason}") from exc


class FetchError(RuntimeError):
    pass


class PaperPageParser(HTMLParser):
    def __init__(self, html_text: str, *, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self._capture_anchor: str | None = None
        self._anchor_parts: list[str] = []
        self._abstract_depth: int | None = None
        self._abstract_parts: list[str] = []
        self.feed(html_text)

    @property
    def abstract(self) -> str | None:
        for key in ("citation_abstract", "dc.description", "description", "og:description"):
            value = self.meta.get(key)
            if value and looks_like_abstract(value):
                return clean_text(value)
        captured = clean_text(" ".join(self._abstract_parts))
        if looks_like_abstract(captured):
            return captured
        return None

    @property
    def best_pdf_url(self) -> str | None:
        if "citation_pdf_url" in self.meta:
            return urllib.parse.urljoin(self.base_url, self.meta["citation_pdf_url"])
        ranked = sorted(self.links, key=score_pdf_link, reverse=True)
        return ranked[0][0] if ranked and score_pdf_link(ranked[0]) > 0 else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").strip().lower()
            content = attr.get("content", "").strip()
            if name and content:
                self.meta[name] = html.unescape(content)
        if tag == "a" and attr.get("href"):
            self._capture_anchor = urllib.parse.urljoin(self.base_url, attr["href"])
            self._anchor_parts = []
        if self._abstract_depth is not None:
            self._abstract_depth += 1
        elif tag in {"section", "div", "p"}:
            class_id = " ".join([attr.get("class", ""), attr.get("id", "")]).lower()
            if any(hint in class_id for hint in ABSTRACT_CONTAINER_HINTS):
                self._abstract_depth = 1

    def handle_data(self, data: str) -> None:
        if self._capture_anchor is not None:
            self._anchor_parts.append(data)
        if self._abstract_depth is not None:
            self._abstract_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_anchor is not None:
            self.links.append((self._capture_anchor, clean_text(" ".join(self._anchor_parts))))
            self._capture_anchor = None
            self._anchor_parts = []
        if self._abstract_depth is not None:
            self._abstract_depth -= 1
            if self._abstract_depth <= 0:
                self._abstract_depth = None


def score_pdf_link(link: tuple[str, str]) -> int:
    url, label = link
    url_lower = url.lower()
    label_lower = label.lower()
    score = 0
    if is_pdf_url(url):
        score += 5
    if "pdf" in label_lower:
        score += 4
    if label_lower in {"paper", "download", "full text", "full-text"}:
        score += 3
    if any(skip in url_lower for skip in ("slides", "presentation", "appendix", "poster")):
        score -= 4
    return score


def pdf_output_path(output_dir: Path, paper: PaperRow, pdf_url: str) -> Path:
    suffix = Path(urllib.parse.urlparse(pdf_url).path).suffix.lower()
    if suffix != ".pdf":
        suffix = ".pdf"
    key_hash = hashlib.sha1(paper.dblp_key.encode("utf-8")).hexdigest()[:10]
    slug = slugify(paper.title)[:80] or "paper"
    filename = f"{paper.year}-{paper.id}-{slug}-{key_hash}{suffix}"
    return output_dir / "pdf" / paper.venue / str(paper.year) / filename


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def is_pdf_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return bool(PDF_EXT_RE.search(path))


def clean_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", html.unescape(text)).strip()


def looks_like_abstract(text: str) -> bool:
    cleaned = clean_text(text)
    return len(cleaned) >= 120 and len(cleaned.split()) >= 20


def parse_charset(content_type: str) -> str | None:
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip('"')
    return None


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
