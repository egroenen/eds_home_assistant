#!/bin/bash
# Home Assistant API helper script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/.env"

AUTH=(-H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json")

case "${1:-help}" in
  states)
    # List all states, optionally filtered by domain: ha states light
    DOMAIN="${2:-}"
    if [ -n "$DOMAIN" ]; then
      curl -s "$HASS_SERVER/api/states" "${AUTH[@]}" | python3 -c "
import json,sys
states=json.load(sys.stdin)
for s in sorted(states, key=lambda x:x['entity_id']):
    if s['entity_id'].startswith('$DOMAIN.'):
        name=s['attributes'].get('friendly_name','')
        print(f'{s[\"entity_id\"]:55s} {s[\"state\"]:15s} {name}')
"
    else
      curl -s "$HASS_SERVER/api/states" "${AUTH[@]}" | python3 -c "
import json,sys
states=json.load(sys.stdin)
for s in sorted(states, key=lambda x:x['entity_id']):
    name=s['attributes'].get('friendly_name','')
    print(f'{s[\"entity_id\"]:55s} {s[\"state\"]:15s} {name}')
"
    fi
    ;;
  state)
    # Get full state of an entity: ha state light.kitchen
    curl -s "$HASS_SERVER/api/states/$2" "${AUTH[@]}" | python3 -m json.tool
    ;;
  services)
    # List services, optionally filtered by domain: ha services light
    DOMAIN="${2:-}"
    if [ -n "$DOMAIN" ]; then
      curl -s "$HASS_SERVER/api/services" "${AUTH[@]}" | python3 -c "
import json,sys
for svc in json.load(sys.stdin):
    if svc['domain']=='$DOMAIN':
        for name in sorted(svc['services']):
            print(f'{svc[\"domain\"]}.{name}')
"
    else
      curl -s "$HASS_SERVER/api/services" "${AUTH[@]}" | python3 -c "
import json,sys
for svc in json.load(sys.stdin):
    for name in sorted(svc['services']):
        print(f'{svc[\"domain\"]}.{name}')
"
    fi
    ;;
  call)
    # Call a service: ha call light.turn_on '{"entity_id":"light.kitchen"}'
    DOMAIN=$(echo "$2" | cut -d. -f1)
    SERVICE=$(echo "$2" | cut -d. -f2)
    DATA="${3:-{\}}"
    curl -s -X POST "$HASS_SERVER/api/services/$DOMAIN/$SERVICE" "${AUTH[@]}" -d "$DATA" | python3 -m json.tool
    ;;
  automations)
    # List all automations
    curl -s "$HASS_SERVER/api/states" "${AUTH[@]}" | python3 -c "
import json,sys
states=json.load(sys.stdin)
for s in sorted(states, key=lambda x:x['entity_id']):
    if s['entity_id'].startswith('automation.'):
        name=s['attributes'].get('friendly_name','')
        print(f'{s[\"entity_id\"]:55s} {s[\"state\"]:15s} {name}')
"
    ;;
  config)
    # Get HA config
    curl -s "$HASS_SERVER/api/config" "${AUTH[@]}" | python3 -m json.tool
    ;;
  api)
    # Raw API call: ha api GET /api/states/light.kitchen
    METHOD="${2:-GET}"
    ENDPOINT="${3:-/api/}"
    DATA="${4:-}"
    if [ -n "$DATA" ]; then
      curl -s -X "$METHOD" "$HASS_SERVER$ENDPOINT" "${AUTH[@]}" -d "$DATA" | python3 -m json.tool
    else
      curl -s -X "$METHOD" "$HASS_SERVER$ENDPOINT" "${AUTH[@]}" | python3 -m json.tool
    fi
    ;;
  help|*)
    echo "Usage: ha <command> [args]"
    echo ""
    echo "Commands:"
    echo "  states [domain]         - List entity states (optionally filtered by domain)"
    echo "  state <entity_id>       - Get full state of an entity"
    echo "  services [domain]       - List available services"
    echo "  call <service> [json]   - Call a service"
    echo "  automations             - List all automations"
    echo "  config                  - Show HA configuration"
    echo "  api <METHOD> <endpoint> [json] - Raw API call"
    ;;
esac
