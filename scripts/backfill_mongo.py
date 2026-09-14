"""One-shot copy of the retired Neon Postgres database into MongoDB.

Reads every table with asyncpg and writes the equivalent documents with Motor,
preserving each row's integer primary key so that plan-version chains
(``generated_plans.previous_plan_id``), ``runs.user_id`` and every id the
frontend already has in a URL keep resolving after the cutover.

Usage:
    DATABASE_URL=<neon-url> MONGODB_URI=<npe-atlas-url> \
        uv run python scripts/backfill_mongo.py [--drop] [--dry-run]

Safe to re-run: each document is upserted by ``_id``, so a partial run can be
finished by running it again. Never writes to Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from decimal import Decimal
from typing import Any

import asyncpg
from bson import Decimal128

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

# Both connection strings normally live in .env; load it so the script can be run
# without exporting them by hand.
load_dotenv()

from src.app.db.mongo import ensure_indexes, get_db, init_client, sync_counter  # noqa: E402

# table -> collection. Ordered so that referenced rows land before the rows that
# point at them; the copy does not enforce referential integrity, but keeping the
# order makes a partial run leave a coherent database behind.
TABLES: list[tuple[str, str]] = [
    ("users", "users"),
    ("jira_tickets", "jira_tickets"),
    ("runs", "runs"),
    ("generated_plans", "generated_plans"),
    ("plan_test_cases", "plan_test_cases"),
    ("bug_analyses", "bug_analyses"),
    ("feedback_events", "feedback_events"),
    ("ticket_walkthroughs", "ticket_walkthroughs"),
    ("test_plan_progress", "test_plan_progress"),
    ("ticket_holds", "ticket_holds"),
]

# Postgres enum columns arrive as plain strings and the Mongo models store them
# as strings too, so no conversion is needed. Decimal is the one type BSON will
# not take as-is.
BATCH = 500


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return Decimal128(value)
    return value


def _to_document(record: asyncpg.Record) -> dict[str, Any]:
    doc = {k: _normalize(v) for k, v in dict(record).items()}
    doc["_id"] = doc.pop("id")
    return doc


def _pg_url(raw: str) -> str:
    """asyncpg rejects the sslmode/channel_binding params libpq accepts."""
    url = raw.replace("postgresql+asyncpg://", "postgresql://")
    return re.sub(r"[?&](sslmode|channel_binding)=[^&]*", "", url)


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    """Decode json/jsonb into Python objects instead of raw strings.

    Without this asyncpg hands back the serialized text — `'["a","b"]'`, and the
    literal `'null'` for a SQL NULL — and the copy stores that string verbatim.
    The documents then look fine by row count while being the wrong *type*: the
    Pydantic models reject them on read, and `find_seed_regression_tests` filters
    on `regression_tests.0`, which a string never matches, so it would have
    returned nothing forever without erroring.

    Affects all ten jsonb columns on bug_analyses plus runs.source_provenance.
    `runs.ticket_keys` is a real Postgres ARRAY and decodes natively, which is why
    plan lookups worked while these did not.
    """
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def copy_table(conn: asyncpg.Connection, table: str, collection: str, *, dry_run: bool) -> tuple[int, int]:
    """Copy one table. Returns (rows read, highest id seen)."""
    rows = await conn.fetch(f'SELECT * FROM "{table}" ORDER BY id')
    if not rows:
        return 0, 0

    highest = max(r["id"] for r in rows)
    if dry_run:
        return len(rows), highest

    db = get_db()
    coll = db[collection]
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        # Upsert rather than insert so a re-run after a partial failure converges
        # instead of colliding on duplicate _id.
        from pymongo import ReplaceOne

        await coll.bulk_write(
            [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in (_to_document(r) for r in chunk)],
            ordered=False,
        )
    return len(rows), highest


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", action="store_true", help="drop each target collection first")
    parser.add_argument("--dry-run", action="store_true", help="count rows without writing")
    args = parser.parse_args()

    pg_raw = os.environ.get("DATABASE_URL")
    if not pg_raw:
        print("DATABASE_URL (the Neon database to read from) is not set", file=sys.stderr)
        return 2
    if not os.environ.get("MONGODB_URI"):
        print("MONGODB_URI (the Mongo database to write to) is not set", file=sys.stderr)
        return 2

    init_client()
    if not args.dry_run:
        await ensure_indexes()

    conn = await asyncpg.connect(_pg_url(pg_raw))
    await _register_json_codec(conn)
    try:
        if args.drop and not args.dry_run:
            db = get_db()
            for _, collection in TABLES:
                await db[collection].drop()
            # Counters are derived from the copied ids below; clear them too so a
            # re-run with --drop cannot inherit a stale high-water mark.
            await db.counters.drop()
            await ensure_indexes()

        total = 0
        print(f"{'table':<24} {'rows':>8}  {'max id':>8}")
        print("-" * 44)
        for table, collection in TABLES:
            count, highest = await copy_table(conn, table, collection, dry_run=args.dry_run)
            total += count
            print(f"{table:<24} {count:>8}  {highest:>8}")
            if highest and not args.dry_run:
                # Without this the first post-migration insert would allocate id 1
                # and collide with copied data.
                await sync_counter(collection, highest)
        print("-" * 44)
        print(f"{'TOTAL':<24} {total:>8}")

        if args.dry_run:
            print("\n(dry run — nothing written)")
            return 0

        print("\nVerifying document counts against the source tables...")
        db = get_db()
        mismatches = 0
        for table, collection in TABLES:
            pg_count = await conn.fetchval(f'SELECT count(*) FROM "{table}"')
            mongo_count = await db[collection].count_documents({})
            flag = "ok" if pg_count == mongo_count else "MISMATCH"
            if pg_count != mongo_count:
                mismatches += 1
            print(f"  {table:<24} postgres={pg_count:<8} mongo={mongo_count:<8} {flag}")
        if mismatches:
            print(f"\n{mismatches} collection(s) did not match. Re-run to converge.", file=sys.stderr)
            return 1
        print("\nAll collections match the source row counts.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
