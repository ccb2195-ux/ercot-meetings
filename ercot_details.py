#!/usr/bin/env python3
"""
ERCOT meeting detail parser — step 2.

Reads meetings.csv (from ercot_enumerate.py), fetches each meeting detail
page (cached to ./cache_details/), and extracts:

  1. Key Documents  -> documents.csv   (the download queue for step 3)
  2. Agenda items   -> agenda.csv      (who presents what — searchable for free)

No files are downloaded in this step. Review documents.csv before running
the downloader.

Usage:
  python ercot_details.py                      # process all of meetings.csv
  python ercot_details.py --limit 5            # first 5 meetings (test run)
  python ercot_details.py --input meetings.csv --skip-cancelled

Dependencies: pip install requests beautifulsoup4
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CACHE_DIR = Path("cache_details")
DOCS_CSV = Path("documents.csv")
AGENDA_CSV = Path("agenda.csv")
SLEEP_SECONDS = 1.5

HEADERS = {
    "User-Agent": "ercot-minutes-research/0.1 (personal research project; contact: you@example.com)"
}

# Documents live under /files/docs/YYYY/MM/DD/...
DOC_HREF_RE = re.compile(r"/files/docs/\d{4}/\d{2}/\d{2}/", re.I)

# Metadata line next to each doc link, e.g. "May 22, 2026 - pdf - 130.3 KB"
META_RE = re.compile(
    r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})\s*-\s*([a-zA-Z0-9]{2,5})\s*-\s*([\d.,]+\s*[KMG]?B)",
)

# Rough minutes detector for convenience flagging (review, don't trust blindly)
MINUTES_RE = re.compile(r"\bminutes\b", re.I)


def slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def fetch_detail(session: requests.Session, url: str) -> str | None:
    cache_file = CACHE_DIR / f"{slug_from_url(url)}.html"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  [error] {url} -> {exc}")
        return None
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(resp.text, encoding="utf-8")
    time.sleep(SLEEP_SECONDS)
    return resp.text


def parse_documents(html: str, meeting: dict) -> list[dict]:
    """Extract Key Documents rows. One row per unique /files/docs/ link."""
    soup = BeautifulSoup(html, "html.parser")
    docs = []
    seen = set()
    for a in soup.find_all("a", href=DOC_HREF_RE):
        href = a["href"]
        url = href if href.startswith("http") else f"https://www.ercot.com{href}"
        if url in seen:
            continue
        seen.add(url)

        title = a.get_text(strip=True) or a.get("title", "").strip()

        # Metadata usually sits in text near the link; search the parent
        # container's text, falling back to empty fields if not found.
        posted, claimed_type, size = "", "", ""
        node = a
        for _ in range(3):
            if node.parent is None:
                break
            node = node.parent
            m = META_RE.search(node.get_text(" ", strip=True))
            if m:
                posted, claimed_type, size = m.group(1), m.group(2).lower(), m.group(3)
                break

        # Claimed type from URL extension as a second opinion
        ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""

        docs.append({
            "meeting_url": meeting["url"],
            "meeting_date": meeting["date"],
            "committee": meeting["committee"],
            "meeting_title": meeting["title"],
            "doc_title": title,
            "doc_url": url,
            "posted_date": posted,
            "claimed_type": claimed_type or ext,
            "url_ext": ext,
            "size": size,
            "looks_like_minutes": "yes" if MINUTES_RE.search(title) else "",
        })
    return docs


def parse_agenda(html: str, meeting: dict) -> list[dict]:
    """
    Extract agenda rows from the agenda table, fail-soft.

    v1 heuristic: find a <table> whose header row mentions 'Topic', then
    read (item, topic, topic_type, presenter) per row. Many working-group
    pages have no agenda table at all — that's fine, we return [].
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        header_text = table.get_text(" ", strip=True)[:200].lower()
        if "topic" not in header_text:
            continue
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c != ""]  # drop spacer cells
            if not cells or cells[0].lower() in ("item", "topic"):
                continue  # header row
            # Expected shape: item, topic, topic_type, presenter — but be tolerant
            item = cells[0] if len(cells) > 1 else ""
            topic = cells[1] if len(cells) > 1 else cells[0]
            topic_type = cells[2] if len(cells) > 2 else ""
            presenter = cells[3] if len(cells) > 3 else ""
            rows.append({
                "meeting_url": meeting["url"],
                "meeting_date": meeting["date"],
                "committee": meeting["committee"],
                "item": item,
                "topic": topic,
                "topic_type": topic_type,
                "presenter": presenter,
            })
        if rows:
            break  # first plausible agenda table wins
    return rows


def main():
    ap = argparse.ArgumentParser(description="Parse ERCOT meeting detail pages.")
    ap.add_argument("--input", default="meetings.csv")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N meetings.")
    ap.add_argument("--skip-cancelled", action="store_true")
    args = ap.parse_args()

    meetings = list(csv.DictReader(open(args.input, encoding="utf-8")))
    if args.skip_cancelled:
        meetings = [m for m in meetings if m["status"] != "cancelled"]
    if args.limit:
        meetings = meetings[: args.limit]

    print(f"Processing {len(meetings)} meetings from {args.input}")
    session = requests.Session()

    all_docs, all_agenda = [], []
    n_pages_ok = n_pages_failed = n_no_docs = 0

    for i, mtg in enumerate(meetings, 1):
        html = fetch_detail(session, mtg["url"])
        if html is None:
            n_pages_failed += 1
            continue
        n_pages_ok += 1
        docs = parse_documents(html, mtg)
        agenda = parse_agenda(html, mtg)
        if not docs:
            n_no_docs += 1
        all_docs.extend(docs)
        all_agenda.extend(agenda)
        if i % 10 == 0 or i == len(meetings):
            print(f"  {i}/{len(meetings)} pages | {len(all_docs)} docs | {len(all_agenda)} agenda rows")

    doc_fields = ["meeting_url", "meeting_date", "committee", "meeting_title",
                  "doc_title", "doc_url", "posted_date", "claimed_type",
                  "url_ext", "size", "looks_like_minutes"]
    with DOCS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=doc_fields)
        w.writeheader()
        w.writerows(all_docs)

    ag_fields = ["meeting_url", "meeting_date", "committee", "item", "topic",
                 "topic_type", "presenter"]
    with AGENDA_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ag_fields)
        w.writeheader()
        w.writerows(all_agenda)

    n_minutes = sum(1 for d in all_docs if d["looks_like_minutes"])
    type_counts = {}
    for d in all_docs:
        type_counts[d["claimed_type"] or "?"] = type_counts.get(d["claimed_type"] or "?", 0) + 1

    print(f"\nPages: {n_pages_ok} ok, {n_pages_failed} failed, {n_no_docs} had no documents")
    print(f"Documents: {len(all_docs)} -> {DOCS_CSV}")
    print(f"  by type: {type_counts}")
    print(f"  flagged as possible minutes: {n_minutes}")
    print(f"Agenda rows: {len(all_agenda)} -> {AGENDA_CSV}")
    if n_pages_failed:
        print("⚠️  Some pages failed — re-run to retry (cache skips completed ones).")


if __name__ == "__main__":
    main()
