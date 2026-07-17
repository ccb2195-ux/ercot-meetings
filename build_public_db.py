#!/usr/bin/env python3
"""
Build a slim public database for deployment.

Takes the full local ercot.db (~917 MB) and produces ercot_public.db
(~400 MB) by:
  - Keeping all metadata: meetings, documents, entities, entity_blocklist
  - Keeping the full FTS search index (so search quality is unchanged)
  - Replacing 400 MB of full document text with 300-char teasers
  - Running VACUUM to reclaim space

The public DB is what gets uploaded to GitHub Releases and served on Render.
The full local ercot.db stays on your machine for pipeline operations.

Usage:
  python build_public_db.py
  python build_public_db.py --input ercot.db --output ercot_public.db
"""
import argparse
import os
import sqlite3
import time
from pathlib import Path


def build(local_path: str, public_path: str):
    local_path = os.path.abspath(local_path)
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Local DB not found: {local_path}")

    if os.path.exists(public_path):
        os.remove(public_path)
        print(f"Removed existing {public_path}")

    print(f"Building public DB from {local_path} → {public_path}")
    t0 = time.time()

    pub = sqlite3.connect(public_path)
    pub.execute("PRAGMA journal_mode=WAL")
    pub.execute(f"ATTACH DATABASE '{local_path}' AS src")

    # --- Metadata tables (copied verbatim) ---
    for table in ("meetings", "documents", "entity_blocklist"):
        schema_row = pub.execute(
            f"SELECT sql FROM src.sqlite_master WHERE type='table' AND name='{table}'"
        ).fetchone()
        if schema_row:
            pub.executescript(schema_row[0] + ";")
            pub.execute(f"INSERT INTO {table} SELECT * FROM src.{table}")
            count = pub.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count:,} rows")

    # Copy any indexes on those tables
    for idx_row in pub.execute(
        "SELECT sql FROM src.sqlite_master WHERE type='index' AND tbl_name IN "
        "('meetings','documents','entity_blocklist') AND sql IS NOT NULL"
    ).fetchall():
        try:
            pub.executescript(idx_row[0] + ";")
        except Exception:
            pass

    # Entities (may have indexes)
    pub.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id           INTEGER PRIMARY KEY,
            document_id  INTEGER NOT NULL,
            entity_text  TEXT    NOT NULL,
            entity_type  TEXT    NOT NULL,
            count        INTEGER NOT NULL DEFAULT 1,
            UNIQUE (document_id, entity_text, entity_type)
        );
    """)
    pub.execute("INSERT INTO entities SELECT * FROM src.entities")
    count = pub.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    print(f"  entities: {count:,} rows")
    pub.executescript("""
        CREATE INDEX IF NOT EXISTS idx_entities_text ON entities(entity_text);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_doc  ON entities(document_id);
    """)

    # --- document_summary: first 300 chars per document ---
    has_text = pub.execute(
        "SELECT COUNT(*) FROM src.sqlite_master WHERE type='table' AND name='document_text'"
    ).fetchone()[0]

    pub.executescript("""
        CREATE TABLE document_summary (
            document_id INTEGER PRIMARY KEY,
            teaser      TEXT
        );
    """)
    if has_text:
        pub.execute("""
            INSERT INTO document_summary (document_id, teaser)
            SELECT document_id, substr(text, 1, 300)
            FROM src.document_text
        """)
        count = pub.execute("SELECT COUNT(*) FROM document_summary").fetchone()[0]
        print(f"  document_summary: {count:,} teasers (300 chars each)")
    else:
        print("  document_summary: skipped (no source document_text table)")

    # --- FTS5: contentless index (inverted index only, no text stored) ---
    # Contentless FTS stores the search index without the source text,
    # so snippet() won't work but MATCH + rank ordering works perfectly.
    pub.executescript("""
        CREATE VIRTUAL TABLE document_fts USING fts5(
            text,
            content='',
            tokenize='unicode61'
        );
    """)
    if has_text:
        print("  Building FTS index (this takes a minute)…", flush=True)
        pub.execute("""
            INSERT INTO document_fts(rowid, text)
            SELECT document_id, text FROM src.document_text
        """)
        fts_count = pub.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0]
        print(f"  document_fts: {fts_count:,} documents indexed")
    else:
        print("  document_fts: skipped (no source document_text)")

    pub.commit()  # must commit before detaching the source DB
    pub.execute("DETACH DATABASE src")

    print("  Running VACUUM…", flush=True)
    pub.execute("VACUUM")
    pub.close()

    elapsed = time.time() - t0
    size_mb = os.path.getsize(public_path) / 1_048_576
    print(f"\n✓ Done in {elapsed:.0f}s — {public_path} is {size_mb:.0f} MB")

    local_mb = os.path.getsize(local_path) / 1_048_576
    print(f"  Reduced from {local_mb:.0f} MB → {size_mb:.0f} MB "
          f"({100 * (1 - size_mb / local_mb):.0f}% smaller)")


def main():
    ap = argparse.ArgumentParser(description="Build slim public DB for deployment.")
    ap.add_argument("--input",  default="ercot.db",        help="Full local DB path")
    ap.add_argument("--output", default="ercot_public.db", help="Output slim DB path")
    args = ap.parse_args()
    build(args.input, args.output)


if __name__ == "__main__":
    main()
