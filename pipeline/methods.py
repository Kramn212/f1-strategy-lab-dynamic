"""
methods.py - the "Method" tab text for every chart.

Each entry explains: where the data comes from, every filter step (with the
real thresholds read from config.py), the formula used for each plotted
value, the baseline, assumptions and limitations. eda.py attaches the entry
to the chart object, and the dashboard renders it. Because the numbers come
from config.py, the text always matches what the code actually did.
"""
import config

# ---- building blocks shared by several charts ---------------------------
def _clean_lap_steps():
    return [
        "Start from every lap that passed validation (physically plausible duration, valid numbers).",
        "Remove laps with no recorded duration.",
        "Remove out-laps (first lap after leaving the pits - cold tyres, pit-lane exit).",
        "Remove laps that overlap a safety car, virtual safety car or red-flag period "
        "(taken from race-control messages; see limitations).",
        f"Compute the adjusted time = lap time + {config.FUEL_CORR_S_PER_LAP} s x lap number "
        "(adds back the time gained as the car burns fuel).",
        f"Remove laps whose adjusted time is more than {config.SLOW_LAP_FACTOR}x that driver's median "
        "adjusted time (in-laps, traffic, incidents). The median uses laps that survived the previous steps.",
    ]


FUEL_ASSUMPTION = (f"The fuel correction of {config.FUEL_CORR_S_PER_LAP} s per lap is an assumption, "
                   "not a measurement; the true value varies by circuit and car.")
RC_LIMIT = ("Safety-car periods are detected from the wording of race-control messages "
            "(DEPLOYED ... ENDING / IN THIS LAP, red flag ... clear/green). If the wording changes "
            "or messages are missing, those laps are only caught by the slow-lap rule.")


def get(chart_id):
    """Return the method dict for a chart id (see eda.py for the ids)."""
    m = METHODS[chart_id]() if callable(METHODS[chart_id]) else METHODS[chart_id]
    m.setdefault("assumptions", [])
    m.setdefault("limitations", [])
    return m


def _mk(summary, source, steps, formula, baseline, assumptions=(), limitations=()):
    return {"summary": summary, "source": list(source), "steps": list(steps), "formula": formula,
            "baseline": baseline, "assumptions": list(assumptions), "limitations": list(limitations)}


