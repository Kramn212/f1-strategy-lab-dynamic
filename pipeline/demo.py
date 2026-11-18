"""
demo.py - build SAMPLE dashboard data with no internet connection.

Run:  python pipeline/demo.py
Then: python -m http.server -d public 8000   ->  open http://localhost:8000

It runs the real ETL + analysis against fake_api.py (3 synthetic 2024 races, 6 drivers).
The synthetic numbers only look like F1 data; the point is to preview every chart, the
expand/Data/Method/Rejected tabs and the track map. Real data: python pipeline/etl.py
then python pipeline/eda.py (this overwrites public/data/).
"""
import shutil
import tempfile
from pathlib import Path

import config
import eda
import etl
import fake_api


def main():
    tmp = Path(tempfile.mkdtemp())
    config.CACHE_DIR, config.DB_PATH = tmp / "cache", tmp / "demo.db"
    config.YEARS, config.LOCATION_YEARS = [2024], [2024]
    if config.OUT_DIR.exists():
        shutil.rmtree(config.OUT_DIR)                      # drop stale files from earlier runs
    etl.get = fake_api.get
    etl.main()
    eda.main()
    print(f"\nDemo data written to {config.OUT_DIR}")


if __name__ == "__main__":
    main()
