"""
transform.py - the TRANSFORM step of the ETL pipeline (pydantic + pandas).

Two jobs
--------
1. validate_session()  pydantic checks every raw record. Good records go on to be loaded;
                       bad ones are kept (with every error and the full raw record) so the
                       dashboard can show what was rejected and why.
2. build_race()        pandas cleans the validated laps and builds every chart of ONE race as
                       plain JSON-ready dicts, plus the track-map file.

Every chart is a dict:  id, title, scope (race | both | driver | track), plot (how to draw it),
columns, rows (THE rows plotted - the website draws the chart AND its data table from this one
list), method (explanation), tables (which DB tables feed it), status (ok | insufficient_data).

Accounting rule, checked by the tests, for every table of every race:
    received = rejected (validation) + excluded (cleaning) + used
"""
import json

import numpy as np
import pandas as pd
from pydantic import ValidationError

import config
import models

TABLES = {"laps": models.Lap, "stints": models.Stint, "pit": models.Pit,
          "weather": models.Weather, "position": models.Position, "location": models.Location}
COLS = {t: list(m.model_fields) for t, m in TABLES.items()}
REJECT_COLS = ["stage", "table", "session_key", "driver_number", "lap_number", "rule", "reason", "errors", "raw"]


# ============================================================ 1. VALIDATE (pydantic)
def rule_code(table, err):
    """Stable id of a failed rule, e.g. LAPS_LAP_DURATION_GREATER_THAN."""
    loc = "_".join(str(p) for p in err["loc"]) or "ROW"
    return f"{table}_{loc}_{err['type']}".upper()


def validate_session(raw, session_key):
    """
    Validate every table of one session.
    Returns (valid_rows {table: [dict]}, counts [dict], rejected [dict]).
    """
    valid, counts, rejected = {}, [], []
    for table, model in TABLES.items():
        good = []
        for rec in raw.get(table, []):
            try:
                good.append(model.model_validate(rec).model_dump())
            except ValidationError as exc:
                errs = exc.errors()
                rec = rec if isinstance(rec, dict) else {"value": repr(rec)}
                rejected.append({
                    "stage": "validation", "table": table, "session_key": session_key,
                    "driver_number": rec.get("driver_number"), "lap_number": rec.get("lap_number"),
                    "rule": rule_code(table, errs[0]),
                    "reason": "; ".join(f"{'.'.join(map(str, e['loc'])) or 'row'}: {e['msg']}" for e in errs),
                    "errors": json.dumps([{"field": ".".join(map(str, e["loc"])), "type": e["type"],
                                           "msg": e["msg"]} for e in errs]),     # ALL errors
                    "raw": json.dumps(rec, default=str)})                         # the FULL record
        valid[table] = good
        counts.append({"session_key": session_key, "table": table,
                       "received": len(raw.get(table, [])), "rejected": len(raw.get(table, [])) - len(good)})
    return valid, counts, rejected


# ================================================================== helpers
def dt(s):
    """ISO timestamps -> timezone-aware datetime64[ns] (NaT when missing)."""
    return pd.to_datetime(s, utc=True, format="ISO8601").dt.as_unit("ns")


def val(v, nd=3):
    """One value made JSON-safe: numpy -> python, NaN/NaT -> None, floats rounded."""
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


def chart(cid, title, scope, plot, columns, rows, tables, min_rows=1):
    return {"id": cid, "title": title, "scope": scope, "plot": plot, "columns": columns, "rows": rows,
            "method": METHODS[cid](), "tables": tables, "dropped": [], "n": len(rows), "need": min_rows,
            "status": "ok" if len(rows) >= min_rows else "insufficient_data"}


def exclusion(table, k, driver_number, lap_number, rule, why, raw):
    """A valid row removed by a cleaning rule (same shape as a validation rejection)."""
    return {"stage": "cleaning", "table": table, "session_key": k, "driver_number": val(driver_number),
            "lap_number": val(lap_number), "rule": rule, "reason": why, "errors": None,
            "raw": json.dumps({a: val(b, 6) for a, b in raw.items()})}


