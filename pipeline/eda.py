"""
eda.py - Analyse the SQLite data and write the JSON the website loads.

Run:  python pipeline/eda.py        (after etl.py)

Output (config.OUT_DIR = public/data/)
--------------------------------------
index.json              years -> Grand Prix list, drivers, data coverage, quality counts, thresholds
race_<session>.json     everything for ONE race: charts + per-driver data + quality counts
rejected_<session>.json every row removed for that race (validation rejects + cleaning exclusions)
track_<session>.json    track outline + all car positions on one clock (only if location data exists)
season_<year>.json      pooled charts for one season
overall.json            pooled charts for all seasons

Every chart is a dict:
    id, title, scope        scope = race | both | driver | track | pooled
    plot                    how to draw it (type + which columns are x / y / group)
    columns                 [{key, label, unit}]   column descriptions
    rows                    THE rows that are plotted - the website draws the chart AND the
                            data table from this one list, so they can never disagree
    method                  explanation + formula (methods.py)
    tables                  which DB tables feed it (selects the rows shown in the "Rejected" tab)
    dropped                 aggregation-stage drops (e.g. an age group with too few laps)
    status                  "ok" | "insufficient_data"

Accounting rule, checked by tests, for every table of every race:
    received = rejected (validation) + excluded (cleaning) + used
"""
import json
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

import config
import etl
import methods

TEXT_COLS = {"date", "date_start", "date_end", "compound", "category", "flag", "message",
             "name_acronym", "full_name", "team_name", "team_colour", "meeting_name", "circuit",
             "country", "location", "session_name", "table", "stage", "rule", "reason", "errors", "raw"}
TABLE_NAMES = list(etl.TABLES)
RES_COLS = ["driver", "team", "start_pos", "finish_pos", "gained", "stops", "laps", "best_lap",
            "median_lap", "lap_std", "avg_delta_field"]


# ============================================================== helpers
def dt(s):
    """Parse ISO timestamps to timezone-aware datetime64[ns] (NaT for missing)."""
    return pd.to_datetime(s, utc=True, format="ISO8601").dt.as_unit("ns")


def val(v, nd=3):
    """Make one value JSON-safe: numpy -> python, NaN/NaT -> None, floats rounded."""
    if v is None or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return round(float(v), nd) if np.isfinite(v) else None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def recs(df, cols=None):
    """DataFrame -> list of JSON-safe dicts."""
    cols = cols or list(df.columns)
    return [{c: val(v) for c, v in zip(cols, row)} for row in df[cols].itertuples(index=False, name=None)]


def col(key, label, unit=""):
    return {"key": key, "label": label, "unit": unit}


def chart(cid, title, scope, plot, columns, rows, tables, method=None, min_rows=1, dropped=None):
    """Assemble one chart dict (see module docstring)."""
    return {"id": cid, "title": title, "scope": scope, "plot": plot, "columns": columns,
            "rows": rows, "method": methods.get(method or cid), "tables": tables,
            "dropped": dropped or [], "status": "ok" if len(rows) >= min_rows else "insufficient_data",
            "n": len(rows), "need": min_rows}


def exclusion(table, k, driver_number, lap_number, rule, why, raw, stage="cleaning"):
    """One removed row. Same shape as a validation rejection so the website shows both together."""
    return {"stage": stage, "table": table, "session_key": k, "driver_number": val(driver_number),
            "lap_number": val(lap_number), "rule": rule, "reason": why, "errors": None,
            "raw": json.dumps({a: val(b, 6) for a, b in raw.items()})}


def write_json(path, obj):
    path.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))


# ================================================================ load
def load():
    """Read every table from SQLite; numeric columns coerced to numbers."""
    con = sqlite3.connect(config.DB_PATH)
    T = {}
    for name in etl.all_columns():
        df = pd.read_sql(f"SELECT * FROM [{name}]", con)
        for c in df.columns:
            if c not in TEXT_COLS:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        T[name] = df
    con.close()
    return T


# ============================================= safety-car / red-flag periods
def _s(v):
    return "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v).upper()


