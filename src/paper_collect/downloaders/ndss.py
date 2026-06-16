from __future__ import annotations

import urllib.parse

from .common import (
    CollectedArtifacts,
    DownloadOptions,
    FetchError,
    FetchResponse,
    PaperPageParser,
    PaperRow,
    VenueDownloader,
    candidate_urls,
    dedupe,
    extract_abstract_from_pdf_response,
    fetch_url,
    is_pdf_url,
)


NDSS_HTML_ABSTRACT_YEARS = frozenset({2010, 2011, 2012, 2013, 2014, 2015, 2017})
NDSS_PDF_ABSTRACT_YEARS = frozenset({2016, 2018, 2019, 2020, 2021, 2022})


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


def ndss_2016_current_pdf_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    old_prefix = "/ndss/wp-content/uploads/sites/25/"
    if parsed.netloc == "wp.internetsociety.org" and parsed.path.startswith(old_prefix):
        relative_path = parsed.path[len(old_prefix) :]
        return f"https://www.ndss-symposium.org/wp-content/uploads/{relative_path}"
    return url
