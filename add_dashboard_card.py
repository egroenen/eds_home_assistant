#!/usr/bin/env python3
"""Add the Solar Optimizer card to the Home dashboard."""
import json

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_home"

with open(LOVELACE_PATH) as f:
    config = json.load(f)

section0 = config['data']['config']['views'][0]['sections'][0]

# Check if already added
for card in section0['cards']:
    if card.get('title') == 'Solar Optimizer':
        print("Solar Optimizer card already exists, skipping.")
        exit(0)

optimizer_card = {
    "type": "markdown",
    "title": "Solar Optimizer",
    "content": (
        "{% set plan = state_attr('sensor.solar_optimizer_status', 'plan') %}\n"
        "{% set learn = state_attr('sensor.solar_optimizer_status', 'learning') %}\n"
        "{% set today = state_attr('sensor.solar_optimizer_status', 'today') %}\n"
        "{% set days = state_attr('sensor.solar_optimizer_status', 'days_of_data') %}\n"
        "{% set updated = state_attr('sensor.solar_optimizer_status', 'updated_at') %}\n"
        "{% set wi = {'sunny':'☀️','cloudy':'☁️','partlycloudy':'⛅','rainy':'🌧️'} %}\n"
        "{% if plan %}\n"
        "**Tomorrow** {{ wi.get(plan.weather, '🌤️') }} {{ plan.weather | title }}\n"
        "| | |\n"
        "|:--|--:|\n"
        "| Solar forecast | {{ '%.1f' | format(plan.solar_forecast) }} kWh |\n"
        "| Adjusted | {{ '%.1f' | format(plan.adjusted_solar) }} kWh |\n"
        "| Peak deficit | {{ '%.1f' | format(plan.deficit) }} kWh |\n"
        "| **Charge to** | **{{ plan.overnight_soc }}%** |\n"
        "{% endif %}\n"
        "{% if today %}\n"
        "**Today** {{ '✅' if not today.peak_grid_used else '⚠️' }}"
        "{{ '%.1f' | format(today.production) }} produced · "
        "{{ '%.1f' | format(today.consumption) }} consumed"
        "{% endif %}"
    ),
    "grid_options": {
        "columns": 9,
        "rows": 3
    },
    "card_mod": {
        "style": (
            "ha-card {\n"
            "  font-size: 0.85rem;\n"
            "}\n"
            "ha-markdown {\n"
            "  padding: 0 12px !important;\n"
            "}\n"
            "table {\n"
            "  width: 100%;\n"
            "  margin: 4px 0 !important;\n"
            "}\n"
            "th, td {\n"
            "  padding: 1px 4px !important;\n"
            "}\n"
        )
    }
}

# Shrink weather card to 9 columns to make room
section0['cards'][7]['grid_options']['columns'] = 9
section0['cards'][7]['forecast_rows'] = 3

# Insert optimizer card before weather (position 7)
section0['cards'].insert(7, optimizer_card)

with open(LOVELACE_PATH, 'w') as f:
    json.dump(config, f, indent=2)

print("Solar Optimizer card added to dashboard. Restart HA to see it.")