# ============================================================ method texts
def _clean_steps():
    return ["Start from the laps that passed validation.",
            "Remove laps with no recorded duration.",
            "Remove out-laps (first lap after leaving the pits: cold tyres, pit-lane exit).",
            f"Adjusted time = lap time + {config.FUEL_CORR_S_PER_LAP} s x lap number (adds back time gained as fuel burns off).",
            f"Remove laps whose adjusted time is more than {config.SLOW_LAP_FACTOR}x the driver's median adjusted time "
            "(in-laps, traffic, incidents and safety-car laps)."]


FUEL = (f"The fuel correction ({config.FUEL_CORR_S_PER_LAP} s per lap) is an assumption, not a measurement.")
SC = "There is no separate safety-car detection: safety-car laps are only removed by the slow-lap rule."


def _m(summary, source, steps, formula, baseline, assumptions=(), limitations=()):
    return {"summary": summary, "source": list(source), "steps": list(steps), "formula": formula,
            "baseline": baseline, "assumptions": list(assumptions), "limitations": list(limitations)}


METHODS = {
    "race_result": lambda: _m(
        "Start, finish, stops and pace of every driver.", ["position", "laps", "stints"],
        ["Start position = a driver's first position sample; finish = the last sample.",
         "Stops = number of tyre stints - 1.", "Lap statistics use the clean laps:", *_clean_steps()],
        "gained = start - finish;  lap_std = standard deviation of clean lap times;  "
        "avg_delta_field = mean(adjusted time - median adjusted time of all clean laps)",
        "Field median pace for the delta; otherwise none.", [FUEL],
        ["Finish is the last live position sample, not the official classification (penalties are not reflected).", SC]),
    "position_by_lap": lambda: _m(
        "Race position at the start of each lap.", ["position", "laps"],
        ["For each lap take the position sample nearest to the lap's start time."],
        "position(driver, lap) = nearest position sample to lap.date_start", "Absolute position (1 = leader).",
        limitations=["A position change inside a lap shows on the next lap."]),
    "lap_times": lambda: _m(
        "Clean lap time of every driver.", ["laps"], _clean_steps(),
        "plotted value = real lap time in seconds (not the fuel-adjusted time)", "None.", [FUEL], [SC]),
    "tyre_strategy": lambda: _m(
        "Each driver's tyre stints.", ["stints"], ["One bar per stint from lap_start - 1 to lap_end."],
        "bar length = lap_end - lap_start + 1", "-"),
    "pit_stops": lambda: _m(
        "Pit-lane time of every stop.", ["pit"],
        [f"Remove stops of {config.PIT_MAX_S} s or more (repairs / red-flag stops)."],
        "plotted value = pit-lane time in seconds", "-",
        limitations=["Pit-lane time only, not the total race time lost."]),
    "degradation": lambda: _m(
        "How much slower the car gets as the tyres age, per compound.", ["laps", "stints"],
        [*_clean_steps(), "Tyre age = age at stint start + lap - stint start lap + 1.",
         f"Keep ages up to {config.DEG_MAX_AGE}.",
         "delta = adjusted time - driver's median adjusted time.",
         f"Average delta per (compound, age); keep points with >= {config.MIN_N_DEG} laps and compounds with "
         f">= {config.MIN_AGES_PER_COMPOUND} ages."],
        "degradation(compound, age) = mean(adjusted time - driver's median adjusted time)",
        "Each driver's own median pace.", [FUEL],
        ["The track gets faster during a race, which flatters old tyres.", SC]),
    "weather": lambda: _m(
        "Track and air temperature during the session.", ["weather"], ["No filtering beyond validation."],
        "minute = (sample time - first sample time) / 60", "-",
        limitations=["One weather station, about one sample a minute."]),
    "pace_delta": lambda: _m(
        "A driver's clean pace compared with the field and with their teammate, lap by lap.", ["laps"],
        [*_clean_steps(), "delta vs field = adjusted time - median adjusted time of all clean laps.",
         "delta vs teammate = adjusted time - teammate's adjusted time on the same lap."],
        "delta_field = adj - median(adj, all clean laps);  delta_teammate = adj - adj_teammate(lap)",
        "Field median / the teammate. Negative = faster.", [FUEL],
        ["The field median mixes different tyre ages and fuel loads."]),
    "driver_laps": lambda: _m(
        "Every lap of the driver, including removed ones.", ["laps", "stints", "position"],
        ["Nothing is hidden: 'status' is 'clean' or the cleaning rule that removed the lap."],
        "status = clean, or the id of the rule the lap failed", "-"),
    "driver_stints": lambda: _m(
        "Per-stint summary.", ["stints", "laps"],
        [*_clean_steps(), "avg pace = mean clean lap time in the stint.",
         "degradation = slope of a straight line fitted to (tyre age, adjusted time); needs >= 5 clean laps."],
        "degradation = slope of the least-squares fit  adj_time ~ tyre_age", "-", [FUEL],
        ["A straight line cannot show a tyre 'cliff'."]),
    "track": lambda: _m(
        "Car positions on the circuit, scrubbable through the race.", ["location", "position"],
        [f"Location was thinned when downloaded: one sample per driver every {config.LOC_STEP_S} s.",
         "Samples with x = y = z = 0 are removed (invalid frames).",
         "All drivers are placed on one clock (seconds since session start) by linear interpolation; "
         "before a driver's first / after their last sample no dot is drawn.",
         "The outline is one driver's clean lap drawn through its own samples.",
         "The running order comes from the position table, not from where the dots are on the drawing."],
        "x(t), y(t) = linear interpolation between the two nearest samples of that driver", "-",
        limitations=["Sampling is coarse, so the outline is polygonal.",
                     "'Received' location rows are counted after thinning."]),
}


