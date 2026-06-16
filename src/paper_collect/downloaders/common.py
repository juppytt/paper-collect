from __future__ import annotations

import html
import http.client
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


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


class FetchError(RuntimeError):
    pass


def candidate_urls(paper: PaperRow) -> list[str]:
    urls: list[str] = []
    for url in paper.ee:
        if url.startswith("http://") or url.startswith("https://"):
            urls.append(url)
    if paper.doi:
        urls.append(f"https://doi.org/{paper.doi}")
    return dedupe(urls)


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


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def is_pdf_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return bool(PDF_EXT_RE.search(path))


def clean_markup_text(text: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", text))


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
