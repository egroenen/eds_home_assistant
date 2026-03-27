"""MetOcean API (MetService NZ) weather forecast client."""

import json
import logging
import urllib.request
from datetime import datetime, timedelta

from . import config
from .config import METOCEAN_API_URL, METOCEAN_LAT, METOCEAN_LON, PEAK_START_HOUR, PEAK_END_HOUR

log = logging.getLogger("solar_optimizer")


def get_metocean_hourly(target_date):
    """Fetch hourly forecast from MetOcean API for peak hours on target_date.

    Returns list of dicts with hour, condition, cloud_coverage, precipitation,
    temperature — same format as HomeAssistantAPI.get_hourly_forecast().
    Falls back to empty list on any error.
    """
    try:
        # NZ is UTC+12 (NZST) or UTC+13 (NZDT during DST)
        prev_day = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        from_utc = f"{prev_day}T17:00:00Z"

        body = json.dumps({
            "points": [{"lat": METOCEAN_LAT, "lon": METOCEAN_LON}],
            "variables": [
                "air.temperature.at-2m",
                "cloud.cover",
                "precipitation.rate",
                "radiation.flux.downward.shortwave",
            ],
            "time": {
                "from": from_utc,
                "interval": "1h",
                "repeat": 36,
            },
        }).encode()

        # Read API key at call time (set by load_env after import)
        api_key = config.METOCEAN_API_KEY

        req = urllib.request.Request(
            METOCEAN_API_URL,
            data=body,
            method="POST",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        times = data["dimensions"]["time"]["data"]
        temps_k = data["variables"]["air.temperature.at-2m"]["data"]
        clouds = data["variables"]["cloud.cover"]["data"]
        precips = data["variables"]["precipitation.rate"]["data"]
        shortwave = data["variables"].get("radiation.flux.downward.shortwave", {}).get("data", [])

        nz_offset = timedelta(hours=13)  # NZDT

        peak_hours = []
        for i, t_str in enumerate(times):
            utc_dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            local_dt = utc_dt + nz_offset
            local_date = local_dt.strftime("%Y-%m-%d")
            local_hour = local_dt.hour

            if local_date != target_date:
                continue
            if not (PEAK_START_HOUR <= local_hour < PEAK_END_HOUR):
                continue

            temp_c = temps_k[i] - 273.15
            cloud_pct = clouds[i]
            precip_mm = precips[i]

            if precip_mm > 0.5:
                condition = "rainy"
            elif cloud_pct >= 80:
                condition = "cloudy"
            elif cloud_pct >= 40:
                condition = "partlycloudy"
            else:
                condition = "sunny"

            sw_wm2 = shortwave[i] if i < len(shortwave) else None

            peak_hours.append({
                "hour": local_hour,
                "condition": condition,
                "cloud_coverage": cloud_pct,
                "precipitation": precip_mm,
                "temperature": round(temp_c, 1),
                "shortwave_wm2": round(sw_wm2, 1) if sw_wm2 is not None else None,
            })

        log.info(f"MetOcean: got {len(peak_hours)} peak hours for {target_date}")
        return peak_hours

    except Exception as e:
        log.warning(f"MetOcean API failed, will fall back to HA forecast: {e}")
        return []