# ============================================================ lap cleaning
def flag_laps(L, k):
    """
    Apply the cleaning rules in order; a lap is removed by the first rule it fails.
    Adds L["status"] ("clean" or the rule id). Returns (L, exclusion records).
    """
    ex, alive = [], pd.Series(True, index=L.index)
    L["status"] = "clean"

    def drop(mask, rule, why):
        m = alive & mask
        L.loc[m, "status"] = rule
        for r in L[m].itertuples():
            ex.append(exclusion("laps", k, r.driver_number, r.lap_number, rule, why,
                                {"lap_duration": r.lap_duration, "date_start": r.date_start,
                                 "is_pit_out_lap": r.is_pit_out_lap, "adj": r.adj}))
        alive.loc[m] = False

    drop(L.lap_duration.isna(), "LAP_NULL_DURATION", "lap has no recorded duration")
    drop(L.is_pit_out_lap.fillna(0).astype(bool), "LAP_PIT_OUT", "out-lap after a pit stop")
    L["med"] = L[alive].groupby("driver_number").adj.transform("median")
    drop(L.adj > L.med * config.SLOW_LAP_FACTOR, "LAP_SLOW",
         f"adjusted time > {config.SLOW_LAP_FACTOR}x the driver's median (in-lap, traffic, incident, safety car)")
    return L, ex


def attach_stints(L, stints):
    """Add compound and tyre age to every lap by matching it to the stint that contains it."""
    L["compound"], L["age"] = None, np.nan
    if stints.empty or L.empty:
        return L
    m = L[["driver_number", "lap_number"]].merge(stints, on="driver_number")
    m = m[(m.lap_number >= m.lap_start) & (m.lap_number <= m.lap_end)]
    m = m.assign(age=m.tyre_age_at_start.fillna(0) + m.lap_number - m.lap_start + 1)
    m = m.drop_duplicates(["driver_number", "lap_number"])[["driver_number", "lap_number", "compound", "age"]]
    return L.drop(columns=["compound", "age"]).merge(m, on=["driver_number", "lap_number"], how="left")


