from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .db import import_dblp
from .dblp import DEFAULT_MAX_YEAR, DEFAULT_VENUES, collect_sample_records, normalize_venues, summarize_dblp
from .download import DownloadOptions, download_papers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-collect")
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("dblp-summary", help="Summarize accepted-paper rows in a DBLP XML dump.")
    add_dblp_args(summary)
    summary.add_argument(
        "--stop-after-matches",
        type=int,
        default=None,
        help="Stop after this many matched papers. Intended for parser smoke tests.",
    )

    sample = subparsers.add_parser("dblp-sample", help="Print matched DBLP rows as JSON lines.")
    add_dblp_args(sample)
    sample.add_argument("--limit", type=int, default=10, help="Number of matched papers to print.")

    import_cmd = subparsers.add_parser("dblp-import", help="Import matched DBLP rows into SQLite.")
    add_dblp_args(import_cmd)
    import_cmd.add_argument("--db", required=True, type=Path, help="SQLite manifest path.")
    import_cmd.add_argument("--batch-size", type=int, default=1000, help="SQLite insert batch size.")
    import_cmd.add_argument(
        "--stop-after-matches",
        type=int,
        default=None,
        help="Stop after this many matched papers. Intended for import smoke tests.",
    )

    download = subparsers.add_parser("download", help="Download abstracts and/or PDFs for manifest rows.")
    download.add_argument("--db", required=True, type=Path, help="SQLite manifest path.")
    download.add_argument(
        "--target",
        choices=("abstract", "pdf", "both"),
        default="both",
        help="Which artifact to collect.",
    )
    download.add_argument(
        "--venues",
        nargs="+",
        default=None,
        help="Venue codes: sp, ccs, security, uss, ndss. Omit to include all venues.",
    )
    download.add_argument("--year", type=int, default=None, help="Exact publication year.")
    download.add_argument("--year-from", type=int, default=None, help="Inclusive lower publication year.")
    download.add_argument("--year-to", type=int, default=None, help="Inclusive upper publication year.")
    download.add_argument("--paper-id", dest="paper_ids", action="append", type=int, default=[], help="Manifest paper id.")
    download.add_argument("--dblp-key", dest="dblp_keys", action="append", default=[], help="Exact DBLP key.")
    download.add_argument("--title-contains", default=None, help="Case-insensitive title substring filter.")
    download.add_argument("--output-dir", type=Path, default=Path("data/raw"), help="Root directory for downloaded files.")
    download.add_argument("--limit", type=int, default=None, help="Maximum rows to process.")
    download.add_argument("--force", action="store_true", help="Refresh artifacts even if already present.")
    download.add_argument("--dry-run", action="store_true", help="Resolve rows and URLs without writing files or DB updates.")
    download.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds.")
    download.add_argument("--sleep", type=float, default=0.0, help="Delay between papers in seconds.")
    download.add_argument("--chrome-path", default=None, help="Chrome/Chromium binary for browser-based downloaders.")
    download.add_argument("--browser-headless", action="store_true", help="Run browser-based downloaders in headless mode.")

    return parser


def add_dblp_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--xml", required=True, type=Path, help="Path to dblp.xml or dblp.xml.gz.")
    parser.add_argument("--max-year", type=int, default=DEFAULT_MAX_YEAR)
    parser.add_argument(
        "--venues",
        nargs="+",
        default=list(DEFAULT_VENUES),
        help="Venue codes: sp, ccs, security, uss, ndss.",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "dblp-summary":
        venues = normalize_venues(args.venues)
        summary = summarize_dblp(
            args.xml,
            venues=venues,
            max_year=args.max_year,
            stop_after_matches=args.stop_after_matches,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.command == "dblp-sample":
        venues = normalize_venues(args.venues)
        for record in collect_sample_records(args.xml, venues=venues, max_year=args.max_year, limit=args.limit):
            print(json.dumps(record.to_dict(), sort_keys=True))
        return 0

    if args.command == "dblp-import":
        venues = normalize_venues(args.venues)
        result = import_dblp(
            xml_path=args.xml,
            db_path=args.db,
            venues=venues,
            max_year=args.max_year,
            batch_size=args.batch_size,
            stop_after_matches=args.stop_after_matches,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "download":
        venues = normalize_venues(args.venues) if args.venues else None
        targets = frozenset({"abstract", "pdf"} if args.target == "both" else {args.target})
        options = DownloadOptions(
            targets=targets,
            output_dir=args.output_dir,
            force=args.force,
            dry_run=args.dry_run,
            limit=args.limit,
            timeout=args.timeout,
            sleep_seconds=args.sleep,
            chrome_path=args.chrome_path,
            browser_headless=args.browser_headless,
        )
        with sqlite3.connect(args.db) as conn:
            result = download_papers(
                conn,
                venues=venues,
                year=args.year,
                year_from=args.year_from,
                year_to=args.year_to,
                paper_ids=args.paper_ids,
                dblp_keys=args.dblp_keys,
                title_contains=args.title_contains,
                options=options,
            )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
