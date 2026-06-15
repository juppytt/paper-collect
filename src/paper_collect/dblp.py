from __future__ import annotations

import gzip
import re
import xml.sax
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.sax import InputSource
from xml.sax.handler import ContentHandler, EntityResolver

DEFAULT_MAX_YEAR = 2022
DEFAULT_VENUES = frozenset({"sp", "ccs", "security", "ndss"})

VENUE_CROSSREF_PREFIXES = {
    "sp": "conf/sp",
    "ccs": "conf/ccs",
    "security": "conf/uss",
    "ndss": "conf/ndss",
}

VENUE_ALIASES = {
    "sp": "sp",
    "ieee-sp": "sp",
    "ieee_sp": "sp",
    "ccs": "ccs",
    "security": "security",
    "usenix": "security",
    "usenix-security": "security",
    "usenix_security": "security",
    "uss": "security",
    "ndss": "ndss",
}

FIELD_NAMES = frozenset({"author", "title", "booktitle", "pages", "year", "crossref", "url", "ee"})
DOI_URL_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\..+)$", re.IGNORECASE)


@dataclass(frozen=True)
class DblpRecord:
    dblp_key: str
    venue: str
    year: int
    title: str
    authors: tuple[str, ...]
    booktitle: str | None
    pages: str | None
    crossref: str
    dblp_url: str | None
    doi: str | None
    ee: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dblp_key": self.dblp_key,
            "venue": self.venue,
            "year": self.year,
            "title": self.title,
            "authors": list(self.authors),
            "booktitle": self.booktitle,
            "pages": self.pages,
            "crossref": self.crossref,
            "dblp_url": self.dblp_url,
            "doi": self.doi,
            "ee": list(self.ee),
        }


class StopDblpScan(Exception):
    pass


class LocalDblpDtdResolver(EntityResolver):
    def __init__(self, xml_path: Path):
        self.dtd_path = xml_path.with_name("dblp.dtd")

    def resolveEntity(self, publicId: str | None, systemId: str | None) -> InputSource | str:
        if systemId and Path(systemId).name == "dblp.dtd" and self.dtd_path.exists():
            source = InputSource()
            source.setSystemId(str(self.dtd_path))
            source.setByteStream(self.dtd_path.open("rb"))
            return source
        return systemId or ""


class DblpHandler(ContentHandler):
    def __init__(
        self,
        venues: set[str],
        max_year: int,
        min_year: int | None = None,
        on_record: Callable[[DblpRecord], None] | None = None,
        stop_after_matches: int | None = None,
    ):
        super().__init__()
        self.venues = venues
        self.max_year = max_year
        self.min_year = min_year
        self.on_record = on_record
        self.stop_after_matches = stop_after_matches
        self.matched = 0
        self.seen_inproceedings = 0
        self.stopped_early = False

        self._inside_inproceedings = False
        self._record_key: str | None = None
        self._fields: dict[str, list[str]] = {}
        self._current_field: str | None = None
        self._text_parts: list[str] = []

    def startElement(self, name: str, attrs: xml.sax.xmlreader.AttributesImpl) -> None:
        if name == "inproceedings":
            self._inside_inproceedings = True
            self._record_key = attrs.get("key")
            self._fields = {}
            return

        if self._inside_inproceedings and self._current_field is None and name in FIELD_NAMES:
            self._current_field = name
            self._text_parts = []

    def characters(self, content: str) -> None:
        if self._current_field is not None:
            self._text_parts.append(content)

    def endElement(self, name: str) -> None:
        if self._inside_inproceedings and name == self._current_field:
            text = normalize_text("".join(self._text_parts))
            if text:
                self._fields.setdefault(name, []).append(text)
            self._current_field = None
            self._text_parts = []
            return

        if name == "inproceedings" and self._inside_inproceedings:
            self.seen_inproceedings += 1
            record = build_record(self._record_key, self._fields, self.venues, self.max_year, self.min_year)
            self._inside_inproceedings = False
            self._record_key = None
            self._fields = {}
            if record is None:
                return
            if self.on_record is not None:
                self.on_record(record)
            self.matched += 1
            if self.stop_after_matches is not None and self.matched >= self.stop_after_matches:
                self.stopped_early = True
                raise StopDblpScan