def attach_position(L, pos):
    """Add the race position nearest to each lap's start time."""
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


def degradation_rows(C):
    """Mean delta per (compound, tyre age); thin groups and short curves are not plotted."""
    d = C[C.age.between(1, config.DEG_MAX_AGE)].dropna(subset=["compound", "delta"])
    if d.empty:
        return []
    g = d.groupby(["compound", "age"]).delta.agg(["mean", "count"]).reset_index()
    g = g[g["count"] >= config.MIN_N_DEG]
    ages = g.groupby("compound").age.nunique()
    g = g[g.compound.isin(ages[ages >= config.MIN_AGES_PER_COMPOUND].index)].sort_values(["compound", "age"])
    return [{"compound": r.compound, "age": int(r.age), "delta": val(r.mean), "n": int(r.count)} for r in g.itertuples()]


# ============================================================ one race
def build_race(s, T, counts, rejected_df):
    """
    Analyse ONE session.
    s = session row (dict); T = {table: DataFrame} of its validated data (+ "drivers");
    counts = counts rows for the session; rejected_df = its validation rejections.
    Returns (race_json, rejected_rows, track_json_or_None).
    """
    k = int(s["session_key"])
    laps, stints, pit, wx, pos, loc, drv = (T[n] for n in ("laps", "stints", "pit", "weather", "position",
                                                           "location", "drivers"))
    # driver lookup: number -> code / name / team / colour
    dm = {}
    for r in drv.drop_duplicates("driver_number").itertuples():
        n = int(r.driver_number)
        dm[n] = {"number": n, "code": r.name_acronym if isinstance(r.name_acronym, str) else str(n),
                 "name": r.full_name if isinstance(r.full_name, str) else str(n),
                 "team": r.team_name if isinstance(r.team_name, str) else "Unknown",
                 "colour": f"#{r.team_colour}" if isinstance(r.team_colour, str) and r.team_colour else None}
    for tbl in (laps, stints, pos, pit):
        for n in tbl.driver_number.dropna().astype(int).unique():
            dm.setdefault(n, {"number": n, "code": str(n), "name": str(n), "team": "Unknown", "colour": None})
    code = lambda n: dm[int(n)]["code"]                                   # noqa: E731
    team = lambda n: dm[int(n)]["team"]                                   # noqa: E731

    # ---- laps: cleaning rules, then the CLEAN laps ---------------------------------
    L = laps.copy()
    L["t0"] = dt(L.date_start)
    L["adj"] = L.lap_duration + config.FUEL_CORR_S_PER_LAP * L.lap_number
    L, ex = flag_laps(L, k)
    L = attach_position(attach_stints(L, stints), pos)
    L["driver"], L["team"] = L.driver_number.map(code), L.driver_number.map(team)

    C = L[L.status == "clean"].copy()
    C["delta"] = C.adj - C.groupby("driver_number").adj.transform("median")      # vs driver's own median
    C["fdelta"] = C.adj - C.adj.median()                                          # vs the field
    n_t = C.groupby(["team", "lap_number"]).adj.transform("count")
    s_t = C.groupby(["team", "lap_number"]).adj.transform("sum")
    C["delta_team"] = np.where(n_t == 2, C.adj - (s_t - C.adj), np.nan)           # vs teammate
    stops = (stints.groupby("driver_number").stint_number.nunique() - 1).clip(lower=0)

    # ---- pit stops: drop repair-length stops ---------------------------------------
    P = pit.copy()
    for r in P[P.pit_duration >= config.PIT_MAX_S].itertuples():
        ex.append(exclusion("pit", k, r.driver_number, r.lap_number, "PIT_TOO_LONG",
                            f"pit-lane time {r.pit_duration:.1f}s >= {config.PIT_MAX_S}s", {"pit_duration": r.pit_duration}))
    P = P[P.pit_duration < config.PIT_MAX_S].copy()
    P["driver"], P["team"] = P.driver_number.map(code), P.driver_number.map(team)

    # ---- location: drop invalid all-zero frames ------------------------------------
    zero = (loc.x == 0) & (loc.y == 0) & (loc.z == 0)
    for r in loc[zero].itertuples():
        ex.append(exclusion("location", k, r.driver_number, None, "LOC_ZERO_POSITION",
                            "x = y = z = 0 (invalid frame)", {"date": r.date}))
    loc = loc[~zero]

    # ---- quality counts: received = rejected + excluded + used ---------------------
    used = {"laps": len(C), "pit": len(P), "location": len(loc), "stints": len(stints),
            "weather": len(wx), "position": len(pos)}
    cdf = pd.DataFrame(counts, columns=["session_key", "table", "received", "rejected"])
    quality = {}
    for t in TABLES:
        c = cdf[cdf.table == t]
        quality[t] = {"received": int(c.received.sum()), "rejected": int(c.rejected.sum()),
                      "excluded": sum(1 for e in ex if e["table"] == t), "used": int(used[t])}

    # ---- per-driver summary --------------------------------------------------------
    pos_s = pos.assign(pt=dt(pos.date)).sort_values("pt")
    start_p, fin_p = pos_s.groupby("driver_number").position.first(), pos_s.groupby("driver_number").position.last()
    g = C.groupby("driver_number")
    best, med_l, std_l, avg_fd = g.lap_duration.min(), g.lap_duration.median(), g.lap_duration.std(), g.fdelta.mean()
    done = L.groupby("driver_number").lap_number.max()
    result = []
    for n in sorted(dm):
        sp, fp = start_p.get(n, np.nan), fin_p.get(n, np.nan)
        result.append({"driver": code(n), "team": team(n), "start_pos": val(sp), "finish_pos": val(fp),
                       "gained": val(sp - fp), "stops": val(stops.get(n, np.nan)), "laps": val(done.get(n, np.nan)),
                       "best_lap": val(best.get(n, np.nan)), "median_lap": val(med_l.get(n, np.nan)),
                       "lap_std": val(std_l.get(n, np.nan)), "avg_delta_field": val(avg_fd.get(n, np.nan))})
    result.sort(key=lambda r: (r["finish_pos"] is None, r["finish_pos"] or 0))

    # ---- stints --------------------------------------------------------------------
    stint_rows, strat_rows = [], []
    for r in stints.sort_values(["driver_number", "stint_number"]).itertuples():
        sub = C[(C.driver_number == r.driver_number) & C.lap_number.between(r.lap_start, r.lap_end)]
        s2 = sub.dropna(subset=["age", "adj"])
        slope = np.polyfit(s2.age, s2.adj, 1)[0] if len(s2) >= 5 and s2.age.nunique() > 1 else np.nan
        base = {"driver": code(r.driver_number), "stint": int(r.stint_number), "compound": r.compound,
                "lap_start": int(r.lap_start), "lap_end": int(r.lap_end), "laps": int(r.lap_end - r.lap_start + 1)}
        strat_rows.append(base)
        stint_rows.append({**base, "avg_pace": val(sub.lap_duration.mean()), "deg_per_lap": val(slope, 4)})

    # ---- charts --------------------------------------------------------------------
    lapc = [col("driver", "Driver"), col("team", "Team"), col("lap", "Lap")]
    ren = {"lap_number": "lap"}
    charts = [
        chart("race_result", "Race result", "race", {"type": "table"}, [
            col("driver", "Driver"), col("team", "Team"), col("start_pos", "Start pos"), col("finish_pos", "Finish pos"),
            col("gained", "Gained"), col("stops", "Stops"), col("laps", "Laps"), col("best_lap", "Best clean lap", "s"),
            col("median_lap", "Median lap", "s"), col("lap_std", "Lap std dev", "s"),
            col("avg_delta_field", "Avg delta vs field", "s")], result, ["position", "laps", "stints"]),
        chart("position_by_lap", "Position by lap", "both",
              {"type": "line", "x": "lap", "y": "position", "group": "driver", "yreverse": True,
               "xlabel": "Lap", "ylabel": "Position"}, lapc + [col("position", "Position")],
              recs(L.dropna(subset=["position"]).sort_values(["driver", "lap_number"]).rename(columns=ren),
                   ["driver", "team", "lap", "position"]), ["position", "laps"]),
        chart("lap_times", "Clean lap times", "both",
              {"type": "line", "x": "lap", "y": "lap_time", "group": "driver", "xlabel": "Lap", "ylabel": "Lap time (s)"},
              lapc + [col("lap_time", "Lap time", "s"), col("compound", "Compound")],
              recs(C.sort_values(["driver", "lap_number"]).rename(columns={**ren, "lap_duration": "lap_time"}),
                   ["driver", "team", "lap", "lap_time", "compound"]), ["laps"]),
        chart("tyre_strategy", "Tyre strategy", "both", {"type": "stints"},
              [col("driver", "Driver"), col("stint", "Stint"), col("compound", "Compound"), col("lap_start", "From lap"),
               col("lap_end", "To lap"), col("laps", "Laps")], strat_rows, ["stints"]),
        chart("pit_stops", "Pit stops", "both",
              {"type": "bar", "x": "label", "y": "duration", "color_key": "team", "xlabel": "Stop",
               "ylabel": "Pit-lane time (s)"},
              [col("label", "Stop"), col("driver", "Driver"), col("team", "Team"), col("lap", "Lap"),
               col("duration", "Pit-lane time", "s")],
              [{"label": f"{r.driver} L{int(r.lap_number)}", "driver": r.driver, "team": r.team,
                "lap": int(r.lap_number), "duration": val(r.pit_duration)}
               for r in P.sort_values(["lap_number", "driver"]).itertuples()], ["pit"]),
        chart("degradation", "Tyre degradation", "race",
              {"type": "line", "x": "age", "y": "delta", "group": "compound", "xlabel": "Tyre age (laps)",
               "ylabel": "Seconds slower than driver median"},
              [col("compound", "Compound"), col("age", "Tyre age", "laps"),
               col("delta", "Mean delta to driver median", "s"), col("n", "Laps (n)")],
              degradation_rows(C), ["laps", "stints"], min_rows=config.MIN_AGES_PER_COMPOUND),
        chart("weather", "Weather", "race",
              {"type": "line", "x": "minute", "ys": ["track_temperature", "air_temperature"],
               "xlabel": "Minutes since first sample", "ylabel": "Temperature (C)"},
              [col("minute", "Minute", "min"), col("track_temperature", "Track temp", "C"),
               col("air_temperature", "Air temp", "C"), col("rainfall", "Rainfall")],
              recs(wx.assign(minute=(dt(wx.date) - dt(wx.date).min()).dt.total_seconds() / 60).sort_values("minute"),
                   ["minute", "track_temperature", "air_temperature", "rainfall"]), ["weather"], min_rows=2),
        chart("pace_delta", "Pace vs field and vs teammate", "driver",
              {"type": "line", "x": "lap", "ys": ["delta_field", "delta_teammate"], "xlabel": "Lap",
               "ylabel": "Seconds (negative = faster)"},
              lapc + [col("delta_field", "Delta vs field", "s"), col("delta_teammate", "Delta vs teammate", "s")],
              recs(C.sort_values(["driver", "lap_number"]).rename(columns={**ren, "fdelta": "delta_field",
                                                                          "delta_team": "delta_teammate"}),
                   ["driver", "team", "lap", "delta_field", "delta_teammate"]), ["laps"]),
        chart("driver_laps", "Lap-by-lap table", "driver", {"type": "table"},
              lapc + [col("lap_time", "Lap time", "s"), col("s1", "S1", "s"), col("s2", "S2", "s"), col("s3", "S3", "s"),
                      col("compound", "Compound"), col("age", "Tyre age"), col("position", "Position"),
                      col("status", "Status")],
              recs(L.sort_values(["driver", "lap_number"]).rename(columns={
                  **ren, "lap_duration": "lap_time", "duration_sector_1": "s1", "duration_sector_2": "s2",
                  "duration_sector_3": "s3"}),
                  ["driver", "team", "lap", "lap_time", "s1", "s2", "s3", "compound", "age", "position", "status"]),
              ["laps", "stints", "position"]),
        chart("driver_stints", "Stint breakdown", "driver", {"type": "table"},
              [col("driver", "Driver"), col("stint", "Stint"), col("compound", "Compound"), col("lap_start", "From lap"),
               col("lap_end", "To lap"), col("laps", "Laps"), col("avg_pace", "Avg clean pace", "s"),
               col("deg_per_lap", "Degradation", "s/lap")], stint_rows, ["stints", "laps"]),
    ]

    track = build_track(s, loc, L, pos, dm)
    tc = chart("track", "Track map", "track", {"type": "track", "file": f"track_{k}.json"},
               [col("driver", "Driver"), col("x", "x"), col("y", "y"), col("position", "Running order"),
                col("time_s", "Time", "s")], [], ["location", "position"], min_rows=0)
    tc["status"], tc["n"] = ("ok" if track else "insufficient_data"), (len(track["cars"]) if track else 0)
    charts.append(tc)

    rejected_rows = recs(rejected_df, REJECT_COLS) + ex
    race = {"schema_version": config.SCHEMA_VERSION, "session_key": k, "year": int(s["year"]),
            "name": s["meeting_name"], "circuit": s["circuit"], "country": s["country"], "date": s["date_start"],
            "drivers": sorted(dm.values(), key=lambda d: (d["team"], d["number"])), "quality": quality,
            "rejected_file": f"rejected_{k}.json", "track_file": f"track_{k}.json" if track else None,
            "charts": charts}
    return race, rejected_rows, track


