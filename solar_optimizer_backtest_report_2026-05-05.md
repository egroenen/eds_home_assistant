# Solar Optimizer Backtest Report

Date: `2026-05-05`

## Scope

This backtest used the live HA database snapshot copied to:

- `/tmp/solar_optimizer_live.db`

Scoring objective:

- Peak import cost only
- Peak value rate: `0.32 / kWh`
- Cost formula: `Peak kWh * 0.32`

No off-peak charging penalty was included, because the user confirmed that money should be treated as directly proportional to recorded `Peak` usage.

## Candidate Results

Backtest window:

- `22` days with enough historical plan, forecast-tracking, and hourly actual data to simulate candidate outcomes

Results:

| Profile | Engine | Days | Simulated Peak kWh | Simulated Cost | Avg Target SOC |
|:--|:--|--:|--:|--:|--:|
| `radiation-fitted-safe` | `radiation` | 22 | 38.3 | 12.26 | 85.2 |
| `cloud-baseline` | `cloud` | 22 | 38.3 | 12.26 | 100.0 |
| `blended-fitted` | `blended` | 22 | 38.3 | 12.26 | 100.0 |
| `capped-radiation-fitted` | `capped_radiation` | 22 | 38.3 | 12.26 | 100.0 |
| `capped-radiation-fitted-safe` | `capped_radiation` | 22 | 38.3 | 12.26 | 100.0 |
| `radiation-fitted` | `radiation` | 22 | 42.0 | 13.46 | 77.5 |
| `original` | `radiation` | 22 | 44.3 | 14.18 | 73.0 |

## Winner

Best-fit profile:

- `radiation-fitted-safe`
- engine: `radiation`
- simulated peak import: `38.3 kWh`
- simulated peak cost: `12.26`

Why this won:

- It tied the best peak-cost result
- but achieved that tie with a lower average target SOC than the max-charge candidates

## Best-Fit Parameter Set

The winning profile currently contains these notable changes from the working set:

- `safety_margin_pct = 15.0`
- `sw_efficiency_9 = 0.0505`
- `sw_efficiency_10 = 0.0473`
- `sw_efficiency_11 = 0.0293`
- `sw_efficiency_12 = 0.0230`
- `sw_efficiency_13 = 0.0161`
- `sw_efficiency_14 = 0.0120`
- `sw_efficiency_15 = 0.0093`
- `sw_efficiency_16 = 0.0061`
- `sw_efficiency_17 = 0.0032`
- `sw_efficiency_18 = 0.0000`
- `sw_efficiency_19 = 0.0000`
- `sw_efficiency_20 = 0.0000`

Interpretation:

- the historical fit preferred a more conservative radiation model than the current live set
- and it also preferred a slightly larger daytime reserve target

## Generated Profiles

The backtest wrote these named profiles into the tested snapshot DB:

- `original`
- `cloud-baseline`
- `radiation-fitted`
- `radiation-fitted-safe`
- `blended-fitted`
- `capped-radiation-fitted`
- `capped-radiation-fitted-safe`
- `best-fit`

## Commands

List profiles in the tested snapshot:

```bash
SOLAR_OPTIMIZER_DB_PATH=/tmp/solar_optimizer_live.db python3 -m solar_optimizer profile list
```

Show the best-fit profile:

```bash
SOLAR_OPTIMIZER_DB_PATH=/tmp/solar_optimizer_live.db python3 -m solar_optimizer profile show best-fit
```

Load the best-fit profile into the tested snapshot DB:

```bash
SOLAR_OPTIMIZER_DB_PATH=/tmp/solar_optimizer_live.db python3 -m solar_optimizer profile load best-fit
```

## Important Note

The generated profiles were saved into the copied snapshot DB in `/tmp`, not pushed back to the live HA database.

That means:

- the architecture and commands are implemented
- the historical winner is identified
- but the live optimizer has not yet been switched to `best-fit`
