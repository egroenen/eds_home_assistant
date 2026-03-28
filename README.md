# Ed's Home Assistant Configuration

Local working copies of configuration files for a Home Assistant OS instance running on a VM. Changes are edited locally, pushed via SSH, and reloaded via the HA REST API.

## Setup

- **Home Assistant OS** 2026.3.3 with Supervisor
- **Inverter:** Deye hybrid inverter with 15 kWh battery (10% reserve)
- **Location:** Christchurch, New Zealand
- **Custom integrations:** Evnex, Feedparser, HACS, Solarman, Waste Collection Schedule, Xiaomi Home

## Configuration Files

| File | Description |
|:--|:--|
| `configuration.yaml` | Main config (sensors, templates, groups, integrations) |
| `automations.yaml` | All automations |
| `scenes.yaml` | Saved scenes |
| `scripts.yaml` | Scripts |
| `tts_phrases.yaml` | TTS phrase definitions |
| `secrets.yaml` | Credentials (not committed) |

## Solar Battery Optimizer

A self-learning battery charging optimizer for the Deye hybrid inverter, located in the `solar_optimizer/` package. It controls TOU (Time-of-Use) charging schedules via Solarman Modbus registers to minimise grid usage during peak hours.

### Architecture

The system runs as a Python package on the HA VM, invoked by HA automations at scheduled times. It follows a forecast → plan → execute → learn loop:

```
                  ┌──────────────┐
                  │  MetOcean API │  (shortwave radiation, cloud, temp)
                  └──────┬───────┘
                         │
    ┌────────────┐       ▼         ┌────────────────┐
    │ Forecast.  │──▶ models.py ──▶│   planner.py   │
    │ Solar API  │   (dual solar   │ (simulate battery│
    └────────────┘    models +     │  scenarios, pick │
                      consumption)  │  optimal SOC)   │
                                   └───────┬────────┘
                                           │
                                           ▼
                                    registers.py
                                   (write TOU slots
                                    to Deye inverter)
                         ┌─────────────────┘
                         │
    ┌────────────┐       ▼         ┌────────────────┐
    │ hourly     │──▶ polling.py ──▶│  charging.py   │
    │ snapshots  │   (track actual  │ (adjust charge  │
    └────────────┘    vs forecast)  │  window in      │
                                    │  real-time)     │
                         │          └────────────────┘
                         ▼
                    learning.py
                   (nightly: adjust
                    all correction
                    factors from
                    actual outcomes)
```

### Module Overview

| Module | Role |
|:--|:--|
| `__main__.py` | CLI entry point and command dispatcher (`optimize`, `poll`, `record`, `status`, `history`, etc.) |
| `config.py` | All constants: battery specs, peak hours, register addresses, sensor entity IDs, seasonal defaults |
| `db.py` | SQLite database (WAL mode) with tables for plans, outcomes, hourly logs, learning params, forecast tracking |
| `ha_api.py` | HTTP client for HA REST API -- reads sensors, writes inverter registers via Solarman |
| `metocean.py` | Fetches hourly weather forecasts from MetOcean API (MetService NZ data) |
| `models.py` | Dual solar forecast models, hourly consumption distribution, battery simulation |
| `planner.py` | Core optimizer: evaluates charging scenarios, picks SOC target, generates TOU slot config |
| `registers.py` | Encodes and writes TOU time/power/SOC/enable registers to Deye inverter via Modbus |
| `charging.py` | Real-time overnight charging adjustment -- shifts Slot 2 start and scales power to hit SOC target by 06:30 |
| `polling.py` | Hourly snapshot recording (SOC, grid, PV, load) and forecast-vs-actual tracking |
| `learning.py` | Nightly learning cycle: weather corrections, consumption patterns, temperature factors, SW efficiency |
| `dashboard.py` | Writes JSON status for HA dashboard cards and CLI `status`/`history` display |

### Dual Solar Forecast Models

