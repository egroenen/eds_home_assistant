"""Home Assistant API client."""

import json
import logging
import urllib.error
import urllib.request
from datetime import date, timedelta

from .config import SENSORS, PEAK_START_HOUR, PEAK_END_HOUR

log = logging.getLogger("solar_optimizer")


class HomeAssistantAPI:
    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token

    def _request(self, method, path, data=None):
        url = f"{self.server}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            log.error(f"HA API error {e.code}: {e.read().decode()}")
            raise
        except urllib.error.URLError as e:
            log.error(f"HA API connection error: {e}")
            raise

    def get_state(self, entity_id):
        return self._request("GET", f"/api/states/{entity_id}")

    def get_sensor_float(self, entity_id):
        state = self.get_state(entity_id)
        val = state.get("state", "")
        if val in ("unavailable", "unknown", ""):
            log.warning(f"Sensor {entity_id} is {val}")
            return None
        try:
            return float(val)
        except ValueError:
            log.warning(f"Sensor {entity_id} has non-numeric state: {val}")
            return None

    def get_weather(self):
        state = self.get_state(SENSORS["weather"])
        attrs = state.get("attributes", {})
        return {
            "condition": state.get("state", "unknown"),
            "cloud_coverage": attrs.get("cloud_coverage", 50),
            "temperature": attrs.get("temperature"),
            "humidity": attrs.get("humidity"),
        }

    def get_weather_forecast(self):
        """Get daily weather forecast via HA service call."""
        try:
            result = self._request("POST", "/api/services/weather/get_forecasts?return_response", {
                "entity_id": SENSORS["weather"],
                "type": "daily",
            })
            if isinstance(result, dict):
                sr = result.get("service_response", result)
                entity_data = sr.get(SENSORS["weather"], sr)
                if isinstance(entity_data, dict):
                    forecasts = entity_data.get("forecast", [])
                elif isinstance(entity_data, list):
                    forecasts = entity_data
                else:
                    forecasts = []
            else:
                forecasts = []

            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            today_str = date.today().isoformat()
            for fc in forecasts:
                fc_date = fc.get("datetime", "")[:10]
                if fc_date == tomorrow or fc_date == today_str:
                    pass
            if len(forecasts) >= 2:
                return forecasts[1]
            elif forecasts:
                return forecasts[0]
        except Exception as e:
            log.warning(f"Could not get weather forecast: {e}")
        return {}

    def get_hourly_forecast(self, target_date=None):
        """Get hourly weather forecast for a specific date."""
        try:
            result = self._request("POST", "/api/services/weather/get_forecasts?return_response", {
                "entity_id": SENSORS["weather"],
                "type": "hourly",
            })
            sr = result.get("service_response", result)
            entity_data = sr.get(SENSORS["weather"], sr)
            if isinstance(entity_data, dict):
                forecasts = entity_data.get("forecast", [])
            elif isinstance(entity_data, list):
                forecasts = entity_data
            else:
                forecasts = []

            if target_date is None:
                target_date = date.today().isoformat()

            peak_hours = []
            for fc in forecasts:
                fc_dt = fc.get("datetime", "")
                if not fc_dt[:10] == target_date:
                    continue
                try:
                    hour = int(fc_dt[11:13])
                except (ValueError, IndexError):
                    continue
                if PEAK_START_HOUR <= hour < PEAK_END_HOUR:
                    peak_hours.append({
                        "hour": hour,
                        "condition": fc.get("condition", "cloudy").lower(),
                        "cloud_coverage": fc.get("cloud_coverage", 50),
                        "precipitation": fc.get("precipitation", 0) or 0,
                        "temperature": fc.get("temperature"),
                    })
            return peak_hours
        except Exception as e:
            log.warning(f"Could not get hourly forecast: {e}")
            return []

    def write_register(self, register, value):
        """Write a single holding register via Solarman (using write_multiple)."""
        log.info(f"Writing register {register} = {value}")
        self._request("POST", "/api/services/solarman/write_multiple_holding_registers", {
            "register": register,
            "values": [int(value)],
        })

    def call_service(self, domain, service, data=None):
        return self._request("POST", f"/api/services/{domain}/{service}", data or {})
