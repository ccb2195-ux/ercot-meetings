#!/usr/bin/env python3
"""
Backfill one historical year of ERCOT meetings.

Run once a month (manually or alongside monthly_update.py) to gradually
expand the archive backward, one year at a time.

Usage:
  python backfill_year.py             # auto-detect next oldest year
  python backfill_year.py --year 2019 # specific year
  python backfill_year.py --dry-run   # preview without running
  python backfill_year.py --status    # show backfill log
"""
import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "ercot.db"
EARLIEST_YEAR = 2010  # ERCOT calendar floor — adjust if data goes further back


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS backfill_log (
        year         INTEGER PRIMARY KEY,
        completed_at TEXT
    )""")
    db.commit()
    return db


def run(cmd):
    print(f"\n>>> {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run([str(c) for c in cmd], cwd=SCRIPT_DIR, check=True)


def main():
    ap = argparse.ArgumentParser(description="Backfill one historical ERCOT year.")
    ap.add_argument("--year", type=int, help="Specific year to backfill")
    ap.add_argument("--dry-run", action="store_true", help="Print plan without running")
    ap.add_argument("--status", action="store_true", help="Show backfill log and exit")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print("ERROR: ercot.db not found — run monthly_update.py first.", file=sys.stderr)
        sys.exit(1)

    db = get_db()

    if args.status:
        rows = db.execute(
            "SELECT year, completed_at FROM backfill_log ORDER BY year DESC"
        ).fetchall()
        oldest = db.execute(
            "SELECT MIN(substr(date,1,4)) FROM meetings"
        ).fetchone()[0]
        print(f"Oldest year in DB: {oldest}")
        print(f"\nBackfill log ({len(rows)} years completed):")
        for year, completed_at in rows:
            print(f"  {year}  —  {completed_at}")
        if not rows:
            print("  (none yet)")
        return

    if args.year:
        target_year = args.year
    else:
        oldest = db.execute(
            "SELECT MIN(substr(date,1,4)) FROM meetings"
        ).fetchone()[0]
        if not oldest:
            print("No meetings in DB — run monthly_update.py first.", file=sys.stderr)
            sys.exit(1)
        done = {r[0] for r in db.execute("SELECT year FROM backfill_log")}
        target_year = int(oldest) - 1
        while target_year in done:
            target_year -= 1
        if target_year < EARLIEST_YEAR:
            print(f"No more years to backfill (floor is {EARLIEST_YEAR}).")
            sys.exit(0)

    from_date = f"{target_year}-01-01"
    to_date   = f"{target_year}-12-31"

    print(f"=== ERCOT Backfill: {target_year} ({from_date} → {to_date}) ===", flush=True)

    if args.dry_run:
        print("Dry run — would execute:")
        print(f"  ercot_enumerate.py --from {from_date} --to {to_date}")
        print(f"  ercot_details.py")
        print(f"  ercot_download.py")
        print(f"  build_db.py")
        print(f"  extract_text.py")
        print(f"  extract_entities.py")
        return

    run(["python", SCRIPT_DIR / "ercot_enumerate.py", "--from", from_date, "--to", to_date])
    run(["python", SCRIPT_DIR / "ercot_details.py"])
    run(["python", SCRIPT_DIR / "ercot_download.py"])
    run(["python", SCRIPT_DIR / "build_db.py"])
    run(["python", SCRIPT_DIR / "extract_text.py"])
    run(["python", SCRIPT_DIR / "extract_entities.py"])

    db.execute(
        "INSERT OR REPLACE INTO backfill_log (year, completed_at) VALUES (?, datetime('now'))",
        (target_year,),
    )
    db.commit()

    print(f"\n{'='*60}", flush=True)
    print(f"✓ Backfill for {target_year} complete.", flush=True)
    print(f"  Publish a new GitHub release with ercot.db to update the live site.", flush=True)
    print(f"  Next auto-target will be: {target_year - 1}", flush=True)


if __name__ == "__main__":
    main()
