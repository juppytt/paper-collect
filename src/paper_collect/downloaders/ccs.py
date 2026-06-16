from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .common import CollectedArtifacts, DownloadOptions, FetchError, PaperRow, VenueDownloader


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