def neutral_intervals(rc):
    """
    Periods when racing was neutralised, as [(start, end), ...] timestamps.

    Heuristic on race-control messages: a period opens on "... DEPLOYED" (safety car / VSC)
    or a RED flag, and closes on "... IN THIS LAP" / "... ENDING" (safety car) or a
    GREEN/CLEAR flag (after a red flag). An unclosed period runs to the last message.
    """
    if rc.empty:
        return []
    r = rc.assign(t=dt(rc.date)).sort_values("t")
    out, start, kind = [], None, None
    for t, cat, flag, msg in zip(r.t, r.category, r.flag, r.message):
        cat, flag, msg = _s(cat), _s(flag), _s(msg)
        if start is None:
            if cat == "SAFETYCAR" and "DEPLOYED" in msg:
                start, kind = t, "sc"
            elif flag == "RED":
                start, kind = t, "red"
        else:
            sc_end = kind == "sc" and cat == "SAFETYCAR" and ("IN THIS LAP" in msg or "ENDING" in msg)
            red_end = kind == "red" and flag in ("GREEN", "CLEAR")
            if sc_end or red_end:
                out.append((start, t))
                start = None
    if start is not None:
        out.append((start, r.t.max()))
    return out


# ============================================================ lap cleaning
def flag_laps(L, neutral, k):
    """
    Apply the cleaning rules IN ORDER. A lap is removed by the first rule it fails.
    Adds L["status"] = "clean" or the rule id, and returns (L, list of exclusion records).
    """
    ex, alive = [], pd.Series(True, index=L.index)
    L["status"] = "clean"

    def drop(mask, rule, why):
        m = alive & mask.fillna(False)
        L.loc[m, "status"] = rule
        for r in L[m].itertuples():
            ex.append(exclusion("laps", k, r.driver_number, r.lap_number, rule, why,
                                {"lap_duration": r.lap_duration, "date_start": r.date_start,
                                 "is_pit_out_lap": r.is_pit_out_lap, "adj": r.adj}))
        alive.loc[m] = False

    drop(L.lap_duration.isna(), "LAP_NULL_DURATION", "lap has no recorded duration")
    drop(L.is_pit_out_lap.fillna(0).astype(bool), "LAP_PIT_OUT",
         "out-lap after a pit stop (cold tyres, pit-lane exit)")
    nm = pd.Series(False, index=L.index)
    for s0, e0 in neutral:
        nm |= (L.t0 < e0) & (L.t1 > s0)
    drop(nm, "LAP_NEUTRALISED", "lap overlaps a safety car / virtual safety car / red-flag period")
    L["med"] = L[alive].groupby("driver_number").adj.transform("median")
    drop(L.adj > L.med * config.SLOW_LAP_FACTOR, "LAP_SLOW",
         f"adjusted lap time > {config.SLOW_LAP_FACTOR}x the driver's median (in-lap, traffic or incident)")
    return L, ex


def attach_stints(L, stints):
    """Add compound + tyre age to every lap by matching it to the stint that contains it."""
    L["compound"], L["age"] = None, np.nan
    if stints.empty or L.empty:
        return L
    m = L[["driver_number", "lap_number"]].merge(stints, on="driver_number")
    m = m[(m.lap_number >= m.lap_start) & (m.lap_number <= m.lap_end)]
    m = m.assign(age=m.tyre_age_at_start.fillna(0) + m.lap_number - m.lap_start + 1)
    m = m.drop_duplicates(["driver_number", "lap_number"])[["driver_number", "lap_number", "compound", "age"]]
    return L.drop(columns=["compound", "age"]).merge(m, on=["driver_number", "lap_number"], how="left")


def attach_position(L, pos):
    """Add the race position at (nearest to) each lap's start."""
    L["position"] = np.nan
    if pos.empty or L.t0.isna().all():
        return L
    P = pos.assign(pt=dt(pos.date)).dropna(subset=["pt"]).sort_values("pt")[["driver_number", "pt", "position"]]
    left = L.dropna(subset=["t0"]).sort_values("t0")[["driver_number", "lap_number", "t0"]]
    m = pd.merge_asof(left, P.rename(columns={"position": "pos_m"}), left_on="t0", right_on="pt",
                      by="driver_number", direction="nearest")
    L = L.drop(columns=["position"]).merge(m[["driver_number", "lap_number", "pos_m"]],
                                           on=["driver_number", "lap_number"], how="left")
    return L.rename(columns={"pos_m": "position"})


def attach_weather(L, wx):
    """Add track temperature and rain flag nearest to each lap's start."""
    L["track_temp"], L["rain"] = np.nan, 0
    if wx.empty or L.t0.isna().all():
        return L
    W = wx.assign(wt=dt(wx.date)).dropna(subset=["wt"]).sort_values("wt")[["wt", "track_temperature", "rainfall"]]
    left = L.dropna(subset=["t0"]).sort_values("t0")[["driver_number", "lap_number", "t0"]]
    m = pd.merge_asof(left, W, left_on="t0", right_on="wt", direction="nearest")
    L = L.drop(columns=["track_temp", "rain"]).merge(
        m[["driver_number", "lap_number", "track_temperature", "rainfall"]],
        on=["driver_number", "lap_number"], how="left")
    return L.rename(columns={"track_temperature": "track_temp", "rainfall": "rain"})