Both models run in parallel with accuracy tracked hourly in the `forecast_tracking` table. A learned `preferred_solar_model` parameter (0=cloud, 1=radiation, threshold 0.5) selects which one drives planning.

- **Model A (cloud):** Applies per-hour weather condition and cloud-coverage corrections to Forecast.Solar's daily total. Each weather condition (sunny, partly cloudy, cloudy, rainy) has a learned correction factor.
- **Model B (radiation):** Converts MetOcean shortwave radiation forecasts (W/m²) directly into production using per-hour learned efficiency factors (`sw_efficiency_7` through `sw_efficiency_20`). Each hour has its own factor because panels face different directions -- morning sun favours east-facing panels while afternoon sun favours west-facing.

### Shortwave Efficiency Learning

The per-hour SW efficiency is learned nightly from the median ratio of actual PV output to forecast shortwave radiation. An adaptive learning rate resists large swings caused by transient cloud cover: small corrections (real efficiency drift) apply at up to 30%, while large deviations (cloud transients) are exponentially dampened down to ~3%. This prevents a single cloudy hour from distorting the learned efficiency.

### Battery Simulation

Hour-by-hour simulation through peak hours (7am--9pm) using:
- Bell-curve solar weights centred around 1pm
- Double-hump consumption weights (morning + evening peaks)
- Temperature-based consumption factor (4 bands: cold/cool/mild/warm)
- Weekend/weekday adjustment factor

The simulation catches intra-day timing issues (e.g., cloudy morning draining the battery before a sunny afternoon) that a simple daily energy balance would miss.

### Overnight Charging Strategy

- **Never charge before midnight** -- Slot 6 (21:00--00:00) held at reserve SOC with grid charge disabled
- **Deferred charging** -- Slot 1 (00:00--04:00) at reserve; hourly polling dynamically shifts Slot 2 start time so the battery reaches its target SOC by 06:30
- **Power scaling** -- if running behind schedule, charge power ramps up to 10 kW

### Forecast Freezing

At 7am (peak start), the hourly forecast is frozen into the `daily_plan` row. All daytime tracking compares actuals against this stable baseline rather than a continuously-updating forecast, giving meaningful Actual-vs-Forecast comparisons on the dashboard.

### Dashboard

- **Summary card** on the Home dashboard -- plan vs actuals table with correction factors
- **Detail view** (`/dashboard-home/solar-detail`) -- tap the summary card to open:
  - Hourly forecast table: both model predictions, actual production, consumption, forecast vs actual SOC
  - Learning parameters, model accuracy comparison, SW efficiency with today's actuals
- **History tab** (`/dashboard-home/solar-history`) -- daily plan-vs-actual outcomes
- Cards use `card_mod` with `ha-markdown$` selector for shadow DOM styling

### Schedule (via HA automations)

| When | Command | What it does |
|:--|:--|:--|
| Every hour | `poll` | Record metrics, track forecast models, adjust overnight charging |
| 21:00 | `record` | Record daily outcome, backup DB, run all learning rules |
| 21:05 | `optimize` | Calculate tomorrow's plan, write TOU registers to inverter |

### Deye Inverter Registers

TOU configuration is written via Solarman `write_multiple_holding_registers`:
- 6 time slots (registers 250--255), power limits (256--261), SOC targets (268--273), enable flags (274--279), master enable (248)
- Time encoding: `hour × 100 + minute` (decimal packed)
- Enable flags: 0=none, 1=grid charge, 2=gen charge, 3=both
- 0.5s delay between register writes to avoid bus contention

## Helper Script

The `./ha` script wraps common HA API calls:

```bash
./ha states [domain]          # List entity states
./ha state <entity_id>        # Full state of one entity
./ha services [domain]        # List available services
./ha call <service> [json]    # Call a service
./ha automations              # List all automations
./ha config                   # Show HA config
./ha api <METHOD> <endpoint>  # Raw API call
```

## Workflow

1. Pull latest config from HA (in case of UI edits)
2. Edit YAML locally
3. Push to HA via `scp`
4. Reload via REST API
5. Validate entity states and check logs
