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

## Important Notes

- **Always sync before editing** — changes made in the HA UI (automations, scenes) write directly to the YAML on the VM. Pull latest first to avoid overwriting.
- **Validate YAML** before pushing — HA will fail to load broken YAML silently or log errors.
- **secrets.yaml** contains sensitive values. Never commit it to git.
- **`.env`** contains the API token. Never commit it to git.
- The HA instance runs **HA OS 2026.3.3** with Supervisor.
- Custom components: evnex, feedparser, hacs, solarman, waste_collection_schedule, xiaomi_home
- User is in **New Zealand** — metric units, NZD power pricing schedules matter for automations.
