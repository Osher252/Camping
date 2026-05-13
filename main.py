"""
Camping forecast — daily run.

Pulls ECMWF ensemble forecasts for two campsites, scores each day's
"campability" probability based on family-comfort thresholds, identifies
multi-day windows, tracks how forecasts have evolved across recent runs,
and emails an alert with the appropriate urgency level.

Designed to run as a daily GitHub Actions cron job.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fetch import fetch_ensemble, EnsembleData
from email_template import render_email, send_email

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITES = {
    "penn_meadow": {
        "name": "Penn Meadow Farm",
        "lat": 51.6294,
        "lon": -0.6608,
        "region": "Chiltern Hills, Bucks",
    },
    "wowo": {
        "name": "WOWO (Wapsbourne Manor)",
        "lat": 50.9928,
        "lon": 0.0040,
        "region": "Sheffield Park, East Sussex",
    },
    "burnbake": {
        "name": "Burnbake Campsite",
        "lat": 50.6585,
        "lon": -2.0335,
        "region": "Isle of Purbeck, Dorset",
    },
}

# Comfort thresholds — calibrated for camping with young kids
DAY_MAX_MIN_C = 17.0          # daytime warmth (kids running around)
OVERNIGHT_MIN_MIN_C = 11.0    # overnight comfort (fire-side, sleeping)
PRECIP_MAX_MM = 2.0           # daily total
MIN_WINDOW_NIGHTS = 2         # need at least 2 consecutive days

# Alert cascade — confidence thresholds at each stage
WATCH_PROB_MIN = 0.30         # day 8-14: a window may be forming
ORGANIZE_PROB_MIN = 0.50      # day 5-7: start organizing things
COMMIT_PROB_MIN = 0.70        # day 1-4: book the site
COMMIT_STABILITY_RUNS = 2     # window must have held across this many prior runs

# Heartbeat — after this many silent days in a row (no alert, not Monday),
# force a status email so the user knows the script is still running.
MAX_CONSECUTIVE_SKIPS = 3

# State retention — how many past forecast runs to keep for stability tracking
STATE_RETENTION_RUNS = 7

STATE_PATH = Path("state.json")

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass
class DayScore:
    """Per-day probability that the day meets all comfort thresholds."""
    date: str  # ISO date
    p_campable: float
    p50_day_max: float
    p50_overnight_min: float
    p50_precip: float


@dataclass
class Window:
    """A run of consecutive campable days."""
    start: str
    end: str
    nights: int
    joint_prob: float  # P(all days in window campable, same ensemble members)
    days_ahead: int    # days from today until window start


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def aggregate_to_days(ens: EnsembleData) -> dict[str, dict[int, dict]]:
    """
    Reduce hourly per-member data to daily per-member summaries.

    Returns: { date_str: { member_idx: {day_max, overnight_min, precip} } }

    Definitions:
      - day_max: max temperature 10:00–19:00 local (warmth for kids)
      - overnight_min: min temperature 20:00 (day N) – 07:00 (day N+1)
                       — represents the cold trough you'd actually feel
      - precip: 24h total starting 00:00 local
    """
    out: dict[str, dict[int, dict]] = {}

    # Build a date index of hourly slots
    times = [datetime.fromisoformat(t) for t in ens.times]
    n_members = len(ens.temperature_by_member)

    # Group hours by local date
    by_date: dict[date, list[int]] = {}
    for i, t in enumerate(times):
        by_date.setdefault(t.date(), []).append(i)

    sorted_dates = sorted(by_date.keys())

    for d in sorted_dates:
        # Skip first/last partial days for overnight calc — we need next day's 7am
        next_day = d + timedelta(days=1)
        if next_day not in by_date:
            continue

        date_str = d.isoformat()
        out[date_str] = {}

        for m in range(n_members):
            temps = ens.temperature_by_member[m]
            precs = ens.precipitation_by_member[m]

            # Day max: hours 10–19 of date d
            day_idxs = [i for i in by_date[d] if 10 <= times[i].hour <= 19]
            # Overnight: hours 20–23 of date d + hours 0–7 of date d+1
            overnight_idxs = (
                [i for i in by_date[d] if times[i].hour >= 20]
                + [i for i in by_date[next_day] if times[i].hour <= 7]
            )
            # Precip: all 24 hours of date d
            precip_idxs = by_date[d]

            if not day_idxs or not overnight_idxs or not precip_idxs:
                continue

            day_max = max(temps[i] for i in day_idxs if temps[i] is not None)
            overnight_min = min(temps[i] for i in overnight_idxs if temps[i] is not None)
            precip_total = sum(precs[i] for i in precip_idxs if precs[i] is not None)

            out[date_str][m] = {
                "day_max": day_max,
                "overnight_min": overnight_min,
                "precip": precip_total,
            }

    return out


def score_days(per_day: dict[str, dict[int, dict]]) -> list[DayScore]:
    """
    Compute P(campable) per day = fraction of ensemble members where all
    thresholds are met, plus median values for context.
    """
    scores = []
    for date_str in sorted(per_day.keys()):
        members = per_day[date_str]
        if not members:
            continue

        n = len(members)
        n_campable = sum(
            1 for m in members.values()
            if m["day_max"] >= DAY_MAX_MIN_C
            and m["overnight_min"] >= OVERNIGHT_MIN_MIN_C
            and m["precip"] < PRECIP_MAX_MM
        )

        day_maxes = sorted(m["day_max"] for m in members.values())
        overnights = sorted(m["overnight_min"] for m in members.values())
        precips = sorted(m["precip"] for m in members.values())
        mid = n // 2

        scores.append(DayScore(
            date=date_str,
            p_campable=n_campable / n,
            p50_day_max=day_maxes[mid],
            p50_overnight_min=overnights[mid],
            p50_precip=precips[mid],
        ))
    return scores


def find_windows(
    per_day: dict[str, dict[int, dict]],
    scores: list[DayScore],
    today: date,
) -> list[Window]:
    """
    Find consecutive runs of ≥MIN_WINDOW_NIGHTS days where each day's
    campability is at least WATCH_PROB_MIN. Compute joint probability
    (fraction of members where ALL days in window are campable).
    """
    # Index members per day
    sorted_dates = sorted(per_day.keys())
    n_members = max((len(per_day[d]) for d in sorted_dates), default=0)

    def is_member_campable(date_str: str, m: int) -> bool:
        d = per_day.get(date_str, {}).get(m)
        if not d:
            return False
        return (
            d["day_max"] >= DAY_MAX_MIN_C
            and d["overnight_min"] >= OVERNIGHT_MIN_MIN_C
            and d["precip"] < PRECIP_MAX_MM
        )

    score_by_date = {s.date: s for s in scores}
    windows: list[Window] = []

    i = 0
    while i < len(sorted_dates):
        # Find run start: day with p_campable >= WATCH_PROB_MIN
        if score_by_date[sorted_dates[i]].p_campable < WATCH_PROB_MIN:
            i += 1
            continue

        j = i
        while (
            j + 1 < len(sorted_dates)
            and score_by_date[sorted_dates[j + 1]].p_campable >= WATCH_PROB_MIN
        ):
            j += 1

        run_len = j - i + 1
        if run_len >= MIN_WINDOW_NIGHTS:
            run_dates = sorted_dates[i:j + 1]
            # Joint prob: fraction of members campable on ALL days of run
            joint = sum(
                1 for m in range(n_members)
                if all(is_member_campable(d, m) for d in run_dates)
            ) / max(n_members, 1)

            start_date = date.fromisoformat(run_dates[0])
            windows.append(Window(
                start=run_dates[0],
                end=run_dates[-1],
                nights=run_len,
                joint_prob=joint,
                days_ahead=(start_date - today).days,
            ))
        i = j + 1

    return windows


# ---------------------------------------------------------------------------
# Stability tracking — has a window held across recent runs?
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"runs": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def update_state(state: dict, run_date: str, scores_by_site: dict[str, list[DayScore]]) -> dict:
    """Append today's run to state, prune old runs."""
    run = {
        "run_date": run_date,
        "sites": {
            site_id: {s.date: round(s.p_campable, 3) for s in scores}
            for site_id, scores in scores_by_site.items()
        },
    }
    state["runs"] = ([r for r in state["runs"] if r["run_date"] != run_date]
                     + [run])
    state["runs"] = sorted(state["runs"], key=lambda r: r["run_date"])
    state["runs"] = state["runs"][-STATE_RETENTION_RUNS:]
    return state


