from __future__ import annotations

import re

from .common import (
    CollectedArtifacts,
    DownloadOptions,
    FetchError,
    PaperRow,
    VenueDownloader,
    clean_markup_text,
    looks_like_abstract,
    post_graphql_json,
)


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
