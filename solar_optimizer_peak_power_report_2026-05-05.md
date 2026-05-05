# Solar Optimizer Peak-Power Report

Date: `2026-05-05`

## Scope

This report is based on the live Home Assistant SQLite database copied from `/config/solar_optimizer.db` on the HA VM.

Data coverage:

- `daily_outcome`: 42 days (`2026-03-24` to `2026-05-04`)
- `daily_plan`: 42 days (`2026-03-25` to `2026-05-05`)
- `hourly_log`: 1,276 rows
- `forecast_tracking`: 538 rows

Important caveat:

- `daily_outcome` gives a full 42-day success view.
- Only 23 days have complete `07:00` and `21:00` hourly snapshots, so the deeper peak-window timing analysis uses those 23 days.

## Executive Summary

The optimizer is partially successful, but not yet reliably avoiding peak grid use.

- Across all 42 recorded outcomes, only 20 days stayed at or under `0.5 kWh` of peak-period grid import.
- That is a `47.6%` success rate.
- Average peak-period grid import is `3.1 kWh/day`.
- Over the last 14 days, success has slipped to `42.9%`, with average peak import rising to `3.9 kWh/day`.

The main issue is not that the system is underestimating consumption. If anything, it is already conservative on load.

The real issues are:

1. Some failure days are physically impossible to cover even from a full battery.
2. On the rest, the planner is too optimistic about when solar will arrive and how much of it will be usable during the day.
3. Several learned database parameters are no longer used by the planner, so adjusting them would not change behavior.

## Success Metrics

From `daily_outcome` over 42 days:

- Success days (`peak_grid_kwh <= 0.5`): `20 / 42`
- Success rate: `47.6%`
- Average peak grid import: `3.105 kWh/day`
- Last 7 days success rate: `42.9%`
- Last 14 days success rate: `42.9%`
- Last 30 days success rate: `53.3%`
- Days ending at `10%` battery SOC: `18 / 42`

From the 23 days with complete `07:00` to `21:00` hourly traces:

- Success days: `11 / 23`
- Success rate: `47.8%`
- Average peak grid import: `3.57 kWh`
- Median peak grid import: `0.7 kWh`
- Average `07:00` starting SOC: `72.2%`
- Average `21:00` ending SOC: `23.3%`

## What The Database Says

### 1. Load estimates are already conservative

Across the 23 fully traceable days:

- Average actual peak-period load: `36.94 kWh`
- Average planned peak-period load: `41.32 kWh`
- Average planner load error: `-4.38 kWh`

So the optimizer is already planning for more peak load than actually arrives.

That means increasing `consumption_avg_autumn`, `daily_consumption_avg`, `weekday_factor`, or `weekend_factor` is unlikely to be the best first move if the goal is specifically reducing peak import. Those metrics are already biased on the safe side.

### 2. Solar timing and solar usability are the bigger problem

Overall average planned peak solar is close to actual:

- Planned average peak solar: `35.80 kWh`
- Actual average peak solar: `36.54 kWh`

But this average hides the real issue: on bad days the planner can be badly wrong about usable solar inside the peak window.

Examples:

- `2026-05-04`: planned peak solar `36.2 kWh`, actual peak solar `10.2 kWh`, peak grid import `18.5 kWh`
- `2026-04-22`: planned peak solar `22.8 kWh`, actual peak solar `14.8 kWh`, peak grid import `18.3 kWh`
- `2026-04-12`: planned peak solar `11.0 kWh`, actual peak solar `7.2 kWh`, peak grid import `12.2 kWh`

### 3. Some failure days were impossible anyway

Using actual peak load and actual peak solar, 5 of the 12 failures in the 23-day detailed sample would have required more than `100%` starting SOC to fully avoid peak grid import.

Those days cannot be fixed by tuning the overnight target alone.

### 4. The remaining failures are timing failures

7 of the 12 failures were theoretically coverable by total daily energy, but still imported during peak hours.

That means:

- battery energy existed in total
- but the system ran short before the solar arrived, or
- the solar model was too optimistic about the within-day profile

