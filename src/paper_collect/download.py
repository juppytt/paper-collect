from __future__ import annotations

import hashlib
import html
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
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
PDF_ABSTRACT_BOUNDARY_RE = re.compile(
    r"(?is)\babstract\s*[\-:\u2013\u2014]*\s*"
    r"(?P<body>.*?)"
    r"(?=\n\s*(?:index terms|keywords)\b"
    r"|\n\s*(?:I\.?\s*)?\n?\s*(?:I\s*N\s*T\s*R\s*O\s*D\s*U\s*C\s*T\s*I\s*O\s*N|introduction)\b"
    r"|\n\s*(?:1|I)\.?\s+introduction\b)"
)
PDF_ABSTRACT_START_RE = re.compile(r"(?is)\babstract\s*[\-:\u2013\u2014]*\s*")
PDF_ABSTRACT_MAX_PAGES = 2
NDSS_HTML_ABSTRACT_YEARS = frozenset({2010, 2011, 2012, 2013, 2014, 2015, 2017})
NDSS_PDF_ABSTRACT_YEARS = frozenset({2016, 2018, 2019, 2020, 2021, 2022})
CSDL_GRAPHQL_URL = "https://www.computer.org/csdl/api/v1/graphql"
CSDL_SP_GROUP_ID = "1000646"
CSDL_SP_PROCEEDINGS_QUERY = """
query ($groupId: String) {
  proceedings(groupId: $groupId) {
    id
    acronym
    title
    year
  }
}
"""
CSDL_SP_TOC_QUERY = """
query ($proceedingId: String!, $limitResults: Int, $skipResults: Int) {
  articlesByProceeding: articlesByProceedingWithPagination(
    proceedingId: $proceedingId
    limit: $limitResults
    skip: $skipResults
  ) {
    totalResults
    articleResults {
      id
      doi
      title
      fno
      idPrefix
      pages
      year
      authors {
        fullName
      }
    }
  }
}
"""
CSDL_SP_ARTICLE_QUERY = """
query ($articleId: String!) {
  article: articleById(articleId: $articleId) {
    id
    doi
    title
    abstract
    normalizedAbstract
    hasPdf
    fno
    idPrefix
    pages
    year
  }
}
"""


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
    text_path: str | None = None


@dataclass(frozen=True)
class DownloadOptions:
    targets: frozenset[str]
    output_dir: Path
    force: bool = False
    dry_run: bool = False
    limit: int | None = None
    timeout: float = 30.0
    sleep_seconds: float = 0.0
    chrome_path: str | None = None
    browser_headless: bool = False


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


@dataclass(frozen=True)
class CollectedArtifacts:
    abstract: str | None = None
    pdf_url: str | None = None
    pdf_response: FetchResponse | None = None
    local_pdf_path: Path | None = None