# ============================================ degradation (race and pooled)
def degradation_rows(C, min_n, scope_label):
    """
    Mean delta per (compound, tyre age). Returns (rows, dropped). Groups with too few laps
    are NOT plotted but are reported in `dropped` (aggregation stage) - nothing vanishes silently.
    """
    d = C[C.age.between(1, config.DEG_MAX_AGE)].dropna(subset=["compound", "delta"])
    if d.empty:
        return [], []
    g = d.groupby(["compound", "age"]).delta.agg(["mean", "count"]).reset_index()
    small = g[g["count"] < min_n]
    big = g[g["count"] >= min_n]
    few_ages = big.groupby("compound").age.nunique()
    short = set(few_ages[few_ages < config.MIN_AGES_PER_COMPOUND].index) | (set(g.compound) - set(big.compound))
    dropped = [{"stage": "aggregation", "table": "laps", "session_key": None, "driver_number": None,
                "lap_number": None, "rule": "DEG_MIN_SAMPLE",
                "reason": f"{r.compound} age {int(r.age)}: only {int(r.count)} lap(s), need {min_n} ({scope_label})",
                "errors": None, "raw": json.dumps({"compound": r.compound, "age": int(r.age), "n": int(r.count)})}
               for r in small.itertuples()]
    dropped += [{"stage": "aggregation", "table": "laps", "session_key": None, "driver_number": None,
                 "lap_number": None, "rule": "DEG_MIN_AGES",
                 "reason": f"{c}: fewer than {config.MIN_AGES_PER_COMPOUND} usable tyre ages - curve not drawn",
                 "errors": None, "raw": json.dumps({"compound": c})} for c in sorted(short)]
    big = big[~big.compound.isin(short)].sort_values(["compound", "age"])
    rows = [{"compound": r.compound, "age": int(r.age), "delta": val(r.mean), "n": int(r.count)}
            for r in big.itertuples()]
    return rows, dropped


DEG_COLS = [col("compound", "Compound"), col("age", "Tyre age", "laps"),
            col("delta", "Mean delta to driver median", "s"), col("n", "Laps (n)")]
DEG_PLOT = {"type": "line", "x": "age", "y": "delta", "group": "compound",
            "xlabel": "Tyre age (laps)", "ylabel": "Seconds slower than driver median"}


