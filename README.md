# F1 Strategy Lab

An **ETL pipeline** for Formula 1 race data from the free [OpenF1](https://openf1.org) API, with a
dashboard on top: **Year → Grand Prix → Driver**.

| ETL step | What happens | Library |
|---|---|---|
| **Extract** | Download sessions, laps, stints, pit stops, weather, positions and car locations from the API. Throttled, cached, retried. | `requests` |
| **Transform** | Validate every record against a schema (bad ones are kept, with the reason), then clean laps and compute race statistics. | `pydantic`, `pandas` |
| **Load** | Store validated data in SQLite and write the JSON the dashboard reads. | `sqlite3`, `json` |

The dashboard has a **Refresh data** button: it asks the Python server to look for new races and
load only those.

*Unofficial project; not affiliated with Formula 1 or the FIA.*

---

## 1. Quick start

Python 3.10+ is needed (Node 18+ only for the optional JS test).

```bash
pip install -r pipeline/requirements.txt

# A) sample data, no internet (seconds)
python pipeline/demo.py
python pipeline/server.py            # open http://localhost:8000

# B) real data
python pipeline/run.py --limit 3     # first 3 new races (newest first); repeat for more, or omit --limit
python pipeline/server.py            # open http://localhost:8000, use the Refresh button for new races
```

The pipeline waits ~2.2 s between API calls (about 27 a minute) to respect OpenF1's rate limit.
A race costs about 6 requests plus ~20 more if it has track-map data, so loading three seasons
takes a long time the first time. Everything is cached and loaded incrementally afterwards.

**Tests (no internet needed)**

```bash
python pipeline/tests/offline_test.py   # whole pipeline + server against a fake API
node tests/helpers.test.js              # pure JS helpers + strategy simulator
```

---

## 2. The Refresh button

`python pipeline/server.py` serves the dashboard **and** a small API (standard library only):

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | state (`idle` / `running` / `done` / `error`), progress message, `done`/`total`, last result |
| `POST /api/refresh` | start a refresh in the **background** and return immediately |

The button starts the job, then the page polls the status every 1.5 s and shows the progress. When
the job finishes, the page reloads the new data automatically.

**Why a background job:** one race takes a minute or more because of the API rate limit, far longer
than a web request may last.

**Protection** (so nobody can hammer OpenF1 through your button):
* only one refresh runs at a time (`409` if you click during a run);
* a cooldown between refreshes, `REFRESH_COOLDOWN_S` (`429` with the wait time);
* each click loads at most `MAX_PER_REFRESH` races; click again for more;
* optional password: set the environment variable `REFRESH_KEY`, and the page will ask for it (`401` if wrong).

On a **static host** (plain Vercel, no Python server) `/api/status` does not exist, so the button
is hidden and the dashboard is read-only.

### How a refresh finds new races without downloading everything

1. Ask OpenF1 for the **session list** of each year, one small request per year. **Never cached.**
2. Compare the session keys with the races already complete in SQLite.
3. A race is only loaded once it has ended plus `PUBLISH_DELAY_S` (OpenF1 publishes data late).
4. Only those races are downloaded, validated, loaded and turned into JSON.
5. A race that came back **empty** is retried on later refreshes with fresh downloads, and given up
   on after `GIVE_UP_DAYS`.
6. Years checked: `YEARS` plus the current calendar year (`INCLUDE_CURRENT_YEAR`).

A refresh with nothing new makes one request per year and downloads no race data.

---

## 3. What is cached

| What | Where | Rule |
|---|---|---|
| Raw API responses | `cache/` (git-ignored) | Keyed by endpoint + parameters; never re-requested. |
| **Session and meeting lists** | not cached | Always asked fresh, otherwise new races could never appear. |
| **Empty answers** | not cached | A race requested before OpenF1 published it is asked again next time. |
| Location (track) data | `cache/` | Thinned to one sample per driver per `LOC_STEP_S` seconds *before* caching. |
| Validated data | `data/f1.db` (SQLite) | The pipeline's memory: how it knows what it already has. |
| Dashboard JSON | `public/data/` | Served with `no-cache` by `server.py`, so new data shows immediately. |

The cache has no expiry and only grows; delete `cache/` any time (the database is kept, so nothing is
re-downloaded for races you already have). To redo a race from scratch, delete `data/f1.db` and `cache/`.

---

## 4. Project layout

| File | Job |
|---|---|
| `pipeline/config.py` | **All** settings and thresholds. |
| `pipeline/models.py` | Pydantic schemas for every OpenF1 table. |
| `pipeline/extract.py` | **E**: requests, throttling, caching, session lists, per-race download. |
| `pipeline/transform.py` | **T**: validation + pandas cleaning + every chart + the track-map data + method texts. |
| `pipeline/load.py` | **L**: SQLite storage and JSON output. |
| `pipeline/run.py` | Orchestrator: `refresh()` finds new races, runs E → T → L. Also the command line. |
| `pipeline/server.py` | Serves the dashboard and `/api/refresh`, `/api/status`. |
| `pipeline/fake_api.py`, `demo.py` | Fake OpenF1 for the demo and the tests. |
| `public/index.html`, `style.css`, `app.js` | The dashboard (no build step). |

---

## 5. The dashboard

Selectors for Year, Grand Prix and Driver (the URL remembers them, so links can be shared).

| Tab | Content |
|---|---|
| **Race** | Result table, position by lap, clean lap times, tyre strategy, pit stops, tyre degradation, weather. |
| **Driver** | The same race filtered to one driver, plus a lap-by-lap table, pace vs field and teammate, stint breakdown. |
| **Track map** | Circuit with a dot per driver, time slider, play/pause, speed, running order. A dashed line on the lap charts follows the slider. Click a dot to open that driver. |
| **Strategy lab** | Simulates 1- and 2-stop plans from this race's measured degradation. |
| **Data quality** | Rejected / excluded / used counts and rates per season, table and race. |

**Every chart has an Expand window:** **Chart**, **Data** (the exact plotted rows + CSV; chart and
table come from the same list), **Method** (source, steps with real thresholds, formula, baseline,
assumptions, limitations) and **Rejected / excluded** (every removed row: stage, rule id, reason,
driver, lap, full raw record; filter and CSV).

### Accounting rule (nothing disappears silently)

For every table of every race: `received = rejected (validation) + excluded (cleaning) + used`. The
tests check it.

---

## 6. Definitions and cleaning rules

All numbers are in `pipeline/config.py`.

* **Adjusted time** = lap time + 0.03 s × lap number (adds back time gained as fuel burns off; an *assumption*).
* **Delta to driver median** / **delta to field** = adjusted time minus the driver's / the race's median adjusted time.
* **Tyre age** = age at stint start + lap − stint start lap + 1. **Stops** = tyre stints − 1.
* **Finish position** = last live position sample (not the official classification).

Cleaning rules, in order; a lap is removed by the first rule it fails, and every removal is recorded:

1. `LAP_NULL_DURATION`: no lap time.
2. `LAP_PIT_OUT`: out-lap after a pit stop.
3. `LAP_SLOW`: adjusted time more than **1.07×** the driver's median (in-laps, traffic, incidents, **safety-car laps**).
4. `PIT_TOO_LONG`: pit-lane time of **40 s or more**.
5. `LOC_ZERO_POSITION`: location frames with x = y = z = 0.

Validation rule ids look like `LAPS_LAP_DURATION_GREATER_THAN` (table, field, pydantic error type).

---

## 7. Output files (`public/data/`)

`index.json` (all races, drivers, quality counts; the only file loaded at start-up) ·
`race_<session>.json` (all charts of a race) · `rejected_<session>.json` (every removed row, loaded only
when a Rejected tab opens) · `track_<session>.json` (outline + positions on one clock).
`schema_version` is checked by the site; bump `SCHEMA_VERSION` in `config.py` and `app.js` together
if you change the layout. After changing a threshold, run `python pipeline/run.py --rebuild` (no downloads).

---

## 8. Hosting

### Option 1: run it on your computer
`python pipeline/server.py`. The Refresh button works; the data lives in `data/` and `public/data/`.

### Option 2: host the Python server (Refresh button works online)
You need a host that runs a **Python web service with a persistent disk** (the pipeline stores its
database and cache on disk). Platforms such as Render, Railway or Fly offer this; check the current
plans, because free tiers may sleep or reset their disk. The generic setup:

* **Build command:** `pip install -r pipeline/requirements.txt`
* **Start command:** `python pipeline/server.py` (the host provides `PORT`; the server reads it)
* **Persistent disk:** mount it somewhere and set the environment variable `F1_STATE_DIR` to that
  path. The database, cache and generated JSON then survive restarts.
* **Environment variables:** `REFRESH_KEY` (recommended: a password for the button).
* Open the site, click **Refresh data** (a few races per click), repeat until you have the seasons you want.

### Option 3: keep the static Vercel site (read-only, no button)
Your existing Vercel deployment still works as a read-only dashboard: run the pipeline locally,
commit `public/data/`, and push. The Refresh button is hidden there. Keep Vercel's **Root Directory**
set to `public`, so it does not try to build the Python code.

---

## 9. Known limitations

* Finish positions are the last live position sample, not the official classification. OpenF1 also has
  `session_result` and `starting_grid` endpoints that could replace this (verify their fields in OpenF1's docs).
* There is no safety-car detection: safety-car laps are removed by the slow-lap rule only.
* The 0.03 s/lap fuel correction is one assumed value for every circuit.
* The strategy lab ignores traffic, safety cars and track evolution: a guide, not a prediction.
* The track outline is polygonal because location is sampled every 2 s.
* Track-map data is only downloaded for years in `LOCATION_YEARS` (default 2025).
* OpenF1 may require authentication for live data; historical data was open when this was written, so
  check OpenF1's current terms if refreshing fails during a race weekend.
