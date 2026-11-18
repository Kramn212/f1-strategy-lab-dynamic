"""
etl.py - Extract -> Validate -> Load.

Run:  python pipeline/etl.py

What it does
------------
1. EXTRACT  Calls the OpenF1 API (throttled, cached, retried) for every
            session in config.YEARS / config.SESSION_TYPES.
2. VALIDATE Each record is checked against models.py. Failures go to the
            `rejected` table with ALL errors and the FULL raw record.
3. LOAD     Everything is written to SQLite (config.DB_PATH).

Bookkeeping tables written next to the data
-------------------------------------------
sessions   one row per session (race name, circuit, dates ...)
drivers    driver code / name / team / team colour per session
counts     per session + table: rows received from API and rows rejected
coverage   per session + table: did the API return anything at all?
rejected   every record that failed validation (stage = "validation").
           eda.py later appends cleaning exclusions (stage = "cleaning").

Incremental: sessions already in the DB are skipped, so re-running only
fetches new races. Nothing is ever silently dropped: for every table
    received = rejected + excluded + used
and the counts needed to prove that are stored.
"""
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlencode

import pandas as pd
import requests
from pydantic import ValidationError

import config
import models

# table name -> validation model
TABLES = {
    "laps": models.Lap, "stints": models.Stint, "pit": models.Pit,
    "weather": models.Weather, "position": models.Position,
    "race_control": models.RaceControl, "location": models.Location,
}
# column layout of the bookkeeping tables
META_COLS = {
    "sessions": ["session_key", "meeting_key", "year", "session_name", "meeting_name",
                 "circuit", "country", "location", "date_start", "date_end"],
    "drivers": ["session_key", "driver_number", "name_acronym", "full_name", "team_name", "team_colour"],
    "counts": ["session_key", "table", "received", "rejected"],
    "coverage": ["session_key", "table", "available"],
    "rejected": ["stage", "table", "session_key", "driver_number", "lap_number",
                 "rule", "reason", "errors", "raw"],
}


def all_columns():
    """table name -> list of columns, for validated tables and bookkeeping tables."""
    cols = {t: list(m.model_fields) for t, m in TABLES.items()}
    cols.update(META_COLS)
    return cols


# ============================================================ 1. EXTRACT
_last_call = 0.0


def get(endpoint, _post=None, **params):
    """
    GET one OpenF1 endpoint and return the decoded JSON list.

    * Responses are cached on disk (cache/), keyed by endpoint + parameters,
      so re-running never repeats a request.
    * Calls are spaced config.REQUEST_GAP_S apart (rate limit).
    * `_post` (optional) transforms the data BEFORE it is cached. Used to
      downsample the huge location table so the cache stays small.
    * Parameter names ending in > or < become OpenF1 range filters:
      get("location", **{"date>": "2025-03-16T04:00:00"}) -> ...&date>=2025-03-16T04:00:00
    """
    global _last_call
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = json.dumps(params, sort_keys=True)
    cache_file = config.CACHE_DIR / f"{endpoint}_{hashlib.md5(key.encode()).hexdigest()[:12]}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    url = f"{config.BASE_URL}/{endpoint}?" + urlencode(params, safe="<>:")
    for attempt in range(config.MAX_RETRIES):
        wait = config.REQUEST_GAP_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = requests.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:                       # rate limited -> back off
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 404:                       # OpenF1 answers 404 for "no rows"
            data = []
            break
        r.raise_for_status()
        data = r.json()
        break
    else:
        raise RuntimeError(f"OpenF1 request kept failing: {url}")

    if _post:
        data = _post(data)
    cache_file.write_text(json.dumps(data))
    return data


