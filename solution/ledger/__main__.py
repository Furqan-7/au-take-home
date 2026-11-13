"""Command line entry point.

    python -m ledger ingest data/drop_1 [data/drop_2 ...]
    python -m ledger report --out reports/
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .db import connect
from .ingest import ingest_drop, load_dispositions, load_schemas
from .pii import Pii
from .report import build_reports

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ledger", description="Interaction ledger & reporting pipeline")
    parser.add_argument("--db", default="ledger.db", help="SQLite database file (default: ledger.db)")
    parser.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS,
                        help="directory with <workflow>.json schemas and dispositions.csv")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="ingest one or more drop directories, in the order given")
    p_ingest.add_argument("drops", nargs="+", type=Path)
    p_report = sub.add_parser("report", help="write the five report files")
    p_report.add_argument("--out", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    started = time.perf_counter()
    conn = connect(args.db)
    load_dispositions(conn, args.schemas / "dispositions.csv")
    if args.command == "ingest":
        schemas = load_schemas(args.schemas)
        pii = Pii.from_env()
        for drop in args.drops:
            ingest_drop(conn, pii, schemas, drop)
    else:
        build_reports(conn, args.out)
    conn.close()
    print(f"{args.command} done in {time.perf_counter() - started:.2f}s")


if __name__ == "__main__":
    main()
