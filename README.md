# Camping forecast — Penn Meadow / WOWO

Daily-emailed family camping window finder. Pulls the ECMWF 51-member
ensemble forecast for two campsites near London, identifies multi-night
windows that meet child-friendly comfort thresholds, tracks how those
windows evolve across daily runs, and emails an alert calibrated to your
booking timeline.

## What it does

Each morning at 06:00 UTC, GitHub Actions runs `main.py`, which:

1. Fetches a 15-day, 51-member ECMWF ensemble forecast for both sites from
   the free Open-Meteo API (no API key).
2. Aggregates hourly per-member data into per-day summaries
   (day max, overnight min, total precip).
3. Computes **P(campable)** for each day = fraction of ensemble members
   where the day meets all comfort thresholds.
4. Finds **windows**: consecutive runs of ≥2 days with non-trivial
   probability, scored by **joint probability** (fraction of members
   campable on *all* days of the window — properly handles weather
   autocorrelation, unlike naive multiplication).
5. Tracks each window's history across the last 7 daily runs to measure
   forecast **stability**.
6. Classifies the best window into an alert level and emails you.

## Alert cascade

| Days ahead | Alert     | Triggers when                                  | What to do                              |
|-----------:|-----------|------------------------------------------------|-----------------------------------------|
| 8–14       | 🔵 WATCH  | P ≥ 30%                                        | Notice it. Don't plan around it yet.    |
| 5–7        | 🟡 ORGANIZE | P ≥ 50%                                      | Sort gear, talk to the family, hold dates. |
| 1–4        | 🟢 COMMIT | P ≥ 70% **and** held in ≥2 prior runs          | Book the site. |

A window must have appeared in the forecast for at least two consecutive
prior days before the system suggests committing — this prevents acting
on a freshly-appeared window that may evaporate tomorrow.

A status digest is emailed on Mondays regardless, so you never go a full
week wondering if it's still running.

## Comfort thresholds (the opinionated bit)

Edit these in `main.py` if you disagree:

```python
DAY_MAX_MIN_C = 17.0          # daytime warmth
OVERNIGHT_MIN_MIN_C = 11.0    # comfort by the fire, sleeping kids
PRECIP_MAX_MM = 2.0           # daily total
MIN_WINDOW_NIGHTS = 2
```

8°C overnight is a "tough it out" number for adults; 11°C is the threshold
where small kids stay comfortable by a fire in fleeces.

## Setup (about 20 minutes)

### 1. Fork or create the repo

Put these files in a new GitHub repo (private is fine).

### 2. Get a Resend API key (free tier: 100 emails/day)

1. Sign up at [resend.com](https://resend.com).
2. Verify a sending domain, OR use their `onboarding@resend.dev` sender
   for testing (your `FROM_EMAIL` would be omitted).
3. Create an API key in the Resend dashboard.

### 3. Add GitHub Actions secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret           | Value                                                |
|------------------|------------------------------------------------------|
| `ALERT_EMAIL`    | Your email address                                   |
| `RESEND_API_KEY` | The API key from Resend                              |
| `FROM_EMAIL`     | e.g. `Camping <forecast@yourdomain.com>` (optional)  |

### 4. Run it once manually

**Actions → Daily camping forecast → Run workflow** to verify it works
before waiting for the cron. The first run will have no stability
history (so no COMMIT alerts possible yet), but should still send an
email if a window is forming.

### 5. Done

The workflow runs at 06:00 UTC daily. State is committed back to the repo
in `state.json` so stability tracking persists across runs.

## Customizing sites

Edit `SITES` in `main.py`. Coordinates can be grabbed from Google Maps
(right-click → "What's here?"). The system handles any UK location.

## Running locally

```bash
python3 main.py            # uses live API, prints email to stdout if no ALERT_EMAIL
python3 test_local.py      # synthetic data, writes sample email to /tmp/sample_email.html
```

## What you're trusting

- **Open-Meteo's free tier** doesn't guarantee uptime. If it goes down,
  you'll see a workflow failure in the Actions tab. No data on those days.
- **ECMWF IFS at 0.25°** is one of the world's best medium-range models
  but it's still a forecast. Day-7 windows realize ~50–65% of the time;
  day-14 windows are mostly noise. The cascade is designed around this.
- **The thresholds are mine**, not yours. Tune them after a couple of
  trips when you learn what your kids actually tolerate.

## How to read the email

- Top banner = most urgent alert across both sites.
- **14-day strip**: one cell per day, coloured grey (no chance) → green
  (high chance). Number in cell = P(campable) for that day.
- **Window block** below each site: best window, joint probability, and
  how it's tracked across recent runs.

## Limitations / honest caveats

- ECMWF ensemble only goes to 15 days. The "day 14 watch" alert is on the
  ragged edge of forecast skill.
- Overnight min at the campsite can run 1–3°C colder than the nearest
  grid point in a clear-sky cold pool (Penn Meadow is mildly elevated;
  WOWO sits in a damp Sussex valley). The thresholds bake in some buffer
  but don't trust them blindly — if it's borderline, look at the Met
  Office app for the morning of departure.
- No wind / cloud / dew point in the scoring. A windy day at 18°C feels
  colder than a still day at 15°C. Add `wind_speed_10m` and a wind
  threshold to `fetch.py` if you want this.
