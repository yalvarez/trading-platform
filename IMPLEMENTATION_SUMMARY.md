# 🚀 Advanced Trading Platform - Implementation Summary

**Current Architecture:** TradePulse-only dual-TP trading system with n8n/Ollama exception handling

---

## ✨ System Overview

A containerized trading automation platform that:
- Ingests Telegram messages 24/7
- Parses TradePulse-format signals only
- Opens dual MT5 positions (TP1 + runner legs) per signal
- Applies mechanical breakeven + proportional trailing
- Accepts external trade management decisions via HTTP API
- Posts trade events to n8n webhooks (optional)
- Exposes REST API for external trade control

---

## 🏗️ **6 Core Services**

### 1. redis
- Pub/sub messaging engine
- Streams: `raw_messages`, `signals`, management actions
- Deduplication cache (TTL-based)

### 2. mt5_acct1
- MT5 terminal running on Docker with VNC web UI
- RPyC server (port 8001) for remote order management

### 3. telegram_ingestor
- Reads all subscribed Telegram channels
- Publishes raw text to Redis `raw_messages` stream
- Telethon-based async client

### 4. router_parser
- Consumes Redis `raw_messages`
- **TradePulse parser only** — recognizes signal patterns
- Unrecognized text → POST to `N8N_INBOUND_WEBHOOK_URL`
- Signals → Redis `signals` stream with structured fields:
  - `symbol`, `direction`, `entry_range`, `sl`, `tps`
  - `hint_price` (for "fast" signals)
  - `provider_tag` (always "TradePulse")

### 5. trade_orchestrator
- Consumes Redis `signals` stream
- Opens **2 MT5 positions per signal** (same group_id):
  - `tp1_leg`: closes volume at TP1, applies BE+trailing to remainder
  - `runner_leg`: no fixed TP close, proportional trailing from entry
- Mechanical management loop (every 2s):
  - Checks price updates via MT5 connection
  - Applies trailing stop logic (proportional drawdown)
  - Emits trade events (optional: POST to `N8N_WEBHOOK_URL`)
- HTTP API `/mgmt/action` (port 8200):
  - Receives: `close_now`, `move_sl_be_now`, `note_sl_hit`, `signal_correction`, `ignore`
  - Auth: `X-N8N-Action-Key: <N8N_ACTION_API_KEY>`
  - Expected latency: <1s for in-memory group lookup

### 6. trade_api
- REST API (port 8100) for external trade control
- CRUD operations: list, get, create, update, delete trades
- Auth: `X-API-Key: <TRADE_API_KEY>`
- Direct MT5Client connection (independent of trade_orchestrator state)

---

## 💰 **Dual-TP Position Management**

Every signal entry opens exactly **2 MT5 positions**:

```
Signal: XAUUSD BUY, Entry 2500, SL 2490, TP1 2515, TP2 2530, Lot 0.01
  ↓
Position 1 (ticket=12345): leg="tp1", lot=0.01
  - Closes volume at TP1 (2515) — no manual action needed
  - When TP1 closes, triggers BE on the runner leg

Position 2 (ticket=12346): leg="runner", lot=0.01, group_id=12345
  - No fixed TP close
  - After BE is applied, follows proportional trailing formula
  - Expected close: via trailing as price advances and retraces
```

**Group ID:** Both positions share the first ticket's ID for coordinated management.

**Breakeven Logic (runner leg only):**
- Triggered when the **TP1 leg closes** (TP1 is hit and the TP1 position fully closes at TP1 price)
- Runner leg's SL moves to **exactly** `entry_price` (no offset, no percentage)
- This happens once per group, automatically
- After BE, the trailing formula takes over

**Trailing Logic (mechanical loop, runner leg only, after BE is applied):**
- Runs continuously every 2+ seconds (fail-silent on MT5 connection/price errors)
- **Formula:**
  - `unit = tp2_price - tp1_price` (BUY); reversed for SELL
  - `current_advance = current_price - tp1_price` (BUY); reversed for SELL
  - `multiple = current_advance / unit` (ratio of how many "units" past tp1 the price has moved)
  - `peak_multiple = max(peak_multiple, multiple)` (only increases, never decreases)
  - **New SL:** `new_sl = tp1_price + (peak_multiple * unit) / 3`
- Example: if tp1=2515, tp2=2530 (unit=15), and price hits 2545 (multiple=2.0), then SL trails at 2515 + (2.0 * 15) / 3 = 2525 pips
- **No cap:** If price runs far past tp2, peak_multiple can exceed 1.0 and the SL keeps trailing proportionally

