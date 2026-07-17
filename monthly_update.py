#!/usr/bin/env python3
"""
Monthly ERCOT scraping pipeline.

Runs on the 1st of each month via Windows Task Scheduler.
Enumerates the current month, downloads new documents, updates the DB,
extracts text and entities, then prompts for GitHub release + Render redeploy.
"""
import calendar
import os
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def run(cmd):
    print(f"\n>>> {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run([str(c) for c in cmd], cwd=SCRIPT_DIR, check=True)


def main():
    today = date.today()
    from_date = today.replace(day=1).isoformat()
    last_day = calendar.monthrange(today.year, today.month)[1]
    to_date = today.replace(day=last_day).isoformat()
    tag = today.strftime("v%Y-%m")

    print(f"=== ERCOT Monthly Update: {from_date} → {to_date} ===", flush=True)

    run(["python", SCRIPT_DIR / "ercot_enumerate.py", "--from", from_date, "--to", to_date])
    run(["python", SCRIPT_DIR / "ercot_details.py"])
    run(["python", SCRIPT_DIR / "ercot_download.py"])
    run(["python", SCRIPT_DIR / "build_db.py"])
    run(["python", SCRIPT_DIR / "extract_text.py"])
    run(["python", SCRIPT_DIR / "extract_entities.py"])

    # Build the slim public DB (strips full text, keeps FTS + metadata)
    print(f"\n=== Building slim public DB ===", flush=True)
    run(["python", SCRIPT_DIR / "build_public_db.py"])

    print(f"\n{'='*60}", flush=True)
    print(f"✓ Pipeline complete for {today.strftime('%B %Y')}.", flush=True)
    print(f"\nTo publish the updated database:", flush=True)
    print(f"  1. Go to https://github.com/YOUR_USERNAME/YOUR_REPO/releases/new", flush=True)
    print(f"  2. Tag: {tag}", flush=True)
    print(f"  3. Attach ercot_public.db as a release asset", flush=True)
    print(f"  4. Publish the release", flush=True)
    print(f"  5. Copy the ercot_public.db asset download URL", flush=True)
    print(f"  6. Update DB_RELEASE_URL in Render dashboard → Environment", flush=True)

    hook = os.environ.get("RENDER_DEPLOY_HOOK")
    if hook:
        print(f"\nTriggering Render redeploy…", flush=True)
        try:
            urllib.request.urlopen(hook)
            print("  Redeploy triggered.", flush=True)
        except Exception as e:
            print(f"  WARNING: Redeploy failed: {e}", flush=True)
    else:
        print(f"\n  Tip: Set RENDER_DEPLOY_HOOK in a .env file to trigger Render", flush=True)
        print(f"       redeploys automatically after the DB_RELEASE_URL is updated.", flush=True)


if __name__ == "__main__":
    main()
