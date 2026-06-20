# Solar Optimizer Peak Power Report - 2026-06-10

## Executive summary

The optimiser is reasonably accurate at forecasting solar from MetOcean shortwave radiation, but it is not currently efficient enough at eliminating peak grid import in winter. The main issue is not a single bad constant: many recent days need more usable stored energy than the battery can provide from the 07:00-21:00 peak window.

There was also a real edge-case bug. When Forecast.Solar reported zero or was unavailable, several paths stopped planning from solar entirely even though MetOcean shortwave radiation was still available. That made some plans record zero solar while the house later produced useful PV. The code has been changed so those zero Forecast.Solar cases fall through to the radiation engine instead of giving up.

## Current performance

Measured from the live Home Assistant database snapshot taken on 2026-06-10.

| Window | Complete days | Success days (`Peak <= 0.5 kWh`) | Peak import | Peak cost at 0.32/kWh | Avg peak/day |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-05-06 to 2026-06-10 | 36 | 3 (8.3%) | 230.5 kWh | 73.76 | 6.40 kWh |
| 2026-06-01 to 2026-06-10 | 10 | 0 (0.0%) | 86.5 kWh | 27.68 | 8.65 kWh |

Physical limit indicators:

| Window | Avg target SOC | Days target 100% | Days ending at 10% | Energy-impossible days | Avoidable fail days |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-05-06 to 2026-06-10 | 93.6% | 27 | 31 | 18 | 15 |
| 2026-06-01 to 2026-06-10 | 85.5% | 4 | 10 | 6 | 4 |

Interpretation: the system is trying hard, but in June the house often drains the battery to reserve before the peak window ends. Six of the first ten June days were not solvable by parameters alone because the peak-period load minus peak-period solar exceeded what the battery could cover from 100% SOC.

## Forecast accuracy

Radiation forecasting remains the more accurate solar model.

| Window | Cloud model MAE | Radiation model MAE | Radiation better days |
| --- | ---: | ---: | ---: |
| 2026-05-06 to 2026-06-10 | 1376 Wh/hour | 796 Wh/hour | 28 of 36 |
| 2026-06-01 to 2026-06-10 | 1063 Wh/hour | 626 Wh/hour | 6 of 10 |

Interpretation: the optimiser should continue learning from shortwave radiation, but the best cost outcome is not always the most solar-accurate model. For peak-cost avoidance, the backtest currently prefers the blended profile because it tends to charge more conservatively.

## Backtest after zero-forecast fix

The patched backtest now includes 57 usable historical days, including days where the daily Forecast.Solar value was zero.

| Profile | Engine | Days | Simulated peak import | Simulated peak cost | Avg target SOC |
| --- | --- | ---: | ---: | ---: | ---: |
| blended-fitted | blended | 57 | 192.7 kWh | 61.66 | 97.4% |
| cloud-baseline | cloud | 57 | 192.7 kWh | 61.66 | 99.9% |
| capped-radiation-fitted | capped_radiation | 57 | 192.7 kWh | 61.66 | 99.9% |
| capped-radiation-fitted-safe | capped_radiation | 57 | 192.7 kWh | 61.66 | 100.0% |
| radiation-fitted-safe | radiation | 57 | 197.9 kWh | 63.34 | 87.9% |
| original | radiation | 57 | 200.9 kWh | 64.30 | 83.3% |
| radiation-fitted | radiation | 57 | 200.9 kWh | 64.30 | 84.2% |

Best one-hit adjustment: load the `best-fit` profile generated from `blended-fitted`. It reduces simulated peak import by 8.2 kWh versus the current radiation-only original set over the 57-day test window.

## Changes made

- Planning no longer falls straight to failsafe when Forecast.Solar is zero; it now attempts radiation-only planning from MetOcean shortwave.
- Overnight target revision now also uses radiation when the daily solar forecast is zero.
- Dashboard detail generation now shows the radiation fallback instead of silently dropping detail.
- Forecast tracking now records radiation-vs-actual rows even when Forecast.Solar is zero.
- Backtesting now includes zero Forecast.Solar days by evaluating them through the radiation engine.
- Live Home Assistant was updated, `best-fit` was regenerated and loaded, and the 2026-06-11 plan was written.

