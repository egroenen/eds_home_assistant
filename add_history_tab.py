#!/usr/bin/env python3
"""Create/update the Solar History view on the Home dashboard."""
import json

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_home"

with open(LOVELACE_PATH) as f:
    config = json.load(f)

# Weather emoji map for compact display
WX_ICONS = "{% set wi = {'sunny':'☀️','cloudy':'☁️','partlycloudy':'⛅','rainy':'🌧️'} %}\n"

history_content = (
    "{% set days = state_attr('sensor.solar_optimizer_history', 'days') %}\n"
    "{% set updated = state_attr('sensor.solar_optimizer_history', 'updated_at') %}\n"
    + WX_ICONS +
    "{% if days and days | length > 0 %}\n"
    "### Solar Optimizer History\n\n"
    "| Date | Weather | Forecast | Actual | Delta | Use | Net | Off-peak | Peak | Sold | SOC | Acc |\n"
    "|:--|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|\n"
    "{% for d in days %}"
    "| {{ d.date[5:] }} "
    "| {{ wi.get(d.plan_weather, '🌤️') }}"
    "{% if d.actual_weather and d.actual_weather != d.plan_weather %}"
    "→{{ wi.get(d.actual_weather, '🌤️') }}"
    "{% endif %} "
    "| {{ d.forecast_kwh }} | {{ d.production_kwh }} "
    "| {% if d.solar_delta is not none %}"
    "{{ '+' if d.solar_delta > 0 }}{{ d.solar_delta }}"
    "{% endif %} "
    "| {{ d.consumption_kwh }} "
    "| {% if d.net_balance is not none %}"
    "{{ '+' if d.net_balance > 0 }}{{ d.net_balance }}"
    "{% endif %} "
    "| {{ d.off_peak_grid_kwh }} "
    "| {% if d.peak_grid_used %}"
    "**{{ d.peak_grid_kwh }}**"
    "{% else %}"
    "{{ d.peak_grid_kwh }}"
    "{% endif %} "
    "| {{ d.grid_sold_kwh }} "
    "| {{ d.soc_target }}% "
    "| {% if d.forecast_accuracy is not none %}"
    "{{ '%.0f' | format(d.forecast_accuracy * 100) }}%"
    "{% endif %} |\n"
    "{% endfor %}\n\n"
    "<sub>Forecast = Forecast.Solar raw kWh · "
    "Actual = inverter production kWh · "
    "Delta = actual − forecast · "
    "Net = production − consumption · "
    "Acc = actual/forecast · "
    "Peak grid **bold** = grid used during peak hours · "
    "Updated: {{ updated }}</sub>\n"
    "{% else %}\n"
    "No history data yet. History is recorded daily at 9pm.\n"
    "{% endif %}"
)

history_view = {
    "title": "Solar History",
    "path": "solar-history",
    "type": "panel",
    "cards": [
        {
            "type": "markdown",
            "content": history_content,
            "card_mod": {
                "style": {
                    "ha-markdown$": (
                        "table { width: 100%; border-collapse: collapse; "
                        "margin: 4px 0 !important; } "
                        "th, td { padding: 2px 6px !important; "
                        "white-space: nowrap; } "
                        "tr:nth-child(even) { background: var(--table-row-alternative-background-color); }"
                    ),
                    ".": "ha-card { font-size: 0.85rem; padding: 8px; }",
                },
            },
        }
    ],
}

views = config['data']['config']['views']
found = False
for i, v in enumerate(views):
    if v.get('path') == 'solar-history':
        views[i] = history_view
        found = True
        break
if not found:
    views.append(history_view)

with open(LOVELACE_PATH, 'w') as f:
    json.dump(config, f, indent=2)

print(f"History view {'updated' if found else 'created'}. Content length: {len(history_content)}")
