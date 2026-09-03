## Inicialización de Redis Streams

Si ves el error:

```
redis.exceptions.ResponseError: NOGROUP No such key 'raw_messages' or consumer group 'router_group' in XREADGROUP with GROUP option
```

Debes crear el stream y el grupo de consumidores en Redis antes de iniciar los servicios dependientes. Ejecuta:

```
docker exec -it atp-redis redis-cli XGROUP CREATE raw_messages router_group $ MKSTREAM
```

Esto crea el stream `raw_messages` y el grupo `router_group` si no existen.

# auto-trading-platform

**TradePulse-only dual-TP trading architecture** running on Docker with:
- Telegram message ingestion via Telethon
- TradePulse signal parsing → dual MT5 position opening (TP1 + runner legs)
- Mechanical breakeven + proportional trailing stop management
- External trade control via n8n/Ollama exception flow
- Trade notifications via n8n webhook (optional)

## Quick Start

### Prerequisites
- Docker + Docker Compose (Linux amd64)
- Telegram credentials (api_id, api_hash, phone number)
- One active MT5 account (multi-account structurally supported, single-account in use)

### Setup

1. Copy `.env.example` to `.env` and fill in:
   - Telegram credentials: `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`
   - MT5 account config in `ACCOUNTS_JSON` (single entry for now)
   - Security keys: `N8N_ACTION_API_KEY`, `TRADE_API_KEY` (both **REQUIRED** — services will not start without them)
   - Optional: `N8N_WEBHOOK_URL` + `N8N_WEBHOOK_TOKEN` for trade notifications

2. Launch:
```bash
docker compose up -d --build
```

3. Check logs:
```bash
docker compose logs -f trade_orchestrator
```

## Architecture

### 6 Core Services

1. **redis**: Pub/sub messaging (raw_messages, signals, management streams)
2. **mt5_acct1**: MT5 terminal + RPyC server (port 8001)
3. **telegram_ingestor**: Reads all subscribed Telegram channels → publishes raw messages to Redis
4. **router_parser**: Parses raw messages with TradePulse parser only; non-signal text → n8n inbound webhook
5. **trade_orchestrator**: Opens dual-TP positions per signal, manages mechanical BE/trailing, receives external mgmt decisions via `/mgmt/action` endpoint (port 8200)
6. **trade_api**: External trade control (CRUD /trades endpoint, port 8100)

### Signal Flow

```
Telegram Channel
    ↓
[telegram_ingestor]
    ↓ (raw message)
Redis: raw_messages stream
    ↓
[router_parser]
    ↓
    ├─ TradePulse parser → match ✓
    │  ↓
    │  Redis: signals stream (symbol, direction, entry_range, sl, tps, hint_price, etc)
    │
    └─ TradePulse parser → no match
       ↓
       POST to N8N_INBOUND_WEBHOOK_URL (for external n8n/Ollama processing)
    ↓
[trade_orchestrator]
    ↓
    └─ Per signal: open 2 MT5 positions (group_id = ticket of first)
       ├─ tp1_leg: closes at TP1, BE+trailing on remainder
       └─ runner_leg: uncapped proportional trailing, no fixed TP close
    ↓
Trade Events (opened, TP hit, etc)
    ├─ POST to N8N_WEBHOOK_URL (if configured)
    └─ In-memory group state tracking
```

### Trade Opening: Dual-TP Model

Every entry signal opens **two MT5 positions** with the same group_id:

- **TP1 leg** (tp1_leg=true): closes volume at TP1 price; BE+trailing on remainder
- **Runner leg** (tp1_leg=false): no fixed TP close; proportional trailing from entry

**Breakeven Logic (TP1 leg only):**
- When TP1 is hit and 70% closed: move SL to entry + BREAKEVEN_OFFSET_PIPS (mechanical, no configurability per-account)

**Trailing Logic (both legs, mechanical loop):**
- Runner leg: trail from entry with proportional drawdown (50% of peak gain)
- TP1 remainder: trail from peak price with fixed pips retrace (configurable via env)
- Updates every 2+ seconds, fail-silent on price/volume errors

No more `general` / `be_pips` / `be_pnl` / `reentry` trading_mode system — dual-TP is the **only** behavior.

## Configuration

### Account Setup (ACCOUNTS_JSON)

Single active account for now; list structure enables future multi-account (but only one should have `active: true`):

