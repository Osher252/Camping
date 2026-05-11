"""
Smoke test: feeds synthetic ensemble data through aggregation, scoring,
window detection, and email rendering — no network required.

Run:  python test_local.py

Confirms the pipeline produces sensible output before deploying.
"""

import random
from datetime import date, datetime, timedelta

from fetch import EnsembleData
from main import (
    aggregate_to_days, score_days, find_windows,
    update_state, best_alert_per_site,
    SITES,
)
from email_template import render_email


def synth_ensemble(days: int = 15, seed: int = 42) -> EnsembleData:
    """
    Generate a fake 15-day, 51-member ensemble that includes:
    - days 1-3: cold/wet (rules out commit alert)
    - days 4-6: borderline
    - days 7-10: warm window with high agreement (organize/watch alert)
    - days 11-15: cooling off
    """
    rng = random.Random(seed)
    start = datetime(2026, 5, 11, 0, 0)
    n_hours = days * 24
    times = [(start + timedelta(hours=h)).isoformat(timespec="minutes")
             for h in range(n_hours)]

    n_members = 51
    temp_by_member = []
    prec_by_member = []

    for m in range(n_members):
        # Per-member random offset
        m_offset = rng.gauss(0, 0.8)
        t_series = []
        p_series = []
        for h in range(n_hours):
            day = h // 24
            hour_of_day = h % 24
            # Diurnal cycle: cool overnight, warm afternoon
            diurnal = -5 * (1 if 0 <= hour_of_day < 6 else 0) + \
                      8 * (1 if 12 <= hour_of_day < 17 else 0)
            # Base trend across days
            if day < 3:
                base = 10  # cold start
                rain_p = 0.6
            elif day < 6:
                base = 15  # warming
                rain_p = 0.3
            elif day < 10:
                base = 19  # warm window
                rain_p = 0.05
            else:
                base = 14  # cooling
                rain_p = 0.4

            t = base + diurnal + m_offset + rng.gauss(0, 1.2)
            t_series.append(round(t, 1))
            p = rng.expovariate(1 / max(rain_p, 0.01)) if rng.random() < rain_p else 0.0
            p_series.append(round(p, 2))

        temp_by_member.append(t_series)
        prec_by_member.append(p_series)

    return EnsembleData(
        times=times,
        temperature_by_member=temp_by_member,
        precipitation_by_member=prec_by_member,
        latitude=51.63,
        longitude=-0.66,
        elevation=150.0,
        timezone="Europe/London",
    )


def main():
    print("=== Synthetic data smoke test ===\n")
    today = date(2026, 5, 11)

    # Pretend we have one prior run that scored the upcoming window similarly
    # (so stability check has something to work with)
    prior_state = {
        "runs": [
            {
                "run_date": "2026-05-09",
                "sites": {
                    "penn_meadow": {
                        "2026-05-17": 0.55,
                        "2026-05-18": 0.62,
                        "2026-05-19": 0.58,
                    },
                    "wowo": {
                        "2026-05-17": 0.65,
                        "2026-05-18": 0.70,
                        "2026-05-19": 0.68,
                    },
                },
            },
            {
                "run_date": "2026-05-10",
                "sites": {
                    "penn_meadow": {
                        "2026-05-17": 0.60,
                        "2026-05-18": 0.65,
                    },
                    "wowo": {
                        "2026-05-17": 0.72,
                        "2026-05-18": 0.78,
                    },
                },
            },
        ]
    }

    per_site_scores = {}
    per_site_windows = {}
    for site_id in SITES:
        ens = synth_ensemble(seed=hash(site_id) % 1000)
        per_day = aggregate_to_days(ens)
        scores = score_days(per_day)
        # Override today for deterministic test
        windows = find_windows(per_day, scores, today)
        per_site_scores[site_id] = scores
        per_site_windows[site_id] = windows
        print(f"{site_id}: {len(scores)} days, {len(windows)} windows")
        for w in windows:
            print(f"  {w.start}→{w.end} ({w.nights}n) in {w.days_ahead}d, "
                  f"joint P={w.joint_prob:.0%}")

    state = update_state(prior_state, today.isoformat(), per_site_scores)

    alerts = {}
    for site_id, windows in per_site_windows.items():
        result = best_alert_per_site(windows, state, site_id)
        if result:
            alerts[site_id] = result
            w, level, stab = result
            print(f"\n{site_id} alert: {level.upper()} "
                  f"({w.start}, P={w.joint_prob:.0%}, stab={stab})")
        else:
            print(f"\n{site_id} alert: none")

    html, subject = render_email(
        sites=SITES,
        scores_by_site=per_site_scores,
        windows_by_site=per_site_windows,
        alerts=alerts,
        run_date=today.isoformat(),
        state=state,
    )

    with open("/tmp/sample_email.html", "w") as f:
        f.write(html)
    print(f"\nSubject: {subject}")
    print(f"HTML written to /tmp/sample_email.html ({len(html)} chars)")


if __name__ == "__main__":
    main()
