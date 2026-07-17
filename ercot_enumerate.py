#!/usr/bin/env python3
"""
ERCOT meeting calendar enumerator — v1 (recon + verification).

What it does:
  1. Splits a date range into chunks (default: monthly).
  2. Fetches each chunk from https://www.ercot.com/calendar?fromDate=...&toDate=...
  3. Saves raw HTML to ./cache/ (fetch once, parse many times).
  4. Parses meeting records: guid, date, title, committee, status, url.
  5. Verifies against silent truncation: also fetches the WHOLE range in one
     request and compares the total count against the sum of the chunks.
  6. Writes results to meetings.csv.

Usage:
  python ercot_enumerate.py --from 2025-01-01 --to 2025-12-31
  python ercot_enumerate.py --from 2025-01-01 --to 2025-12-31 --dry-run
  python ercot_enumerate.py --from 2025-01-01 --to 2025-12-31 --no-verify

Dependencies:
  pip install requests beautifulsoup4
"""

import argparse
import csv
import hashlib
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.ercot.com/calendar"
CACHE_DIR = Path("cache")
OUTPUT_CSV = Path("meetings.csv")
SLEEP_SECONDS = 1.5  # be polite; boring scrapers don't get blocked

# Identify ourselves honestly. Anonymous default UAs get blocked more often,
# and if the admin ever wonders who we are, this answers the question.
HEADERS = {
    "User-Agent": "ercot-minutes-research/0.1 (personal research project; contact: you@example.com)"
}

# Meeting detail links look like /calendar/06012026-Technology-_-Security-Committee
# The 8-digit MMDDYYYY prefix is our discriminator vs. nav links like /committees/calendar
EVENT_HREF_RE = re.compile(r"/calendar/(\d{8})-")


def month_chunks(start: date, end: date):
    """Yield (chunk_start, chunk_end) pairs covering [start, end] month by month."""
    cur = start
    while cur <= end:
        # last day of cur's month
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        chunk_end = min(nxt - timedelta(days=1), end)
        yield cur, chunk_end
        cur = nxt


def fetch(session: requests.Session, from_d: date, to_d: date) -> str:
    """Fetch one calendar range, using the disk cache if we already have it."""
    params = {"fromDate": from_d.isoformat(), "toDate": to_d.isoformat()}
    cache_key = hashlib.sha1(f"{params}".encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"calendar_{from_d}_{to_d}_{cache_key}.html"

    if cache_file.exists():
        print(f"  [cache] {from_d} → {to_d}")
        return cache_file.read_text(encoding="utf-8")

    print(f"  [fetch] {from_d} → {to_d}")
    resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(resp.text, encoding="utf-8")
    time.sleep(SLEEP_SECONDS)
    return resp.text


def parse_meetings(html: str) -> list[dict]:
    """
    Extract meeting records from one calendar page.

    Strategy notes (v1 — expect to iterate once we see real DOM):
      - Anchor href with 8-digit MMDDYYYY prefix identifies an event link.
      - The anchor's title attribute carries the full committee name.
      - Date is parsed from the URL slug, NOT page layout (more robust).
      - GUID comes from the nearest following 'Add to calendar' ical link.
      - CANCELLED/RESCHEDULED status appears as text near the link.
    """
    soup = BeautifulSoup(html, "html.parser")
    meetings = []
    seen_urls = set()

    for a in soup.find_all("a", href=EVENT_HREF_RE):
        href = a["href"]
        m = EVENT_HREF_RE.search(href)
        mmddyyyy = m.group(1)
        try:
            mtg_date = datetime.strptime(mmddyyyy, "%m%d%Y").date()
        except ValueError:
            continue  # 8 digits but not a date; skip

        title = a.get_text(strip=True)
        committee = a.get("title", "").strip()

        # Status: look at text immediately around the anchor for markers.
        # The page shows e.g. "TAC Meeting - Webex Only (CANCELLED)".
        context = a.parent.get_text(" ", strip=True) if a.parent else title
        status = "active"
        if "CANCELLED" in context.upper():
            status = "cancelled"
        elif "RESCHEDULED" in context.upper():
            status = "rescheduled"

        # Dedup within this page (nav sidebars can repeat links).
        url = href if href.startswith("http") else f"https://www.ercot.com{href}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        meetings.append({
            "date": mtg_date.isoformat(),
            "title": title,
            "committee": committee,
            "status": status,
            "url": url,
        })

    return meetings


def main():
    ap = argparse.ArgumentParser(description="Enumerate ERCOT committee meetings.")
    ap.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the fetch plan without making any requests.")
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip the whole-range truncation check.")
    args = ap.parse_args()

    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    if end < start:
        sys.exit("--to must be on or after --from")

    chunks = list(month_chunks(start, end))
    print(f"Plan: {len(chunks)} monthly requests covering {start} → {end}")
    if args.dry_run:
        for c_start, c_end in chunks:
            print(f"  would fetch {BASE_URL}?fromDate={c_start}&toDate={c_end}")
        if not args.no_verify:
            print(f"  would fetch {BASE_URL}?fromDate={start}&toDate={end}  (verification)")
        return

    session = requests.Session()

    # --- Pass 1: chunked fetch ---
    all_meetings: dict[str, dict] = {}
    per_chunk_counts = []
    for c_start, c_end in chunks:
        html = fetch(session, c_start, c_end)
        records = parse_meetings(html)
        per_chunk_counts.append(len(records))
        for r in records:
            key = r["url"]
            prev = all_meetings.get(key)
            # If the same meeting appears in multiple chunks, never let an
            # 'active' sighting overwrite a 'cancelled'/'rescheduled' one.
            if prev and prev["status"] != "active" and r["status"] == "active":
                continue
            all_meetings[key] = r

    chunk_total = sum(per_chunk_counts)
    unique_total = len(all_meetings)
    print(f"\nChunked pass: {chunk_total} records, {unique_total} unique meetings")

    # --- Pass 2: truncation check ---
    if not args.no_verify:
        html = fetch(session, start, end)
        whole = parse_meetings(html)
        print(f"Whole-range pass: {len(whole)} records")
        if len(whole) < unique_total:
            print("⚠️  WHOLE-RANGE RESULT IS SMALLER THAN CHUNKED SUM — "
                  "the server likely caps large ranges. Trust the chunked data; "
                  "always chunk requests.")
        elif len(whole) > unique_total:
            print("⚠️  Whole-range found MORE than chunks — check chunk boundary "
                  "logic (off-by-one on dates?).")
        else:
            print("✓ Counts match — no truncation detected at this range size.")

    # --- Output ---
    rows = sorted(all_meetings.values(), key=lambda r: (r["date"], r["title"]))
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "title", "committee", "status", "url"])
        w.writeheader()
        w.writerows(rows)

    n_cancelled = sum(1 for r in rows if r["status"] == "cancelled")
    print(f"\nWrote {len(rows)} meetings to {OUTPUT_CSV}")
    print(f"  cancelled: {n_cancelled}")


if __name__ == "__main__":
    main()
