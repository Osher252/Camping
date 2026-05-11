"""
Email rendering + Resend sender.

Produces an HTML email with:
- Headline alert (commit / organize / watch / status)
- Side-by-side comparison of both sites
- 14-day campability strip per site
- Window detail with median day-max, overnight-min, precip
- Stability note: how this window has tracked across recent runs
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, datetime
from typing import Optional

BRAND_EMOJI = "🏕️"
FLOURISH = "⋆｡𖦹°✩"

LEVEL_META = {
    "commit": {
        "emoji": "🟢",
        "headline": "BOOK IT",
        "color": "#0a7a3f",
        "subtitle": "High confidence — book within 24h",
    },
    "organize": {
        "emoji": "🟡",
        "headline": "ORGANIZE",
        "color": "#b8860b",
        "subtitle": "Moderate confidence — start prepping, don't book yet",
    },
    "watch": {
        "emoji": "🔵",
        "headline": "WATCHING",
        "color": "#1e6ba8",
        "subtitle": "Early signal — too soon to act, just be aware",
    },
    "none": {
        "emoji": "⚪",
        "headline": "NO WINDOWS",
        "color": "#888",
        "subtitle": "Nothing camp-worthy in the next 14 days",
    },
}


def render_email(
    sites: dict,
    scores_by_site: dict[str, list],
    windows_by_site: dict[str, list],
    alerts: dict[str, tuple],
    run_date: str,
    state: dict,
) -> tuple[str, str]:
    """Return (html, subject) for the daily email."""

    # Determine top-level alert level (most urgent across both sites)
    best_level = "none"
    best_site_id = None
    best_window = None
    best_stability = 0
    level_rank = {"commit": 0, "organize": 1, "watch": 2, "none": 3}
    for site_id, (window, level, stab) in alerts.items():
        if level_rank[level] < level_rank[best_level]:
            best_level = level
            best_site_id = site_id
            best_window = window
            best_stability = stab

    meta = LEVEL_META[best_level]
    if best_window:
        subject = (
            f"{BRAND_EMOJI} {meta['emoji']} {meta['headline']}: "
            f"{sites[best_site_id]['name']} {_fmt_window_dates(best_window)} "
            f"({best_window.joint_prob:.0%}) {FLOURISH}"
        )
    else:
        subject = (
            f"{BRAND_EMOJI} Camping forecast {run_date}: "
            f"no windows yet {FLOURISH}"
        )

    # Build site sections
    site_sections = "\n".join(
        _render_site_section(
            site_id=sid,
            site=sites[sid],
            scores=scores_by_site.get(sid, []),
            windows=windows_by_site.get(sid, []),
            alert=alerts.get(sid),
            state=state,
        )
        for sid in sites
    )

    today = date.fromisoformat(run_date)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          color: #222; max-width: 640px; margin: 0 auto; padding: 16px; }}
  .headline {{ background: {meta['color']}; color: white; padding: 20px;
              border-radius: 8px; margin-bottom: 24px; }}
  .headline h1 {{ margin: 0; font-size: 22px; }}
  .headline p {{ margin: 6px 0 0; opacity: 0.95; }}
  .site {{ border: 1px solid #e0e0e0; border-radius: 8px;
          padding: 16px; margin-bottom: 20px; }}
  .site h2 {{ margin: 0 0 4px; font-size: 18px; }}
  .site .region {{ color: #666; font-size: 13px; margin-bottom: 14px; }}
  .strip {{ display: flex; gap: 2px; margin: 12px 0; }}
  .day {{ flex: 1; text-align: center; padding: 6px 2px; border-radius: 3px;
         font-size: 10px; color: #fff; min-width: 28px; }}
  .day .dow {{ font-weight: bold; }}
  .day .pct {{ font-size: 9px; opacity: 0.9; }}
  .window {{ background: #f5f8fa; padding: 10px 12px; border-radius: 6px;
            margin-top: 10px; font-size: 14px; }}
  .stability {{ color: #666; font-size: 12px; margin-top: 4px; }}
  .footer {{ color: #888; font-size: 11px; margin-top: 24px; text-align: center; }}
  table.detail {{ border-collapse: collapse; font-size: 12px; margin-top: 8px; width: 100%; }}
  table.detail td {{ padding: 3px 8px; }}
  table.detail td.label {{ color: #666; }}
</style>
</head>
<body>

<div class="headline">
  <h1>{meta['emoji']} {meta['headline']}{_format_headline_window(best_window, best_site_id, sites)}</h1>
  <p>{meta['subtitle']}</p>
</div>

{site_sections}

<div class="footer">
  Run {run_date} · ECMWF IFS 51-member ensemble (Open-Meteo) ·
  Thresholds: day max ≥{17}°C, overnight min ≥{11}°C, precip &lt;{2}mm ·
  Min 2 consecutive nights
</div>

</body>
</html>"""

    return html, subject


def _format_headline_window(window, site_id, sites) -> str:
    if not window:
        return ""
    return (
        f": {sites[site_id]['name']} · "
        f"{_fmt_window_dates(window)} ({window.nights}n) · "
        f"{window.joint_prob:.0%} confidence"
    )


def _fmt_window_dates(window) -> str:
    """Format e.g. '2026-05-18→2026-05-20' as 'Mon 18 → Wed 20 May'."""
    start = date.fromisoformat(window.start)
    end = date.fromisoformat(window.end)
    if start.month == end.month:
        return f"{start.strftime('%a %d')} → {end.strftime('%a %d %b')}"
    return f"{start.strftime('%a %d %b')} → {end.strftime('%a %d %b')}"


