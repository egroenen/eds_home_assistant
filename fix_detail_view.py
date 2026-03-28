#!/usr/bin/env python3
"""Create/update the Solar Detail view with two-column layout."""
import json

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_home"

with open(LOVELACE_PATH) as f:
    config = json.load(f)

# Shared template variables
_set_vars = (
    "{% set d = state_attr('sensor.solar_optimizer_status', 'detail') %}\n"
    "{% set plan = state_attr('sensor.solar_optimizer_status', 'plan') %}\n"
    "{% set learn = state_attr('sensor.solar_optimizer_status', 'learning') %}\n"
    "{% set updated = state_attr('sensor.solar_optimizer_status', 'updated_at') %}\n"
)

# Left card: hourly forecast table
left_content = (
    _set_vars +
    "{% if d %}\n"
    "### Hourly Forecast ({{ d.plan_date }})\n\n"
    "| Hr | Cond | Temp | Short<br>wave | Cloud | Rad | Prod | Con<br>sump | Forecast | Actual | +/- |\n"
    "|--:|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|\n"
    "{% for h in d.hours %}"
    "| {{ h.hour }} | {{ h.condition[:5] }} | "
    "{{ h.temp if h.temp else '-' }} | "
    "{{ h.sw_wm2 if h.sw_wm2 else '-' }} | "
    "{{ h.solar_cloud }} | {{ h.solar_rad }} | "
    "{{ h.actual_pv_kwh if h.actual_pv_kwh is not none else '-' }} | "
    "{{ h.consumption }} | "
    "{{ h.battery_soc if h.battery_soc is not none else '-' }}% | "
    "{{ h.actual_soc if h.actual_soc is not none else '-' }}{% if h.actual_soc is not none %}%{% endif %} | "
    "{% if h.soc_diff is not none %}"
    "{% if h.soc_diff > 0 %}+{% endif %}{{ h.soc_diff }}"
    "{% else %}-{% endif %} |\n"
    "{% endfor %}\n\n"
    "### Algorithm Summary\n\n"
    "The raw solar forecast from Forecast.Solar is **{{ d.raw_solar }} kWh** for {{ d.plan_date }}. "
    "Using hourly weather data from **{{ d.source }}**, the **{{ learn.active_model }}** model "
    "{% if learn.active_model == 'cloud' %}"
    "applies per-hour weather corrections (cloud coverage and condition type) to estimate "
    "**{{ d.cloud_total }} kWh** of usable peak solar"
    "{% else %}"
    "uses shortwave radiation intensity to distribute solar production, estimating "
    "**{{ d.rad_total }} kWh** of usable peak solar"
    "{% endif %}. "
    "{% if d.rad_total and d.cloud_total != d.rad_total %}"
    "For comparison, the {{ 'cloud' if learn.active_model == 'radiation' else 'radiation' }} model "
    "estimates {{ d.cloud_total if learn.active_model == 'radiation' else d.rad_total }} kWh. "
    "{% endif %}\n\n"
    "Daily consumption for **{{ d.season }}** averages **{{ d.seasonal_avg }} kWh/day**. "
    "During peak hours (7am–9pm), the model distributes 70% of that"
    "{% if d.avg_temp %}"
    ", applies a temperature factor of {{ d.temp_factor }} "
    "(avg {{ d.avg_temp }}C — "
    "{{ 'warm, less heating' if d.temp_factor < 0.95 else 'cool, more heating' if d.temp_factor > 1.05 else 'mild, baseline' }})"
    "{% endif %}"
    "{% if d.day_factor and d.day_factor != 1.0 %}"
    ", and a **weekend factor of {{ d.day_factor }}** "
    "(everyone home — higher base load and heating)"
    "{% endif %}"
    ", giving **{{ d.consumption_total }} kWh** of peak consumption.\n\n"
    "A **30% outage reserve** is maintained overnight (Slots 1 and 6) as a safety "
    "net for power outages. The battery charges to 30% after 9pm via cheap off-peak "
    "grid, then the optimizer tops up to the **{{ plan.overnight_soc }}% target** "
    "as late as possible before 6:30am to minimise time at high SOC.\n\n"
    "The hour-by-hour battery simulation starts at **{{ plan.overnight_soc }}%** and "
    "tracks solar production against consumption through each peak hour. "
    "{% if d.sim_min_soc <= 15 %}"
    "The battery reaches a tight minimum of **{{ d.sim_min_soc }}%** at {{ d.sim_min_hour }}:00, "
    "just above the 10% reserve — there is very little margin and peak grid usage is likely "
    "if conditions are worse than forecast."
    "{% elif d.sim_min_soc <= 30 %}"
    "The battery dips to **{{ d.sim_min_soc }}%** at {{ d.sim_min_hour }}:00, "
    "providing a reasonable buffer above the 10% reserve."
    "{% else %}"
    "The battery remains comfortable, with a minimum of **{{ d.sim_min_soc }}%** at {{ d.sim_min_hour }}:00, "
    "well above reserve."
    "{% endif %}"
    " The target of {{ plan.overnight_soc }}% was chosen by binary search simulation "
    "as the lowest SOC that keeps the battery above 20% (reserve + safety margin) "
    "all day, avoiding unnecessary grid charging when free solar is forecast."
    " The main deficit occurs in the "
    "{% if d.sim_min_hour <= 12 %}morning{% elif d.sim_min_hour <= 17 %}afternoon{% else %}evening{% endif %} "
    "hours when "
    "{% if d.sim_min_hour <= 12 %}solar production has not yet ramped up"
    "{% elif d.sim_min_hour <= 17 %}cloud cover reduces solar output"
    "{% else %}solar production has dropped off while evening consumption peaks{% endif %}.\n"
    "{% else %}\n"
    "No detail data available yet.\n"
    "{% endif %}"
)