METHODS = {
    "race_result": lambda: _mk(
        "Summary of every driver's race: start, finish, stops and pace consistency.",
        ["position", "laps", "stints"],
        ["Start position = a driver's first recorded position sample; finish position = the last sample.",
         "Stops = number of tyre stints - 1.",
         "Best / median / std lap use the CLEAN laps (steps below).", *_clean_lap_steps()],
        "positions gained = start position - finish position;  lap_std = standard deviation of clean lap times",
        "Each driver against their own laps; no cross-driver baseline.",
        limitations=["OpenF1 does not provide the official classification here, so 'finish position' is the last "
                     "live position sample and can differ from the official result (penalties, DSQ).", RC_LIMIT]),

    "position_by_lap": lambda: _mk(
        "Race position of every driver at the start of each lap.",
        ["position", "laps"],
        ["For each lap, take the position sample closest in time to the lap start timestamp."],
        "position(driver, lap) = nearest position sample to lap.date_start",
        "Absolute race position (1 = leader).",
        limitations=["Nearest-sample matching: a position change inside a lap shows up on the next lap."]),

    "lap_times": lambda: _mk(
        "Clean lap times of every driver.",
        ["laps", "race_control"], _clean_lap_steps(),
        "plotted value = the real lap time (seconds), not the fuel-adjusted one",
        "None - raw clean lap times.",
        [FUEL_ASSUMPTION], [RC_LIMIT]),

    "tyre_strategy": lambda: _mk(
        "Each driver's tyre stints: compound and lap range.",
        ["stints"],
        ["Each stint becomes one bar from lap_start-1 to lap_end."],
        "bar length = lap_end - lap_start + 1 laps",
        "-",
        limitations=["If a stint has no recorded end the API's lap_end is used as given."]),

    "pit_stops": lambda: _mk(
        "Pit-lane time of every stop.",
        ["pit"],
        ["Remove stops with a pit-lane time >= " + str(config.PIT_MAX_S) +
         " s (repairs / red-flag stops that say nothing about normal stop speed)."],
        "plotted value = pit-lane time (entry to exit), seconds",
        "-",
        limitations=["This is time in the pit lane, not the total race time lost by stopping."]),

    "degradation": lambda: _mk(
        "How much slower the car gets as the tyres age, per compound.",
        ["laps", "stints", "race_control"],
        [*_clean_lap_steps(),
         "Attach compound and tyre age: age = age at stint start + lap - stint start lap + 1.",
         f"Keep ages up to {config.DEG_MAX_AGE}.",
         "delta = adjusted lap time - driver's median adjusted time (so +0.5 means 0.5 s slower than the driver's typical pace).",
         f"Average delta per (compound, age); keep a point only if it has >= {config.MIN_N_DEG_RACE} laps in a "
         f"race ({config.MIN_N_DEG_POOLED} when pooling races); keep a compound only with >= "
         f"{config.MIN_AGES_PER_COMPOUND} ages."],
        "degradation(compound, age) = mean( adj_time - driver_median_adj_time )",
        "Each driver's own median pace.",
        [FUEL_ASSUMPTION],
        ["Tyre age and track evolution overlap: the track gets faster over a race, which flatters old tyres.", RC_LIMIT]),

    "weather": lambda: _mk(
        "Track and air temperature during the session.",
        ["weather"], ["No filtering beyond validation."],
        "minute = (sample time - first sample time) / 60",
        "-", limitations=["Weather is sampled about once a minute at one point of the circuit."]),

    "pace_delta": lambda: _mk(
        "A driver's clean lap time compared with the whole field and with their teammate, lap by lap.",
        ["laps", "race_control"],
        [*_clean_lap_steps(),
         "delta vs field = adjusted time - median adjusted time of ALL drivers' clean laps in the race.",
         "delta vs teammate = adjusted time - teammate's adjusted time on the same lap (needs both clean)."],
        "delta_field = adj - median(adj, all clean laps);  delta_teammate = adj - adj_teammate(lap)",
        "Field median of the race / the teammate's same-lap time. Negative = faster.",
        [FUEL_ASSUMPTION],
        ["Field median mixes different tyre ages and fuel loads; use it as a rough guide."]),

    "driver_laps": lambda: _mk(
        "Every lap of the driver, including the ones removed from the analysis.",
        ["laps", "stints", "position"],
        ["No lap is hidden: the 'status' column says 'clean' or the rule that removed the lap."],
        "status = clean or the id of the first cleaning rule the lap failed",
        "-", limitations=[RC_LIMIT]),

    "driver_stints": lambda: _mk(
        "Per-stint summary for each driver.",
        ["stints", "laps"],
        [*_clean_lap_steps(),
         "avg pace = mean clean lap time inside the stint.",
         "deg per lap = slope of a straight line fitted to (tyre age, adjusted time); only for stints with >= 5 clean laps."],
        "deg_per_lap = slope of least-squares fit  adj_time ~ tyre_age",
        "-", [FUEL_ASSUMPTION],
        ["Straight-line fit; real degradation is often non-linear (a 'cliff')."]),

    "season_drivers": lambda: _mk(
        "Averages per driver over the selected races.",
        ["position", "laps", "stints"],
        ["Per race: start/finish from position data, pace = mean delta vs field of clean laps, stops from stints.",
         "Then averaged over the races the driver took part in."],
        "avg_* = arithmetic mean over races",
        "Field median pace of each race.",
        limitations=["Finish positions are last live samples, not official classifications.", RC_LIMIT]),

    "pooled_degradation": lambda: _mk(
        "Tyre degradation per compound, pooled over many races.",
        ["laps", "stints"],
        [*_clean_lap_steps(), f"Keep ages up to {config.DEG_MAX_AGE}.",
         "delta = adjusted time - driver's median adjusted time (per race).",
         f"Average per (compound, age), keep points with >= {config.MIN_N_DEG_POOLED} laps and compounds with >= "
         f"{config.MIN_AGES_PER_COMPOUND} ages."],
        "degradation(compound, age) = mean over all pooled races of (adj - driver_median_adj)",
        "Each driver's own median pace in that race.",
        [FUEL_ASSUMPTION],
        ["Pools different circuits, temperatures and car generations; use the per-race chart for detail."]),

    "pooled_pit_team": lambda: _mk(
        "Median pit-lane time per team.",
        ["pit"],
        [f"Remove stops >= {config.PIT_MAX_S} s.", f"Keep teams with >= {config.MIN_PIT_TEAM_POOLED} stops."],
        "plotted value = median pit-lane time per team (seconds)",
        "-", limitations=["Pit-lane time only; excludes time lost braking in and accelerating out."]),

    "pooled_pace_stops": lambda: _mk(
        "Average pace vs number of stops.",
        ["laps", "stints"],
        [*_clean_lap_steps(),
         "Per driver per race: mean delta vs field over clean laps, and number of stops (3 or more grouped as '3+').",
         f"Average over driver-races per stop count; keep groups with >= {config.MIN_STOPS_N} driver-races."],
        "pace = mean over driver-races of ( mean(adj - field_median_adj) )",
        "Field median per race. Negative = faster than the field.",
        [FUEL_ASSUMPTION],
        ["Correlation, not causation: faster cars also tend to need fewer stops."]),

    "pooled_pace_temp": lambda: _mk(
        "Average pace relative to the driver's median, by track temperature band.",
        ["laps", "weather"],
        [*_clean_lap_steps(),
         "Match every lap to the weather sample nearest its start time.",
         f"Group into 5-degree bands; keep bands with >= {config.MIN_TEMP_BAND_N} laps."],
        "pace(band) = mean(adj - driver_median_adj)",
        "Each driver's median pace in that race.",
        [FUEL_ASSUMPTION], ["Hotter races are also different circuits, so band differences mix several effects."]),

    "pooled_compound_rain": lambda: _mk(
        "Share of clean laps on each compound, dry vs rain.",
        ["laps", "stints", "weather"],
        [*_clean_lap_steps(),
         "A lap is 'Rain' if the weather sample nearest its start reports rainfall > 0.",
         f"Keep weather classes with >= {config.MIN_RAIN_N} laps."],
        "share(compound | weather) = laps on compound / all laps in that weather class",
        "-", limitations=["Rain flag comes from one weather station."]),

    "track": lambda: _mk(
        "Car positions on a circuit drawing, scrubbable through the race.",
        ["location", "position", "laps"],
        ["Location samples were downsampled when loaded: one per driver every " + str(config.LOC_STEP_S) + " s.",
         "Samples with x = y = z = 0 (invalid frames) are removed.",
         "All drivers are put on one shared clock (seconds since session start) by linear interpolation. "
         "Before a driver's first and after their last sample (e.g. a retirement) the dot is simply not drawn; "
         "gaps in the middle are bridged by the interpolation.",
         "The circuit outline is one driver's clean lap drawn through its location samples.",
         "The running order comes from the position table, NOT from where the dots are on the drawing."],
        "x(t), y(t) = linear interpolation between the two nearest samples of that driver",
        "-",
        limitations=["Coarse sampling makes the outline polygonal and can cut corners.",
                     "Location data may be missing for some races; the map is then unavailable.",
                     "'Received' location rows are counted after downsampling."]),
}
