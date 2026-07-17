#!/usr/bin/env python3
"""
ERCOT database builder — step 4.

Reads meetings.csv + documents.csv, checks docs/ for downloaded files,
and builds ercot.db (SQLite) with:

  meetings       — one row per meeting
  documents      — one row per downloaded document
  document_text  — FTS5 virtual table (populated by extraction step later)

No manifest needed — docs/ folder is the source of truth for what's downloaded.
Filename reconstruction uses the same logic as ercot_download.py.

Usage:
  python build_db.py
  python build_db.py --meetings meetings.csv --documents documents.csv --db ercot.db

Dependencies: stdlib only
"""

import argparse
import csv
import sqlite3
from pathlib import Path

from ercot_download import make_filename

DOCS_DIR = Path("docs")
DB_PATH  = Path("ercot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    url       TEXT PRIMARY KEY,
    date      TEXT,
    committee TEXT,
    title     TEXT,
    status    TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_url        TEXT REFERENCES meetings(url),
    meeting_date       TEXT,
    committee          TEXT,
    doc_title          TEXT,
    doc_url            TEXT UNIQUE,
    filename           TEXT UNIQUE,
    claimed_type       TEXT,
    url_ext            TEXT,
    size_bytes         INTEGER,
    looks_like_minutes INTEGER DEFAULT 0,
    extraction_status  TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_text USING fts5(
    text,
    content='documents',
    content_rowid='id'
);
"""


def url_ext_from(doc_url: str) -> str:
    last_seg = doc_url.rsplit("/", 1)[-1]
    return last_seg.rsplit(".", 1)[-1].lower() if "." in last_seg else "bin"


def main():
    ap = argparse.ArgumentParser(description="Build ercot.db from meetings.csv + documents.csv.")
    ap.add_argument("--meetings",  default="meetings.csv")
    ap.add_argument("--documents", default="documents.csv")
    ap.add_argument("--db",        default=str(DB_PATH))
    args = ap.parse_args()

    disk_files = (
        {f.name for f in DOCS_DIR.iterdir() if f.is_file()}
        if DOCS_DIR.exists() else set()
    )
    print(f"{len(disk_files)} files on disk in {DOCS_DIR}/")

    with open(args.meetings, encoding="utf-8") as f:
        meetings = list(csv.DictReader(f))

    with open(args.documents, encoding="utf-8") as f:
        raw_docs = list(csv.DictReader(f))

    # Dedup by doc_url — same order and logic as the downloader
    seen_urls: dict[str, dict] = {}
    for d in raw_docs:
        if d["doc_url"] not in seen_urls:
            seen_urls[d["doc_url"]] = d
    unique_docs = list(seen_urls.values())

    # Reconstruct filenames in the same pass order as the downloader so
    # numbering (_2, _3 ...) stays consistent
    used_names: set[str] = set()
    doc_rows = []

    for doc in unique_docs:
        ext      = doc.get("url_ext") or url_ext_from(doc["doc_url"])
        filename = make_filename(doc, ext, used_names)
        used_names.add(filename)

        if filename not in disk_files:
            continue  # not downloaded yet — skip

        size_bytes = (DOCS_DIR / filename).stat().st_size
        doc_rows.append({
            "meeting_url":        doc["meeting_url"],
            "meeting_date":       doc["meeting_date"],
            "committee":          doc["committee"],
            "doc_title":          doc["doc_title"],
            "doc_url":            doc["doc_url"],
            "filename":           filename,
            "claimed_type":       doc.get("claimed_type", ""),
            "url_ext":            ext,
            "size_bytes":         size_bytes,
            "looks_like_minutes": 1 if doc.get("looks_like_minutes") == "yes" else 0,
            "extraction_status":  "",
        })

    print(f"{len(unique_docs)} unique docs in {args.documents} → {len(doc_rows)} downloaded")

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)

    db.executemany(
        "INSERT OR REPLACE INTO meetings (url, date, committee, title, status) VALUES (?,?,?,?,?)",
        [(m["url"], m["date"], m["committee"], m["title"], m["status"]) for m in meetings],
    )

    db.executemany(
        """INSERT OR IGNORE INTO documents
               (meeting_url, meeting_date, committee, doc_title, doc_url,
                filename, claimed_type, url_ext, size_bytes,
                looks_like_minutes, extraction_status)
           VALUES
               (:meeting_url, :meeting_date, :committee, :doc_title, :doc_url,
                :filename, :claimed_type, :url_ext, :size_bytes,
                :looks_like_minutes, :extraction_status)""",
        doc_rows,
    )

    db.commit()

    n_meetings = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    n_docs     = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_minutes  = db.execute("SELECT COUNT(*) FROM documents WHERE looks_like_minutes=1").fetchone()[0]
    types      = db.execute(
        "SELECT url_ext, COUNT(*) FROM documents GROUP BY url_ext ORDER BY COUNT(*) DESC"
    ).fetchall()

    print(f"\nercot.db written to {args.db}:")
    print(f"  meetings:         {n_meetings}")
    print(f"  documents:        {n_docs}  ({n_minutes} flagged as possible minutes)")
    print(f"  by type:          {dict(types)}")
    print(f"  document_text:    empty — populated by extraction step")

    db.close()


if __name__ == "__main__":
    main()