---

## 🔐 **Signal Processing**

### TradePulse Parser

Recognizes patterns like:
```
ORO BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530
ORO SCALP BUY Entry: 2500, SL: 2495, TP1: 2505, TP2: 2510
Compra ORO ahora @2500       (fast pattern, auto-derives SL/TPs)
```

### Unrecognized Text

Forwarded to `N8N_INBOUND_WEBHOOK_URL` as JSON:
```json
{
  "text": "original message",
  "chat_id": -5250557024,
  "message_id": 12345,
  "timestamp": "2026-09-03T15:30:45Z"
}
```

External n8n/Ollama flow can:
- Classify as management command (adjust SL/TP, close, etc)
- Classify as other provider format (forward elsewhere)
- Classify as noise (discard)
- POST decision to `/mgmt/action` if management

---

## 🔄 **Management API Flow**

```
n8n/Ollama Flow (external)
  ↓ (receives unrecognized text via N8N_INBOUND_WEBHOOK_URL)
  ↓ (processes via LLM, determines action)
  ↓ POST to /mgmt/action with:
    {
      "action": "close_now" | "move_sl_be_now" | "note_sl_hit" | "signal_correction" | "ignore",
      "symbol": "XAUUSD",
      "raw_text": "the original channel message text",
      "correction": {"field": "sl" | "tp1" | "tp2", "value": 2495.0}  // only for signal_correction
    }
  ↓
[trade_orchestrator]
  ↓ (validates auth X-N8N-Action-Key header)
  ↓ (resolves active group for symbol server-side — caller does NOT supply group_id)
  ↓ (applies action: close all positions, move SL, adjust TP, etc)
  ↓
Response: {"status": "closed", "group_id": 12345}
```

**Auth is fail-closed:** Service refuses to start if `N8N_ACTION_API_KEY` is unset.

**Key differences from naive API:**
- `group_id` is **resolved server-side** by looking up the most recent active group for the symbol
- `raw_text` is **required** on every request (audit trail for external decisions)
- Only `signal_correction` action has a `correction` object; other actions ignore it

---

## 📱 **Trade Notifications**

If `N8N_WEBHOOK_URL` is configured, all trade events are POSTed as JSON:

```json
{
  "event": "trade_opened",
  "account_name": "My MT5 Account",
  "symbol": "XAUUSD",
  "ticket": 12345,
  "group_id": 12345,
  "direction": "BUY",
  "entry_price": 2500.50,
  "sl_price": 2490.00,
  "tp_prices": [2515.00, 2530.00],
  "lot": 0.01,
  "timestamp": "2026-09-03T15:30:45Z"
}
```

Optional token: `N8N_WEBHOOK_TOKEN` in Authorization header.

---

## 🌐 **Trade API Endpoints**

**Base:** `http://trade_api:8100`
**Auth:** `X-API-Key: <TRADE_API_KEY>` (fail-closed)

### List All Trades
```bash
GET /trades
```
Response: `[{ticket, symbol, direction, volume, sl, tp}, ...]`

### Get Specific Trade
```bash
GET /trades/{ticket}
```
Response: `{ticket, symbol, direction, volume, sl, tp}`

### Open Trade
```bash
POST /trades
{
  "symbol": "XAUUSD",
  "direction": "BUY",
  "volume": 0.01,
  "sl": 2490.0,
  "tp": 2515.0
}
```
Response: `{ticket, symbol, direction, volume, sl, tp}`

**Notes:**
- `entry_price` is NOT supplied by caller — computed from live MT5 tick price
- `tp` is a single optional float, NOT an array
- `volume` is the lot size (NOT `lot`)
- `sl` and `tp` are single prices (NOT `sl_price`, `tp_prices`)
- `group_id` is NOT used — trade_api operates independently of trade_orchestrator's group state

### Update Trade (Modify SL/TP)
```bash
PATCH /trades/{ticket}
{
  "sl": 2492.0,
  "tp": 2520.0
}
```
Response: `{ticket, symbol, direction, volume, sl, tp}`

**Notes:**
- Both `sl` and `tp` are optional; include only what you want to change
- Single float values (NOT arrays)

### Close Trade
```bash
DELETE /trades/{ticket}
```
Response: `{"status": "closed", "ticket": ticket}`

---

## ⚙️ **Environment Configuration**

### Required Variables