# ============================================================ track map
def build_track(s, loc, L, pos, dm):
    """All drivers' location samples on one clock -> track JSON, or None if there is too little data."""
    if loc.empty or L.empty or loc.driver_number.nunique() < 2:
        return None
    t0 = dt(pd.Series([s["date_start"]])).iloc[0]
    X = loc.assign(t=(dt(loc.date) - t0).dt.total_seconds()).dropna(subset=["t"]).sort_values("t")
    grid = np.arange(max(0.0, X.t.min()), X.t.max(), config.LOC_STEP_S)
    if len(grid) < 10:
        return None
    P = pos.assign(pt=(dt(pos.date) - t0).dt.total_seconds()).dropna(subset=["pt"]).sort_values("pt")
    ints = lambda a: [None if not np.isfinite(v) else int(round(v)) for v in a]       # noqa: E731

    cars = {}
    for n, g in X.groupby("driver_number"):
        pg = P[P.driver_number == n]
        idx = np.searchsorted(pg.pt.values, grid, side="right") - 1
        order = [None if i < 0 else int(pg.position.values[i]) for i in idx] if len(pg) else [None] * len(grid)
        cars[dm[int(n)]["code"]] = {"number": int(n), "pos": order,
                                    "x": ints(np.interp(grid, g.t, g.x, left=np.nan, right=np.nan)),
                                    "y": ints(np.interp(grid, g.t, g.y, left=np.nan, right=np.nan))}
    # outline = one driver's clean lap through its own samples
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
        m = int(95 / config.LOC_STEP_S)
        outline = list(zip(ints(gb.x.iloc[:m]), ints(gb.y.iloc[:m])))
    starts = np.sort((L[L.driver_number == best].t0.dropna() - t0).dt.total_seconds().values)
    return {"session_key": int(s["session_key"]), "step": config.LOC_STEP_S, "t_start": float(grid[0]),
            "n": int(len(grid)), "outline": outline,
            "lap_at": [int(v) for v in np.searchsorted(starts, grid, side="right")], "cars": cars}