This is the class of failure most likely to improve from tuning.

## Model Assessment

The radiation model is still better overall, but not always.

Weighted hourly MAE from `forecast_tracking`:

- Last 7 days: radiation `1105 Wh`, cloud `2081 Wh`
- Last 14 days: radiation `1037 Wh`, cloud `2170 Wh`
- Full tracked history: radiation `1556 Wh`, cloud `1845 Wh`

So the radiation model is still the better default on average.

But it failed badly on some recent cloudy days:

- `2026-05-04`: cloud total `8.1 kWh`, radiation total `35.4 kWh`, actual `10.4 kWh`
- `2026-05-05`: cloud total `7.6 kWh`, radiation total `30.6 kWh`, actual `13.6 kWh`

This means the worst misses are coming from overly optimistic radiation-driven usable-solar estimates on low-yield days.

## Parameters That Actually Matter

These affect tomorrow’s plan right now:

- `safety_margin_pct`
- `max_overnight_soc`
- `consumption_avg_autumn`
- `daily_consumption_avg`
- `weekday_factor`
- `weekend_factor`
- `temp_factor_*`
- `sw_efficiency_9` through `sw_efficiency_20`

These do not currently change planning behavior in the normal path:

- `base_overnight_soc`
- `preferred_solar_model`

These are only fallback-only or rarely used:

- `peak_solar_ratio`
- `peak_consumption_ratio`

The code path in [planner.py](/home/eddyg/projects/home-assistant/solar_optimizer/planner.py:91) always picks the radiation model whenever radiation output exists, and it never reads `preferred_solar_model`.

The learned `base_overnight_soc` is updated nightly in [learning.py](/home/eddyg/projects/home-assistant/solar_optimizer/learning.py:242), but the planner never uses it when selecting the overnight SOC target.

## Recommended One-Shot Adjustment

The highest-confidence metric-only change is:

- Increase `safety_margin_pct` from `10` to `20`

Why this is the best immediate move:

- It is active in the planner.
- It directly raises the daytime reserve target from `20%` to `30%`.
- It helps the coverable timing failures, which are the largest fixable class.
- It avoids spending effort on parameters that are already conservative or unused.
- It does not depend on retraining or waiting for more data.

Why I do not recommend `base_overnight_soc` as the first adjustment:

- It is already at `100`
- and, more importantly, it is not used by the planner right now

Why I do not recommend increasing consumption metrics first:

- the planner is already overestimating peak load by about `4.4 kWh` on average

## Secondary Adjustment If You Want A More Conservative Batch

If you want a stronger autumn/cloudy-day bias, the next best family of metrics is the shortwave efficiency set:

- `sw_efficiency_10`
- `sw_efficiency_11`
- `sw_efficiency_12`
- `sw_efficiency_13`
- `sw_efficiency_14`
- `sw_efficiency_15`
- `sw_efficiency_16`

Use this only if you want to deliberately make the radiation model less optimistic during the middle of the day.

I would treat this as a second pass, not the first pass, because:

- radiation is still better overall than cloud
- but it has a few very bad misses
- a blanket reduction will improve cloudy protection while increasing overcharging on clear days

## Suggested Change Order

1. Change `safety_margin_pct` from `10` to `20`
2. Observe another 7 to 10 days
3. If cloudy-day misses still dominate, then reduce the `sw_efficiency_10` to `sw_efficiency_16` band by roughly `10%` to `15%`

## Practical Recommendation

If you want a single change with the best chance of improving peak avoidance immediately, make it:

```bash
ssh -i ~/.ssh/id_ha root@192.168.69.215 \
  "cd /config && python3 solar_optimizer.py set-param safety_margin_pct 20"
```

If you want the biggest real improvement after that, it is probably not another DB tweak. It is a code fix:

- make the planner actually honor `preferred_solar_model`, or
- blend cloud and radiation outputs on low-confidence days, or
- fall back to cloud when shortwave looks high but cloud cover stays very high

That would target the actual source of the largest misses much more directly than pushing the learned DB parameters further.
