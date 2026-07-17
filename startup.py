#!/usr/bin/env python3
"""Download ercot.db from GitHub Releases if not present. Run before gunicorn."""
import os
import sys
import urllib.request

db_path = os.environ.get("DB_PATH", "ercot.db")

if os.path.exists(db_path):
    size_mb = os.path.getsize(db_path) / 1_048_576
    print(f"Database found: {db_path} ({size_mb:.0f} MB)", flush=True)
    sys.exit(0)

url = os.environ.get("DB_RELEASE_URL")
if not url:
    print("ERROR: ercot.db not found and DB_RELEASE_URL is not set.", file=sys.stderr)
    print("Set DB_RELEASE_URL to the GitHub release asset download URL.", file=sys.stderr)
    sys.exit(1)

print(f"Downloading database from GitHub Releases…", flush=True)

def progress(count, block_size, total_size):
    if total_size > 0 and count % 500 == 0:
        pct = min(count * block_size / total_size * 100, 100)
        print(f"  {pct:.0f}%", flush=True)

urllib.request.urlretrieve(url, db_path, reporthook=progress)
size_mb = os.path.getsize(db_path) / 1_048_576
print(f"Done. {db_path} ({size_mb:.0f} MB)", flush=True)
