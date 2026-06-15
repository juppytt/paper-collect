from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import import_dblp
from .dblp import DEFAULT_MAX_YEAR, DEFAULT_VENUES, collect_sample_records, normalize_venues, summarize_dblp


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
    venues = normalize_venues(args.venues)

    if args.command == "dblp-summary":
        summary = summarize_dblp(
            args.xml,
            venues=venues,
            max_year=args.max_year,
            stop_after_matches=args.stop_after_matches,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.command == "dblp-sample":
        for record in collect_sample_records(args.xml, venues=venues, max_year=args.max_year, limit=args.limit):
            print(json.dumps(record.to_dict(), sort_keys=True))
        return 0

    if args.command == "dblp-import":
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

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