def window_stability(state: dict, site_id: str, window: Window) -> int:
    """
    Count how many of the prior (not current) runs ALSO showed this window's
    start date as having p_campable >= ORGANIZE_PROB_MIN. Used to decide
    whether to fire the day-4 COMMIT alert.
    """
    prior_runs = state["runs"][:-1]  # exclude current run
    count = 0
    for run in prior_runs:
        site_scores = run["sites"].get(site_id, {})
        if site_scores.get(window.start, 0) >= ORGANIZE_PROB_MIN:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Alert decision
# ---------------------------------------------------------------------------


def classify_window(window: Window, stability: int) -> str | None:
    """
    Return alert level for this window, or None if no alert.

    - "commit"   (days 1-4): high confidence + has held in prior runs
    - "organize" (days 5-7): moderate confidence, start preparing
    - "watch"   (days 8-14): early signal, keep an eye on it
    """
    d = window.days_ahead
    p = window.joint_prob

    if d <= 4 and p >= COMMIT_PROB_MIN and stability >= COMMIT_STABILITY_RUNS:
        return "commit"
    if 5 <= d <= 7 and p >= ORGANIZE_PROB_MIN:
        return "organize"
    if 8 <= d <= 14 and p >= WATCH_PROB_MIN:
        return "watch"
    # Edge cases: high prob inside day 1-4 but no stability history yet
    if d <= 4 and p >= ORGANIZE_PROB_MIN:
        return "organize"
    return None


