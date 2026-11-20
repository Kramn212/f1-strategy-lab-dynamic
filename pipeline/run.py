"""
run.py - the pipeline orchestrator:  Extract -> Transform -> Load, incrementally.

Command line:
    python pipeline/run.py               load everything that is new (first run = everything; slow)
    python pipeline/run.py --limit 3     load at most 3 new races
    python pipeline/run.py --rebuild     rebuild every race JSON from the database (no downloads)
The website's "Refresh" button calls refresh() through server.py.

How a refresh finds new races WITHOUT downloading everything again
-------------------------------------------------------------------
1. Ask OpenF1 for the session LIST of each year (1 small request per year, never cached).
2. Compare the session keys with the ones already complete in SQLite.
3. A new race is only loaded once it has ended + config.PUBLISH_DELAY_S (OpenF1 publishes late).
4. Only those races are downloaded, validated, loaded and turned into JSON.
5. A race that was loaded EMPTY (data not yet published) is retried on later refreshes, with
   fresh downloads, until config.GIVE_UP_DAYS have passed.
"""
import argparse
from datetime import datetime, timezone

import config
import extract
import load
import transform


def _session_row(s, names):
    """OpenF1 session record -> the row stored in our `sessions` table."""
    return {"session_key": s["session_key"], "meeting_key": s.get("meeting_key"), "year": s["year"],
            "session_name": s.get("session_name"),
            "meeting_name": names.get(s.get("meeting_key")) or f"{s.get('country_name')} Grand Prix",
            "circuit": s.get("circuit_short_name"), "country": s.get("country_name"),
            "location": s.get("location"), "date_start": s.get("date_start"), "date_end": s.get("date_end")}


def build_and_write(k):
    """TRANSFORM + LOAD(json) for one stored session. Skipped when the session has no laps."""
    T = load.read_session(k)
    if T["laps"].empty:
        return False
    s = T["sessions"].iloc[0].to_dict()
    race, rejected_rows, track = transform.build_race(s, T, T["counts"].to_dict("records"), T["rejected"])
    load.write_race(k, race, rejected_rows, track)
    return True


def refresh(progress=lambda msg, done=None, total=None: print(msg), limit=None, now=None):
    """
    Find new races and load them. Returns a summary dict:
    {new: [names], remaining: n, waiting: [names], unavailable: n, errors: [text]}
    """
    now = now or datetime.now(timezone.utc)
    years = sorted(set(config.YEARS) | ({now.year} if config.INCLUDE_CURRENT_YEAR else set()))
    progress("Checking OpenF1 for new races…")
    complete = load.complete_sessions()
    attempted = {int(k) for k in load.all_sessions().session_key}
    summary = {"new": [], "remaining": 0, "waiting": [], "unavailable": 0, "errors": []}

    todo = []
    for y in years:
        for s in extract.list_sessions(y):
            s["year"] = y
            if s["session_key"] in complete:
                continue
            if not extract.is_ready(s, now):
                summary["waiting"].append(f"{y} {s.get('country_name')}")
                continue
            age_days = (now - datetime.fromisoformat(s["date_end"])).days
            if s["session_key"] in attempted and age_days > config.GIVE_UP_DAYS:
                summary["unavailable"] += 1               # tried before, still empty, too old: give up
                continue
            todo.append(s)
    todo.sort(key=lambda s: s["date_start"], reverse=True)    # newest first
    batch = todo[:limit] if limit else todo
    summary["remaining"] = len(todo) - len(batch)

    names = {}
    for i, s in enumerate(batch, 1):
        label = f"{s['year']} {s.get('country_name')}"
        progress(f"Loading {label}", i - 1, len(batch))
        try:
            if s["year"] not in names:
                names[s["year"]] = extract.meeting_names(s["year"])
            raw = extract.fetch_session(s, fresh=s["session_key"] in attempted,
                                        progress=lambda m, i=i, label=label: progress(f"{label}: {m.strip()}", i - 1, len(batch)))
            valid, counts, rejected = transform.validate_session(raw, s["session_key"])
            load.save_session(_session_row(s, names[s["year"]]), raw["drivers"], valid, counts, rejected)
            if build_and_write(s["session_key"]):
                summary["new"].append(label)
            else:
                summary["waiting"].append(f"{label} (no data published yet)")
        except Exception as exc:                          # one bad race must not stop the others
            summary["errors"].append(f"{label}: {exc}")
    progress("Updating index…", len(batch), len(batch))
    load.write_index()
    return summary


def rebuild(progress=print):
    """Recreate every race JSON from the database (use after changing thresholds in config.py)."""
    for k in sorted(load.complete_sessions()):
        progress(f"rebuilding {k}")
        build_and_write(k)
    load.write_index()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="load at most this many new races")
    ap.add_argument("--rebuild", action="store_true", help="rebuild JSON from the database, no downloads")
    a = ap.parse_args()
    if a.rebuild:
        rebuild()
    else:
        print(refresh(limit=a.limit))
