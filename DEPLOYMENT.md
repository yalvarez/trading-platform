# Deployment Notes

## Telegram Session Sharing

- The project stores the Telethon session at `services/telegram_ingestor/telegram_ingestor.session`.
- `telegram_ingestor` mounts this file read-write so the session can be created from the host.
- `trade_orchestrator` mounts it read-only for potential future use (currently not actively consumed).

## Environment Variables

Ensure the following env vars exist in `.env`:
- `TG_API_ID`: Telegram API ID
- `TG_API_HASH`: Telegram API hash
- `TG_PHONE`: Phone number tied to Telegram account
- `REDIS_URL`: Redis connection URL (default: `redis://redis:6379/0`)
- `N8N_ACTION_API_KEY`: REQUIRED for `/mgmt/action` endpoint (fail-closed)
- `TRADE_API_KEY`: REQUIRED for trade_api (fail-closed)
- `N8N_INBOUND_WEBHOOK_URL`: n8n webhook for unrecognized signal text
- `N8N_EVENT_WEBHOOK_URL`: (optional) n8n webhook receiving the audit/Telegram event envelope
- `N8N_EVENT_WEBHOOK_TOKEN`: (optional) auth token for that webhook, sent as `X-N8N-Token`
- `CHANNEL_NAMES_JSON`: (optional) `chat_id` → channel name mapping used in event messages

> Every event is also appended to `data/audit_log.jsonl`, which `docker-compose.yml`
> bind-mounts to the host so it survives rebuilds. See
> `docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md`.

## Quick Docker Commands

**Rebuild and restart:**
```bash
docker compose up -d --build
```

**View trade_orchestrator logs:**
```bash
docker compose logs -f trade_orchestrator
```

**Check service status:**
```bash
docker compose ps
```

**Access a container:**
```bash
docker compose exec trade_orchestrator bash
```

**Stop everything:**
```bash
docker compose down
```

## Management API Testing

**Invoke /mgmt/action endpoint for close_now (from host):**
```bash
curl -X POST http://localhost:8200/mgmt/action \
  -H "X-N8N-Action-Key: $N8N_ACTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "close_now",
    "chat_id": "-1001234567890",
    "raw_text": "manual close from external n8n flow"
  }'
```
This closes every active trade group opened from that `chat_id`, and returns a per-group result list, e.g. `{"status": "completed", "results": [{"group_id": 5, "status": "closed"}]}`.

**Invoke /mgmt/action endpoint for signal_correction (with SL adjustment):**
```bash
curl -X POST http://localhost:8200/mgmt/action \
  -H "X-N8N-Action-Key: $N8N_ACTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "signal_correction",
    "chat_id": "-1001234567890",
    "raw_text": "false signal, adjust stop loss",
    "correction": {
      "field": "sl",
      "value": 2495.0
    }
  }'
```
This applies the correction to the most recent active group of that `chat_id` only, e.g. `{"status": "applied", "group_id": 5}`.

## Trade API Testing

**List all trades:**
```bash
curl -H "X-API-Key: $TRADE_API_KEY" http://localhost:8100/trades
```

**Get specific trade:**
```bash
curl -H "X-API-Key: $TRADE_API_KEY" http://localhost:8100/trades/12345
```

**Open a trade:**
```bash
curl -X POST http://localhost:8100/trades \
  -H "X-API-Key: $TRADE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XAUUSD",
    "direction": "BUY",
    "volume": 0.01,
    "sl": 2490.0,
    "tp": 2515.0
  }'
```

**Update trade (modify SL and TP):**
```bash
curl -X PATCH http://localhost:8100/trades/12345 \
  -H "X-API-Key: $TRADE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sl": 2492.0,
    "tp": 2520.0
  }'
```

**Close a trade:**
```bash
curl -X DELETE http://localhost:8100/trades/12345 \
  -H "X-API-Key: $TRADE_API_KEY"
```

## Architecture Overview

6 core services:
1. **redis**: Pub/sub for signals and management
2. **mt5_acct1**: MT5 terminal + RPyC server
3. **telegram_ingestor**: Reads Telegram → Redis raw_messages
4. **router_parser**: Parses signals (TradePulse only) → Redis signals or n8n webhook
5. **trade_orchestrator**: Opens dual-TP positions, manages BE/trailing, receives /mgmt/action decisions
6. **trade_api**: External REST API for trade control

The mechanical dual-TP model is now the only trading behavior — no more configurable trading_mode system.
