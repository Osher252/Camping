"""
Open-Meteo ECMWF ensemble API client.

Endpoint docs: https://open-meteo.com/en/docs/ensemble-api
- Free, no API key required
- ECMWF IFS ensemble: 51 members
- Hourly resolution, up to 15 forecast days
- Returns per-member time series with _memberNN suffix

Response shape (relevant subset):
{
  "latitude": ..., "longitude": ...,
  "hourly": {
    "time": ["2026-05-11T00:00", ...],
    "temperature_2m_member01": [...],
    "temperature_2m_member02": [...],
    ...
    "precipitation_member01": [...],
    ...
  }
}
"""

from __future__ import annotations

import re
import urllib.request
import urllib.parse
import json
from dataclasses import dataclass
from typing import Optional

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# ECMWF IFS at 0.25° — 51-member ensemble, good for UK
DEFAULT_MODEL = "ecmwf_ifs025"


@dataclass
class EnsembleData:
    """Parsed ensemble response."""
    times: list[str]                              # ISO timestamps
    temperature_by_member: list[list[Optional[float]]]   # [member][hour]
    precipitation_by_member: list[list[Optional[float]]] # [member][hour]
    latitude: float
    longitude: float
    elevation: float
    timezone: str


def fetch_ensemble(
    lat: float,
    lon: float,
    forecast_days: int = 15,
    model: str = DEFAULT_MODEL,
    timeout: int = 30,
) -> EnsembleData:
    """Fetch ECMWF ensemble forecast for a point and parse into structured form."""
    params = {
        "latitude": f"{lat}",
        "longitude": f"{lon}",
        "hourly": "temperature_2m,precipitation",
        "models": model,
        "forecast_days": str(forecast_days),
        "timezone": "Europe/London",
    }
    url = f"{ENSEMBLE_URL}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": "camping-forecast/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    return _parse_response(data)


def _parse_response(data: dict) -> EnsembleData:
    hourly = data.get("hourly", {})
    if not hourly:
        raise RuntimeError(f"No hourly data in response: {data}")

    times = hourly.get("time", [])
    if not times:
        raise RuntimeError("Empty time array in response")

    # Collect per-member series for each variable
    temp_members: dict[int, list] = {}
    prec_members: dict[int, list] = {}

    temp_member_re = re.compile(r"^temperature_2m_member(\d+)$")
    prec_member_re = re.compile(r"^precipitation_member(\d+)$")
    # Also accept the "control" run (no suffix) as member 0
    for key, series in hourly.items():
        if key == "temperature_2m":
            temp_members[0] = series
        elif key == "precipitation":
            prec_members[0] = series
        elif m := temp_member_re.match(key):
            temp_members[int(m.group(1))] = series
        elif m := prec_member_re.match(key):
            prec_members[int(m.group(1))] = series

    if not temp_members or not prec_members:
        raise RuntimeError(
            f"Missing per-member fields. Got keys: {list(hourly.keys())[:10]}..."
        )

    # Sort by member index, return as aligned lists
    member_ids = sorted(set(temp_members) & set(prec_members))
    temp_by_member = [temp_members[m] for m in member_ids]
    prec_by_member = [prec_members[m] for m in member_ids]

    return EnsembleData(
        times=times,
        temperature_by_member=temp_by_member,
        precipitation_by_member=prec_by_member,
        latitude=data.get("latitude", 0.0),
        longitude=data.get("longitude", 0.0),
        elevation=data.get("elevation", 0.0),
        timezone=data.get("timezone", "UTC"),
    )