# =========================================================== one race
def process_race(s, T, cnt):
    """
    Analyse ONE session. Returns (race_json, rejected_rows, track_json_or_None,
    pool_dict_for_season_charts, quality_dict).
    """
    k = int(s["session_key"])
    pick = lambda n: T[n][T[n].session_key == k].copy()          # noqa: E731
    laps, stints, pit, wx, pos, rc, drv, loc = (pick(n) for n in (
        "laps", "stints", "pit", "weather", "position", "race_control", "drivers", "location"))

    # ---- driver lookup (code, name, team, colour) --------------------
    dm = {}
    for r in drv.drop_duplicates("driver_number").itertuples():
        n = int(r.driver_number)
        dm[n] = {"number": n,
                 "code": r.name_acronym if isinstance(r.name_acronym, str) else str(n),
                 "name": r.full_name if isinstance(r.full_name, str) else str(n),
                 "team": r.team_name if isinstance(r.team_name, str) else "Unknown",
                 "colour": f"#{r.team_colour}" if isinstance(r.team_colour, str) and r.team_colour else None}
    for tbl in (laps, stints, pos, pit):
        for n in tbl.driver_number.dropna().astype(int).unique():
            dm.setdefault(n, {"number": n, "code": str(n), "name": str(n), "team": "Unknown", "colour": None})
    code = lambda n: dm[int(n)]["code"]                          # noqa: E731
    team = lambda n: dm[int(n)]["team"]                          # noqa: E731
    ex = []                                                      # every cleaning exclusion of this race

    # ---- laps: attach info, flag cleaning rules ----------------------
    L = laps.copy()
    L["t0"] = dt(L.date_start)
    L["t1"] = L.t0 + pd.to_timedelta(L.lap_duration, unit="s")
    L["adj"] = L.lap_duration + config.FUEL_CORR_S_PER_LAP * L.lap_number
    L, lex = flag_laps(L, neutral_intervals(rc), k)
    ex += lex
    L = attach_stints(L, stints)
    L = attach_position(L, pos)
    L = attach_weather(L, wx)
    L["driver"] = L.driver_number.map(code)
    L["team"] = L.driver_number.map(team)

    C = L[L.status == "clean"].copy()                            # the CLEAN laps
    C["delta"] = C.adj - C.groupby("driver_number").adj.transform("median")
    C["fdelta"] = C.adj - C.adj.median()
    cnt_t = C.groupby(["team", "lap_number"]).adj.transform("count")
    tot_t = C.groupby(["team", "lap_number"]).adj.transform("sum")
    C["delta_team"] = np.where(cnt_t == 2, C.adj - (tot_t - C.adj), np.nan)

    stops = (stints.groupby("driver_number").stint_number.nunique() - 1).clip(lower=0)
    C["stops"] = C.driver_number.map(stops)

    # ---- pit stops ----------------------------------------------------
    P = pit.copy()
    for r in P[P.pit_duration >= config.PIT_MAX_S].itertuples():
        ex.append(exclusion("pit", k, r.driver_number, r.lap_number, "PIT_TOO_LONG",
                            f"pit-lane time {r.pit_duration:.1f}s >= {config.PIT_MAX_S}s (repair / red-flag stop)",
                            {"pit_duration": r.pit_duration}))
    P = P[P.pit_duration < config.PIT_MAX_S].copy()
    P["driver"], P["team"] = P.driver_number.map(code), P.driver_number.map(team)

    # ---- location: drop invalid all-zero frames ----------------------
    zero = (loc.x == 0) & (loc.y == 0) & (loc.z == 0)
    for r in loc[zero].itertuples():
        ex.append(exclusion("location", k, r.driver_number, None, "LOC_ZERO_POSITION",
                            "x = y = z = 0: invalid / not-yet-streaming frame", {"date": r.date}))
    loc = loc[~zero]

    # ---- quality counts (received = rejected + excluded + used) -------
    used = {"laps": len(C), "pit": len(P), "location": len(loc), "stints": len(stints),
            "weather": len(wx), "position": len(pos), "race_control": len(rc)}
    quality = {}
    for t in TABLE_NAMES:
        c = cnt[(cnt.session_key == k) & (cnt.table == t)]
        quality[t] = {"received": int(c.received.sum()), "rejected": int(c.rejected.sum()),
                      "excluded": sum(1 for e in ex if e["table"] == t), "used": int(used[t])}

    # ---- per-driver summary numbers ----------------------------------
    pos_s = pos.assign(pt=dt(pos.date)).sort_values("pt")
    start_pos = pos_s.groupby("driver_number").position.first()
    fin_pos = pos_s.groupby("driver_number").position.last()
    g = C.groupby("driver_number")
    best, med_l, std_l = g.lap_duration.min(), g.lap_duration.median(), g.lap_duration.std()
    avg_fd = g.fdelta.mean()
    done = L.groupby("driver_number").lap_number.max()

    result = []
    for n in sorted(dm):
        sp, fp = start_pos.get(n, np.nan), fin_pos.get(n, np.nan)
        result.append({"driver": code(n), "team": team(n), "start_pos": val(sp), "finish_pos": val(fp),
                       "gained": val(sp - fp), "stops": val(stops.get(n, np.nan)), "laps": val(done.get(n, np.nan)),
                       "best_lap": val(best.get(n, np.nan)), "median_lap": val(med_l.get(n, np.nan)),
                       "lap_std": val(std_l.get(n, np.nan)), "avg_delta_field": val(avg_fd.get(n, np.nan))})
    result.sort(key=lambda r: (r["finish_pos"] is None, r["finish_pos"] or 0))

    # ---- stints (strategy chart + driver stint table) ----------------
    stint_rows, strat_rows = [], []
    for r in stints.sort_values(["driver_number", "stint_number"]).itertuples():
        sub = C[(C.driver_number == r.driver_number) & C.lap_number.between(r.lap_start, r.lap_end)]
        sub2 = sub.dropna(subset=["age", "adj"])
        slope = np.polyfit(sub2.age, sub2.adj, 1)[0] if len(sub2) >= 5 and sub2.age.nunique() > 1 else np.nan
        base = {"driver": code(r.driver_number), "stint": int(r.stint_number), "compound": r.compound,
                "lap_start": int(r.lap_start), "lap_end": int(r.lap_end), "laps": int(r.lap_end - r.lap_start + 1)}
        strat_rows.append(base)
        stint_rows.append({**base, "avg_pace": val(sub.lap_duration.mean()), "deg_per_lap": val(slope, 4)})

    # ---- charts -------------------------------------------------------
    deg_rows, deg_drop = degradation_rows(C, config.MIN_N_DEG_RACE, "this race")
    lap_cols = [col("driver", "Driver"), col("team", "Team"), col("lap", "Lap")]
    charts = [
        chart("race_result", "Race result", "race", {"type": "table"}, [
            col("driver", "Driver"), col("team", "Team"), col("start_pos", "Start pos"),
            col("finish_pos", "Finish pos"), col("gained", "Gained"), col("stops", "Stops"),
            col("laps", "Laps"), col("best_lap", "Best clean lap", "s"), col("median_lap", "Median lap", "s"),
            col("lap_std", "Lap std dev", "s"), col("avg_delta_field", "Avg delta vs field", "s")],
            result, ["position", "laps", "stints"]),
        chart("position_by_lap", "Position by lap", "both",
              {"type": "line", "x": "lap", "y": "position", "group": "driver", "yreverse": True,
               "xlabel": "Lap", "ylabel": "Position"},
              lap_cols + [col("position", "Position")],
              recs(L.dropna(subset=["position"]).sort_values(["driver", "lap_number"])
                   .rename(columns={"lap_number": "lap"}), ["driver", "team", "lap", "position"]),
              ["position", "laps"]),
        chart("lap_times", "Clean lap times", "both",
              {"type": "line", "x": "lap", "y": "lap_time", "group": "driver",
               "xlabel": "Lap", "ylabel": "Lap time (s)"},
              lap_cols + [col("lap_time", "Lap time", "s"), col("compound", "Compound")],
              recs(C.sort_values(["driver", "lap_number"]).rename(columns={"lap_number": "lap", "lap_duration": "lap_time"}),
                   ["driver", "team", "lap", "lap_time", "compound"]),
              ["laps", "race_control"]),
        chart("tyre_strategy", "Tyre strategy", "both", {"type": "stints"},
              [col("driver", "Driver"), col("stint", "Stint"), col("compound", "Compound"),
               col("lap_start", "From lap"), col("lap_end", "To lap"), col("laps", "Laps")],
              strat_rows, ["stints"]),
        chart("pit_stops", "Pit stops", "both",
              {"type": "bar", "x": "label", "y": "duration", "color_key": "team",
               "xlabel": "Stop", "ylabel": "Pit-lane time (s)"},
              [col("label", "Stop"), col("driver", "Driver"), col("team", "Team"),
               col("lap", "Lap"), col("duration", "Pit-lane time", "s")],
              [{"label": f"{r.driver} L{int(r.lap_number)}", "driver": r.driver, "team": r.team,
                "lap": int(r.lap_number), "duration": val(r.pit_duration)}
               for r in P.sort_values(["lap_number", "driver"]).itertuples()],
              ["pit"]),
        chart("degradation", "Tyre degradation (this race)", "race", DEG_PLOT, DEG_COLS, deg_rows,
              ["laps", "stints", "race_control"], dropped=deg_drop,
              min_rows=config.MIN_AGES_PER_COMPOUND),
        chart("weather", "Weather", "race",
              {"type": "line", "x": "minute", "ys": ["track_temperature", "air_temperature"],
               "xlabel": "Minutes since first sample", "ylabel": "Temperature (C)"},
              [col("minute", "Minute", "min"), col("track_temperature", "Track temp", "C"),
               col("air_temperature", "Air temp", "C"), col("rainfall", "Rainfall")],
              recs(wx.assign(minute=(dt(wx.date) - dt(wx.date).min()).dt.total_seconds() / 60)
                   .sort_values("minute"), ["minute", "track_temperature", "air_temperature", "rainfall"]),
              ["weather"], min_rows=2),
        chart("pace_delta", "Pace vs field and vs teammate", "driver",
              {"type": "line", "x": "lap", "ys": ["delta_field", "delta_teammate"],
               "xlabel": "Lap", "ylabel": "Seconds (negative = faster)"},
              lap_cols + [col("delta_field", "Delta vs field", "s"), col("delta_teammate", "Delta vs teammate", "s")],
              recs(C.sort_values(["driver", "lap_number"])
                   .rename(columns={"lap_number": "lap", "fdelta": "delta_field", "delta_team": "delta_teammate"}),
                   ["driver", "team", "lap", "delta_field", "delta_teammate"]),
              ["laps", "race_control"]),
        chart("driver_laps", "Lap-by-lap table", "driver", {"type": "table"},
              lap_cols + [col("lap_time", "Lap time", "s"), col("s1", "S1", "s"), col("s2", "S2", "s"),
                          col("s3", "S3", "s"), col("compound", "Compound"), col("age", "Tyre age"),
                          col("position", "Position"), col("status", "Status")],
              recs(L.sort_values(["driver", "lap_number"]).rename(columns={
                  "lap_number": "lap", "lap_duration": "lap_time", "duration_sector_1": "s1",
                  "duration_sector_2": "s2", "duration_sector_3": "s3"}),
                  ["driver", "team", "lap", "lap_time", "s1", "s2", "s3", "compound", "age", "position", "status"]),
              ["laps", "stints", "position"]),
        chart("driver_stints", "Stint breakdown", "driver", {"type": "table"},
              [col("driver", "Driver"), col("stint", "Stint"), col("compound", "Compound"),
               col("lap_start", "From lap"), col("lap_end", "To lap"), col("laps", "Laps"),
               col("avg_pace", "Avg clean pace", "s"), col("deg_per_lap", "Degradation", "s/lap")],
              stint_rows, ["stints", "laps"]),
    ]

    # ---- track map ----------------------------------------------------
    track = build_track(s, loc, L, pos, dm)
    tc = chart("track", "Track map", "track", {"type": "track", "file": f"track_{k}.json"},
               [col("driver", "Driver"), col("x", "x"), col("y", "y"),
                col("position", "Running order"), col("time_s", "Time", "s")],
               [], ["location", "position"], min_rows=0)
    tc["status"] = "ok" if track else "insufficient_data"
    tc["n"] = len(track["cars"]) if track else 0
    charts.append(tc)

    # ---- rejected / excluded rows (validation + cleaning) -------------
    rj = T["rejected"][T["rejected"].session_key == k]
    rejected_rows = recs(rj, etl.META_COLS["rejected"]) + ex

    race = {"schema_version": config.SCHEMA_VERSION, "session_key": k, "year": int(s["year"]),
            "name": s["meeting_name"] + ("" if s["session_name"] in (None, "Race") else f" ({s['session_name']})"),
            "circuit": s["circuit"], "country": s["country"], "date": s["date_start"],
            "drivers": sorted(dm.values(), key=lambda d: (d["team"], d["number"])),
            "quality": quality, "rejected_file": f"rejected_{k}.json",
            "track_file": f"track_{k}.json" if track else None, "charts": charts}

    pool = {"C": C.assign(session_key=k, year=int(s["year"]))[
                ["session_key", "year", "driver_number", "driver", "team", "compound", "age", "delta",
                 "fdelta", "track_temp", "rain", "stops"]],
            "P": P.assign(session_key=k)[["session_key", "driver", "team", "pit_duration"]],
            "R": pd.DataFrame(result, columns=RES_COLS).assign(session_key=k, year=int(s["year"])),
            "quality": quality, "k": k}
    return race, rejected_rows, track, pool, quality


