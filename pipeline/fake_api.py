"""
fake_api.py - a small, deterministic stand-in for the OpenF1 API.

Used by tests/offline_test.py and demo.py so the whole pipeline can run with NO internet:

    etl.get = fake_api.get          # replace the real HTTP function

It produces 3 races (2024) x 6 drivers x 3 teams x 30 laps, including the awkward cases the
real pipeline must survive: an implausible lap time, a lap without a time, a safety car,
out-laps, a repair-length pit stop, an invalid pit record, all-zero location frames and a
race with rain. The data is synthetic - it only resembles Formula 1 data.

Set `EMPTY = {"pit": {9003}}` to make an endpoint return nothing for a session.
Add a race with `RACES[9004] = (...)` to simulate a new race appearing on the API.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

UTC = timezone.utc
RACES = {  # session_key -> (meeting_key, country, circuit, start)
    9001: (101, "Bahrain", "Sakhir", datetime(2024, 3, 2, 15, 0, tzinfo=UTC)),
    9002: (102, "Saudi Arabia", "Jeddah", datetime(2024, 3, 9, 17, 0, tzinfo=UTC)),
    9003: (103, "Australia", "Melbourne", datetime(2024, 3, 24, 4, 0, tzinfo=UTC)),
}
DRIVERS = [  # number, code, name, team, colour
    (1, "AAA", "Alex Alpha", "Team Red", "E10600"), (11, "BBB", "Ben Bravo", "Team Red", "E10600"),
    (44, "CCC", "Cal Charlie", "Team Silver", "27F4D2"), (63, "DDD", "Dan Delta", "Team Silver", "27F4D2"),
    (4, "EEE", "Eli Echo", "Team Orange", "FF8000"), (81, "FFF", "Finn Foxtrot", "Team Orange", "FF8000"),
]
N_LAPS = 30
CALLS = []          # every endpoint requested (tests use this to prove incremental loading)
EMPTY = {}          # endpoint -> set of session keys for which to return no rows
_cache = {}


def iso(t):
    return t.isoformat()


def _build(key):
    """Generate every table for one session."""
    rng = np.random.default_rng(key)
    start = RACES[key][3]
    d = {k: [] for k in ("laps", "stints", "pit", "position", "weather", "location")}
    starts = {}                                         # (driver index, lap) -> start offset in seconds
    for i, (num, _, _, team, _) in enumerate(DRIVERS):
        base = 90 + (i // 2) * 0.5 + (i % 2) * 0.2
        pits = [10, 21] if i % 2 == 0 else [14]
        comps = ["MEDIUM", "HARD", "SOFT"] if i % 2 == 0 else ["SOFT", "HARD"]
        t, age, stint = 0.0, 0, 0
        d["stints"].append({"session_key": key, "driver_number": num, "stint_number": 1, "compound": comps[0],
                            "lap_start": 1, "lap_end": pits[0], "tyre_age_at_start": 0})
        for lap in range(1, N_LAPS + 1):
            starts[(i, lap)] = t
            age += 1
            dur = base + 0.07 * age + rng.normal(0, 0.12)
            if lap == 1:
                dur += 12                                       # standing start: caught by the slow-lap rule
            if 12 <= lap <= 14:
                dur += 25                                       # safety car
            if lap in pits:
                dur += 3                                        # in-lap
            out = lap - 1 in pits
            if out:
                dur += 5
            rec = {"session_key": key, "driver_number": num, "lap_number": lap, "lap_duration": round(dur, 3),
                   "is_pit_out_lap": out if out else None, "date_start": iso(start + timedelta(seconds=t)),
                   "duration_sector_1": round(dur * 0.3, 3), "duration_sector_2": round(dur * 0.4, 3),
                   "duration_sector_3": round(dur * 0.3, 3), "st_speed": 310.0}
            if num == 11 and lap == 5:
                rec["lap_duration"] = 500.0                     # implausible -> rejected by validation
            if num == 63 and lap == 7:
                rec["lap_duration"] = None                      # no time set -> excluded by cleaning
            d["laps"].append(rec)
            t += dur
            if lap in pits:
                age = 0
                stint += 1
                nxt = pits[stint] if stint < len(pits) else N_LAPS
                d["stints"].append({"session_key": key, "driver_number": num, "stint_number": stint + 1,
                                    "compound": comps[stint], "lap_start": lap + 1, "lap_end": nxt,
                                    "tyre_age_at_start": 0})
                d["pit"].append({"session_key": key, "driver_number": num, "lap_number": lap,
                                 "lane_duration": round(22 + rng.normal(0, 0.5), 2)})
        end = t
        # location: 1 Hz points on an ellipse, first 5 frames invalid (all zero) for the first driver
        for sec in range(int(end)):
            phi = 2 * np.pi * (sec / 90.0 - i * 0.004)
            x, y, z = (int(5000 * np.cos(phi)), int(3000 * np.sin(phi)), 100)
            if i == 0 and sec < 5:
                x = y = z = 0
            d["location"].append({"session_key": key, "driver_number": num,
                                  "date": iso(start + timedelta(seconds=sec)), "x": x, "y": y, "z": z})
    d["pit"][0]["lane_duration"] = 65.0                         # repair-length stop -> excluded by cleaning
    d["pit"][-1]["lane_duration"] = 0.5                         # impossible -> rejected by validation
    # positions at every lap start, ordered by elapsed time
    for lap in range(1, N_LAPS + 1):
        order = sorted(range(len(DRIVERS)), key=lambda i: (starts[(i, lap)], i))
        for rank, i in enumerate(order, 1):
            d["position"].append({"session_key": key, "driver_number": DRIVERS[i][0], "position": rank,
                                  "date": iso(start + timedelta(seconds=starts[(i, lap)] + 1))})
    for m in range(-5, 125):                                    # weather, once a minute
        d["weather"].append({"session_key": key, "date": iso(start + timedelta(minutes=m)),
                             "air_temperature": 24 + np.sin(m / 20), "track_temperature": 33 + 3 * np.sin(m / 25),
                             "humidity": 40, "rainfall": 1 if key == 9003 and m > 40 else 0})
    return d


def get(endpoint, fresh=False, _post=None, **params):
    """Same signature as extract.get; answers from the generated data."""
    CALLS.append(endpoint)
    key = params.get("session_key")
    if endpoint == "meetings":
        return [{"meeting_key": v[0], "meeting_name": f"{v[1]} Grand Prix"} for v in RACES.values()]
    if endpoint == "sessions":
        return [{"session_key": k, "meeting_key": v[0], "session_name": params.get("session_name", "Race"),
                 "country_name": v[1], "circuit_short_name": v[2], "location": v[2],
                 "date_start": iso(v[3]), "date_end": iso(v[3] + timedelta(hours=2))} for k, v in RACES.items()
                if v[3].year == params.get("year", v[3].year)]
    if key in EMPTY.get(endpoint, set()):
        return []
    if endpoint == "drivers":
        return [{"session_key": key, "driver_number": n, "name_acronym": c, "full_name": f, "team_name": t,
                 "team_colour": col} for n, c, f, t, col in DRIVERS]
    table = endpoint
    if key not in _cache:
        _cache[key] = _build(key)
    rows = _cache[key][table]
    if "driver_number" in params:
        rows = [r for r in rows if r["driver_number"] == params["driver_number"]]
    rows = [dict(r) for r in rows]
    return _post(rows) if _post else rows