```json
ACCOUNTS_JSON=[
  {
    "name": "My MT5 Account",
    "host": "mt5_acct1",
    "port": 8001,
    "active": true,
    "fixed_lot": 0.01,
    "chat_id": 1234567890
  }
]
```

**Fields:**
- `name`: display name for logs/notifications
- `host`: Docker service name or IP
- `port`: RPyC server port
- `active`: only one account can be active
- `fixed_lot`: volume per trade (lot size)
- `chat_id`: Telegram chat ID for notifications (if using `TelegramNotifier`)

### Environment Variables

**Telegram API:**
- `TG_API_ID`: from https://my.telegram.org
- `TG_API_HASH`: from https://my.telegram.org
- `TG_PHONE`: phone number tied to the account

**Redis:**
- `REDIS_URL`: default `redis://redis:6379/0`

**Trading:**
- `TRADING_WINDOWS`: HH:MM-HH:MM format (e.g., `06:00-22:00`); set to `00:00-23:59` for 24/7
- `DEFAULT_SL_XAUUSD_PIPS`: fallback SL width for gold (e.g., 60)
- `DEFAULT_SL_PIPS`: fallback SL width for other symbols (e.g., 100)
- `ENTRY_WAIT_SECONDS`: max time to wait for price to enter range (e.g., 90)
- `ENTRY_POLL_MS`: poll interval while waiting (e.g., 200 ms)
- `DEDUP_TTL_SECONDS`: duplicate signal detection window (e.g., 120 seconds)

**Signal Processing:**
- `N8N_INBOUND_WEBHOOK_URL`: n8n webhook URL for text not recognized as signals
- `N8N_WEBHOOK_URL`: (optional) n8n webhook for trade event notifications
- `N8N_WEBHOOK_TOKEN`: (optional) auth token for trade notification webhook

**Management & Trade APIs:**
- `N8N_ACTION_API_KEY`: API key for trade_orchestrator `/mgmt/action` endpoint (REQUIRED)
- `MGMT_API_PORT`: port for `/mgmt/action` (default 8200)
- `TRADE_API_KEY`: API key for trade_api endpoints (REQUIRED)

**MT5 VNC Web UI:**
- `MT5_WEB_USER`: VNC web UI username
- `MT5_WEB_PASS`: VNC web UI password

## API Endpoints

### trade_orchestrator Management API

**Endpoint:** `POST /mgmt/action` (port 8200)

**Authentication:** Header `X-N8N-Action-Key: <N8N_ACTION_API_KEY>`

**Request body:**
```json
{
  "action": "close_now|move_sl_be_now|note_sl_hit|signal_correction|ignore",
  "symbol": "XAUUSD",
  "group_id": 12345,
  "notes": "optional notes"
}
```

