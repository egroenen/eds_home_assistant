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

A self-learning battery charging optimizer for the Deye hybrid inverter, located in the `solar_optimizer/` package. It controls TOU charging schedules via Solarman registers to minimise grid usage.

**Key features:**
- Dual solar forecast models (cloud-cover and shortwave radiation), accuracy tracked over time
- Per-hour shortwave efficiency learning -- accounts for panels facing different directions producing differently at different times of day
- Hour-by-hour battery simulation through peak hours with temperature-based consumption adjustments
- Deferred overnight charging -- dynamically shifts charge window to reach target SOC by 6:30 am
- Time-aware planning -- safe to run `optimize` at any time; before 06:30 plans for today, after plans for tomorrow; register writes deferred during overnight charging
- Forecast freezing at 7am -- hourly forecast locked into DB at peak start for stable daytime tracking against actuals
- Daily learning cycle that adjusts weather corrections, seasonal consumption, temperature factors, weekend/weekday patterns, and shortwave efficiency
- Dashboard with summary card, hourly detail view (Solar Detail tab), and historical plan-vs-actual table (Solar History tab)

**Schedule (via HA automations):**
- Hourly -- poll metrics, track forecast models against actuals, adjust overnight charging
- 21:00 -- record daily outcome, backup DB, run learning (including per-hour SW efficiency), write history
- 21:05 -- calculate tomorrow's plan, write TOU registers

**Weather data:**
- Primary: MetOcean API (MetService NZ)
- Fallback: HA weather entity (met.no)

**Radiation model:**

The radiation model converts MetOcean shortwave radiation forecasts (W/m²) directly into predicted production using per-hour learned efficiency factors (`sw_efficiency_7` through `sw_efficiency_20`). Each hour has its own factor because panels face different directions -- morning sun favours east-facing panels while afternoon sun favours west-facing. The efficiency is learned nightly from the median ratio of actual PV output to forecast shortwave, blended with a 0.3 learning rate. This replaces the original approach of proportionally distributing Forecast.Solar's daily total.

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
