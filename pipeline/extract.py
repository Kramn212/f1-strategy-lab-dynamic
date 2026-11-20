"""
extract.py - the EXTRACT step of the ETL pipeline.

Everything that talks to the OpenF1 API lives here, using the `requests` library.

Caching rules (what is and is not remembered between runs)
----------------------------------------------------------
* Each response is saved in cache/ keyed by endpoint + parameters, so repeating the same
  request never hits the API again.
* `fresh=True` ignores the cache. It is used for the session and meeting LISTS - those change
  when a new race is added, so caching them forever would hide new races.
* EMPTY replies are never cached. If a race is requested before OpenF1 has published its
  data, the next run asks again instead of remembering "nothing".
* Location data is thinned (one sample per driver every config.LOC_STEP_S seconds) BEFORE
  it is cached, so the cache stays small.
"""
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

import config

_last_call = 0.0


def get(endpoint, fresh=False, _post=None, **params):
    """
    GET one OpenF1 endpoint and return the decoded JSON list.

    Range filters: a parameter name ending in > or < becomes an OpenF1 range filter,
    e.g. get("location", **{"date>": "2025-03-16T04:00:00"}) requests ...&date>=2025-03-16T04:00:00
    `_post` (optional) transforms the data before it is cached.
    """
    global _last_call
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = json.dumps(params, sort_keys=True)
    cache_file = config.CACHE_DIR / f"{endpoint}_{hashlib.md5(key.encode()).hexdigest()[:12]}.json"
    if not fresh and cache_file.exists():
        return json.loads(cache_file.read_text())

    url = f"{config.BASE_URL}/{endpoint}?" + urlencode(params, safe="<>:")
    data = None
    for attempt in range(config.MAX_RETRIES):
        wait = config.REQUEST_GAP_S - (time.time() - _last_call)       # stay under the rate limit
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = requests.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:                                       # "slow down" -> back off, retry
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 404:                                       # OpenF1 uses 404 for "no rows"
            data = []
            break
        r.raise_for_status()
        data = r.json()
        break
    if data is None:
        raise RuntimeError(f"OpenF1 request kept failing: {url}")

    if _post:
        data = _post(data)
    if data:                                                           # never cache an empty answer
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


def list_sessions(year):
    """
    The race sessions of one year. ONE small request, never cached, so a newly added race shows up.
    Only the cheap session list is asked for here - no race data is downloaded.
    """
    return get("sessions", fresh=True, year=year, session_name=config.SESSION_TYPE)


def meeting_names(year):
    """meeting_key -> Grand Prix name (not cached; only asked for when there is a new race)."""
    return {m["meeting_key"]: m.get("meeting_name") or m.get("meeting_official_name")
            for m in get("meetings", fresh=True, year=year)}


def is_ready(session, now=None):
    """A race is ready to load once it ended (plus a delay for OpenF1 to publish the data)."""
    end = session.get("date_end")
    if not end:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(end)).total_seconds() > config.PUBLISH_DELAY_S


def fetch_session(s, fresh=False, progress=print):
    """
    Download every table of ONE session. Returns {table: [raw records]} plus the drivers list.
    `fresh=True` re-downloads even if the responses are cached (used for half-loaded races).
    """
    k, y = s["session_key"], int(s["year"])
    raw = {"drivers": get("drivers", fresh=fresh, session_key=k)}
    for table in ("laps", "stints", "pit", "weather", "position"):
        progress(f"  downloading {table}")
        raw[table] = get(table, fresh=fresh, session_key=k)
    for p in raw["pit"]:                                  # the API's field name differs between seasons
        p["pit_duration"] = p.get("lane_duration") or p.get("pit_duration")

    raw["location"] = []
    if y in config.LOCATION_YEARS and s.get("date_start") and s.get("date_end"):
        window = {"date>": s["date_start"], "date<": s["date_end"]}
        numbers = sorted({d["driver_number"] for d in raw["drivers"]})
        for i, n in enumerate(numbers, 1):
            progress(f"  downloading track positions {i}/{len(numbers)}")
            raw["location"] += get("location", fresh=fresh, _post=_downsample_location,
                                   session_key=k, driver_number=n, **window)
    return raw
