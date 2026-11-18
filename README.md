# F1 Strategy Lab

A drill-down dashboard for Formula 1 race analysis built on the free [OpenF1](https://openf1.org) API.

**Year → Grand Prix → Driver.** Pick a season, pick a race, optionally pick a driver, and every
table and chart changes with the selection. Every chart has an **Expand** button that shows
*the exact rows plotted*, *the method and formula used*, and *every row that was removed* on the
way to that chart (and why).

*Unofficial project; not affiliated with Formula 1 or the FIA.*

---

## 1. Quick start

You need Python 3.10+ (and optionally Node 18+ for the JS test).

```bash
pip install -r pipeline/requirements.txt

# A) Preview with SAMPLE data - no internet, takes seconds
python pipeline/demo.py
python -m http.server -d public 8000          # open http://localhost:8000

# B) Real data from OpenF1 (slow the first time, see "Run time")
python pipeline/etl.py                        # download + validate -> data/f1.db
python pipeline/eda.py                        # analyse -> public/data/*.json
python -m http.server -d public 8000
```

> Open the site through `http://localhost`, **not** by double-clicking `index.html`
> (`file://` pages are not allowed to load the JSON files).

**Run time.** The pipeline waits ~2.2 s between API calls (about 27 per minute) to respect
OpenF1's rate limit. Roughly: 9 calls per race, plus ~20 per race that has location data. A
2023-2025 load is on the order of an hour the first time. Responses are cached in `cache/`, so
re-runs are fast, and only *new* races are fetched.

**Tests (no internet needed)**

```bash
python pipeline/tests/offline_test.py         # whole pipeline against a fake API
node tests/helpers.test.js                    # pure JS helpers + strategy simulator
```

---

## 2. How it works

```
OpenF1 API ──etl.py──▶ SQLite (data/f1.db) ──eda.py──▶ public/data/*.json ──▶ browser (app.js)
              │ validate (models.py)           │ clean + analyse                │ draw charts + tables
              └▶ rejected table                └▶ rejected_<race>.json          └▶ Expand: Data / Method / Rejected
```

| File | Job |
|---|---|
| `pipeline/config.py` | **All** settings and thresholds (years, fuel correction, slow-lap factor, minimum sample sizes...). |
| `pipeline/models.py` | Validation schemas (pydantic) for every OpenF1 table. |
| `pipeline/etl.py` | Extract (throttled, cached, retried) → validate → load into SQLite. Incremental. |
| `pipeline/eda.py` | Per-race analysis + pooled (season / overall) charts → JSON files. |
| `pipeline/methods.py` | The "Method" text of every chart, built from `config.py` so text and code can't drift apart. |
| `pipeline/fake_api.py` | Deterministic fake OpenF1 server for tests and the demo. |
| `pipeline/demo.py` | Builds sample dashboard data offline. |
| `public/index.html`, `style.css`, `app.js` | The static dashboard (no build step). |
| `vercel.json` | Tells Vercel to serve `public/` and how to cache the data files. |

### Database tables (SQLite)

`sessions`, `drivers`, `laps`, `stints`, `pit`, `weather`, `position`, `race_control`, `location`
hold the data. Four bookkeeping tables make the pipeline auditable:

* `counts`: per session and table, rows **received** from the API and rows **rejected**.
* `coverage`: per session and table, whether the API returned anything at all.
* `rejected`: every record that failed validation (`stage = validation`).
* `excluded`: every valid row that cleaning removed (`stage = cleaning`), written by `eda.py`.

### The accounting rule (nothing disappears silently)

For **every table of every race**:

```
received  =  rejected (validation)  +  excluded (cleaning)  +  used
```

This is checked by the test suite and is what the "Rejected / excluded" tab and the
**Data quality** tab show. A third kind of removal, **aggregation** (an age group with too few laps
to plot), is listed per chart but is *not* part of the table counts because those laps are still
"used" by other charts.

---

## 3. The dashboard

**Top bar:** Year, Grand Prix and Driver selectors. The URL stores your choices
(`#y=2024&gp=9001&d=AAA&tab=race`), so you can share or bookmark a view.

| Tab | What it shows |
|---|---|
| **Overall** | Charts pooled over every season loaded. |
| **Season** | The same pooled charts for the selected year only, plus a driver summary table. |
| **Race** | Result table, position by lap, clean lap times, tyre strategy, pit stops, tyre degradation, weather, for the selected Grand Prix. |
| **Driver** | The same race, filtered to one driver, plus lap-by-lap table, pace vs field and teammate, stint breakdown. |
| **Track map** | Circuit drawing with a dot per driver, a time slider, play/pause, speed and a running-order panel. A white dashed line on the lap charts follows the slider. Click a dot to open that driver. |
| **Strategy lab** | Simulates 1- and 2-stop plans from the measured degradation curves (the selected race if there is one, otherwise all seasons). |
| **Data quality** | Rejection and exclusion counts and rates per season, table and race. |

### The Expand window (every chart)

| Tab | Content |
|---|---|
| **Chart** | A larger chart. |
| **Data** | The exact rows plotted (sortable, CSV download). The chart and this table are drawn from the *same* list, so they cannot disagree. |
| **Method** | Where the data comes from, each filter step with its real threshold, the formula, the baseline, assumptions and limitations. |
| **Rejected / excluded** | Counts (received, rejected, excluded, used, rates) plus every removed row relevant to this chart: stage, rule id, reason, driver, lap, and the full raw record. Filter by stage or rule; CSV download. |

---

## 4. Definitions and cleaning rules

All numbers live in `pipeline/config.py`.

| Term | Meaning |
|---|---|
| **Clean lap** | A lap that passed validation and every cleaning rule below. |
| **Adjusted time** | `lap time + 0.03 s × lap number`: adds back time gained as fuel burns off. *An assumption, not a measurement.* |
| **Delta to driver median** | `adjusted time − that driver's median adjusted time` in the race. |
| **Delta to field** | `adjusted time − median adjusted time of all clean laps` in the race. |
| **Tyre age** | `age at stint start + lap − stint start lap + 1`. |
| **Stops** | Number of tyre stints − 1. |
| **Finish position** | Last live position sample (not the official classification, so penalties are not reflected). |

Cleaning rules, applied **in this order** (a lap is removed by the first rule it fails; each
removal is recorded with its rule id):

1. `LAP_NULL_DURATION`: no lap time recorded.
2. `LAP_PIT_OUT`: out-lap after a pit stop.
3. `LAP_NEUTRALISED`: overlaps a safety car, virtual safety car or red flag (from race-control messages).
4. `LAP_SLOW`: adjusted time more than **1.07×** the driver's median (in-laps, traffic, incidents).
5. `PIT_TOO_LONG`: pit-lane time of **40 s or more** (repairs, red-flag stops).
6. `LOC_ZERO_POSITION`: location frames with x = y = z = 0.

Validation rule ids look like `LAPS_LAP_DURATION_GREATER_THAN` (table, field, pydantic error type).

---

## 5. Output files (`public/data/`)

| File | Content |
|---|---|
| `index.json` | Schema version, years, every Grand Prix per year (name, circuit, date, drivers, data coverage, quality counts) and the thresholds used. The only file loaded at start-up. |
| `race_<session>.json` | All charts of one race, drivers, quality counts. Loaded when a race is selected. |
| `rejected_<session>.json` | Every removed row of that race. Loaded only when a *Rejected* tab is opened. |
| `track_<session>.json` | Circuit outline + all car positions on one clock. Loaded by the Track map. |
| `season_<year>.json`, `overall.json` | Pooled charts. |

A **chart** object contains: `id`, `title`, `scope` (`race`/`both`/`driver`/`track`/`pooled`),
`plot` (how to draw it), `columns`, `rows` (the plotted rows), `method`, `tables`, `dropped`,
`status` (`ok`/`insufficient_data`). `schema_version` is checked by the site; if you change the
layout, bump `SCHEMA_VERSION` in `config.py` and `app.js`.

---

## 6. Common changes

* **Different seasons:** edit `YEARS` in `config.py`, re-run `etl.py` then `eda.py`.
* **Add a new race:** just re-run both scripts (only the new session is downloaded).
* **Track map for more seasons:** add the year to `LOCATION_YEARS`. Location data is by far the
  biggest download (≈20 API calls per race); a race's `track_*.json` is about 0.1-0.6 MB.
* **Sprint / qualifying:** add the name (e.g. `"Sprint"`) to `SESSION_TYPES`. Tables already carry
  `session_key`; they show up as extra entries in the Grand Prix list.
* **Change a threshold:** edit `config.py`; the Method text updates automatically.
* **Add a chart:** build it with `chart(...)` in `eda.py` (rows + columns + plot spec), add a
  method entry in `methods.py`. If it uses an existing plot type (`line`, `bar`, `stacked`,
  `stints`, `table`) the website needs no change, and Expand/Data/Method/Rejected work automatically.
* **Re-run from scratch:** delete `data/f1.db` (keep `cache/` to avoid re-downloading).

---

## 7. Deploying to Vercel

The site is 100 % static: there is **no build step and no server**. Vercel just serves the
`public/` folder. The important detail: **`public/data/` must be in your repository** (it is not
git-ignored), because it is generated on your machine, not on Vercel.

### Option A: GitHub (recommended: redeploys on every push)

1. Generate the data locally: `python pipeline/etl.py && python pipeline/eda.py`
   (or `python pipeline/demo.py` just to test the deployment with sample data).
2. Check that `public/data/index.json` exists, then push the project to a GitHub repository:
   ```bash
   git init && git add . && git commit -m "F1 Strategy Lab"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
   (`/cache/` and `/data/` at the project root are git-ignored, which is intended; `public/data/` is NOT ignored and must be committed.)
3. Go to <https://vercel.com/new>, **Import** the repository.
4. On the configuration screen:
   * **Framework Preset:** *Other*
   * **Build Command / Install Command:** leave empty
   * **Output Directory:** `public` (already set by `vercel.json`, so it may be pre-filled)
5. Click **Deploy**. You get a URL like `https://<repo>.vercel.app`.

### Option B: Vercel CLI (no GitHub)

```bash
npm i -g vercel
vercel login
vercel            # first run answers a few questions; accept the defaults -> preview URL
vercel --prod     # publish to the production URL
```

### Updating after each race

```bash
python pipeline/etl.py && python pipeline/eda.py   # downloads only the new race
git add public/data && git commit -m "Add latest race" && git push
```
Vercel redeploys automatically (Option A). With the CLI, run `vercel --prod` again.

### What `vercel.json` does

* `outputDirectory: "public"`: serve that folder as the site root.
* `cleanUrls`: tidy URLs.
* A `Cache-Control` header for `/data/*` (5 minutes in the browser, longer on Vercel's CDN) so
  repeat visits are fast. If you push new data and still see old data, hard-refresh (Ctrl/Cmd+Shift+R).

### Size and limits

Per race: a race file is roughly 100 KB, the rejected file 15-50 KB and (if present) the track
file 0.1-0.6 MB, so a 3-season site with tracks for one season is in the tens of MB. Vercel's
plan limits on deployment size and bandwidth change over time: check the current numbers in the Vercel
docs before adding location data for many seasons. If you hit limits, reduce `LOCATION_YEARS`, or raise
`LOC_STEP_S` in `config.py` to thin out the location samples.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "No data found" on the site | `public/data/index.json` was not committed. Run the pipeline, `git add public/data`, push. |
| Blank charts, console error about `Chart` | The Chart.js CDN is blocked; download `chart.umd.min.js` into `public/` and change the `<script src>` in `index.html`. |
| "data files are from a different version" | Re-run `python pipeline/eda.py` so the JSON matches `app.js`. |
| Track map says "unavailable" | No location data for that race: the year is not in `LOCATION_YEARS`, or OpenF1 has none. |
| Works on Vercel, not by double-click | Expected: serve over HTTP (`python -m http.server -d public`). |

---

## 8. Known limitations

* Finish positions come from live position samples, not the official classification.
* Safety-car detection reads race-control message wording; if OpenF1 changes the wording those laps
  are only caught by the slow-lap rule.
* The 0.03 s/lap fuel correction is a single assumed value for all circuits.
* Pooled charts mix circuits, temperatures and car generations; use per-race charts for detail.
* The strategy lab ignores traffic, safety cars and track evolution. It is a guide, not a prediction.
* The track-map outline is polygonal because location is sampled every 2 s.
* Without `SESSION_TYPES` beyond `"Race"`, there is no qualifying or practice analysis yet.