## Current live plan

The live optimiser generated the following plan for 2026-06-11:

- Active profile: `best-fit`
- Active engine: `blended`
- Overnight SOC target: 100%
- Forecast.Solar daily value: 2.6 kWh
- Radiation model total: 22.2 kWh
- Blended active peak solar estimate: 12.2 kWh
- Estimated peak consumption: 35.3 kWh
- Estimated peak deficit: 23.1 kWh

This plan is appropriately conservative. The model expects the battery to still hit reserve before evening even from 100%, so the remaining peak import is likely a capacity/load-shifting problem rather than a tuning-only problem.

## 2026-06-15 charge-bias update

Policy change: when backtested peak-cost results are effectively tied, prefer the profile that charges more. Peak electricity is approximately three times the off-peak price, so a small amount of additional off-peak charging is a better risk than running empty during peak.

The latest live database snapshot produced 62 usable backtest days. Multiple candidates tied on simulated peak import and cost, so the charge-biased ranking selected the highest-SOC tied option:

| Profile | Engine | Days | Simulated peak import | Simulated peak cost | Avg target SOC |
| --- | --- | ---: | ---: | ---: | ---: |
| capped-radiation-charge-biased | capped_radiation | 62 | 238.3 kWh | 76.26 | 100.0% |
| capped-radiation-fitted | capped_radiation | 62 | 238.3 kWh | 76.26 | 100.0% |
| capped-radiation-fitted-safe | capped_radiation | 62 | 238.3 kWh | 76.26 | 100.0% |
| cloud-baseline | cloud | 62 | 238.3 kWh | 76.26 | 99.9% |
| blended-charge-biased | blended | 62 | 238.3 kWh | 76.26 | 99.5% |
| blended-fitted-safe | blended | 62 | 238.3 kWh | 76.26 | 98.7% |
| blended-fitted | blended | 62 | 238.3 kWh | 76.26 | 96.8% |
| original | blended | 62 | 238.3 kWh | 76.26 | 95.4% |
| radiation-fitted-safe | radiation | 62 | 240.6 kWh | 76.98 | 88.7% |
| radiation-fitted | radiation | 62 | 244.3 kWh | 78.18 | 85.8% |

Decision: activate `best-fit` from `capped-radiation-charge-biased`. This preserves the same simulated peak-cost outcome as the less conservative tied profiles, but leaves less room for forecast error to turn into expensive peak import.

## 2026-06-15 generation-accuracy update

The generation estimate was still visibly wrong after the charge-bias update. The cause was systematic, not random: `capped_radiation` was using the very low Forecast.Solar daily value as a cap, so it preserved a conservative charging decision but badly understated likely generation.

Measured over 62 complete days:

| Candidate | Engine | Peak cost | Avg target SOC | Generation ratio | Solar MAE/day |
| --- | --- | ---: | ---: | ---: | ---: |
| capped-radiation-charge-biased | capped_radiation | 76.26 | 100.0% | 0.20x | 22.6 kWh |
| radiation-current | radiation | 76.26 | 92.9% | 0.74x | 9.0 kWh |
| radiation-median-charge | radiation | 76.26 | 91.9% | 0.87x | 7.1 kWh |
| radiation-p60-charge, 35% margin | radiation | 76.26 | 100.0% | 0.99x | 6.3 kWh |

Decision: activate a new `radiation-accuracy-charge-biased` profile. It uses the radiation engine, 60th-percentile shortwave efficiencies, and a 35% safety margin. This keeps the same historical peak-cost result and a 100% average target SOC, while making the solar estimate much closer to actual generation.

## 2026-06-20 one-week review

Reviewed the first four complete days after the generation-accuracy calibration, 2026-06-16 to 2026-06-19.

