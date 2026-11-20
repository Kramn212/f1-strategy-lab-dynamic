"""
load.py - the LOAD step of the ETL pipeline.

Two destinations
----------------
1. SQLite (config.DB_PATH)   validated data + bookkeeping. This is the pipeline's memory: it
                             is how a refresh knows which races it already has.
2. JSON files (config.OUT_DIR)  what the website reads (index.json, race_<key>.json,
                             rejected_<key>.json, track_<key>.json).

SQLite tables
-------------
sessions, drivers, laps, stints, pit, weather, position, location   the data
counts     per session + table: rows received from the API and rows rejected by validation
rejected   every record that failed validation (full raw record, all errors)

Saving a session first DELETES that session's old rows, so re-loading a race can never
create duplicates.
"""
import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

import config
import transform

META_COLS = {
    "sessions": ["session_key", "meeting_key", "year", "session_name", "meeting_name", "circuit",
                 "country", "location", "date_start", "date_end"],
    "drivers": ["session_key", "driver_number", "name_acronym", "full_name", "team_name", "team_colour"],
    "counts": ["session_key", "table", "received", "rejected"],
    "rejected": transform.REJECT_COLS,
}
TEXT_COLS = {"date", "date_start", "date_end", "compound", "name_acronym", "full_name", "team_name", "team_colour",
             "meeting_name", "circuit", "country", "location", "session_name", "table", "stage", "rule", "reason",
             "errors", "raw"}


def all_cols():
    return {**transform.COLS, **META_COLS}


def connect():
    """Open the database, creating any missing table."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name, cols in all_cols().items():
        if name not in have:
            pd.DataFrame(columns=cols).to_sql(name, con, index=False)
    return con


def complete_sessions():
    """Keys of sessions whose laps were received (i.e. the race data really is in the database)."""
    con = connect()
    rows = con.execute("SELECT session_key FROM counts WHERE [table]='laps' AND received>0").fetchall()
    con.close()
    return {int(r[0]) for r in rows}


def save_session(s, raw_drivers, valid, counts, rejected):
    """Replace everything stored for one session (delete, then insert)."""
    k = int(s["session_key"])
    session_row = {c: s.get(c) for c in META_COLS["sessions"]}
    drivers, seen = [], set()
    for d in raw_drivers:
        if d["driver_number"] not in seen:
            seen.add(d["driver_number"])
            drivers.append({"session_key": k, **{c: d.get(c) for c in META_COLS["drivers"][1:]}})
    payload = {**valid, "sessions": [session_row], "drivers": drivers, "counts": counts, "rejected": rejected}
    con = connect()
    for name in all_cols():
        con.execute(f"DELETE FROM [{name}] WHERE session_key=?", (k,))
    con.commit()
    for name, cols in all_cols().items():
        pd.DataFrame(payload.get(name, []), columns=cols).to_sql(name, con, if_exists="append", index=False)
    con.commit()
    con.close()


def read_session(k):
    """Everything stored for one session as DataFrames (numbers converted back from SQLite)."""
    con = connect()
    out = {}
    for name in all_cols():
        df = pd.read_sql(f"SELECT * FROM [{name}] WHERE session_key=?", con, params=(int(k),))
        for c in df.columns:
            if c not in TEXT_COLS:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        out[name] = df
    con.close()
    return out


def all_sessions():
    con = connect()
    df = pd.read_sql("SELECT * FROM sessions ORDER BY date_start", con)
    con.close()
    return df


# ------------------------------------------------------------------ JSON output
def write_json(name, obj):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.OUT_DIR / name).write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))


def write_race(k, race, rejected_rows, track):
    write_json(f"race_{k}.json", race)
    write_json(f"rejected_{k}.json", {"session_key": k, "rows": rejected_rows})
    if track:
        write_json(f"track_{k}.json", track)


def race_file_exists(k):
    return (config.OUT_DIR / f"race_{k}.json").exists()


def write_index():
    """index.json = the only file the website loads at start-up: every race, its drivers and quality counts."""
    idx = {"schema_version": config.SCHEMA_VERSION,
           "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "years": [], "sessions": {},
           "config": {"fuel_correction_s_per_lap": config.FUEL_CORR_S_PER_LAP, "slow_lap_factor": config.SLOW_LAP_FACTOR,
                      "pit_max_s": config.PIT_MAX_S, "loc_step_s": config.LOC_STEP_S}}
    for s in all_sessions().to_dict("records"):
        f = config.OUT_DIR / f"race_{int(s['session_key'])}.json"
        if not f.exists():
            continue
        race = json.loads(f.read_text())
        idx["sessions"].setdefault(str(race["year"]), []).append({
            "session_key": race["session_key"], "name": race["name"], "circuit": race["circuit"],
            "country": race["country"], "date": race["date"], "file": f.name, "drivers": race["drivers"],
            "quality": race["quality"], "has": {t: q["received"] > 0 for t, q in race["quality"].items()}})
    idx["years"] = sorted(int(y) for y in idx["sessions"])
    write_json("index.json", idx)
    return idx
