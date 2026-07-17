#!/usr/bin/env python3
"""
ERCOT document downloader — step 3.

Reads documents.csv (from ercot_details.py), downloads each unique document
to ./docs/, and writes manifest.csv for tracking.

Naming scheme:
  {meeting_url_slug}__{sanitized_doc_title}.{detected_ext}
  e.g. 01142025-ercot-board-of-directors__Board_Meeting_Minutes.pdf

  If two docs produce the same base name, appends _2, _3, etc.

Behaviour:
  - Validates magic bytes; rejects HTML error pages masquerading as PDFs
  - Resumable: skips doc_urls already in manifest.csv
  - 1.5 s rate limit between requests

Usage:
  python ercot_download.py                  # full run
  python ercot_download.py --limit 10       # first 10 unique URLs (test)
  python ercot_download.py --input documents.csv

Dependencies: pip install requests
"""

import argparse
import csv
import hashlib
import re
import time
from pathlib import Path

import requests

DOCS_DIR = Path("docs")
MANIFEST_CSV = Path("manifest.csv")
SLEEP_SECONDS = 1.5

HEADERS = {
    "User-Agent": "ercot-minutes-research/0.1 (personal research project; contact: you@example.com)"
}

MANIFEST_FIELDS = [
    "filename", "doc_url", "doc_title",
    "meeting_url", "meeting_date", "committee",
    "claimed_type", "detected_type", "size_bytes",
    "sha256", "extraction_status",
]

MAGIC = [
    (b"%PDF",              "pdf"),
    (b"\xd0\xcf\x11\xe0", "ole"),   # legacy .doc / .ppt / .xls
    (b"PK\x03\x04",       "zip"),   # .docx / .pptx / .xlsx
]

HTML_MARKERS = (b"<!doctype", b"<html")


def detect_type(content: bytes, url_ext: str) -> str | None:
    """Return detected extension, or None if content looks like an HTML error page."""
    head = content[:9].lower()
    for marker in HTML_MARKERS:
        if head.startswith(marker):
            return None
    for magic, kind in MAGIC:
        if content[:len(magic)] == magic:
            if kind == "zip":
                return url_ext if url_ext in ("docx", "pptx", "xlsx") else "zip"
            if kind == "ole":
                return url_ext if url_ext in ("doc", "ppt", "xls") else "ole"
            return kind
    return url_ext or "bin"


def meeting_slug(meeting_url: str) -> str:
    """Extract the slug portion of a meeting URL, e.g. '01142025-ercot-board'."""
    return meeting_url.rstrip("/").rsplit("/", 1)[-1]


def sanitize(text: str) -> str:
    """Turn arbitrary text into a filesystem-safe string."""
    text = text.strip()
    text = re.sub(r"[^\w\s\-]", "", text)   # keep word chars, spaces, hyphens
    text = re.sub(r"\s+", "_", text)         # spaces → underscores
    text = re.sub(r"_+", "_", text)          # collapse multiple underscores
    return text[:120]                         # cap length


def make_filename(doc: dict, ext: str, used_names: set[str]) -> str:
    """Build a unique human-readable filename from meeting_url + doc_title."""
    slug = meeting_slug(doc["meeting_url"])
    title = sanitize(doc["doc_title"])
    base = f"{slug}__{title}"
    candidate = f"{base}.{ext}"
    if candidate not in used_names:
        return candidate
    n = 2
    while True:
        candidate = f"{base}_{n}.{ext}"
        if candidate not in used_names:
            return candidate
        n += 1


def load_manifest() -> tuple[dict[str, dict], set[str]]:
    """Return (rows_by_doc_url, set_of_used_filenames).

    used_names is seeded from both the manifest and whatever files already
    exist in DOCS_DIR — so the docs folder is the cache source of truth.
    """
    by_url: dict[str, dict] = {}
    used: set[str] = set()
    # Seed from existing files on disk first
    if DOCS_DIR.exists():
        for f in DOCS_DIR.iterdir():
            if f.is_file():
                used.add(f.name)
    if not MANIFEST_CSV.exists():
        return by_url, used
    with MANIFEST_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_url[row["doc_url"]] = row
            used.add(row["filename"])
    return by_url, used


def append_manifest(row: dict) -> None:
    write_header = not MANIFEST_CSV.exists()
    with MANIFEST_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="Download ERCOT documents from documents.csv.")
    ap.add_argument("--input", default="documents.csv")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after this many unique URLs (for test runs).")
    args = ap.parse_args()

    DOCS_DIR.mkdir(exist_ok=True)

    docs = list(csv.DictReader(open(args.input, encoding="utf-8")))
    manifest, used_names = load_manifest()

    # Dedup by doc_url — same URL appearing under multiple meetings → download once
    seen_urls: dict[str, dict] = {}
    for d in docs:
        if d["doc_url"] not in seen_urls:
            seen_urls[d["doc_url"]] = d

    unique_docs = list(seen_urls.values())
    if args.limit:
        unique_docs = unique_docs[: args.limit]

    already = sum(1 for u in unique_docs if u["doc_url"] in manifest)
    print(f"{len(docs)} rows in {args.input} → {len(unique_docs)} unique URLs")
    print(f"  {already} already in manifest — skipping")

    session = requests.Session()
    n_ok = n_skipped = n_rejected = n_failed = 0

    for i, doc in enumerate(unique_docs, 1):
        url = doc["doc_url"]

        if url in manifest and (DOCS_DIR / manifest[url]["filename"]).exists():
            n_skipped += 1
            continue

        try:
            resp = session.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  [FAIL] {url} → {exc}")
            n_failed += 1
            time.sleep(SLEEP_SECONDS)
            continue

        content = resp.content
        last_seg = url.rsplit("/", 1)[-1]
        url_ext = last_seg.rsplit(".", 1)[-1].lower() if "." in last_seg else ""

        detected = detect_type(content, url_ext)
        if detected is None:
            ct = resp.headers.get("content-type", "?")
            print(f"  [REJECT] HTML error page: {url}  ({len(content)} bytes, {ct})")
            n_rejected += 1
            time.sleep(SLEEP_SECONDS)
            continue

        filename = make_filename(doc, detected, used_names)
        used_names.add(filename)
        dest = DOCS_DIR / filename
        dest.write_bytes(content)

        digest = hashlib.sha256(content).hexdigest()
        row = {
            "filename":          filename,
            "doc_url":           url,
            "doc_title":         doc["doc_title"],
            "meeting_url":       doc["meeting_url"],
            "meeting_date":      doc["meeting_date"],
            "committee":         doc["committee"],
            "claimed_type":      doc["claimed_type"],
            "detected_type":     detected,
            "size_bytes":        len(content),
            "sha256":            digest,
            "extraction_status": "",
        }
        append_manifest(row)
        manifest[url] = row
        n_ok += 1

        if i % 10 == 0 or i == len(unique_docs):
            print(f"  {i}/{len(unique_docs)} processed | "
                  f"ok={n_ok} skip={n_skipped} reject={n_rejected} fail={n_failed}")

        time.sleep(SLEEP_SECONDS)

    print(f"\nDone.  ok={n_ok}  skipped={n_skipped}  rejected={n_rejected}  failed={n_failed}")
    print(f"Files → {DOCS_DIR}/   Manifest → {MANIFEST_CSV}")
    if n_rejected:
        print(f"⚠️  {n_rejected} files rejected as HTML — check those URLs manually.")
    if n_failed:
        print(f"⚠️  {n_failed} requests failed — re-run to retry (already-done rows are skipped).")


if __name__ == "__main__":
    main()
