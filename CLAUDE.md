# Home Assistant Configuration Project

This directory contains local copies of the Home Assistant configuration files. Changes are edited locally, pushed to the HA VM via SSH, and reloaded via the REST API.

## Connection Details

- **HA URL:** `http://192.168.69.215:8123` (from WSL2; `homeassistant.local:8123` from Windows/LAN)
- **API token:** stored in `.env` file (source it or read `HASS_TOKEN` from it)
- **SSH:** `ssh -i ~/.ssh/id_ha root@192.168.69.215` — config lives at `/config/` on the VM

## Workflow for Making Changes

### 1. Edit locally
Edit the YAML files in this directory. The key files are:
- `configuration.yaml` — main config (sensors, templates, groups, integrations)
- `automations.yaml` — all automations
- `scenes.yaml` — saved scenes
- `scripts.yaml` — scripts (currently empty)
- `secrets.yaml` — secrets/credentials (DO NOT commit or share)
- `tts_phrases.yaml` — TTS phrase definitions

### 2. Push to HA
```bash
scp -i ~/.ssh/id_ha <filename> root@192.168.69.215:/config/
```

### 3. Reload configuration
Use the REST API to reload without restarting HA:
```bash
source .env
# Reload core config (configuration.yaml changes):
curl -s -X POST http://192.168.69.215:8123/api/services/homeassistant/reload_core_config \
  -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json"

# Reload automations:
curl -s -X POST http://192.168.69.215:8123/api/services/automation/reload \
  -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json"

# Reload scenes:
curl -s -X POST http://192.168.69.215:8123/api/services/scene/reload \
  -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json"

# Reload scripts:
curl -s -X POST http://192.168.69.215:8123/api/services/script/reload \
  -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json"

# For template sensor changes, a full config reload is needed:
curl -s -X POST http://192.168.69.215:8123/api/services/homeassistant/reload_all \
  -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json"
```

### 4. Validate changes worked
```bash
# Check a specific entity state:
curl -s http://192.168.69.215:8123/api/states/<entity_id> \
  -H "Authorization: Bearer $HASS_TOKEN" | python3 -m json.tool

# Check HA logs for errors:
ssh -i ~/.ssh/id_ha root@192.168.69.215 "tail -50 /config/home-assistant.log"
```

## Helper Script

The `./ha` script wraps common API calls:
```bash
./ha states [domain]          # List entity states (e.g., ./ha states light)
./ha state <entity_id>        # Full state of one entity
./ha services [domain]        # List available services
./ha call <service> [json]    # Call a service (e.g., ./ha call light.turn_on '{"entity_id":"light.kitchen"}')
./ha automations              # List all automations
./ha config                   # Show HA config
./ha api <METHOD> <endpoint>  # Raw API call
```

## Syncing Config from HA

To pull the latest config from HA (do this before editing if changes were made in the HA UI):
```bash
for f in configuration.yaml automations.yaml scripts.yaml scenes.yaml tts_phrases.yaml; do
  scp -i ~/.ssh/id_ha root@192.168.69.215:/config/$f .
done
```
**Note:** Do NOT pull `secrets.yaml` unnecessarily — it's already local.

## Solar Optimizer (`solar_optimizer.py`)

Self-learning battery charging optimizer for Deye hybrid inverter (15kWh battery, 10% reserve). Controls TOU charging via Solarman registers.

### Key Files
- `solar_optimizer.py` — main script (runs on HA at `/config/`)
- `solar_optimizer.db` — SQLite database (learning params, plans, outcomes, hourly logs, forecast tracking)
- `solar_optimizer_status.json` — dashboard data (read by command_line sensor every 5 min)
- `add_dashboard_card.py` — creates/updates the dashboard summary card and detail view

### Schedule (HA automations)
- **Hourly** (`time_pattern /1h`) — `solar_poll`: record metrics, track forecast models, adjust overnight charging
- **21:00** — `solar_record`: record daily outcome, backup DB, run learning
- **21:05** — `solar_optimize`: calculate tomorrow's plan, write TOU registers

