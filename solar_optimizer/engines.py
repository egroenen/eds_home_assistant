"""Pluggable solar forecast engines."""

from .models import (
    build_hourly_solar,
    build_hourly_solar_radiation,
    get_sw_efficiency_map,
)


ENGINE_DESCRIPTIONS = {
    "cloud": "Cloud-corrected Forecast.Solar weights.",
    "radiation": "Direct shortwave-radiation conversion with learned efficiency.",
    "blended": "Average of cloud and radiation where both are available.",
    "capped_radiation": "Radiation forecast capped by a multiple of the cloud model.",
}


def list_engines():
    return [
        {"name": name, "description": ENGINE_DESCRIPTIONS[name]}
        for name in sorted(ENGINE_DESCRIPTIONS)
    ]


def _merge_hourly_maps(*maps):
    hours = sorted({hour for mapping in maps for hour in mapping})
    return hours


def build_engine_hourly_solar(raw_solar, hourly_forecast, params, target_date, engine_name):
    """Build hourly solar for a named engine with diagnostics."""
    solar_cloud = build_hourly_solar(
        raw_solar, hourly_forecast, params, target_date=target_date
    )
    sw_eff_map = get_sw_efficiency_map(params, target_date=target_date)
    solar_rad = build_hourly_solar_radiation(
        raw_solar, hourly_forecast, sw_eff_map, target_date=target_date
    ) or {}

    if engine_name == "cloud":
        active = dict(solar_cloud)
    elif engine_name == "radiation":
        active = dict(solar_rad) if sum(solar_rad.values()) > 0 else dict(solar_cloud)
    elif engine_name == "blended":
        active = {}
        for hour in _merge_hourly_maps(solar_cloud, solar_rad):
            cloud_val = solar_cloud.get(hour)
            rad_val = solar_rad.get(hour)
            if cloud_val is None:
                active[hour] = rad_val
            elif rad_val is None:
                active[hour] = cloud_val
            else:
                active[hour] = (cloud_val + rad_val) / 2.0
    elif engine_name == "capped_radiation":
        active = {}
        for hour in _merge_hourly_maps(solar_cloud, solar_rad):
            cloud_val = solar_cloud.get(hour, 0.0)
            rad_val = solar_rad.get(hour, cloud_val)
            active[hour] = min(rad_val, cloud_val * 1.5) if cloud_val > 0 else rad_val
    else:
        raise ValueError(f"Unknown engine: {engine_name}")

    return {
        "engine": engine_name,
        "hourly_solar": active,
        "cloud_hourly": solar_cloud,
        "radiation_hourly": solar_rad,
        "cloud_total": sum(solar_cloud.values()),
        "radiation_total": sum(solar_rad.values()),
        "active_total": sum(active.values()),
    }
