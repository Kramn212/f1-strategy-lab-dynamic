"""
offline_test.py - end-to-end test with NO internet:  python pipeline/tests/offline_test.py

The real HTTP function is replaced by fake_api.get, and everything runs in a temporary folder.

  1. first refresh loads the races and writes every file the website needs
  2. accounting: received = rejected + excluded + used (every table, every race)
  3. rejected/excluded rows keep rule id, ALL errors and the COMPLETE raw record
  4. every chart is well-formed (plot keys are real columns; chart + table share one row list)
  5. a second refresh downloads NOTHING but the session list (no new race = no data requests)
  6. a NEW race appearing on the API is detected and only that race is downloaded
  7. a race whose data is not published yet is skipped, then picked up (fresh) once it is
  8. empty answers are not cached; the session list is never cached
  9. the web server: status, refresh in the background, cooldown (429), key protection (401)
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                 # noqa: E402
import extract                # noqa: E402
import fake_api               # noqa: E402
import load                   # noqa: E402
import run                    # noqa: E402
import server                 # noqa: E402

read = lambda p: json.loads(Path(p).read_text())      # noqa: E731


def main():
    tmp = Path(tempfile.mkdtemp())
    config.CACHE_DIR, config.DB_PATH, config.OUT_DIR = tmp / "cache", tmp / "f1.db", tmp / "out"
    config.YEARS, config.INCLUDE_CURRENT_YEAR, config.LOCATION_YEARS = [2024], False, [2024]
    real_get = extract.get
    extract.get = fake_api.get
    out = config.OUT_DIR
    quiet = lambda *a, **k: None                      # noqa: E731

    # 1 ---- first refresh
    res = run.refresh(progress=quiet)
    assert len(res["new"]) == 3 and not res["errors"], res
    idx = read(out / "index.json")
    races = idx["sessions"]["2024"]
    assert idx["schema_version"] == config.SCHEMA_VERSION
    assert [r["session_key"] for r in races] == [9001, 9002, 9003] and all(len(r["drivers"]) == 6 for r in races)
    for r in races:
        for f in (r["file"], f"rejected_{r['session_key']}.json", f"track_{r['session_key']}.json"):
            assert (out / f).exists(), f
    print("ok 1  first refresh: 3 races, index + race + rejected + track files")

    # 2 ---- accounting
    for r in races:
        for table, q in read(out / r["file"])["quality"].items():
            assert q["received"] == q["rejected"] + q["excluded"] + q["used"], (r["session_key"], table, q)
    print("ok 2  received = rejected + excluded + used for every table of every race")

    # 3 ---- rejected rows
    rows = read(out / "rejected_9001.json")["rows"]
    val_rows = [x for x in rows if x["stage"] == "validation"]
    assert any(x["rule"] == "LAPS_LAP_DURATION_LESS_THAN" for x in val_rows)
    assert any(x["rule"] == "PIT_PIT_DURATION_GREATER_THAN" for x in val_rows)
    bad = next(x for x in val_rows if x["table"] == "laps")
    assert json.loads(bad["raw"])["lap_duration"] == 500.0 and json.loads(bad["errors"])
    rules = {x["rule"] for x in rows if x["stage"] == "cleaning"}
    for need in ("LAP_NULL_DURATION", "LAP_PIT_OUT", "LAP_SLOW", "PIT_TOO_LONG", "LOC_ZERO_POSITION"):
        assert need in rules, (need, sorted(rules))
    print("ok 3  rejected/excluded rows carry rule id, all errors and the full raw record")

    # 4 ---- chart shape
    for r in races:
        for c in read(out / r["file"])["charts"]:
            keys = {x["key"] for x in c["columns"]}
            assert c["method"]["steps"] and c["method"]["formula"] and c["status"] in ("ok", "insufficient_data")
            assert all(set(row) == keys for row in c["rows"][:50]), c["id"]
            p = c["plot"]
            assert p["type"] in {"line", "bar", "stacked", "stints", "table", "track"}
            for k in [p.get(a) for a in ("x", "y", "group", "color_key")] + p.get("ys", []):
                assert k is None or k in keys, (c["id"], k)
    by = {c["id"]: c for c in read(out / races[0]["file"])["charts"]}
    assert by["lap_times"]["status"] == "ok" and by["race_result"]["rows"][0]["finish_pos"] == 1
    assert by["degradation"]["status"] == "ok" and by["track"]["status"] == "ok"
    print("ok 4  charts well-formed; degradation + track present")

    # 5 ---- second refresh downloads nothing
    fake_api.CALLS.clear()
    res = run.refresh(progress=quiet)
    assert res["new"] == [] and set(fake_api.CALLS) == {"sessions"}, fake_api.CALLS
    print("ok 5  second refresh: only the session list was requested")

    # 6 ---- new race appears
    fake_api.RACES[9004] = (104, "Japan", "Suzuka", datetime(2024, 4, 7, 5, 0, tzinfo=timezone.utc))
    fake_api.CALLS.clear()
    res = run.refresh(progress=quiet)
    assert res["new"] == ["2024 Japan"], res
    assert 9004 in {r["session_key"] for r in read(out / "index.json")["sessions"]["2024"]}
    assert len(fake_api.CALLS) < 40 and fake_api.CALLS.count("laps") == 1, fake_api.CALLS.count("laps")
    print("ok 6  new race detected; only that race downloaded")

    # 7 ---- not published yet -> skipped, later loaded with fresh downloads
    fake_api.RACES[9005] = (105, "China", "Shanghai", datetime(2024, 4, 21, 7, 0, tzinfo=timezone.utc))
    fake_api.EMPTY = {"laps": {9005}}
    day1 = datetime(2024, 4, 22, 12, 0, tzinfo=timezone.utc)             # a day after the race
    res = run.refresh(progress=quiet, now=day1)
    assert "2024 China (no data published yet)" in res["waiting"] and "2024 China" not in res["new"], res
    assert 9005 not in load.complete_sessions()
    fake_api.EMPTY = {}
    res = run.refresh(progress=quiet, now=day1)
    assert res["new"] == ["2024 China"], res
    # a race that stays empty for more than GIVE_UP_DAYS is given up on (no endless retries)
    fake_api.RACES[9007] = (107, "Spain", "Barcelona", datetime(2024, 6, 2, 13, 0, tzinfo=timezone.utc))
    fake_api.EMPTY = {"laps": {9007}}
    run.refresh(progress=quiet, now=datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc))
    late = run.refresh(progress=quiet, now=datetime(2024, 8, 1, tzinfo=timezone.utc))
    assert late["unavailable"] == 1, late
    fake_api.EMPTY = {}
    del fake_api.RACES[9007]
    print("ok 7  unpublished race skipped, picked up once data exists, abandoned after GIVE_UP_DAYS")
    # a race that has not ended yet is 'waiting', not loaded
    now_early = datetime(2024, 4, 21, 8, 0, tzinfo=timezone.utc)
    assert not extract.is_ready({"date_end": "2024-04-21T09:00:00+00:00"}, now_early)

    # 8 ---- cache rules, using the REAL get against a stub HTTP layer
    extract.get = real_get
    calls = []
    class R:                                              # minimal response stub
        def __init__(self, code, data): self.status_code, self._d = code, data
        def json(self): return self._d
        def raise_for_status(self): pass
    extract.requests.get = lambda url, timeout=0: (calls.append(url) or R(200, [] if "/laps" in url else [{"a": 1}]))
    config.REQUEST_GAP_S = 0
    extract.list_sessions(2099); extract.list_sessions(2099)
    assert len(calls) == 2, "the session list must always ask the API (never cached)"
    calls.clear()
    extract.get("drivers", session_key=1); extract.get("drivers", session_key=1)
    assert len(calls) == 1, "non-empty answers ARE cached"
    calls.clear()
    extract.get("laps", session_key=1); extract.get("laps", session_key=1)
    assert len(calls) == 2, "empty answers must not be cached"
    print("ok 8  session list never cached, empty answers never cached")
    extract.get = fake_api.get

    # 9 ---- web server
    config.REFRESH_COOLDOWN_S = 2
    srv = server.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(path, method="GET", headers=None):
        req = urllib.request.Request(base + path, method=method, headers=headers or {}, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    assert call("/api/status")[0] == 200 and json.loads(call("/api/status")[1])["state"] == "idle"
    assert call("/data/index.json")[0] == 200 and call("/")[0] == 200
    assert call("/data/../../etc/passwd")[0] == 404
    fake_api.RACES[9006] = (106, "Miami", "Miami", datetime(2024, 5, 5, 20, 0, tzinfo=timezone.utc))
    code, body = call("/api/refresh", "POST")
    assert code == 202, (code, body)
    assert call("/api/refresh", "POST")[0] == 409                        # already running
    for _ in range(100):                                                 # poll until finished
        st = json.loads(call("/api/status")[1])
        if st["state"] != "running":
            break
        time.sleep(0.1)
    assert st["state"] == "done" and st["last_result"]["new"] == ["2024 Miami"], st
    assert call("/api/refresh", "POST")[0] == 429                        # cooldown
    time.sleep(2.2)
    import os
    os.environ["REFRESH_KEY"] = "secret"
    assert call("/api/refresh", "POST")[0] == 401
    assert call("/api/refresh", "POST", {"X-Refresh-Key": "secret"})[0] == 202
    srv.shutdown()
    print("ok 9  server: status, background refresh, 409 / 429 / 401 protection, no path traversal")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
