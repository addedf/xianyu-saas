#!/usr/bin/env python3
"""Add the nullable order SKU column without changing inventory or order data."""

import argparse
import json
import sqlite3
from pathlib import Path


def migrate(path, *, apply=False):
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if apply else "?mode=ro"))
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(delivery_events)")}
        if not columns:
            raise RuntimeError("delivery_events table is missing")
        before = {
            table: list(connection.execute(f"SELECT status,count(*) FROM {table} GROUP BY status ORDER BY status"))
            for table in ("delivery_events", "inventory")
        }
        changed = "sku_id" not in columns
        if changed and apply:
            connection.execute("ALTER TABLE delivery_events ADD COLUMN sku_id TEXT")
        for table, counts in before.items():
            after = list(connection.execute(f"SELECT status,count(*) FROM {table} GROUP BY status ORDER BY status"))
            if counts != after:
                raise RuntimeError("Migration changed order or inventory counts")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("Database integrity check failed")
        if apply:
            connection.commit()
        return {"needed": changed, "applied": changed and apply, "integrity": integrity, "counts": before}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Apply only after stopping the app and backing up production data")
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    results = []
    for path in sorted((root / "tenants").rglob("delivery_state.db")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeError("Database path escapes data root")
        results.append({"database": str(path.relative_to(root)), **migrate(path, apply=args.apply)})
    print(json.dumps({"apply": args.apply, "databases": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
