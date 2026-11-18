"""
offline_test.py - end-to-end test with NO internet.

Run:  python pipeline/tests/offline_test.py

It swaps the real HTTP function for fake_api.get, runs etl.main() and eda.main() into a temporary
folder and checks the guarantees the dashboard relies on:

  1. index.json lists every race, and every race has its file and its drivers
  2. accounting:  received = rejected + excluded + used   (every table, every race)
  3. rejected rows keep ALL errors and the COMPLETE raw record, with a stable rule id
  4. every chart is well-formed: its columns exist in its rows (chart and table share one list)
  5. a session with a missing table gives 'insufficient_data' charts, not a crash
  6. re-running the ETL does not duplicate rows and does not re-fetch loaded sessions
  7. the track map and its outline are produced; invalid all-zero frames are excluded
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import config            # noqa: E402
import eda               # noqa: E402
import etl               # noqa: E402
import fake_api          # noqa: E402


def read(p):
    return json.loads(Path(p).read_text())


def main():
    tmp = Path(tempfile.mkdtemp())
    config.CACHE_DIR, config.DB_PATH, config.OUT_DIR = tmp / "cache", tmp / "f1.db", tmp / "out"
    config.YEARS, config.LOCATION_YEARS = [2024], [2024]
    etl.get = fake_api.get
    fake_api.EMPTY = {"pit": {9003}}                       # race 9003 has NO pit data

    etl.main()
    eda.main()
    out = config.OUT_DIR

    # 1 ---- index and files
    idx = read(out / "index.json")
    assert idx["schema_version"] == config.SCHEMA_VERSION
    races = idx["sessions"]["2024"]
    assert [r["session_key"] for r in races] == [9001, 9002, 9003], races
    for r in races:
        assert (out / r["file"]).exists() and len(r["drivers"]) == 6
        assert (out / f"rejected_{r['session_key']}.json").exists()
    assert (out / "season_2024.json").exists() and (out / "overall.json").exists()
    print("ok 1  index + files")

    # 2 ---- accounting
    for r in races:
        race = read(out / r["file"])
        for table, q in race["quality"].items():
            assert q["received"] == q["rejected"] + q["excluded"] + q["used"], (r["session_key"], table, q)
    print("ok 2  received = rejected + excluded + used for every table of every race")

    # 3 ---- rejected rows
    rej = read(out / "rejected_9001.json")["rows"]
    val_rows = [x for x in rej if x["stage"] == "validation"]
    cln_rows = [x for x in rej if x["stage"] == "cleaning"]
    assert any(x["rule"] == "LAPS_LAP_DURATION_LESS_THAN" for x in val_rows), "implausible lap not rejected"
    assert any(x["rule"] == "PIT_PIT_DURATION_GREATER_THAN" for x in val_rows), "invalid pit not rejected"
    bad = next(x for x in val_rows if x["table"] == "laps")
    assert json.loads(bad["raw"])["lap_duration"] == 500.0 and json.loads(bad["errors"])
    rules = {x["rule"] for x in cln_rows}
    for need in ("LAP_NULL_DURATION", "LAP_PIT_OUT", "LAP_NEUTRALISED", "LAP_SLOW", "PIT_TOO_LONG", "LOC_ZERO_POSITION"):
        assert need in rules, f"missing cleaning rule {need}: {sorted(rules)}"
    print("ok 3  rejected + excluded rows carry rule id, all errors and the full raw record")

    # 4 ---- chart shape
    for r in races:
        for c in read(out / r["file"])["charts"]:
            assert {"id", "title", "scope", "plot", "columns", "rows", "method", "tables", "status"} <= set(c)
            keys = {x["key"] for x in c["columns"]}
            for row in c["rows"][:50]:
                assert keys == set(row), (c["id"], keys ^ set(row))
            assert c["method"]["steps"] and c["method"]["formula"]
            # the website draws from plot.x / plot.y / plot.group / plot.ys: they must be real columns
            p = c["plot"]
            assert p["type"] in {"line", "bar", "stacked", "stints", "table", "track"}, p
            for k in [p.get(a) for a in ("x", "y", "group", "color_key")] + p.get("ys", []):
                assert k is None or k in keys, (c["id"], k, keys)
    r1 = {c["id"]: c for c in read(out / races[0]["file"])["charts"]}
    assert r1["lap_times"]["status"] == "ok" and r1["race_result"]["rows"][0]["finish_pos"] == 1
    print("ok 4  every chart has matching columns/rows and a method")

    # 5 ---- missing table
    r3 = {c["id"]: c for c in read(out / races[2]["file"])["charts"]}
    assert r3["pit_stops"]["status"] == "insufficient_data" and r3["lap_times"]["status"] == "ok"
    assert idx["sessions"]["2024"][2]["has"]["pit"] is False
    print("ok 5  missing pit data -> insufficient_data, nothing crashed")

    # 6 ---- incremental, no duplicates
    import sqlite3
    count = lambda t: sqlite3.connect(config.DB_PATH).execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]  # noqa
    before = {t: count(t) for t in ("laps", "sessions", "rejected", "counts")}
    fake_api.CALLS.clear()
    etl.main()
    assert {t: count(t) for t in before} == before, "re-run duplicated rows"
    assert set(fake_api.CALLS) <= {"meetings", "sessions"}, fake_api.CALLS
    print("ok 6  re-run: no duplicates, no re-fetch of loaded sessions")

    # 7 ---- track
    tr = read(out / "track_9001.json")
    assert len(tr["cars"]) == 6 and len(tr["outline"]) >= 15 and len(tr["lap_at"]) == tr["n"]
    assert all(len(c["x"]) == tr["n"] for c in tr["cars"].values())
    assert read(out / races[0]["file"])["track_file"] == "track_9001.json"
    print("ok 7  track map built (outline, 6 cars on one clock)")

    # pooled charts
    pooled = {c["id"]: c for c in read(out / "season_2024.json")["charts"]}
    assert {"p_degradation", "p_pit_team", "p_pace_stops", "p_pace_temp", "p_compound_rain", "season_drivers"} <= set(pooled)
    print("ok 8  pooled charts present")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
