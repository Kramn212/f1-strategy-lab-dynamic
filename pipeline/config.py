"""
config.py - the ONE place for every setting and threshold in the pipeline.

Why this file exists
--------------------
Cleaning rules (e.g. "drop laps slower than 1.07x the median") used to be
hard-coded inside the analysis code. They now live here, and `methods.py`
reads the very same values to write the "Method" text shown in the
dashboard. Change a number here and the code AND the on-screen explanation
change together - they can never disagree.

Everything is read at call time (`config.X`), so tests can override values.
"""
from pathlib import Path

# ----------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent        # project root
CACHE_DIR = ROOT / "cache"                            # raw API responses (git-ignored)
DB_PATH = ROOT / "data" / "f1.db"                     # SQLite database (git-ignored)
OUT_DIR = ROOT / "public" / "data"                    # JSON the website loads (commit this!)

# ------------------------------------------------------------------- API
BASE_URL = "https://api.openf1.org/v1"
REQUEST_GAP_S = 2.2          # min seconds between API calls (~27 calls/min, under the rate limit)
MAX_RETRIES = 6              # retries on HTTP 429 / network errors

# ----------------------------------------------------------------- scope
YEARS = [2023, 2024, 2025]           # seasons to load
SESSION_TYPES = ["Race"]             # add "Sprint" / "Qualifying" later - schema already supports it
LOCATION_YEARS = [2025]              # location (x,y) data is HUGE; only these years get a track map
LOC_STEP_S = 2.0                     # keep one location sample per driver every N seconds

# ------------------------------------------------------ cleaning thresholds
FUEL_CORR_S_PER_LAP = 0.03   # assumed gain per lap as fuel burns off (ASSUMPTION, not measured)
SLOW_LAP_FACTOR = 1.07       # lap is "slow" if adjusted time > factor * driver's median
PIT_MAX_S = 40.0             # pit lane time at/above this = repair / red-flag stop, excluded
DEG_MAX_AGE = 35             # tyre ages above this are not plotted in degradation curves

# --------------------------------------------------- minimum sample sizes
MIN_N_DEG_RACE = 3           # laps needed per (compound, tyre age) in ONE race
MIN_N_DEG_POOLED = 15        # ... when pooling a season / all seasons
MIN_AGES_PER_COMPOUND = 4    # a degradation curve needs at least this many ages
MIN_PIT_TEAM_POOLED = 5      # stops needed per team in the pooled pit chart
MIN_TEMP_BAND_N = 100        # laps needed per 5-degree track-temperature band
MIN_RAIN_N = 50              # laps needed per weather class (dry / rain)
MIN_STOPS_N = 5              # driver-races needed per stop-count group

# ---------------------------------------------------------------- output
SCHEMA_VERSION = 2           # bump when the JSON layout changes; the website checks it