def _downsample_location(rows):
    """Keep one location sample per driver every config.LOC_STEP_S seconds."""
    out, last = [], None
    for r in rows:
        try:
            t = datetime.fromisoformat(r["date"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if last is None or t - last >= config.LOC_STEP_S:
            out.append({k: r.get(k) for k in ("session_key", "driver_number", "date", "x", "y", "z")})
            last = t
    return out


# ========================================================== 2. VALIDATE
def rule_code(table, err):
    """Stable machine-readable id for a failed rule, e.g. LAPS_LAP_DURATION_GREATER_THAN."""
    loc = "_".join(str(p) for p in err["loc"]) or "ROW"
    return f"{table}_{loc}_{err['type']}".upper()


def validate(table, rows, session_key, rejected):
    """Return the valid rows (as plain dicts); append every failure to `rejected`."""
    model, good = TABLES[table], []
    for raw in rows:
        try:
            good.append(model.model_validate(raw).model_dump())
        except ValidationError as exc:
            errs = exc.errors()
            raw_d = raw if isinstance(raw, dict) else {"value": repr(raw)}
            rejected.append({
                "stage": "validation", "table": table, "session_key": session_key,
                "driver_number": raw_d.get("driver_number"),
                "lap_number": raw_d.get("lap_number"),
                "rule": rule_code(table, errs[0]),
                "reason": "; ".join(f"{'.'.join(map(str, e['loc'])) or 'row'}: {e['msg']}" for e in errs),
                "errors": json.dumps([{"field": ".".join(map(str, e["loc"])), "type": e["type"],
                                       "msg": e["msg"]} for e in errs]),   # ALL errors, not just the first
                "raw": json.dumps(raw_d, default=str),                      # FULL record, never truncated
            })
    return good


def ingest(table, raw_rows, key, store, rejected, counts, coverage):
    """Validate one table of one session and record counts + coverage."""
    good = validate(table, raw_rows, key, rejected)
    store[table] += good
    counts.append({"session_key": key, "table": table,
                   "received": len(raw_rows), "rejected": len(raw_rows) - len(good)})
    coverage.append({"session_key": key, "table": table, "available": int(len(raw_rows) > 0)})


# ============================================================== 3. LOAD
def read_existing():
    """Load what is already in the DB so a re-run can append instead of rebuild."""
    cols, out = all_columns(), {}
    con = sqlite3.connect(config.DB_PATH) if config.DB_PATH.exists() else None
    for name, c in cols.items():
        df = pd.DataFrame(columns=c)
        if con is not None:
            try:
                df = pd.read_sql(f"SELECT * FROM [{name}]", con)
            except Exception:           # table does not exist yet
                pass
        out[name] = df
    if con is not None:
        con.close()
    return out


def save(existing, new):
    """Append `new` rows (dict of lists) to `existing` frames and rewrite the DB."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    cols = all_columns()
    con = sqlite3.connect(config.DB_PATH)
    for name, c in cols.items():
        add = pd.DataFrame(new.get(name, []), columns=c)
        # object dtype keeps ints/None intact (and avoids concat warnings on empty frames)
        frames = [f.astype(object) for f in (existing[name], add) if len(f)]
        df = pd.concat(frames, ignore_index=True).drop_duplicates() if frames else add
        df.to_sql(name, con, if_exists="replace", index=False)
    con.commit()
    con.close()


# ================================================================ MAIN
def load_session(s, year, names, store, rejected, counts, coverage):
    """Fetch + validate every table of ONE session."""
    k = s["session_key"]
    store["sessions"].append({
        "session_key": k, "meeting_key": s.get("meeting_key"), "year": year,
        "session_name": s.get("session_name"),
        "meeting_name": names.get(s.get("meeting_key")) or f"{s.get('country_name')} Grand Prix",
        "circuit": s.get("circuit_short_name"), "country": s.get("country_name"),
        "location": s.get("location"), "date_start": s.get("date_start"), "date_end": s.get("date_end"),
    })
    drivers = get("drivers", session_key=k)
    seen = set()
    for d in drivers:
        if d["driver_number"] in seen:
            continue
        seen.add(d["driver_number"])
        store["drivers"].append({
            "session_key": k, "driver_number": d["driver_number"],
            "name_acronym": d.get("name_acronym"), "full_name": d.get("full_name"),
            "team_name": d.get("team_name"), "team_colour": d.get("team_colour")})

    pit = get("pit", session_key=k)
    for p in pit:                                    # API field name varies by season
        p["pit_duration"] = p.get("lane_duration") or p.get("pit_duration")
    ingest("laps", get("laps", session_key=k), k, store, rejected, counts, coverage)
    ingest("stints", get("stints", session_key=k), k, store, rejected, counts, coverage)
    ingest("pit", pit, k, store, rejected, counts, coverage)
    ingest("weather", get("weather", session_key=k), k, store, rejected, counts, coverage)
    ingest("position", get("position", session_key=k), k, store, rejected, counts, coverage)
    ingest("race_control", get("race_control", session_key=k), k, store, rejected, counts, coverage)

    loc = []                                          # location: optional + heavy
    if year in config.LOCATION_YEARS and s.get("date_start") and s.get("date_end"):
        window = {"date>": s["date_start"], "date<": s["date_end"]}
        for n in sorted(seen):
            loc += get("location", _post=_downsample_location, session_key=k, driver_number=n, **window)
    ingest("location", loc, k, store, rejected, counts, coverage)


def main():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = read_existing()
    done = set(existing["sessions"]["session_key"].astype(int))
    store = {t: [] for t in all_columns()}
    rejected, counts, coverage = store["rejected"], store["counts"], store["coverage"]

    for year in config.YEARS:
        names = {m["meeting_key"]: m.get("meeting_name") or m.get("meeting_official_name")
                 for m in get("meetings", year=year)}
        for stype in config.SESSION_TYPES:
            for s in get("sessions", year=year, session_name=stype):
                if int(s["session_key"]) in done:
                    continue                                  # incremental: skip loaded sessions
                print(f"  loading {year} {s.get('country_name')} ({s['session_key']})")
                load_session(s, year, names, store, rejected, counts, coverage)
    save(existing, store)
    print(f"ETL done: {len(store['sessions'])} new session(s), {len(rejected)} rejected record(s).")


if __name__ == "__main__":
    main()