### Charging Strategy
- **Never charge before midnight** — Slot 6 (21:00-00:00) at reserve, grid charge disabled
- **Defer charging** — Slot 1 (00:00-04:00) at reserve; hourly poll dynamically shifts Slot 2 start time so battery reaches target SOC by 06:59 (aligned with 07:00 hourly poll reading)
- **Power scaling** — poll can increase charge power up to 10kW if running late

### Weather Forecasts
- **Primary**: MetOcean API (MetService NZ data) — `forecast-v2.metoceanapi.com`
  - API key: stored in `.env` as `METOCEAN_API_KEY` (free plan)
  - Location: Christchurch Parklands (-43.505, 172.698)
  - Variables: `air.temperature.at-2m`, `cloud.cover`, `precipitation.rate`, `radiation.flux.downward.shortwave`
  - Temps returned in **Kelvin** (subtract 273.15). Times in **UTC** (add 13h for NZDT)
  - **Do not add unnecessary variables** — they cost money on the API plan
- **Fallback**: HA weather entity (`weather.forecast_home`, met.no)

### Dual Solar Forecast Models
Both run in parallel, accuracy tracked hourly in `forecast_tracking` table:
- **Model A (cloud)**: per-hour weather condition + cloud% correction × Forecast.Solar daily total
- **Model B (radiation)**: shortwave radiation W/m² proportional distribution of Forecast.Solar total
  - Post-sunset hours zeroed via `SOLAR_LAST_HOUR` monthly map — MetOcean reports diffuse SW radiation past sunset that panels cannot use
- `preferred_solar_model` param learned over time (0=cloud, 1=radiation, threshold 0.5)

### Battery Simulation
Hour-by-hour simulation through peak hours (7am-9pm) using:
- `HOURLY_SOLAR_WEIGHT` — bell curve centered ~1pm
- `HOURLY_CONSUMPTION_WEIGHT` — derived from observed hourly data (flatter midday, lighter edges)
- Temperature-based consumption factor (4 bands: cold/cool/mild/warm)
- Catches intra-day timing issues (e.g., cloudy morning draining battery before sunny afternoon)
- Binary search finds minimum starting SOC that keeps battery above `reserve_target` (20% = BATTERY_RESERVE_PCT + safety_margin) all day
- Daytime floor uses `reserve_target` (20%), NOT `OUTAGE_RESERVE_PCT` (30%) — the outage reserve only applies to overnight slots
- `hourly_soc` records SOC at the **start** of each hour (before energy flow), aligned with the :00 poll readings

### Dashboard
- **Summary card** on Home dashboard — plan vs actuals table with correction factors
- **Detail view** (`/dashboard-home/solar-detail`) — tap summary card to open
  - Hourly forecast table with both model predictions, consumption, battery SOC
  - Learning parameters, model accuracy comparison
- Card uses `card_mod` with `ha-markdown$` selector for shadow DOM styling
- `add_dashboard_card.py` handles create/update — run on HA after pushing changes

### Deye Inverter Registers
- TOU registers: time (250-255), power (256-261), SOC (268-273), enable (274-279), master (248)
- Time encoding: `hour * 100 + minute` (decimal packed)
- Enable: 0=none, 1=grid charge, 2=gen charge, 3=both
- Write via Solarman `write_multiple_holding_registers` service with 0.5s delay between writes

### Database Backups
Daily SQLite backup to `/config/backups/` before recording outcomes. Keeps last 7 days.

## Important Notes

- **Always sync before editing** — changes made in the HA UI (automations, scenes) write directly to the YAML on the VM. Pull latest first to avoid overwriting.
- **Validate YAML** before pushing — HA will fail to load broken YAML silently or log errors.
- **secrets.yaml** contains sensitive values. Never commit it to git.
- **`.env`** contains the API token. Never commit it to git.
- The HA instance runs **HA OS 2026.3.3** with Supervisor.
- Custom components: evnex, feedparser, hacs, solarman, waste_collection_schedule, xiaomi_home
- User is in **New Zealand** — metric units, NZD power pricing schedules matter for automations.
- **HA OS cron doesn't persist** across restarts — use HA automations for scheduled tasks, not cron.