def normalize_venues(venues: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    normalized: set[str] = set()
    for venue in venues:
        key = venue.strip().lower()
        if key not in VENUE_ALIASES:
            allowed = ", ".join(sorted(VENUE_ALIASES))
            raise ValueError(f"unknown venue {venue!r}; expected one of: {allowed}")
        normalized.add(VENUE_ALIASES[key])
    return normalized


def scan_dblp(
    xml_path: Path,
    venues: set[str] | None = None,
    max_year: int = DEFAULT_MAX_YEAR,
    on_record: Callable[[DblpRecord], None] | None = None,
    stop_after_matches: int | None = None,
    min_year: int | None = None,
) -> dict[str, object]:
    xml_path = Path(xml_path)
    venues = set(venues or DEFAULT_VENUES)
    parser = xml.sax.make_parser()
    handler = DblpHandler(
        venues=venues,
        max_year=max_year,
        min_year=min_year,
        on_record=on_record,
        stop_after_matches=stop_after_matches,
    )
    parser.setContentHandler(handler)
    parser.setEntityResolver(LocalDblpDtdResolver(xml_path))

    try:
        with open_xml_stream(xml_path) as stream:
            parser.parse(stream)
    except StopDblpScan:
        pass

    return {
        "matched": handler.matched,
        "seen_inproceedings": handler.seen_inproceedings,
        "stopped_early": handler.stopped_early,
    }


def summarize_dblp(
    xml_path: Path,
    venues: set[str] | None = None,
    max_year: int = DEFAULT_MAX_YEAR,
    stop_after_matches: int | None = None,
    min_year: int | None = None,
) -> dict[str, object]:
    counts: dict[str, int] = {venue: 0 for venue in sorted(venues or DEFAULT_VENUES)}
    min_seen_year: dict[str, int | None] = {venue: None for venue in counts}
    max_seen_year: dict[str, int | None] = {venue: None for venue in counts}
    by_year: dict[str, dict[int, int]] = {venue: {} for venue in counts}

    def on_record(record: DblpRecord) -> None:
        counts[record.venue] += 1
        min_seen_year[record.venue] = (
            record.year if min_seen_year[record.venue] is None else min(min_seen_year[record.venue], record.year)
        )
        max_seen_year[record.venue] = (
            record.year if max_seen_year[record.venue] is None else max(max_seen_year[record.venue], record.year)
        )
        by_year[record.venue][record.year] = by_year[record.venue].get(record.year, 0) + 1

    stats = scan_dblp(
        xml_path,
        venues=set(counts),
        max_year=max_year,
        on_record=on_record,
        stop_after_matches=stop_after_matches,
        min_year=min_year,
    )
    return {
        "year_filter": {"min_year": min_year, "max_year": max_year},
        "max_year": max_year,
        "counts": counts,
        "min_year": min_seen_year,
        "max_seen_year": max_seen_year,
        "by_year": {venue: dict(sorted(years.items())) for venue, years in by_year.items()},
        "scan": stats,
    }


def collect_sample_records(
    xml_path: Path,
    venues: set[str] | None = None,
    max_year: int = DEFAULT_MAX_YEAR,
    limit: int = 10,
    min_year: int | None = None,
) -> list[DblpRecord]:
    records: list[DblpRecord] = []

    def on_record(record: DblpRecord) -> None:
        records.append(record)

    scan_dblp(
        xml_path,
        venues=venues,
        max_year=max_year,
        on_record=on_record,
        stop_after_matches=limit,
        min_year=min_year,
    )
    return records


def build_record(
    dblp_key: str | None,
    fields: dict[str, list[str]],
    venues: set[str],
    max_year: int,
    min_year: int | None = None,
) -> DblpRecord | None:
    if not dblp_key:
        return None

    crossref = first(fields, "crossref")
    venue, crossref_year = venue_from_crossref(crossref)
    if venue is None or venue not in venues:
        return None

    year_text = first(fields, "year")
    year = parse_year(year_text) or crossref_year
    if year is None or year > max_year:
        return None
    if min_year is not None and year < min_year:
        return None

    title = first(fields, "title")
    if not title:
        return None

    ee = tuple(fields.get("ee", []))
    return DblpRecord(
        dblp_key=dblp_key,
        venue=venue,
        year=year,
        title=title,
        authors=tuple(fields.get("author", [])),
        booktitle=first(fields, "booktitle"),
        pages=first(fields, "pages"),
        crossref=crossref,
        dblp_url=normalize_dblp_url(first(fields, "url")),
        doi=first_doi(ee),
        ee=ee,
    )


def venue_from_crossref(crossref: str | None) -> tuple[str | None, int | None]:
    if not crossref:
        return None, None
    for venue, prefix in VENUE_CROSSREF_PREFIXES.items():
        match = re.fullmatch(rf"{re.escape(prefix)}/(\d{{4}})", crossref)
        if match:
            return venue, int(match.group(1))
    return None, None


def first(fields: dict[str, list[str]], name: str) -> str | None:
    values = fields.get(name)
    if not values:
        return None
    return values[0]


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def parse_year(year_text: str | None) -> int | None:
    if not year_text:
        return None
    match = re.search(r"\d{4}", year_text)
    if not match:
        return None
    return int(match.group(0))


def normalize_dblp_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("db/"):
        return f"https://dblp.org/{url}"
    return url


def first_doi(ee_values: tuple[str, ...]) -> str | None:
    for value in ee_values:
        match = DOI_URL_RE.match(value)
        if match:
            return match.group(1)
    return None


def open_xml_stream(xml_path: Path):
    if xml_path.suffix == ".gz":
        return gzip.open(xml_path, "rb")
    return xml_path.open("rb")
