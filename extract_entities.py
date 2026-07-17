#!/usr/bin/env python3
"""
Entity extraction — step 6.

Runs spaCy NER on document_text to find people and organizations.
Also extracts NPRR numbers via regex (deterministic, perfect recall).

Stores results in the entities table: one row per unique entity per document.

Setup:
  pip install spacy
  python -m spacy download en_core_web_sm

Usage:
  python extract_entities.py
  python extract_entities.py --limit 50    # test run
  python extract_entities.py --redo        # reprocess all docs
"""

import argparse
import re
import sqlite3
from pathlib import Path

DB_PATH = Path("ercot.db")

NPRR_RE = re.compile(r'\bNPRR\s*\d+\b', re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    entity_text TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    count       INTEGER DEFAULT 1,
    UNIQUE(document_id, entity_text, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_entities_text ON entities(entity_text);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_doc  ON entities(document_id);
"""

KEEP_TYPES = {"PERSON", "ORG"}

# Common noise words that spaCy misclassifies as entities
STOPWORDS = {
    "ercot", "texas", "tac", "board", "committee", "market", "grid",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "q1", "q2", "q3", "q4", "llc", "inc", "corp", "ltd", "page", "section",
    "agenda", "item", "motion", "vote", "discussion", "meeting", "minutes",
}


def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def is_valid(text: str) -> bool:
    if len(text) < 3:
        return False
    if text.lower() in STOPWORDS:
        return False
    if re.match(r'^[\d\s\.\-,]+$', text):
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Extract named entities from ERCOT documents.")
    ap.add_argument("--limit", type=int, default=0, help="Process only first N docs.")
    ap.add_argument("--redo",  action="store_true", help="Reprocess all docs, not just new ones.")
    args = ap.parse_args()

    import spacy
    print("Loading spaCy en_core_web_sm...")
    nlp = spacy.load("en_core_web_sm")
    # Only need NER — disable tagger/parser for speed
    disabled = [p for p in nlp.pipe_names if p not in ("ner",)]
    nlp.select_pipes(disable=disabled)
    print("Model ready.\n")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()

    if args.redo:
        db.execute("DELETE FROM entities")
        db.commit()

    rows = db.execute(
        """SELECT dt.document_id, dt.text
           FROM document_text dt
           WHERE dt.document_id NOT IN (SELECT DISTINCT document_id FROM entities)
           ORDER BY dt.document_id"""
    ).fetchall()

    if args.limit:
        rows = rows[: args.limit]

    print(f"{len(rows)} documents to process\n")
    n_records = 0

    for i, row in enumerate(rows, 1):
        doc_id = row["document_id"]
        text   = (row["text"] or "")[:50000]  # cap length for speed

        counts = {}

        # spaCy NER
        spacy_doc = nlp(text)
        for ent in spacy_doc.ents:
            if ent.label_ not in KEEP_TYPES:
                continue
            c = clean(ent.text)
            if not is_valid(c):
                continue
            key = (c, ent.label_)
            counts[key] = counts.get(key, 0) + 1

        # NPRR numbers via regex
        for m in NPRR_RE.finditer(text):
            normalized = re.sub(r'\s+', '', m.group().upper())
            key = (normalized, "NPRR")
            counts[key] = counts.get(key, 0) + 1

        for (entity_text, entity_type), count in counts.items():
            db.execute(
                """INSERT OR IGNORE INTO entities
                       (document_id, entity_text, entity_type, count)
                   VALUES (?, ?, ?, ?)""",
                (doc_id, entity_text, entity_type, count),
            )
        n_records += len(counts)

        if i % 100 == 0 or i == len(rows):
            db.commit()
            print(f"  {i}/{len(rows)} docs processed | {n_records} entity records")

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    unique_people = db.execute(
        "SELECT COUNT(DISTINCT entity_text) FROM entities WHERE entity_type='PERSON'"
    ).fetchone()[0]
    unique_orgs = db.execute(
        "SELECT COUNT(DISTINCT entity_text) FROM entities WHERE entity_type='ORG'"
    ).fetchone()[0]
    unique_nprr = db.execute(
        "SELECT COUNT(DISTINCT entity_text) FROM entities WHERE entity_type='NPRR'"
    ).fetchone()[0]

    print(f"\nDone.")
    print(f"  Total records: {total}")
    print(f"  Unique people: {unique_people}")
    print(f"  Unique orgs:   {unique_orgs}")
    print(f"  NPRR numbers:  {unique_nprr}")
    db.close()


if __name__ == "__main__":
    main()