**Actions:**
- `close_now`: close all positions in this group immediately
- `move_sl_be_now`: move SL to breakeven immediately
- `note_sl_hit`: record that SL was hit (for external tracking)
- `signal_correction`: mark signal as false alarm (don't trade again)
- `ignore`: acknowledge but take no action

**Response:**
```json
{
  "status": "ok|error",
  "message": "..."
}
```

### trade_api External Trade Control

**Base URL:** `http://trade_api:8100` (or host IP if exposed)

**Authentication:** Header `X-API-Key: <TRADE_API_KEY>`

#### List All Trades
```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8100/trades
```

#### Get Trade by Ticket
```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8100/trades/12345
```

#### Open Trade
```bash
curl -X POST http://localhost:8100/trades \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XAUUSD",
    "direction": "BUY",
    "entry_price": 2500.50,
    "sl_price": 2490.00,
    "tp_prices": [2515.00, 2530.00],
    "lot": 0.01,
    "group_id": "sig_123"
  }'
```

#### Update Trade (SL/TP)
```bash
curl -X PATCH http://localhost:8100/trades/12345 \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sl_price": 2492.00, "tp_prices": [2520.00, 2535.00]}'
```

#### Close Trade
```bash
curl -X DELETE http://localhost:8100/trades/12345 \
  -H "X-API-Key: YOUR_KEY"
```

## Signal Parsing

The **TradePulse parser only** is active. It recognizes signal patterns like:

```
ORO BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530
```

or with shorthand:

```
Compra ORO ahora @2500
```

Text that doesn't match TradePulse patterns is forwarded to `N8N_INBOUND_WEBHOOK_URL` for processing by external n8n/Ollama flows. This includes:
- Management commands (close, partial, adjust SL/TP)
- Other provider formats
- Noise

## Notifications

### n8n Webhook (Trade Events)

If `N8N_WEBHOOK_URL` is set, all trade events are POSTed:

```json
{
  "event": "trade_opened|tp_hit|partial_close|trailing_activated|...",
  "account_name": "My MT5 Account",
  "symbol": "XAUUSD",
  "ticket": 12345,
  "group_id": 12345,
  "direction": "BUY",
  "entry_price": 2500.50,
  "current_price": 2510.00,
  "sl_price": 2490.00,
  "tp_prices": [2515.00, 2530.00],
  "lot": 0.01,
  "timestamp": "2026-09-03T15:30:45Z"
}
```

## Testing

Run the full test suite:

```bash
python -m pytest -m "not integration" -q
```

Run specific service tests:

```bash
pytest services/router_parser/test_router_parser.py -v
pytest services/trade_orchestrator/test_orchestrator.py -v
pytest services/trade_api/test_trade_api.py -v
```

## Docker Commands

**Restart all services:**
```bash
docker compose down && docker compose up -d --build
```

**View running services:**
```bash
docker compose ps
```

**Check service logs:**
```bash
docker compose logs -f <service_name>
# e.g.: docker compose logs -f trade_orchestrator
```

**Execute into container:**
```bash
docker compose exec <service_name> bash
```

**Stop everything:**
```bash
docker compose down
```

## Troubleshooting

**Services failing to start:**
- Check `.env` has `N8N_ACTION_API_KEY` and `TRADE_API_KEY` set (both are REQUIRED)
- Run `docker compose logs <service_name>` to see error details

**MT5 connection issues:**
- Ensure MT5 container is healthy: `docker compose ps`
- Check account config in `ACCOUNTS_JSON` (host/port must match docker-compose.yml)

**No signals being parsed:**
- Verify Telegram channels are subscribed (telegram_ingestor logs)
- Check that signal text matches TradePulse pattern
- Non-matching text will go to `N8N_INBOUND_WEBHOOK_URL` (if set)

**Trailing/BE not working:**
- Verify mechanical loop is running (trade_orchestrator logs should show tick messages every 2s)
- Check price updates from MT5 (look for "price updated" in logs)

## Project Structure

```
.
├── .env.example                                    # Config template (update with your settings)
├── docker-compose.yml                              # 6 services: redis, mt5_acct1, telegram_ingestor, router_parser, trade_orchestrator, trade_api
├── services/
│   ├── common/
│   │   ├── config.py                               # Env var loading
│   │   ├── mt5_client.py                            # Shared MT5 RPyC connection
│   │   └── ...
│   ├── telegram_ingestor/
│   │   ├── app.py                                  # Reads Telegram channels → Redis raw_messages
│   │   ├── Dockerfile
│   │   └── ...
│   ├── router_parser/
│   │   ├── app.py                                  # TradePulse parser → signals or n8n webhook
│   │   ├── tradepulse_filters.py                   # TradePulse parsing logic
│   │   ├── Dockerfile
│   │   └── ...
│   ├── trade_orchestrator/
│   │   ├── app.py                                  # Main orchestrator + /mgmt/action endpoint
│   │   ├── trade_manager.py                        # Group-based position management
│   │   ├── Dockerfile
│   │   └── ...
│   └── trade_api/
│       ├── app.py                                  # REST API for external trade control
│       ├── Dockerfile
│       └── ...
├── tests/
│   ├── test_orchestrator.py
│   ├── test_router_parser.py
│   └── ...
├── DEPLOYMENT.md                                   # Deployment & session sharing notes
└── IMPLEMENTATION_SUMMARY.md                       # Historical summary (legacy)
```

## Notes

- **Telegram session:** Persisted at `services/telegram_ingestor/telegram_ingestor.session` and shared read-only with trade_orchestrator for potential future use.
- **No Postgres/backend_admin:** Config is environment-variable only; no database backend.
- **No Prometheus/monitoring stack:** Removed; use external n8n webhooks for event-driven alerting.
- **Auth is fail-closed:** Both `N8N_ACTION_API_KEY` and `TRADE_API_KEY` are required at startup; services refuse to start without them.
