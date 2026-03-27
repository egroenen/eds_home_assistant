#!/usr/bin/env python3
"""Add the Solar Optimizer summary and detail cards to the Home dashboard."""
import json

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_home"

with open(LOVELACE_PATH) as f:
    config = json.load(f)

section0 = config['data']['config']['views'][0]['sections'][0]
views = config['data']['config']['views']

# --- Detail view content (hourly breakdown) ---
detail_content = (
    "{% set d = state_attr('sensor.solar_optimizer_status', 'detail') %}\n"
    "{% set plan = state_attr('sensor.solar_optimizer_status', 'plan') %}\n"
    "{% set learn = state_attr('sensor.solar_optimizer_status', 'learning') %}\n"
    "{% set updated = state_attr('sensor.solar_optimizer_status', 'updated_at') %}\n"
    "{% if d %}\n"
    "## Solar Optimizer Detail\n"
    "**Plan date:** {{ d.plan_date }} · **Source:** {{ d.source }}\n\n"
    "**Raw solar:** {{ d.raw_solar }} kWh · "
    "**Cloud model:** {{ d.cloud_total }} kWh · "
    "**Radiation model:** {{ d.rad_total if d.rad_total else 'N/A' }} kWh\n\n"
    "**Active model:** {{ learn.active_model }} (pref {{ learn.model_pref }})\n\n"
    "**Consumption:** {{ d.consumption_total }} kWh "
    "(seasonal {{ d.seasonal }}, temp {{ d.temp_factor }}"
    "{% if d.avg_temp %}, avg {{ d.avg_temp }}°C{% endif %})\n\n"
    "**Simulation:** min SOC {{ d.sim_min_soc }}% at {{ d.sim_min_hour }}:00, "
    "target {{ plan.overnight_soc }}%\n\n"
    "### Hourly Forecast\n\n"
    "| Hour | Weather | Cloud | Temp | SW W/m² | ☁️ Cloud | ☀️ Rad | Cons | Battery |\n"
    "|:--:|:--|--:|--:|--:|--:|--:|--:|--:|\n"
    "{% for h in d.hours %}"
    "| {{ '%02d' | format(h.hour) }} | {{ h.condition[:6] }} | {{ h.cloud }}% |"
    " {{ '%.0f' | format(h.temp) if h.temp else '-' }}° |"
    " {{ '%.0f' | format(h.sw_wm2) if h.sw_wm2 else '-' }} |"
    " {{ '%.2f' | format(h.solar_cloud) }} |"
    " {{ '%.2f' | format(h.solar_rad) }} |"
    " {{ '%.2f' | format(h.consumption) }} |"
    " {{ h.battery_soc if h.battery_soc is not none else '-' }}% |\n"
    "{% endfor %}\n\n"
    "### Learning Parameters\n\n"
    "| Parameter | Value |\n"
    "|:--|--:|\n"
    "| Base SOC | {{ learn.base_soc }}% |\n"
    "| Consumption avg | {{ learn.consumption_avg }} kWh |\n"
    "| ☀️ Sunny correction | {{ learn.sunny_corr }} |\n"
    "| ⛅ Partly cloudy correction | {{ learn.partly_corr }} |\n"
    "| ☁️ Cloudy correction | {{ learn.cloudy_corr }} |\n"
    "| 🌧️ Rainy correction | {{ learn.rainy_corr }} |\n"
    "| 🌡️ Cold (<10°C) | {{ learn.temp_cold }} |\n"
    "| 🌡️ Cool (10-15°C) | {{ learn.temp_cool }} |\n"
    "| 🌡️ Mild (15-20°C) | {{ learn.temp_mild }} |\n"
    "| 🌡️ Warm (>20°C) | {{ learn.temp_warm }} |\n\n"
    "{% if d.model_accuracy %}\n"
    "### Model Accuracy (recent days)\n\n"
    "| Date | Cloud err | Rad err | Hours |\n"
    "|:--|--:|--:|--:|\n"
    "{% for a in d.model_accuracy %}"
    "| {{ a.date }} | {{ a.cloud_err }} | {{ a.rad_err if a.rad_err else '-' }} | {{ a.hours }} |\n"
    "{% endfor %}\n"
    "{% endif %}\n"
    "<sub>Updated: {{ updated }}</sub>\n"
    "{% else %}\n"
    "No detail data available yet. Run a poll or optimize first.\n"
    "{% endif %}"
)

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
        "| Solar | {{ '%.1f' | format(det.raw_solar if det else plan.solar_forecast) }} kWh | |\n"
        "| Adjusted | {{ '%.1f' | format(det.cloud_total if det else plan.adjusted_solar) }} kWh |"
        "{% if live and live.solar is defined %} {{ '%.1f' | format(live.solar) }} kWh"
        "{% elif today %} {{ '%.1f' | format(today.production) }} kWh"
        "{% endif %} |\n"
        "| Peak deficit | {{ '%.1f' | format((det.consumption_total - det.cloud_total) if det else plan.deficit) }} kWh |"
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
