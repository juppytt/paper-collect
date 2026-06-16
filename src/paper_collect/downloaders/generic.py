from __future__ import annotations

from .common import (
    CollectedArtifacts,
    DownloadOptions,
    FetchResponse,
    PaperPageParser,
    PaperRow,
    VenueDownloader,
    candidate_urls,
    fetch_url,
    is_pdf_url,
)


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
