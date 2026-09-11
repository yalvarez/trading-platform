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
   - Optional: `N8N_EVENT_WEBHOOK_URL` + `N8N_EVENT_WEBHOOK_TOKEN` for the audit log / Telegram event feed

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
    └─ Per signal: open 2 MT5 positions (group_id = an incrementing internal counter, not a ticket)
       ├─ tp1_leg: closes at TP1, BE+trailing on remainder
       └─ runner_leg: uncapped proportional trailing, no fixed TP close
    ↓
Trade Events (opened, TP hit, etc)
    ├─ Append to data/audit_log.jsonl (always — primary source of truth)
    ├─ POST envelope to N8N_EVENT_WEBHOOK_URL (if configured)
    │    └─ n8n branches on `channel`: "audit" → Data Table, "both" → Data Table + Telegram
    └─ In-memory group state tracking
```

### Trade Opening: Dual-TP Model

Every entry signal opens **two MT5 positions** with the same group_id:

- **TP1 leg**: closes volume at TP1 price; the remaining open portion becomes subject to mechanical management
- **Runner leg**: no fixed TP close; remains open and follows the mechanical trailing rules

**Breakeven Logic:**
- Triggered when the **TP1 leg closes** (TP1 price is hit and volume closes at that level)
- The runner leg's SL is moved to **exactly** the entry_price (no offset, no configurability)
- After BE is applied, trailing logic engages on the runner

**Trailing Logic (mechanical loop, runner leg only):**
- Runs continuously on a ~100ms poll loop (fail-silent on price/volume errors)
- **Formula:** `unit = tp2_price - tp1_price` (computed once per group; for SELL, reversed)
- `peak_multiple` tracks the highest ratio ever observed: `(current_price - tp1_price) / unit` (only increases, never decreases)
- **SL recomputation:** `new_sl = entry_price + peak_multiple * (tp1_price - entry_price)` (anchored on entry/BE, offset scaled by the entry→tp1 distance — see notes below)
- This formula has **no cap** — if price runs far past tp2, peak_multiple can exceed 1.0 and the SL keeps trailing proportionally
- Example: if entry→tp1=15 pips and peak_multiple reaches 2.0, the SL trails at entry + 2.0 * 15 = entry + 30 pips
- A guard compares every candidate SL against the SL already live in MT5 before sending it (`pos.sl`), and skips the update if the candidate would be worse — same pattern `update_group_signal` and `move_sl_be_now` already use.
- **Offset scale revised 2026-09-09:** originally scaled by `unit` (`new_sl = entry_price + (peak_multiple * unit) / 3`), which coupled two unrelated distances — how far the SL has to travel to reach tp1_price (entry→tp1, driven by the signal's risk) vs. how far tp1 and tp2 are from each other (unit, an independent scale decision). Real case (group 61, live): entry→tp1=34.87pts, unit=40pts — at peak=1.0 (price exactly at tp2) the old formula's SL had only covered unit/3=13.3pts of that 34.87, landing 21.5pts short of tp1 instead of near it as intuitively expected. Scaling by (tp1_price - entry_price) instead makes the SL land exactly on tp1_price at peak=1.0, regardless of how unit relates to that distance. This also meant the live-SL guard above became necessary: `update_group_signal`'s peak_multiple rescale (for signal_correction) is still correct for gating (`multiple > peak_multiple`), but a large jump in unit can make a newly-valid multiple map, via entry→tp1 (which the rescale doesn't touch), to a candidate below the SL already live.
- **Anchor revised 2026-09-08:** originally anchored on `tp1_price` instead of `entry_price`. Since `peak_multiple` starts near 0 right after crossing TP1, that version put the SL only 0-3 points from the live price at that moment — tighter than BE's own margin — so a normal pullback right after TP1 could stop the runner out almost simultaneously with the tp1 leg closing (confirmed in production). Anchoring on `entry_price` makes the SL equal to BE exactly at `peak=0`, then rises from there.
- **Notification note (2026-09-09):** `trailing_updated` is now only logged (`log.info`), not sent through `notify_trade_event` — a single live trade can produce 100+ trailing ticks, which was too noisy for n8n/Telegram.

**TP2 Partial Close (added 2026-09-08):**
- The first time the runner's live price reaches `tp2_price`, 50% of its current volume is closed via `partial_close` — once per group (`tp2_partial_applied` flag, same pattern as `be_applied`)
- The remaining 50% keeps trailing exactly as before — `tp2_price` is NOT a new anchor, `peak_multiple`/SL are untouched by this close
- Simple trigger: `price >= tp2_price` (BUY) / `price <= tp2_price` (SELL), no confirmation threshold
- Rationale: `tp2_price` was previously decorative for the runner (only defined the trailing's scale, never took profit there). Simulations show locking half at TP2 systematically wins when price reverts after touching it, and only costs (bounded to half the volume) when price keeps running without a pullback.

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
- `ENTRY_WAIT_SECONDS`: max time to wait for price to enter range on a full signal that carries one (e.g., 90; gold always uses a fixed 5s window regardless of this setting)
- `ENTRY_POLL_MS`: poll interval while waiting (e.g., 200 ms; gold always polls at 100ms)
- `TOLERANCE_PIPS`: tolerance in pips added to the entry-range edges (e.g., 30)
- `DEDUP_TTL_SECONDS`: duplicate signal detection window (e.g., 120 seconds)

**Signal Processing:**
- `N8N_INBOUND_WEBHOOK_URL`: n8n webhook URL for text not recognized as signals

**Audit Log & Event Notifications:**
- `N8N_EVENT_WEBHOOK_URL`: (optional) n8n webhook receiving the full event envelope
- `N8N_EVENT_WEBHOOK_TOKEN`: (optional) auth token, sent as the `X-N8N-Token` header
- `CHANNEL_NAMES_JSON`: `chat_id` → human-readable channel name mapping used in messages

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

**Request body (all actions except `signal_correction`):**
```json
{
  "action": "close_now",
  "chat_id": "-1001234567890",
  "raw_text": "manual close from external flow"
}
```

**Request body for `signal_correction` action (with correction):**
```json
{
  "action": "signal_correction",
  "chat_id": "-1001234567890",
  "raw_text": "false signal detected, adjust SL",
  "correction": {
    "field": "sl",
    "value": 2495.0
  }
}
```

**Actions:**
- `close_now`: close all positions in every active group opened from this chat immediately
- `move_sl_be_now`: move the runner leg's SL to breakeven (entry price) for every active group of this chat
- `note_sl_hit`: record that SL was hit (for external tracking; no position changes)
- `signal_correction`: apply correction to the most recent active group of this chat (e.g., adjust SL/TP); requires `correction` object with `field` ("sl", "tp1", or "tp2") and `value` (float)
- `ignore`: acknowledge but take no action

**Note:** `group_id` is **not** supplied by the caller — the endpoint resolves ALL active trade groups opened from the same Telegram chat/channel that sent the management message (`chat_id`) server-side, not just one group or one symbol. `raw_text` is **required** on all requests.

**Response for `close_now` and `move_sl_be_now`** (a list of per-group results, since a chat can have multiple active groups):
```json
{
  "status": "completed",
  "results": [
    {"group_id": 5, "status": "closed"},
    {"group_id": 7, "status": "failed", "reason": "partial_close_rejected"}
  ]
}
```

**Response for `note_sl_hit`** (note-only; plural `group_ids`, no MT5 changes):
```json
{
  "status": "noted",
  "group_ids": [5, 7]
}
```

**Response for `signal_correction`** (targets only the most recent active group of the chat):
```json
{
  "status": "applied",
  "group_id": 7
}
```

### trade_api External Trade Control

**Base URL:** `http://trade_api:8100` (or host IP if exposed)