class VenueDownloader:
    def collect(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        raise NotImplementedError


class GenericDownloader(VenueDownloader):
    def collect(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        abstract = paper.abstract
        pdf_url = paper.pdf_url
        pdf_response: FetchResponse | None = None

        for url in candidate_urls(paper):
            if is_pdf_url(url):
                if need_pdf:
                    pdf_url = pdf_url or url
                continue
            if need_abstract or (need_pdf and not pdf_url):
                response = fetch_url(url, timeout=timeout)
                if response.is_pdf:
                    if need_pdf:
                        pdf_url = pdf_url or response.url
                        pdf_response = response
                    continue
                page = PaperPageParser(response.text, base_url=response.url)
                if need_abstract and not abstract:
                    abstract = page.abstract
                if need_pdf and not pdf_url:
                    pdf_url = page.best_pdf_url
                if (not need_abstract or abstract) and (not need_pdf or pdf_url):
                    break

        return CollectedArtifacts(abstract=abstract, pdf_url=pdf_url, pdf_response=pdf_response)


class NDSSDownloader(VenueDownloader):
    def collect(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        if paper.year in NDSS_HTML_ABSTRACT_YEARS:
            return self._collect_html_abstract_year(paper, need_abstract=need_abstract, need_pdf=need_pdf, timeout=timeout)
        if paper.year in NDSS_PDF_ABSTRACT_YEARS:
            return self._collect_pdf_abstract_year(paper, need_abstract=need_abstract, need_pdf=need_pdf, timeout=timeout)
        raise FetchError(f"unsupported NDSS year policy: {paper.year}")

    def _collect_html_abstract_year(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        abstract = paper.abstract
        pdf_url = paper.pdf_url

        for url in self._source_urls(paper):
            if is_pdf_url(url):
                if need_pdf:
                    pdf_url = pdf_url or url
                continue
            response = fetch_url(url, timeout=timeout)
            if response.is_pdf:
                if need_pdf:
                    pdf_url = pdf_url or response.url
                continue
            page = PaperPageParser(response.text, base_url=response.url)
            if need_abstract and not abstract:
                abstract = page.abstract
            if need_pdf and not pdf_url:
                pdf_url = page.best_pdf_url
            if (not need_abstract or abstract) and (not need_pdf or pdf_url):
                break

        return CollectedArtifacts(abstract=abstract, pdf_url=pdf_url)

    def _collect_pdf_abstract_year(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        pdf_url = paper.pdf_url
        pdf_response: FetchResponse | None = None

        for url in self._source_urls(paper):
            if is_pdf_url(url):
                pdf_url = pdf_url or url
                continue
            response = fetch_url(url, timeout=timeout)
            if response.is_pdf:
                pdf_url = pdf_url or response.url
                pdf_response = response
                continue
            page = PaperPageParser(response.text, base_url=response.url)
            if not pdf_url:
                pdf_url = page.best_pdf_url
            if pdf_url:
                break

        abstract = paper.abstract
        if need_abstract:
            if not pdf_url:
                raise FetchError(f"NDSS {paper.year} requires a PDF URL for abstract extraction")
            pdf_response = pdf_response or fetch_url(pdf_url, timeout=timeout)
            abstract = extract_abstract_from_pdf_response(pdf_response)

        return CollectedArtifacts(abstract=abstract, pdf_url=pdf_url, pdf_response=pdf_response)

    def _source_urls(self, paper: PaperRow) -> list[str]:
        urls = candidate_urls(paper)
        if paper.year == 2016:
            return dedupe(ndss_2016_current_pdf_url(url) for url in urls)
        return urls


class SPDownloader(VenueDownloader):
    def __init__(self) -> None:
        self._proceedings_by_year: dict[int, str] | None = None
        self._toc_by_year: dict[int, list[dict[str, object]]] = {}
        self._detail_by_article_id: dict[str, dict[str, object]] = {}

    def collect(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        article = self._article_for_paper(paper, timeout=timeout)
        detail: dict[str, object] = {}
        if need_abstract or need_pdf:
            detail = self._article_detail(str(article["id"]), timeout=timeout)

        abstract = paper.abstract
        if need_abstract:
            abstract = csdl_article_abstract(detail)

        pdf_url = paper.pdf_url
        if need_pdf and detail.get("hasPdf") is not False:
            pdf_url = csdl_pdf_url(str(article["id"]))

        return CollectedArtifacts(abstract=abstract, pdf_url=pdf_url)

    def _article_for_paper(self, paper: PaperRow, *, timeout: float) -> dict[str, object]:
        for article in self._toc_for_year(paper.year, timeout=timeout):
            if article_matches_paper(article, paper):
                return article
        raise FetchError(f"could not match S&P paper in CSDL TOC: year={paper.year} title={paper.title!r}")

    def _toc_for_year(self, year: int, *, timeout: float) -> list[dict[str, object]]:
        if year in self._toc_by_year:
            return self._toc_by_year[year]
        proceeding_id = self._proceeding_id_for_year(year, timeout=timeout)
        payload = post_graphql_json(
            CSDL_GRAPHQL_URL,
            query=CSDL_SP_TOC_QUERY,
            variables={"proceedingId": proceeding_id, "limitResults": 500, "skipResults": 0},
            timeout=timeout,
        )
        results = payload.get("data", {}).get("articlesByProceeding", {}).get("articleResults", [])
        if not isinstance(results, list):
            raise FetchError(f"unexpected CSDL TOC response for S&P {year}")
        self._toc_by_year[year] = [article for article in results if isinstance(article, dict)]
        return self._toc_by_year[year]

    def _proceeding_id_for_year(self, year: int, *, timeout: float) -> str:
        if self._proceedings_by_year is None:
            payload = post_graphql_json(
                CSDL_GRAPHQL_URL,
                query=CSDL_SP_PROCEEDINGS_QUERY,
                variables={"groupId": CSDL_SP_GROUP_ID},
                timeout=timeout,
            )
            proceedings = payload.get("data", {}).get("proceedings", [])
            if not isinstance(proceedings, list):
                raise FetchError("unexpected CSDL proceedings response for S&P")
            by_year: dict[int, str] = {}
            for proceeding in proceedings:
                if not isinstance(proceeding, dict):
                    continue
                if proceeding.get("acronym") != "sp":
                    continue
                proceeding_year = parse_int(proceeding.get("year"))
                proceeding_id = proceeding.get("id")
                if proceeding_year is not None and isinstance(proceeding_id, str):
                    by_year[proceeding_year] = proceeding_id
            self._proceedings_by_year = by_year
        proceeding_id = self._proceedings_by_year.get(year)
        if proceeding_id is None:
            raise FetchError(f"no CSDL S&P proceeding id for year={year}")
        return proceeding_id

    def _article_detail(self, article_id: str, *, timeout: float) -> dict[str, object]:
        if article_id in self._detail_by_article_id:
            return self._detail_by_article_id[article_id]
        payload = post_graphql_json(
            CSDL_GRAPHQL_URL,
            query=CSDL_SP_ARTICLE_QUERY,
            variables={"articleId": article_id},
            timeout=timeout,
        )
        detail = payload.get("data", {}).get("article")
        if not isinstance(detail, dict):
            raise FetchError(f"unexpected CSDL article response for article_id={article_id}")
        self._detail_by_article_id[article_id] = detail
        return detail


class CCSDownloader(VenueDownloader):
    def __init__(self) -> None:
        self._browser: AcmBrowserPdfDownloader | None = None

    def collect(
        self,
        paper: PaperRow,
        *,
        need_abstract: bool,
        need_pdf: bool,
        timeout: float,
        options: DownloadOptions | None = None,
    ) -> CollectedArtifacts:
        if not paper.doi:
            return CollectedArtifacts(abstract=paper.abstract, pdf_url=paper.pdf_url)

        pdf_url = acm_pdf_url(paper.doi)
        local_pdf_path = None
        if need_pdf and options is not None and not options.dry_run:
            if self._browser is None:
                self._browser = AcmBrowserPdfDownloader(
                    chrome_path=options.chrome_path,
                    headless=options.browser_headless,
                    timeout=timeout,
                )
            try:
                local_pdf_path = self._browser.download_pdf(pdf_url)
            except FetchError:
                self.close()
                raise

        return CollectedArtifacts(
            abstract=paper.abstract,
            pdf_url=pdf_url,
            local_pdf_path=local_pdf_path,
        )

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None


class AcmBrowserPdfDownloader:
    def __init__(self, *, chrome_path: str | None, headless: bool, timeout: float) -> None:
        self.chrome_path = chrome_path
        self.headless = headless
        self.timeout = timeout
        self._loop = None
        self._browser_cm = None
        self._browser = None
        self._tab = None

    def download_pdf(self, pdf_url: str) -> Path:
        self._ensure_started()
        return self._loop.run_until_complete(self._download_pdf(pdf_url))  # type: ignore[union-attr]

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            if self._browser_cm is not None:
                self._loop.run_until_complete(self._browser_cm.__aexit__(None, None, None))
        finally:
            self._loop.close()
            self._loop = None
            self._browser_cm = None
            self._browser = None
            self._tab = None

    def _ensure_started(self) -> None:
        if self._loop is not None:
            return
        try:
            import asyncio
            from pydoll.browser.chromium import Chrome
            from pydoll.browser.options import ChromiumOptions
        except ImportError as exc:
            raise FetchError("pydoll-python is required for CCS browser PDF downloads") from exc

        self._loop = asyncio.new_event_loop()
        options = ChromiumOptions()
        chrome_binary = resolve_chrome_path(self.chrome_path)
        if chrome_binary:
            options.binary_location = chrome_binary
        options.headless = self.headless
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--ignore-certificate-errors")
        options.prompt_for_download = False
        options.allow_automatic_downloads = True
        options.open_pdf_externally = True

        self._browser_cm = Chrome(options=options)
        self._browser = self._loop.run_until_complete(self._browser_cm.__aenter__())
        self._tab = self._loop.run_until_complete(self._browser.start())

    async def _download_pdf(self, pdf_url: str) -> Path:
        from pydoll.exceptions import DownloadTimeout

        assert self._browser is not None
        assert self._tab is not None

        download_dir = Path(tempfile.mkdtemp(prefix="paper-collect-acm-"))
        await self._browser.set_download_path(str(download_dir))
        try:
            async with self._tab.expect_download(keep_file_at=str(download_dir), timeout=self.timeout) as download:
                try:
                    await self._tab.go_to(pdf_url, timeout=self.timeout)
                except Exception:
                    pass
        except DownloadTimeout as exc:
            shutil.rmtree(download_dir, ignore_errors=True)
            raise FetchError(f"ACM browser PDF download timed out: {pdf_url}") from exc
        except Exception as exc:
            shutil.rmtree(download_dir, ignore_errors=True)
            raise FetchError(f"ACM browser PDF download failed: {pdf_url}: {exc}") from exc

        file_path = Path(download.file_path) if download.file_path else newest_file(download_dir)
        if file_path is None or not file_path.exists():
            shutil.rmtree(download_dir, ignore_errors=True)
            raise FetchError(f"ACM browser PDF download produced no file: {pdf_url}")
        first = file_path.read_bytes()[:5]
        if not first.startswith(b"%PDF-"):
            shutil.rmtree(download_dir, ignore_errors=True)
            raise FetchError(f"ACM browser download was not a PDF: {pdf_url}")
        return file_path


GENERIC_DOWNLOADER = GenericDownloader()
VENUE_DOWNLOADERS: dict[str, VenueDownloader] = {
    "ccs": CCSDownloader(),
    "ndss": NDSSDownloader(),
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


def candidate_urls(paper: PaperRow) -> list[str]:
    urls: list[str] = []
    for url in paper.ee:
        if url.startswith("http://") or url.startswith("https://"):
            urls.append(url)
    if paper.doi:
        urls.append(f"https://doi.org/{paper.doi}")
    return dedupe(urls)


def acm_pdf_url(doi: str) -> str:
    return f"https://dl.acm.org/doi/pdf/{doi}"


def resolve_chrome_path(explicit_path: str | None) -> str | None:
    if explicit_path:
        return explicit_path
    env_path = os.environ.get("PAPER_COLLECT_CHROME")
    if env_path:
        return env_path
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def newest_file(path: Path) -> Path | None:
    files = [candidate for candidate in path.iterdir() if candidate.is_file()]
    if not files:
        return None
    return max(files, key=lambda candidate: candidate.stat().st_mtime)


def close_downloaders() -> None:
    seen: set[int] = set()
    for downloader in [*VENUE_DOWNLOADERS.values(), GENERIC_DOWNLOADER]:
        if id(downloader) in seen:
            continue
        seen.add(id(downloader))
        close = getattr(downloader, "close", None)
        if callable(close):
            close()


def ndss_2016_current_pdf_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    old_prefix = "/ndss/wp-content/uploads/sites/25/"
    if parsed.netloc == "wp.internetsociety.org" and parsed.path.startswith(old_prefix):
        relative_path = parsed.path[len(old_prefix) :]
        return f"https://www.ndss-symposium.org/wp-content/uploads/{relative_path}"
    return url


def post_graphql_json(
    url: str,
    *,
    query: str,
    variables: dict[str, object],
    timeout: float,
) -> dict[str, object]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"could not fetch {url}: {exc.reason}") from exc
    except http.client.IncompleteRead as exc:
        raise FetchError(f"incomplete read from {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"invalid JSON from {url}") from exc
    if not isinstance(payload, dict):
        raise FetchError(f"unexpected JSON payload from {url}")
    errors = payload.get("errors")
    if errors:
        raise FetchError(f"GraphQL errors from {url}: {errors}")
    return payload


def article_matches_paper(article: dict[str, object], paper: PaperRow) -> bool:
    article_doi = normalize_doi(article.get("doi"))
    paper_doi = normalize_doi(paper.doi)
    if article_doi and paper_doi and article_doi == paper_doi:
        return True
    article_title = article.get("title")
    return isinstance(article_title, str) and normalize_title_key(article_title) == normalize_title_key(paper.title)


def csdl_article_abstract(detail: dict[str, object]) -> str | None:
    for key in ("abstract", "normalizedAbstract"):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            abstract = clean_markup_text(value)
            if looks_like_abstract(abstract):
                return abstract
    return None


def csdl_pdf_url(article_id: str) -> str:
    return f"https://www.computer.org/csdl/pds/api/csdl/proceedings/download-article/{article_id}/pdf"


def normalize_doi(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized.startswith("https://doi.org/"):
        normalized = normalized.removeprefix("https://doi.org/")
    if normalized.startswith("http://doi.org/"):
        normalized = normalized.removeprefix("http://doi.org/")
    if normalized.startswith("https://dx.doi.org/"):
        normalized = normalized.removeprefix("https://dx.doi.org/")
    if normalized.startswith("http://dx.doi.org/"):
        normalized = normalized.removeprefix("http://dx.doi.org/")
    return normalized or None


def parse_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def normalize_title_key(title: str) -> str:
    title = clean_markup_text(title).lower()
    return re.sub(r"[^a-z0-9]+", "", title)


def clean_markup_text(text: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", text))


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
    except http.client.IncompleteRead as exc:
        raise FetchError(f"incomplete read from {url}: {exc}") from exc


class FetchError(RuntimeError):
    pass


def extract_abstract_from_pdf_response(response: FetchResponse) -> str | None:
    if not response.is_pdf:
        raise FetchError(f"PDF abstract extraction did not receive a PDF: {response.url}")
    text = pdf_bytes_to_text(response.body, max_pages=PDF_ABSTRACT_MAX_PAGES)
    return extract_abstract_from_text(text)


def pdf_bytes_to_text(pdf_bytes: bytes, *, max_pages: int) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        raise FetchError("pdftotext is required for PDF abstract extraction")

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "paper.pdf"
        pdf_path.write_bytes(pdf_bytes)
        completed = subprocess.run(
            [pdftotext, "-f", "1", "-l", str(max_pages), str(pdf_path), "-"],
            check=False,
            capture_output=True,
            text=True,
        )
    if completed.returncode != 0:
        stderr = clean_text(completed.stderr)
        raise FetchError(f"pdftotext failed: {stderr or 'unknown error'}")
    return completed.stdout


def extract_abstract_from_text(text: str) -> str | None:
    match = PDF_ABSTRACT_BOUNDARY_RE.search(text)
    if match:
        candidate = clean_text(match.group("body"))
        return candidate if looks_like_abstract(candidate) else None

    start = PDF_ABSTRACT_START_RE.search(text)
    if start is None:
        return None
    candidate = clean_text(text[start.end() : start.end() + 3000])
    if not looks_like_abstract(candidate):
        return None
    return candidate


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