# Middle card: plan summary
middle_content = (
    _set_vars +
    "{% if d %}\n"
    "### Plan Summary\n\n"
    "| | |\n"
    "|:--|--:|\n"
    "| Source | {{ d.source }} |\n"
    "| Raw solar | {{ d.raw_solar }} kWh |\n"
    "| Cloud model | {{ d.cloud_total }} kWh |\n"
    "| Radiation model | {{ d.rad_total if d.rad_total else 'N/A' }} kWh |\n"
    "| **Active model** | **{{ learn.active_model }}** ({{ learn.model_pref }}) |\n"
    "| Season | {{ d.season }} (base {{ d.seasonal_avg }} kWh/day) |\n"
    "| Peak consumption | {{ d.consumption_total }} kWh (7am–9pm) |\n"
    "| Temp factor | {{ d.temp_factor }} |\n"
    "| Day factor | {{ d.day_factor }} |\n"
    "| Avg temp | {{ d.avg_temp }}C |\n"
    "| **Sim min SOC** | **{{ d.sim_min_soc }}%** at {{ d.sim_min_hour }}:00 |\n"
    "| **Target SOC** | **{{ plan.overnight_soc }}%** |\n\n"
    "{% if plan.slots %}\n"
    "### Inverter TOU Slots\n\n"
    "| Slot | Time | SOC | Power | Grid |\n"
    "|--:|:--|--:|--:|:--|\n"
    "{% for s in plan.slots %}"
    "| {{ s.slot }} | {{ s.from }}-{{ s.to }} | {{ s.soc }}% | {{ s.power }}W | {{ s.grid }} |\n"
    "{% endfor %}\n"
    "{% endif %}\n"
    "{% else %}\n"
    "No detail data available yet.\n"
    "{% endif %}"
)