**Authentication:** Header `X-API-Key: <TRADE_API_KEY>`

#### List All Trades
```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8100/trades
```

Response:
```json
[
  {
    "ticket": 12345,
    "symbol": "XAUUSD",
    "direction": "BUY",
    "volume": 0.01,
    "sl": 2490.0,
    "tp": 2515.0
  }
]
```

#### Get Trade by Ticket
```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8100/trades/12345
```

Response:
```json
{
  "ticket": 12345,
  "symbol": "XAUUSD",
  "direction": "BUY",
  "volume": 0.01,
  "sl": 2490.0,
  "tp": 2515.0
}
```

#### Open Trade
```bash
curl -X POST http://localhost:8100/trades \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XAUUSD",
    "direction": "BUY",
    "volume": 0.01,
    "sl": 2490.0,
    "tp": 2515.0
  }'
```

Response:
```json
{
  "ticket": 12345,
  "symbol": "XAUUSD",
  "direction": "BUY",
  "volume": 0.01,
  "sl": 2490.0,
  "tp": 2515.0
}
```

#### Update Trade (SL/TP)
```bash
curl -X PATCH http://localhost:8100/trades/12345 \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sl": 2492.0, "tp": 2520.0}'
```

Note: Both `sl` and `tp` are optional; include only the fields you want to update.

#### Close Trade
```bash
curl -X DELETE http://localhost:8100/trades/12345 \
  -H "X-API-Key: YOUR_KEY"
```

Response:
```json
{
  "status": "closed",
  "ticket": 12345
}
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

### Audit Log + n8n Event Webhook

Every trade lifecycle event goes through a single `EventBus`, which does two things:

1. **Appends the event to `data/audit_log.jsonl`** — always, synchronously, before anything
   else. This local JSONL file is the primary source of truth and the safety net that survives
   a Redis or n8n outage. It is bind-mounted out of the container by `docker-compose.yml`, so
   it persists across rebuilds.
2. **POSTs the same envelope to `N8N_EVENT_WEBHOOK_URL`** (if configured), with
   `N8N_EVENT_WEBHOOK_TOKEN` sent as the `X-N8N-Token` header. Failed deliveries are retried by
   a background worker.

Each event is one envelope with a fixed top-level shape:

```json
{
  "event_id": "0f9c...",
  "event_type": "group_opened",
  "channel": "both",
  "timestamp": "2026-09-10T14:03:11.482Z",
  "message": "🟢 APERTURA — Canal: Oro Premium (grupo 12345)\n...",
  "payload": { "group_id": 12345, "symbol": "XAUUSD", "direction": "BUY", "...": "..." }
}
```

`channel` is the routing decision and is a sibling of `payload`, not nested inside it:

- `"audit"` — n8n writes it to the Data Table only.
- `"both"` — n8n writes it to the Data Table **and** sends `message` verbatim to Telegram.

`message` is pre-rendered Spanish text (built in
`services/trade_orchestrator/event_messages.py`) and is meant to be forwarded as-is — n8n
does no formatting of its own. `payload` carries the structured per-event fields and varies by
`event_type`; see the `_notify(...)` call sites in `trade_manager.py` for the authoritative
field list per event.

Full design, including the event catalogue and which events are `both` vs `audit`:
`docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md`.

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
