#!/usr/bin/env python3
"""Add the Solar Optimizer summary and detail cards to the Home dashboard."""
import json

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_home"

with open(LOVELACE_PATH) as f:
    config = json.load(f)

section0 = config['data']['config']['views'][0]['sections'][0]
views = config['data']['config']['views']

optimizer_card = {
    "type": "markdown",
    "content": (
        "{% set plan = state_attr('sensor.solar_optimizer_status', 'plan') %}\n"
        "{% set learn = state_attr('sensor.solar_optimizer_status', 'learning') %}\n"
        "{% set today = state_attr('sensor.solar_optimizer_status', 'today') %}\n"
        "{% set days = state_attr('sensor.solar_optimizer_status', 'days_of_data') %}\n"
        "{% set updated = state_attr('sensor.solar_optimizer_status', 'updated_at') %}\n"
        "{% set wi = {'sunny':'☀️','cloudy':'☁️','partlycloudy':'⛅','rainy':'🌧️'} %}\n"
        "{% set live = state_attr('sensor.solar_optimizer_status', 'live') %}\n"
        "{% set det = state_attr('sensor.solar_optimizer_status', 'detail') %}\n"
        "{% if plan %}\n"
        "{% set label = 'Today' if now().hour < 22 else 'Tomorrow' %}\n"
        "| {{ label }} | Plan | Actual |\n"
        "|:--|--:|--:|\n"
        "| **Weather** | {{ wi.get(plan.weather, '🌤️') }} {{ plan.weather | title }} |"
        "{% if live and live.weather is defined %} {{ wi.get(live.weather, '🌤️') }} {{ live.weather | title }}"
        "{% endif %} |\n"
        "{% set active_solar = det.active_total if det and det.active_total is defined else plan.adjusted_solar %}\n"
        "| Model solar | {{ '%.1f' | format(active_solar) }} kWh |"
        "{% if live and live.solar is defined %} {{ '%.1f' | format(live.solar) }} kWh"
        "{% elif today %} {{ '%.1f' | format(today.production) }} kWh"
        "{% endif %} |\n"
        "| Peak deficit | {{ '%.1f' | format((det.consumption_total - active_solar) if det else plan.deficit) }} kWh |"
        "{% if live %} {{ '%.1f' | format(live.peak_grid_kwh | default(0)) }} kWh"
        "{% elif today %} {{ '%.1f' | format(today.peak_grid_kwh | default(0)) }} kWh"
        "{% else %} 0.0 kWh{% endif %} |\n"
        "| **Charge to** | **{{ plan.overnight_soc }}%** |"
        "{% if live and live.battery_soc is defined %} **{{ live.battery_soc | int }}%**"
        "{% elif today %} --"
        "{% endif %} |\n"
        "{% endif %}\n"
        "{% if plan %}\n"
        "<sub>{{ wi.get(plan.weather, '') }} correction {{ '%.2f' | format(plan.correction) }}"
        " · 🌡️ temp factor {{ '%.2f' | format(plan.temp_factor) }}"
        " · {{ updated }}"
        " · [detail](/dashboard-home/solar-detail)</sub>\n"
        "{% endif %}"
    ),
    "grid_options": {
        "columns": 8
    },
    "card_mod": {
        "style": {
            "ha-markdown$": (
                "table {\n"
                "  width: 100%;\n"
                "  margin: 4px 0 !important;\n"
                "}\n"
                "th, td {\n"
                "  padding: 1px 4px !important;\n"
                "}\n"
            ),
            ".": (
                "ha-card {\n"
                "  font-size: 0.85rem;\n"
                "}\n"
                "ha-markdown {\n"
                "  padding: 0 12px !important;\n"
                "}\n"
            ),
        }
    }
}

def _update_detail_view():
    """Create or update the Solar Detail view — delegates to fix_detail_view.py.

    Note: The detail view template is maintained in fix_detail_view.py to avoid
    quoting issues between the add_dashboard_card.py string concatenation and
    Jinja template single quotes. This function is a no-op since fix_detail_view.py
    is run separately.
    """
    pass  # Detail view managed by fix_detail_view.py


# Check if already added — update in place if so
for i, card in enumerate(section0['cards']):
    if card.get('title') == 'Solar Optimizer' or 'solar_optimizer_status' in card.get('content', ''):
        section0['cards'][i] = optimizer_card
        _update_detail_view()
        with open(LOVELACE_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        print("Solar Optimizer card updated on dashboard. Restart HA to see it.")
        exit(0)

# Shrink weather card to 9 columns to make room
section0['cards'][7]['grid_options']['columns'] = 9
section0['cards'][7]['forecast_rows'] = 3

# Insert optimizer card before weather (position 7)
section0['cards'].insert(7, optimizer_card)

# Also create/update the detail view
_update_detail_view()

with open(LOVELACE_PATH, 'w') as f:
    json.dump(config, f, indent=2)

print("Solar Optimizer card added to dashboard. Restart HA to see it.")
