#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1"]
# ///
"""Query the telemetry files with DuckDB: skill calls, cost, tokens and tool calls per session, or any SQL of your own.

uv run report.py --logs .logs                       # the summaries
uv run report.py --logs .logs "select * from events limit 5"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

sys.dont_write_bytecode = True

SUMMARIES = ("skills_by_session", "cost_by_session", "tokens_by_session", "tools_by_session")


def connect(logs: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory database with the views loaded. The views need at least one metrics and one events file."""
    connection = duckdb.connect()
    sql = (Path(__file__).with_name("queries.sql")).read_text(encoding="utf-8")
    connection.execute(sql.replace("{logs}", logs.resolve().as_posix()))
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logs", type=Path, default=Path(".logs"))
    parser.add_argument("sql", nargs="?", help="a query over the views metrics, events and the summaries")
    args = parser.parse_args(argv)
    connection = connect(args.logs)
    for query in [args.sql] if args.sql else [f"select * from {view}" for view in SUMMARIES]:
        print(connection.sql(query))
    return 0


if __name__ == "__main__":
    sys.exit(main())