# Right card: learning parameters
right_content = (
    _set_vars +
    "{% if learn %}\n"
    "### Consumption Averages\n\n"
    "{% set season = state_attr('sensor.solar_optimizer_status', 'detail').season if state_attr('sensor.solar_optimizer_status', 'detail') else 'autumn' %}\n"
    "| Season | kWh/day |\n"
    "|:--|--:|\n"
    "| {{ '**Spring**' if season == 'spring' else 'Spring' }} | {{ '**' if season == 'spring' }}{{ learn.consumption_spring }}{{ '**' if season == 'spring' }} |\n"
    "| {{ '**Summer**' if season == 'summer' else 'Summer' }} | {{ '**' if season == 'summer' }}{{ learn.consumption_summer }}{{ '**' if season == 'summer' }} |\n"
    "| {{ '**Autumn**' if season == 'autumn' else 'Autumn' }} | {{ '**' if season == 'autumn' }}{{ learn.consumption_autumn }}{{ '**' if season == 'autumn' }} |\n"
    "| {{ '**Winter**' if season == 'winter' else 'Winter' }} | {{ '**' if season == 'winter' }}{{ learn.consumption_winter }}{{ '**' if season == 'winter' }} |\n"
    "| Year | {{ learn.consumption_year }} |\n"
    "| Legacy (avg×seasonal) | {{ learn.consumption_avg }} |\n\n"
    "### Other\n\n"
    "| | |\n"
    "|:--|--:|\n"
    "| Base SOC | {{ learn.base_soc }}% |\n"
    "| Days of data | {{ state_attr('sensor.solar_optimizer_status', 'days_of_data') }} |\n"
    "| Updated | {{ updated }} |\n\n"
    # SW Efficiency table: shows learned efficiency (Eff), today's actual
    # efficiency (Actual = actual_pv_kwh / shortwave_wm2 from the detail hours),
    # and the difference (Diff).  Actual is only shown for hours with meaningful
    # radiation (>50 W/m²) and production (>0.2 kWh) to filter noise.
    # Uses Jinja2 namespace to accumulate the actual dict inside a for-loop.
    "### SW Efficiency (kWh/W/m²)\n\n"
    "{% if learn.sw_efficiency and d %}\n"
    "{% set eff = learn.sw_efficiency %}\n"
    "{% set ns = namespace(actual={}) %}\n"
    "{% for h in d.hours %}"
    "{% if h.sw_wm2 and h.sw_wm2 > 50 and h.actual_pv_kwh is not none and h.actual_pv_kwh > 0.2 %}"
    "{% set ns.actual = dict(ns.actual, **{h.hour | string: h.actual_pv_kwh / h.sw_wm2}) %}"
    "{% endif %}"
    "{% endfor %}"
    "| Hour | Eff | Actual | Diff |\n"
    "|--:|--:|--:|--:|\n"
    "{% for h in range(7, 21) %}"
    "{% set e = eff[h | string] %}"
    "{% if (h | string) in ns.actual %}"
    "{% set a = ns.actual[h | string] %}"
    "| {{ h }} | {{ '%.4f' | format(e) }} | {{ '%.4f' | format(a) }} | {{ '%+.4f' | format(a - e) }} |\n"
    "{% else %}"
    "| {{ h }} | {{ '%.4f' | format(e) }} | - | - |\n"
    "{% endif %}"
    "{% endfor %}\n"
    "{% endif %}\n\n"
    "### Weather Corrections\n\n"
    "| Condition | Factor |\n"
    "|:--|--:|\n"
    "| Sunny | {{ learn.sunny_corr }} |\n"
    "| Partly cloudy | {{ learn.partly_corr }} |\n"
    "| Cloudy | {{ learn.cloudy_corr }} |\n"
    "| Rainy | {{ learn.rainy_corr }} |\n\n"
    "### Temperature Factors\n\n"
    "| Band | Factor |\n"
    "|:--|--:|\n"
    "| Cold (<10C) | {{ learn.temp_cold }} |\n"
    "| Cool (10-15C) | {{ learn.temp_cool }} |\n"
    "| Mild (15-20C) | {{ learn.temp_mild }} |\n"
    "| Warm (>20C) | {{ learn.temp_warm }} |\n"
    "{% else %}\n"
    "No data yet.\n"
    "{% endif %}"
)

detail_view = {
    "title": "Solar Detail",
    "path": "solar-detail",
    "type": "panel",
    "cards": [
        {
            "type": "custom:layout-card",
            "layout_type": "custom:grid-layout",
            "layout": {
                "grid-template-columns": "1fr 340px 280px",
            },
            "cards": [
                {"type": "markdown", "content": left_content},
                {"type": "markdown", "content": middle_content},
                {"type": "markdown", "content": right_content},
            ],
        }
    ],
}

views = config['data']['config']['views']
found = False
for i, v in enumerate(views):
    if v.get('path') == 'solar-detail':
        views[i] = detail_view
        found = True
        break
if not found:
    views.append(detail_view)

with open(LOVELACE_PATH, 'w') as f:
    json.dump(config, f, indent=2)

print(f"Detail view updated (3-column). Left={len(left_content)}, Middle={len(middle_content)}, Right={len(right_content)}")