def _render_site_section(
    site_id: str,
    site: dict,
    scores: list,
    windows: list,
    alert: Optional[tuple],
    state: dict,
) -> str:
    strip_html = _render_strip(scores)

    if alert:
        window, level, stability = alert
        meta = LEVEL_META[level]
        win_block = f"""
<div class="window" style="border-left: 4px solid {meta['color']};">
  <strong>{meta['emoji']} {meta['headline']}: {_fmt_window_dates(window)}</strong>
  &nbsp;·&nbsp; {window.nights} night{'s' if window.nights > 1 else ''}
  &nbsp;·&nbsp; <strong>{window.joint_prob:.0%}</strong> joint probability
  &nbsp;·&nbsp; in {window.days_ahead} day{'s' if window.days_ahead != 1 else ''}
  {_render_window_detail(window, scores)}
  <div class="stability">{_render_stability(stability, state, site_id, window)}</div>
</div>"""
    elif windows:
        # We have windows but none triggered an alert — show the best one as info
        best = max(windows, key=lambda w: w.joint_prob)
        win_block = f"""
<div class="window">
  <strong>Best candidate: {_fmt_window_dates(best)}</strong>
  &nbsp;·&nbsp; {best.nights}n &nbsp;·&nbsp; {best.joint_prob:.0%}
  &nbsp;·&nbsp; in {best.days_ahead}d
  <div class="stability">Below alert threshold — keep watching.</div>
</div>"""
    else:
        win_block = """
<div class="window" style="color: #888;">
  No multi-night windows meeting thresholds in the next 14 days.
</div>"""

    return f"""
<div class="site">
  <h2>{site['name']}</h2>
  <div class="region">{site['region']}</div>
  {strip_html}
  {win_block}
</div>"""


def _render_strip(scores: list) -> str:
    """Render the 14-day mini bar strip — one cell per day, coloured by probability."""
    cells = []
    for s in scores[:14]:
        d = date.fromisoformat(s.date)
        p = s.p_campable
        color = _prob_color(p)
        cells.append(
            f'<div class="day" style="background:{color};">'
            f'<div class="dow">{d.strftime("%a")}</div>'
            f'<div>{d.day}</div>'
            f'<div class="pct">{int(p*100)}%</div>'
            f'</div>'
        )
    return f'<div class="strip">{"".join(cells)}</div>'


def _prob_color(p: float) -> str:
    """Gradient from grey (low) to green (high) for campability probability."""
    if p < 0.10:
        return "#b0b0b0"
    if p < 0.30:
        return "#7a8a99"
    if p < 0.50:
        return "#5b8aa9"
    if p < 0.70:
        return "#5fa28f"
    return "#0a7a3f"


def _render_window_detail(window, scores: list) -> str:
    """Median day max / overnight min / precip across the window days."""
    score_by_date = {s.date: s for s in scores}
    from datetime import timedelta
    cur = date.fromisoformat(window.start)
    end = date.fromisoformat(window.end)
    day_maxes = []
    overnights = []
    precips = []
    while cur <= end:
        s = score_by_date.get(cur.isoformat())
        if s:
            day_maxes.append(s.p50_day_max)
            overnights.append(s.p50_overnight_min)
            precips.append(s.p50_precip)
        cur += timedelta(days=1)

    if not day_maxes:
        return ""

    return f"""
<table class="detail">
  <tr><td class="label">Day max (median)</td>
      <td>{min(day_maxes):.0f}–{max(day_maxes):.0f}°C</td></tr>
  <tr><td class="label">Overnight min (median)</td>
      <td>{min(overnights):.0f}–{max(overnights):.0f}°C</td></tr>
  <tr><td class="label">Daily precip (median)</td>
      <td>{sum(precips):.1f}mm total</td></tr>
</table>"""


def _render_stability(stability: int, state: dict, site_id: str, window) -> str:
    """Describe how this window has tracked across recent runs."""
    prior_runs = state["runs"][:-1] if len(state["runs"]) > 1 else []
    if not prior_runs:
        return "First run — no stability history yet."

    history_bits = []
    for run in prior_runs[-3:]:
        site_data = run.get("sites", {}).get(site_id, {})
        p = site_data.get(window.start)
        if p is None:
            history_bits.append(f"{run['run_date'][-5:]}: —")
        else:
            history_bits.append(f"{run['run_date'][-5:]}: {int(p*100)}%")

    label = f"Held in {stability} of last {len(prior_runs)} runs" if stability else \
            "New window — hasn't appeared in prior runs"
    return f"{label} &nbsp;|&nbsp; History: {' · '.join(history_bits)}"


# ---------------------------------------------------------------------------
# Resend sender
# ---------------------------------------------------------------------------


def send_email(to_addr: str, subject: str, html: str, cc_addr: Optional[str] = None) -> None:
    """Send via Resend API. Requires RESEND_API_KEY and FROM_EMAIL env vars."""
    api_key = os.environ.get("RESEND_API_KEY")
    from_addr = os.environ.get("FROM_EMAIL", "Camping Forecast <onboarding@resend.dev>")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY not set")

    body = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": html,
    }
    if cc_addr:
        body["cc"] = [a.strip() for a in cc_addr.split(",") if a.strip()]
    payload = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        if "id" not in result:
            raise RuntimeError(f"Resend error: {result}")