```dotenv
# FAIL-CLOSED: Service will not start without these
N8N_ACTION_API_KEY=<32-char hex key>
TRADE_API_KEY=<32-char hex key>

# Telegram
TG_API_ID=<your_api_id>
TG_API_HASH=<your_api_hash>
TG_PHONE=+1234567890

# Redis
REDIS_URL=redis://redis:6379/0

# Webhooks
N8N_INBOUND_WEBHOOK_URL=https://your-n8n-instance.example.com/webhook/inbound
N8N_WEBHOOK_URL=https://your-n8n-instance.example.com/webhook/trades  (optional)
N8N_WEBHOOK_TOKEN=<optional>

# Trading
TRADING_WINDOWS=00:00-23:59
DEFAULT_SL_XAUUSD_PIPS=60
DEFAULT_SL_PIPS=100
ENTRY_WAIT_SECONDS=90
DEDUP_TTL_SECONDS=120

# MT5 Account
ACCOUNTS_JSON=[{"name":"Main","host":"mt5_acct1","port":8001,"active":true,"fixed_lot":0.01,"chat_id":1234567890}]
```

---

## 📦 **What's Removed**

The following have been **deleted entirely** (no longer in use):

- ❌ Postgres database + config_db.py
- ❌ backend_admin service (REST admin UI)
- ❌ market_data service (historical data collection)
- ❌ Prometheus + AlertManager + Loki (monitoring stack)
- ❌ notify_api.py (dedicated notification service)
- ❌ test_notify.py (Telegram-only test script)
- ❌ CHANNELS_CONFIG_JSON (telegram_ingestor now reads all subscribed channels)
- ❌ TelegramNotifierAdapter (no longer active; n8n webhooks replaced it)
- ❌ Trading mode system (`general`, `be_pips`, `be_pnl`, `reentry`)
- ❌ Non-TradePulse parsers (GB_FAST, GB_LONG, GB_SCALP, TOROFX, DAILY_SIGNAL)
- ❌ Deduplication via Redis SETEX (simpler; no longer stored separately)

---

## 🧪 **Testing**

Run the full suite:
```bash
python -m pytest -m "not integration" -q
```

Expected baseline: ~85 passed.

Run specific tests:
```bash
pytest services/router_parser/test_router_parser.py -v
pytest services/trade_orchestrator/test_orchestrator.py -v
pytest services/trade_api/test_trade_api.py -v
```

---

## 🚀 **Deployment**

1. Copy `.env.example` → `.env`, fill in values
2. Ensure `N8N_ACTION_API_KEY` and `TRADE_API_KEY` are set (both required)
3. `docker compose up -d --build`
4. Check logs: `docker compose logs -f trade_orchestrator`
5. Test signal flow: send TradePulse-format message to subscribed Telegram channel

---

## 📊 **Comparison: Old vs New**

| Feature | Old | New |
|---------|-----|-----|
| **Parsers** | 5+ formats (GB, TOROFX, etc) | TradePulse only |
| **Trading Modes** | 4 modes (general, be_pips, be_pnl, reentry) | 1 mode (dual-TP, mechanical) |
| **Position Count** | 1 per signal | 2 per signal (TP1 + runner) |
| **BE Strategy** | Optional, configurable | Automatic on TP1 hit |
| **Trailing** | Optional, manual | Mechanical loop every 2s |
| **Config Storage** | Postgres | Environment variables only |
| **Admin UI** | backend_admin service | None (HTTP APIs only) |
| **Monitoring** | Prometheus + Loki | n8n webhooks (event-driven) |
| **External Exceptions** | None | n8n/Ollama via /mgmt/action |
| **External Trade Control** | None | trade_api REST endpoints |

---

## ✅ **Key Properties**

- **Fail-closed auth:** Both API keys (action + trade) are REQUIRED; services won't start without them
- **Mechanical management:** BE and trailing are automatic, no LLM decision loop needed (except for exception handling)
- **Single-account focus:** Multi-account structure exists, but only one should be active
- **No external dependencies:** Postgres, Prometheus, etc. all removed
- **Event-driven notifications:** n8n webhooks for trade events (optional), n8n inbound webhook for unrecognized text (required URL, but optional to actually send to)

---

## 🎯 Conclusion

The platform is now **production-ready** with:
- ✅ Simplified architecture (6 services, no DB, no monitoring stack)
- ✅ TradePulse-only signal parsing (no multi-provider complexity)
- ✅ Mechanical dual-TP management (no per-account configuration)
- ✅ External exception handling via n8n/Ollama (scalable)
- ✅ Comprehensive HTTP APIs (/mgmt/action + trade_api)
- ✅ Full test coverage (~85 tests)