def best_alert_per_site(
    windows: list[Window],
    state: dict,
    site_id: str,
) -> tuple[Window, str, int] | None:
    """Pick the most urgent actionable window for this site."""
    candidates = []
    for w in windows:
        stability = window_stability(state, site_id, w)
        level = classify_window(w, stability)
        if level:
            candidates.append((w, level, stability))

    if not candidates:
        return None

    # Priority: commit > organize > watch; then earliest start
    level_rank = {"commit": 0, "organize": 1, "watch": 2}
    candidates.sort(key=lambda c: (level_rank[c[1]], c[0].days_ahead))
    return candidates[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> int:
    today = date.today()
    run_date = today.isoformat()
    print(f"=== Camping forecast run: {run_date} ===")

    state = load_state()

    per_site_scores: dict[str, list[DayScore]] = {}
    per_site_windows: dict[str, list[Window]] = {}

    for site_id, cfg in SITES.items():
        print(f"\nFetching {cfg['name']} ({cfg['lat']:.4f}, {cfg['lon']:.4f})...")
        try:
            ens = fetch_ensemble(cfg["lat"], cfg["lon"], forecast_days=15)
        except Exception as e:
            print(f"  ERROR fetching {site_id}: {e}", file=sys.stderr)
            continue

        per_day = aggregate_to_days(ens)
        scores = score_days(per_day)
        windows = find_windows(per_day, scores, today)

        per_site_scores[site_id] = scores
        per_site_windows[site_id] = windows

        print(f"  {len(scores)} days scored, {len(windows)} candidate window(s)")
        for w in windows:
            print(f"    Window {w.start}→{w.end} ({w.nights}n, "
                  f"in {w.days_ahead}d, joint P={w.joint_prob:.0%})")

    if not per_site_scores:
        print("No site data fetched — aborting.")
        return 1

    # Update state BEFORE computing stability, so today's run is included.
    # We save at the end so the consecutive-skips counter is included too.
    state = update_state(state, run_date, per_site_scores)

    # Decide alert per site
    alerts: dict[str, tuple[Window, str, int]] = {}
    for site_id, windows in per_site_windows.items():
        result = best_alert_per_site(windows, state, site_id)
        if result:
            alerts[site_id] = result
            w, level, stab = result
            print(f"\n{site_id} → {level.upper()} | "
                  f"{w.start} ({w.nights}n) | P={w.joint_prob:.0%} | "
                  f"held in {stab} prior runs")

    # Decide whether to email — heartbeat forces a send after too many silent days
    prior_skips = state.get("consecutive_skips", 0)
    heartbeat = prior_skips >= MAX_CONSECUTIVE_SKIPS
    should_email = bool(alerts) or _send_status_digest_today() or heartbeat

    if not should_email:
        state["consecutive_skips"] = prior_skips + 1
        save_state(state)
        remaining = MAX_CONSECUTIVE_SKIPS - state["consecutive_skips"]
        if remaining > 0:
            tail = f"{remaining} more silent day(s) before a heartbeat email fires."
        else:
            tail = "next silent run will trigger a heartbeat email."
        print(f"\nNo alerts and no status digest due — skipping email "
              f"(silent day {state['consecutive_skips']}/{MAX_CONSECUTIVE_SKIPS}; "
              f"{tail})")
        return 0

    # We're going to send — reset the skip counter, then save.
    if heartbeat and not alerts and not _send_status_digest_today():
        print(f"\nHeartbeat: {prior_skips} silent days in a row — "
              f"sending status check-in even though nothing is alert-worthy.")
    state["consecutive_skips"] = 0
    save_state(state)

    html, subject = render_email(
        sites=SITES,
        scores_by_site=per_site_scores,
        windows_by_site=per_site_windows,
        alerts=alerts,
        run_date=run_date,
        state=state,
    )

    to_addr = os.environ.get("ALERT_EMAIL")
    cc_addr = os.environ.get("CC_EMAIL") or None
    if not to_addr:
        print("\nALERT_EMAIL not set — printing email to stdout instead.")
        print(f"\nSubject: {subject}\n\n{html}")
        return 0

    send_email(to_addr, subject, html, cc_addr=cc_addr)
    print(f"\nEmail sent to {to_addr}{f' (cc: {cc_addr})' if cc_addr else ''}: {subject}")
    return 0


def _send_status_digest_today() -> bool:
    """Send a status digest on Mondays even if no alerts — keeps the user oriented."""
    return date.today().weekday() == 0


if __name__ == "__main__":
    sys.exit(run())
