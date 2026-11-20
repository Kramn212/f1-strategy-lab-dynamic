"""
demo.py - build SAMPLE dashboard data with no internet connection.

Run:  python pipeline/demo.py
Then: python pipeline/server.py        ->  open http://localhost:8000

It runs the real pipeline (extract -> transform -> load) against fake_api.py: 3 synthetic 2024
races with 6 drivers. The numbers only LOOK like F1 data; the point is to preview every chart, the
Expand tabs, the track map and the Refresh button. It writes into public/data/ and a temporary
database, so your real data (if any) in data/f1.db is untouched - but public/data/ IS overwritten.
"""
import shutil
import tempfile
from pathlib import Path

import config
import extract
import fake_api
import run


def main():
    tmp = Path(tempfile.mkdtemp())
    config.CACHE_DIR, config.DB_PATH = tmp / "cache", tmp / "demo.db"
    config.YEARS, config.INCLUDE_CURRENT_YEAR, config.LOCATION_YEARS = [2024], False, [2024]
    if config.OUT_DIR.exists():
        shutil.rmtree(config.OUT_DIR)
    extract.get = fake_api.get                       # replace the real HTTP function
    print(run.refresh())
    print(f"\nDemo data written to {config.OUT_DIR}")


if __name__ == "__main__":
    main()