# =========================================================== track map
def build_track(s, loc, L, pos, dm):
    """
    Put every driver's location samples on one clock and return the track JSON, or None if
    there is not enough location data. Coordinates are integers in OpenF1's own units.
    """
    if loc.empty or L.empty or loc.driver_number.nunique() < 2:
        return None
    t0 = dt(pd.Series([s["date_start"]])).iloc[0]
    X = loc.assign(t=(dt(loc.date) - t0).dt.total_seconds()).dropna(subset=["t"]).sort_values("t")
    grid = np.arange(max(0.0, X.t.min()), X.t.max(), config.LOC_STEP_S)
    if len(grid) < 10:
        return None
    P = pos.assign(pt=(dt(pos.date) - t0).dt.total_seconds()).dropna(subset=["pt"]).sort_values("pt")
    ints = lambda a: [None if not np.isfinite(v) else int(round(v)) for v in a]      # noqa: E731

    cars = {}
    for n, g in X.groupby("driver_number"):
        pg = P[P.driver_number == n]
        idx = np.searchsorted(pg.pt.values, grid, side="right") - 1
        order = ([None if i < 0 else int(pg.position.values[i]) for i in idx] if len(pg) else [None] * len(grid))
        cars[dm[int(n)]["code"]] = {
            "number": int(n),
            "x": ints(np.interp(grid, g.t, g.x, left=np.nan, right=np.nan)),
            "y": ints(np.interp(grid, g.t, g.y, left=np.nan, right=np.nan)),
            "pos": order}

    # outline: one driver's clean lap drawn through its own samples
    best = int(L.groupby("driver_number").lap_number.max().idxmax())
    cl = L[(L.driver_number == best) & (L.status == "clean")]
    cl = cl if len(cl) else L[(L.driver_number == best) & L.t0.notna()]
    gb = X[X.driver_number == best]
    outline = []
    if len(cl) and len(gb):
        r = cl.iloc[len(cl) // 2]
        a = (r.t0 - t0).total_seconds()
        w = gb[(gb.t >= a) & (gb.t <= a + (r.lap_duration if np.isfinite(r.lap_duration) else 95))]
        outline = list(zip(ints(w.x), ints(w.y)))
    if len(outline) < 15:
        outline = list(zip(ints(gb.x.iloc[:int(95 / config.LOC_STEP_S)]), ints(gb.y.iloc[:int(95 / config.LOC_STEP_S)])))

    starts = np.sort((L[L.driver_number == best].t0.dropna() - t0).dt.total_seconds().values)
    return {"session_key": int(s["session_key"]), "step": config.LOC_STEP_S, "t_start": float(grid[0]),
            "n": int(len(grid)), "outline": outline,
            "lap_at": [int(v) for v in np.searchsorted(starts, grid, side="right")], "cars": cars}


# ========================================================= pooled charts
def build_pooled(pools, label):
    """Charts that pool many races (a season or everything)."""
    C = pd.concat([p["C"] for p in pools], ignore_index=True)
    P = pd.concat([p["P"] for p in pools], ignore_index=True)
    R = pd.concat([p["R"] for p in pools], ignore_index=True)
    charts = []

    rows, drop = degradation_rows(C, config.MIN_N_DEG_POOLED, label)
    charts.append(chart("p_degradation", f"Tyre degradation ({label})", "pooled", DEG_PLOT, DEG_COLS, rows,
                        ["laps", "stints", "race_control"], "pooled_degradation", dropped=drop,
                        min_rows=config.MIN_AGES_PER_COMPOUND))

    g = P.groupby("team").pit_duration.agg(["median", "count"]).reset_index()
    pit_drop = [{"stage": "aggregation", "table": "pit", "session_key": None, "driver_number": None,
                 "lap_number": None, "rule": "PIT_TEAM_MIN_SAMPLE",
                 "reason": f"{r.team}: only {int(r['count'])} stop(s), need {config.MIN_PIT_TEAM_POOLED}",
                 "errors": None, "raw": json.dumps({"team": r.team, "n": int(r["count"])})}
                for _, r in g[g["count"] < config.MIN_PIT_TEAM_POOLED].iterrows()]
    g = g[g["count"] >= config.MIN_PIT_TEAM_POOLED].sort_values("median")
    charts.append(chart("p_pit_team", f"Median pit-lane time by team ({label})", "pooled",
                        {"type": "bar", "x": "team", "y": "median_pit", "color_key": "team",
                         "xlabel": "Team", "ylabel": "Median pit-lane time (s)"},
                        [col("team", "Team"), col("median_pit", "Median pit-lane time", "s"), col("stops", "Stops (n)")],
                        [{"team": r.team, "median_pit": val(r.median), "stops": int(r.count)} for r in g.itertuples()],
                        ["pit"], "pooled_pit_team", dropped=pit_drop))

    dr = C.dropna(subset=["stops", "fdelta"]).groupby(["session_key", "driver_number"]).agg(
        pace=("fdelta", "mean"), stops=("stops", "first")).reset_index()
    dr["grp"] = dr.stops.clip(upper=3).astype(int).astype(str).replace({"3": "3+"})
    g = dr.groupby("grp").pace.agg(["mean", "count"]).reset_index().sort_values("grp")
    charts.append(chart("p_pace_stops", f"Pace vs number of stops ({label})", "pooled",
                        {"type": "bar", "x": "stops", "y": "pace", "xlabel": "Stops",
                         "ylabel": "Avg delta vs field (s, negative = faster)"},
                        [col("stops", "Stops"), col("pace", "Avg delta vs field", "s"), col("n", "Driver-races (n)")],
                        [{"stops": r.grp, "pace": val(r.mean), "n": int(r.count)}
                         for r in g.itertuples() if r.count >= config.MIN_STOPS_N],
                        ["laps", "stints"], "pooled_pace_stops", min_rows=2))

    t = C.dropna(subset=["track_temp", "delta"]).assign(b=lambda d: (d.track_temp // 5 * 5).astype(int))
    g = t.groupby("b").delta.agg(["mean", "count"]).reset_index()
    charts.append(chart("p_pace_temp", f"Pace vs track temperature ({label})", "pooled",
                        {"type": "bar", "x": "band", "y": "pace", "xlabel": "Track temperature band (C)",
                         "ylabel": "Avg delta to driver median (s)"},
                        [col("band", "Track temp band", "C"), col("pace", "Avg delta", "s"), col("n", "Laps (n)")],
                        [{"band": f"{int(r.b)}-{int(r.b) + 5}", "pace": val(r.mean), "n": int(r.count)}
                         for r in g.itertuples() if r.count >= config.MIN_TEMP_BAND_N],
                        ["laps", "weather"], "pooled_pace_temp", min_rows=2))

    w = C.dropna(subset=["compound"]).assign(wx=lambda d: np.where(d.rain > 0, "Rain", "Dry"))
    rows = []
    for name, grp in w.groupby("wx"):
        if len(grp) >= config.MIN_RAIN_N:
            for comp, n in grp.compound.value_counts().items():
                rows.append({"weather": name, "compound": comp, "pct": val(100 * n / len(grp), 1), "laps": int(n)})
    charts.append(chart("p_compound_rain", f"Compound use, dry vs rain ({label})", "pooled",
                        {"type": "stacked", "x": "weather", "y": "pct", "group": "compound",
                         "xlabel": "Conditions", "ylabel": "% of clean laps"},
                        [col("weather", "Conditions"), col("compound", "Compound"), col("pct", "Share", "%"),
                         col("laps", "Laps (n)")], rows, ["laps", "stints", "weather"], "pooled_compound_rain"))

    s = R.groupby("driver").agg(team=("team", "last"), races=("session_key", "nunique"),
                                avg_start=("start_pos", "mean"), avg_finish=("finish_pos", "mean"),
                                avg_delta=("avg_delta_field", "mean"), avg_stops=("stops", "mean")).reset_index()
    charts.append(chart("season_drivers", f"Driver summary ({label})", "pooled", {"type": "table"},
                        [col("driver", "Driver"), col("team", "Team"), col("races", "Races"),
                         col("avg_start", "Avg start"), col("avg_finish", "Avg finish"),
                         col("avg_delta", "Avg delta vs field", "s"), col("avg_stops", "Avg stops")],
                        recs(s.sort_values("avg_finish"), ["driver", "team", "races", "avg_start", "avg_finish",
                                                           "avg_delta", "avg_stops"]),
                        ["position", "laps", "stints"]))
    return charts


def sum_quality(quals):
    out = {t: {"received": 0, "rejected": 0, "excluded": 0, "used": 0} for t in TABLE_NAMES}
    for q in quals:
        for t in TABLE_NAMES:
            for f in out[t]:
                out[t][f] += q[t][f]
    return out


# ================================================================= main
def main():
    T = load()
    out = config.OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    sessions = T["sessions"].sort_values("date_start")
    index = {"schema_version": config.SCHEMA_VERSION,
             "generated": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
             "years": [], "sessions": {}, "overall_file": "overall.json",
             "config": {"fuel_correction_s_per_lap": config.FUEL_CORR_S_PER_LAP,
                        "slow_lap_factor": config.SLOW_LAP_FACTOR, "pit_max_s": config.PIT_MAX_S,
                        "deg_max_age": config.DEG_MAX_AGE, "loc_step_s": config.LOC_STEP_S}}
    pools, all_ex = defaultdict(list), []

    for s in sessions.to_dict("records"):
        k, y = int(s["session_key"]), int(s["year"])
        race, rejected, track, pool, quality = process_race(s, T, T["counts"])
        write_json(out / f"race_{k}.json", race)
        write_json(out / f"rejected_{k}.json", {"session_key": k, "rows": rejected})
        if track:
            write_json(out / f"track_{k}.json", track)
        pools[y].append(pool)
        all_ex += [e for e in rejected if e["stage"] == "cleaning"]
        index["sessions"].setdefault(str(y), []).append({
            "session_key": k, "name": race["name"], "circuit": race["circuit"], "country": race["country"],
            "date": race["date"], "file": f"race_{k}.json", "drivers": race["drivers"], "quality": quality,
            "has": {t: quality[t]["received"] > 0 for t in TABLE_NAMES}})
        print(f"  {y} {race['name']}: {sum(q['rejected'] for q in quality.values())} rejected, "
              f"{sum(q['excluded'] for q in quality.values())} excluded")

    for y, plist in pools.items():
        keys = [p["k"] for p in plist]
        write_json(out / f"season_{y}.json", {
            "schema_version": config.SCHEMA_VERSION, "label": str(y), "session_keys": keys,
            "quality": sum_quality([p["quality"] for p in plist]), "charts": build_pooled(plist, str(y))})
        index["years"].append(y)
    allp = [p for pl in pools.values() for p in pl]
    if allp:
        write_json(out / "overall.json", {
            "schema_version": config.SCHEMA_VERSION, "label": "all seasons",
            "session_keys": [p["k"] for p in allp],
            "quality": sum_quality([p["quality"] for p in allp]), "charts": build_pooled(allp, "all seasons")})
    index["years"].sort()
    write_json(out / "index.json", index)

    # keep the cleaning exclusions in the DB too, so SQL can audit everything
    con = sqlite3.connect(config.DB_PATH)
    pd.DataFrame(all_ex, columns=etl.META_COLS["rejected"]).to_sql("excluded", con, if_exists="replace", index=False)
    con.close()
    print(f"EDA done -> {out}")


if __name__ == "__main__":
    main()
