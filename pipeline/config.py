"""
config.py - every setting and threshold in ONE place.

The transform step (transform.py) and the "Method" text shown in the dashboard both read these
numbers, so the code and its explanation can never disagree. Values are read at call time
(`config.X`), which is also how the tests override them.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Where state lives. Default = inside the project. On a host with a persistent disk, set the
# environment variable F1_STATE_DIR to that disk's mount path so data survives restarts.
STATE_DIR = Path(os.environ.get("F1_STATE_DIR", ROOT))
CACHE_DIR = STATE_DIR / "cache"                       # raw API responses
DB_PATH = STATE_DIR / "data" / "f1.db"                # SQLite database (validated tables)
OUT_DIR = (STATE_DIR / "out") if "F1_STATE_DIR" in os.environ else ROOT / "public" / "data"  # JSON for the site
PUBLIC_DIR = ROOT / "public"                          # the dashboard (HTML/JS/CSS)

# ---- API (extract) -------------------------------------------------------------
BASE_URL = "https://api.openf1.org/v1"
REQUEST_GAP_S = 2.2          # seconds between calls (~27/min, under OpenF1's rate limit)
MAX_RETRIES = 6

# ---- scope ----------------------------------------------------------------------
YEARS = [2023, 2024, 2025]   # seasons to load
INCLUDE_CURRENT_YEAR = True  # a refresh also looks at the current calendar year (new races!)
SESSION_TYPE = "Race"
LOCATION_YEARS = [2025]      # track-map data is big (~20 calls/race); only these years get it
LOC_STEP_S = 2.0             # keep one location sample per driver every N seconds

# ---- refresh behaviour ------------------------------------------------------------
PUBLISH_DELAY_S = 2 * 3600   # a race counts as "ready" this long after it ended
MAX_PER_REFRESH = 4          # sessions loaded per click on Refresh (CLI has no limit); click again for more
GIVE_UP_DAYS = 14            # a race that stays EMPTY this long after it ended is treated as 'no data published'
REFRESH_COOLDOWN_S = 300     # minimum seconds between two refreshes from the website
# optional: set the environment variable REFRESH_KEY to require a password for the Refresh button

# ---- cleaning thresholds (transform) ---------------------------------------------
FUEL_CORR_S_PER_LAP = 0.03   # assumed time gained per lap as fuel burns off (an ASSUMPTION)
SLOW_LAP_FACTOR = 1.07       # "slow" lap = adjusted time > factor x driver's median
PIT_MAX_S = 40.0             # pit-lane time at/above this = repair or red-flag stop, excluded
DEG_MAX_AGE = 35             # tyre ages above this are not plotted
MIN_N_DEG = 3                # laps needed per (compound, tyre age) point
MIN_AGES_PER_COMPOUND = 4    # a degradation curve needs this many ages

SCHEMA_VERSION = 3           # bump when the JSON layout changes (the site checks it)