| Date | Target SOC | 07:00 SOC | Peak import | Peak cost | Peak load | Peak solar | Required start SOC | Assessment |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-06-16 | 100% | 100% | 0.7 kWh | 0.22 | 41.1 kWh | 32.3 kWh | 68.7% | Small timing/reserve leakage |
| 2026-06-17 | 100% | 100% | 16.2 kWh | 5.18 | 39.6 kWh | 11.1 kWh | 200.0% | Battery-capacity impossible |
| 2026-06-18 | 100% | 100% | 1.4 kWh | 0.45 | 37.7 kWh | 28.0 kWh | 74.7% | Small timing/reserve leakage |
| 2026-06-19 | 100% | 100% | 7.2 kWh | 2.30 | 48.1 kWh | 32.4 kWh | 114.7% | Battery-capacity impossible |

Weekly result: 25.5 kWh peak import, costing 8.16 at 0.32/kWh. Two of the four completed days could not be solved by overnight charging because even a full battery did not contain enough usable energy for the peak-period net load.

Solar estimate check: the calibrated radiation profile estimated 82.6 kWh peak-period solar versus 103.8 kWh actual over these four days. A higher percentile fit would improve these four days, but it worsens the full 66-day history:

| Fit | All-history ratio | All-history MAE/day | Week ratio | Week MAE/day | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| 60th percentile | 0.99x | 6.26 kWh | 0.80x | 5.29 kWh | Keep |
| 70th percentile | 1.10x | 7.09 kWh | 0.89x | 3.66 kWh | Too recent-biased |
| 75th percentile | 1.20x | 8.50 kWh | 0.97x | 2.18 kWh | Chases the week |

Decision: no profile change. The system is already charging to 100%, the active profile remains the best full-history fit, and this week's remaining peak import is mostly battery capacity/timing rather than a solvable overnight-charge target problem.

## 2026-06-20 dashboard estimate display fix

The Solar History tab was still showing `solar_forecast_kwh`, which is the raw Forecast.Solar daily value. In June this value has often been around 2-3 kWh even when the calibrated radiation model estimated 20-25 kWh and actual production was 28-32 kWh. That made the dashboard look as if the optimiser estimate was wrong by 10x, even though the active planning estimate was much closer.

Example after regenerating history:

| Date | Raw Forecast.Solar | Displayed model estimate | Actual production | Model accuracy |
| --- | ---: | ---: | ---: | ---: |
| 2026-06-19 | 2.3 kWh | 24.7 kWh | 32.4 kWh | 131% |
| 2026-06-18 | 2.5 kWh | 21.2 kWh | 28.0 kWh | 132% |
| 2026-06-17 | 2.4 kWh | 10.1 kWh | 11.1 kWh | 109% |
| 2026-06-16 | 2.6 kWh | 24.4 kWh | 32.3 kWh | 132% |

Changes made: the History tab now displays the optimiser's adjusted/model estimate, keeps the raw Forecast.Solar value in JSON as `raw_forecast_kwh`, and computes delta/accuracy against the model estimate. The summary and detail cards now use `active_total` from the active engine instead of assuming the cloud model total.

## 2026-06-20 winter estimate calibration

The corrected dashboard made it clear that the active radiation estimate was still consistently low in June, even though the 100% charge target was correct. The issue was not the daily charge decision; it was that the same global shortwave efficiencies were being used across autumn and winter.

Measured against the live database snapshot:

| Candidate | All-history ratio | All-history MAE/day | June ratio | June MAE/day | Last completed days ratio | Last completed days MAE/day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Current learned radiation | 1.02x | 6.33 kWh | 0.90x | 6.05 kWh | 0.83x | 4.86 kWh |
| Winter scale 1.10 | 1.04x | 6.14 kWh | 0.99x | 5.35 kWh | 0.91x | 3.31 kWh |
| Winter scale 1.20 | 1.06x | 5.95 kWh | 1.08x | 4.66 kWh | 0.99x | 1.75 kWh |

Decision: add seasonal shortwave efficiency scalars and activate `radiation-winter-scaled-charge-biased` with `sw_efficiency_scale_winter = 1.20`. This keeps the same historical peak-cost score and 100% average SOC, while giving materially better winter generation estimates.
