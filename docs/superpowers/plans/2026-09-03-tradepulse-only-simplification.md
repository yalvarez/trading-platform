# TradePulse Dual-TP Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `trading-platform` to a single signal provider (TradePulse), a single active MT5 account, and the minimal service set — `telegram_ingestor`, `router_parser`, `trade_orchestrator`, a new `trade_api` — with (1) a new dual-position opening model (`tp1_leg`/`runner_leg` per signal, mechanical BE + uncapped proportional trailing on the runner) replacing all provider-specific and mode-based management, (2) a `/mgmt/action` endpoint on `trade_orchestrator` that executes exception actions decided by an external n8n/Ollama flow for text the signal parser doesn't recognize, and (3) everything non-essential (backend_admin, market_data, monitoring, Postgres, orphaned MT5 flavors, other-provider parsers/handlers, Telegram-based notifications) removed.

**Architecture:** Telegram → `telegram_ingestor` → Redis Streams → `router_parser` (TradePulse parser only; unrecognized text POSTed straight to an n8n inbound webhook, no Redis MGMT stream) → Redis Streams → `trade_orchestrator` (opens `tp1_leg`+`runner_leg` per signal, BE-on-TP1 + proportional trailing loop, `/mgmt/action` HTTP endpoint for n8n/Ollama-decided exceptions, all in one process) → MT5 via RPyC, with trade events POSTed to a single n8n webhook. A new standalone `trade_api` FastAPI service talks to MT5 directly (via a `MT5Client` moved into `services/common`) for external open/modify/close/status control, independent of the orchestrator's in-memory state.

**Tech Stack:** Python 3.10, FastAPI + uvicorn, Redis Streams (`redis`/`aioredis`), `mt5linux` (RPyC client to `gmag11/metatrader5_vnc`), pytest + pytest-asyncio, Docker Compose.

**Specs:**
- [docs/superpowers/specs/2026-09-03-tradepulse-only-simplification-design.md](../specs/2026-09-03-tradepulse-only-simplification-design.md) (§1-4, §6-10: provider/service scope, `trade_api`, config, testing, docs — unchanged by the spec below)
- [docs/superpowers/specs/2026-09-03-dual-tp-management-and-n8n-ollama-design.md](../specs/2026-09-03-dual-tp-management-and-n8n-ollama-design.md) (replaces §5 of the spec above: opening model, mechanical management, `/mgmt/action`)

## Global Constraints

- Only TradePulse signals remain; all other provider code is deleted, not disabled.
- Single active MT5 account for now; `ACCOUNTS_JSON` stays a list (multi-account flexibility preserved) per spec §2.
- MT5 image stays exactly as wired today: `gmag11/metatrader5_vnc:latest` directly in `docker-compose.yml`, no custom build.
- No new persistence layer (no Postgres, no ledger) — trade events go out via a single n8n webhook POST.
- `trade_api` must NOT depend on `trade_orchestrator`'s in-memory `TradeManager` state; it talks to MT5 directly.
- Every signal opens **two** MT5 positions (`tp1_leg`, `runner_leg`) sharing one `group_id` — never one. The runner never carries a real MT5 TP; its only mechanical exit is the trailing SL (dual-TP spec §3-4).
- `unit = TP2_price − TP1_price` must be validated `> 0` before opening a group; abort and notify on `unit <= 0` (dual-TP spec §7).
- The trailing SL only ever rises, computed from the historical peak multiple of `unit`, never from instantaneous price (dual-TP spec §4).
- `close_now` (from `/mgmt/action`) can close both legs of a group at any point in its lifecycle, regardless of whether TP1 has been hit — mechanical management never blocks an explicit close order (dual-TP spec §4, §7).
- Every deletion or behavior change must be verified by running the test suite before and after.
- Follow existing code patterns (module-level FastAPI apps, `X-API-Key`/custom-header auth via `APIKeyHeader`, `services.common.*` shared imports, `env_validator.py`-style startup validation). Reuse existing low-level MT5 primitives (`modify_sl`, `_best_filling_order_send`, `calcular_sl_default`, `calcular_be_price`, `pips_to_price`, `safe_comment`) rather than re-deriving them — but do **not** reuse `MT5Executor._apply_be` (it never sends its `order_send` call — see the pre-existing-bugs note in Task 4) or `trade_utils.calcular_trailing_retroceso` (its `point` parameter is unused, hardcodes `0.01`/`0.1`) as-is.

---

## Task 1: Baseline test run and orphaned MT5 image cleanup

**Files:**
- Delete: `services/mt5_custom/` (entire directory)
- Delete: `services/mt5_extended/` (entire directory)
- Delete: `services/mt5linux/` (entire directory — note: this is the *service scaffold* directory, not the `mt5linux` pip package used as a Python import; the package stays in `requirements.txt`)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a recorded baseline pytest result other tasks can compare against; confirms these three directories are genuinely unreferenced by `docker-compose.yml` or any Python import.

- [ ] **Step 1: Run the full test suite and record the baseline**

```bash
cd "/c/Users/yalva/source/repos/trading-platform"
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/baseline_pytest.txt
```

Note the pass/fail counts. Some failures are expected (e.g. `tests/test_orchestrator.py` currently fails at import with `ImportError: cannot import name 'NotifierAdapter'` — pre-existing, fixed in Task 8, not caused by this task). Record which tests fail now so later tasks can confirm they don't introduce *new* failures.

- [ ] **Step 2: Confirm the three MT5 directories are unreferenced**

```bash
grep -rn "mt5_custom\|mt5_extended" docker-compose.yml services/ --include="*.py" --include="*.yml"
grep -rn "from mt5linux\|import mt5linux" services/ --include="*.py"
```

Expected: no matches for `mt5_custom`/`mt5_extended` anywhere. `from mt5linux import MetaTrader5` (the pip package, e.g. in `services/trade_orchestrator/mt5_client.py`) is expected and unrelated to the `services/mt5linux/` directory — do not delete the pip dependency.

- [ ] **Step 3: Delete the three orphaned directories**

```bash
git rm -r services/mt5_custom services/mt5_extended services/mt5linux
```

- [ ] **Step 4: Run the test suite again to confirm no change**

```bash
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task1_pytest.txt
diff /tmp/baseline_pytest.txt /tmp/task1_pytest.txt
```

Expected: identical pass/fail outcome.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove orphaned mt5_custom, mt5_extended, mt5linux service dirs

These three directories were never wired into docker-compose.yml —
the compose file uses gmag11/metatrader5_vnc:latest directly for
mt5_acct1. Dead code with no test coverage.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Remove non-TradePulse parsers and their tests from `router_parser`

**Files:**
- Delete: `services/router_parser/parsers_goldbro_fast.py`
- Delete: `services/router_parser/parsers_goldbro_long.py`
- Delete: `services/router_parser/parsers_goldbro_scalp.py`
- Delete: `services/router_parser/parsers_hannah.py`
- Delete: `services/router_parser/parsers_limitless.py`
- Delete: `services/router_parser/parsers_torofx.py`
- Delete: `services/router_parser/parsers_daily_signal.py`
- Delete: `services/router_parser/gb_filters.py`
- Delete: `services/router_parser/torofx_filters.py`
- Delete: `services/router_parser/test_parsers.py`
- Delete: `services/router_parser/test_parsers_all_providers.py`
- Delete: `services/router_parser/test_parsers_hannah.py`
- Delete: `services/router_parser/test_parsers_hannah_only.py`
- Delete: `services/router_parser/test_trading_logic.py`
- Delete: `tests/test_parsers.py`
- Delete: `tests/test_parsers_cases.py`
- Keep as-is: `services/router_parser/parsers_tradepulse.py`, `services/router_parser/parsers_base.py`, `tests/test_parsers_tradepulse.py`
- Delete: `services/router_parser/tradepulse_filters.py` — its only function, `looks_like_followup`, is superseded by Task 3's "anything the signal parser doesn't recognize goes to n8n" rule (dual-TP spec §5.1); verify no other file imports it before deleting (Task 3 Step 1 does this check)

**Interfaces:**
- Consumes: nothing new.
- Produces: `router_parser` directory contains only TradePulse-related parser code, ready for Task 3's `app.py` rewrite.

- [ ] **Step 1: Verify each file to be deleted has no TradePulse-specific content worth preserving**

```bash
grep -iln "tradepulse\|trade_pulse" services/router_parser/parsers_goldbro_fast.py services/router_parser/parsers_goldbro_long.py services/router_parser/parsers_goldbro_scalp.py services/router_parser/parsers_hannah.py services/router_parser/parsers_limitless.py services/router_parser/parsers_torofx.py services/router_parser/parsers_daily_signal.py services/router_parser/gb_filters.py services/router_parser/torofx_filters.py
```

Expected: no output.

- [ ] **Step 2: Delete the parser/filter source files (excluding `tradepulse_filters.py`, handled in Task 3)**

```bash
git rm services/router_parser/parsers_goldbro_fast.py services/router_parser/parsers_goldbro_long.py services/router_parser/parsers_goldbro_scalp.py services/router_parser/parsers_hannah.py services/router_parser/parsers_limitless.py services/router_parser/parsers_torofx.py services/router_parser/parsers_daily_signal.py services/router_parser/gb_filters.py services/router_parser/torofx_filters.py
```

- [ ] **Step 3: Delete the associated test files**

```bash
git rm services/router_parser/test_parsers.py services/router_parser/test_parsers_all_providers.py services/router_parser/test_parsers_hannah.py services/router_parser/test_parsers_hannah_only.py services/router_parser/test_trading_logic.py tests/test_parsers.py tests/test_parsers_cases.py
```

- [ ] **Step 4: Run the router_parser and top-level parser tests to confirm collection succeeds**

```bash
python -m pytest tests/test_parsers_tradepulse.py -v
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task2_pytest.txt
```

Expected: `test_parsers_tradepulse.py` passes fully. Overall suite has the same failures as the Task 1 baseline, minus tests that only existed in the deleted files.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove non-TradePulse parsers and their tests

Deletes GoldBro (fast/long/scalp), Hannah, Limitless, ToroFX, and
daily_signal parsers/filters plus every test file that only exercised
them. TradePulseParser and parsers_base are untouched.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Rewrite `router_parser/app.py` — drop channel routing, send unrecognized text to n8n

**Files:**
- Modify: `services/router_parser/app.py`
- Delete: `services/router_parser/tradepulse_filters.py` (once confirmed unreferenced elsewhere)
- Modify: `services/router_parser/requirements.txt` (add `httpx` if missing)
- Test: `services/router_parser/test_app.py` (new)

**Interfaces:**
- Consumes: `TradePulseParser` from `parsers_tradepulse` (unchanged).
- Produces: `SignalRouter.__init__(self, redis_client, dedup_ttl=120.0)` — no `channels_config`. A new module-level async function `forward_to_n8n(text: str, chat_id: str, webhook_url: str) -> None` that POSTs `{"chat_id": chat_id, "text": text, "timestamp": <ISO8601 UTC now>}` to `webhook_url` via `httpx.AsyncClient`, swallowing and logging any exception (never raises — a failed forward must not crash the main consume loop).

- [ ] **Step 1: Confirm `tradepulse_filters` / `looks_like_followup` has no other consumer before deleting**

```bash
grep -rn "tradepulse_filters\|looks_like_followup" services/ tests/
```

Expected: only `services/router_parser/app.py` (the file this task rewrites) and `services/router_parser/tradepulse_filters.py` itself. If anything else references it, stop and reconsider before deleting.

```bash
git rm services/router_parser/tradepulse_filters.py
```

- [ ] **Step 2: Write the failing test for `forward_to_n8n` and the new `SignalRouter`**

Create `services/router_parser/test_app.py`:
```python
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))  # so `import app` / sibling imports work like the existing app.py does

import pytest
import httpx

from services.router_parser.app import SignalRouter, forward_to_n8n


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.mark.asyncio
async def test_signal_router_has_no_channels_config_param():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    assert not hasattr(r, "channels_config")


@pytest.mark.asyncio
async def test_parse_signal_tries_the_single_parser():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    result = r.parse_signal("XAUUSD BUY NOW", chat_id="-1003321565807")
    assert result is not None
    assert result.symbol == "XAUUSD"
    assert result.direction == "BUY"


@pytest.mark.asyncio
async def test_parse_signal_returns_none_for_unrecognized_text():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    result = r.parse_signal("Spam your feedbacks @trader_ahmed_2", chat_id="-1003321565807")
    assert result is None


@pytest.mark.asyncio
async def test_forward_to_n8n_posts_expected_payload(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await forward_to_n8n("HIT SL. GET READY FOR RECOVERY", "-1003321565807", "https://n8n.example.com/in")

    assert captured["url"] == "https://n8n.example.com/in"
    assert captured["json"]["chat_id"] == "-1003321565807"
    assert captured["json"]["text"] == "HIT SL. GET READY FOR RECOVERY"
    assert "timestamp" in captured["json"]


@pytest.mark.asyncio
async def test_forward_to_n8n_swallows_errors(monkeypatch):
    async def fake_post(self, url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # Must not raise
    await forward_to_n8n("some text", "-1", "https://n8n.example.com/in")
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
python -m pytest services/router_parser/test_app.py -v
```

Expected: FAIL — `SignalRouter` still has `channels_config`, `forward_to_n8n` doesn't exist yet.

- [ ] **Step 4: Rewrite `SignalRouter` — drop channel routing**

In `services/router_parser/app.py`, change:
```python
class SignalRouter:
    def __init__(self, redis_client, dedup_ttl=120.0, channels_config=None):
        from parsers_tradepulse import TradePulseParser
        self.parser_map = {
            'tradepulse': TradePulseParser(),
        }
        self.channels_config = channels_config or {}
        self.deduplicator = SignalDeduplicator(redis_client, ttl_seconds=dedup_ttl)
        self.fast_update_window = FAST_UPDATE_WINDOW_SECONDS
        self.redis = redis_client

    def parse_signal(self, text, chat_id=None):
        norm = text.strip()
        # --- 4. Normal routing ---
        parsers = []
        if chat_id and str(chat_id) in self.channels_config:
            parser_names = self.channels_config[str(chat_id)]
            parsers = [self.parser_map[name] for name in parser_names if name in self.parser_map]
        if not parsers:
            parsers = list(self.parser_map.values())
        for parser in parsers:
```

to:
```python
class SignalRouter:
    def __init__(self, redis_client, dedup_ttl=120.0):
        from parsers_tradepulse import TradePulseParser
        self.parser_map = {
            'tradepulse': TradePulseParser(),
        }
        self.deduplicator = SignalDeduplicator(redis_client, ttl_seconds=dedup_ttl)
        self.fast_update_window = FAST_UPDATE_WINDOW_SECONDS
        self.redis = redis_client

    def parse_signal(self, text, chat_id=None):
        norm = text.strip()
        for parser in self.parser_map.values():
```

(`chat_id` stays a parameter of `parse_signal` — still used in `process_raw_signal`'s fast-signal-key construction.)

- [ ] **Step 5: Remove the TOROFX import and add `forward_to_n8n`**

Remove:
```python
from tradepulse_filters import looks_like_followup
from torofx_filters import looks_like_torofx_management
```

Add (near the top, after existing imports):
```python
import datetime
import httpx


async def forward_to_n8n(text: str, chat_id: str, webhook_url: str) -> None:
    """
    Reenvia texto que el parser de senales no reconocio a un webhook n8n
    de entrada, para que un flujo n8n/Ollama externo decida si es una
    excepcion de gestion accionable (ver dual-TP spec seccion 5.1).
    Nunca levanta: un fallo de red no debe tumbar el loop principal.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10.0)
        if not (200 <= resp.status_code < 300):
            log.warning("[N8N_FORWARD] webhook respondio status=%s chat_id=%s", resp.status_code, chat_id)
    except Exception as e:
        log.warning("[N8N_FORWARD] error reenviando a n8n: %s", e)
```

- [ ] **Step 6: Rewrite the main message loop — replace the `Streams.MGMT` branch with the n8n forward**

Change:
```python
    while True:
        try:
            async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
                text = fields.get("text", "")
                chat_id = fields.get("chat_id", "")

                if looks_like_followup(text):
                    await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "TRADE_PULSE"})
                    log.info("[MGMT] TRADE_PULSE follow-up")
                    await xack(r, Streams.RAW, group, msg_id)
                    continue

                try:
                    sig = await router.process_raw_signal(chat_id, text)
                    if sig:
                        trace_id = uuid.uuid4().hex[:8]
                        sig["chat_id"] = chat_id
                        sig["raw_text"] = text
                        sig["trace"] = trace_id
                        await xadd(r, Streams.SIGNALS, sig)
                        log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
                    else:
                        pass  # log.debug("[DROP] chat=%s parsed=None", chat_id)  # Reduce log noise
                finally:
                    await xack(r, Streams.RAW, group, msg_id)
```

to:
```python
    from services.common.config import config as _config
    n8n_webhook_url = _config.get("N8N_INBOUND_WEBHOOK_URL", "")

    while True:
        try:
            async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
                text = fields.get("text", "")
                chat_id = fields.get("chat_id", "")

                try:
                    sig = await router.process_raw_signal(chat_id, text)
                    if sig:
                        trace_id = uuid.uuid4().hex[:8]
                        sig["chat_id"] = chat_id
                        sig["raw_text"] = text
                        sig["trace"] = trace_id
                        await xadd(r, Streams.SIGNALS, sig)
                        log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
                    elif text.strip():
                        if n8n_webhook_url:
                            await forward_to_n8n(text, chat_id, n8n_webhook_url)
                        else:
                            log.warning("[N8N_FORWARD] N8N_INBOUND_WEBHOOK_URL no configurada — mensaje descartado: %r", text[:80])
                finally:
                    await xack(r, Streams.RAW, group, msg_id)
```

(An empty/whitespace-only `text` is skipped entirely — nothing useful to forward.)

- [ ] **Step 7: Remove now-dead `channels_config`/`CHANNELS_CONFIG_JSON` construction in `main()`**

Change:
```python
async def main():
    import json
    from services.common.env_validator import validate_router_parser
    validate_router_parser()

    from services.common.config import CHANNELS_CONFIG_JSON
    s = Settings.load()
    r = await redis_client(s["redis_url"])
    try:
        channels_config = json.loads(CHANNELS_CONFIG_JSON)
    except Exception as e:
        log.warning(f"CHANNELS_CONFIG_JSON parse error: {e}")
        channels_config = {}
    router = SignalRouter(r, dedup_ttl=s["dedup_ttl_seconds"], channels_config=channels_config)
```

to:
```python
async def main():
    from services.common.env_validator import validate_router_parser
    validate_router_parser()

    s = Settings.load()
    r = await redis_client(s["redis_url"])
    router = SignalRouter(r, dedup_ttl=s["dedup_ttl_seconds"])
```

(The module-level `import os, re, json, logging, uuid` at the top of the file stays — `json` is still used inside `process_raw_signal`.)

- [ ] **Step 8: Add `httpx` to `router_parser/requirements.txt` if missing**

```bash
grep -n "httpx" services/router_parser/requirements.txt
```

If absent, append `httpx` to `services/router_parser/requirements.txt`.

- [ ] **Step 9: Run the tests**

```bash
python -m pytest services/router_parser/test_app.py tests/test_parsers_tradepulse.py -v
python -c "import ast; ast.parse(open('services/router_parser/app.py').read())"
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor(router_parser): drop channel routing and Redis MGMT stream, forward unrecognized text to n8n

SignalRouter no longer branches by chat_id/channels_config — a single
TradePulseParser is always tried. Text that doesn't parse as a signal
is POSTed to N8N_INBOUND_WEBHOOK_URL instead of being published to
the Streams.MGMT Redis stream; tradepulse_filters.looks_like_followup
and the MGMT stream consumer in trade_orchestrator are both retired
by this change (dual-TP spec section 5.1).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Add `N8nWebhookNotifier` HTTP client and `N8nNotifierAdapter`

**Files:**
- Create: `services/common/n8n_notifier.py`
- Create: `services/common/test_n8n_notifier.py`
- Create: `services/trade_orchestrator/notifications/n8n.py`
- Delete: `services/trade_orchestrator/notifications/telegram.py`
- Delete: `services/trade_orchestrator/common/telegram_notifier.py`
- Delete: `services/common/telegram_notifier.py`
- Delete: `services/telegram_ingestor/notify_api.py`

**Interfaces:**
- Produces: `N8nWebhookNotifier` in `services/common/n8n_notifier.py`:
  ```python
  class N8nWebhookNotifier:
      def __init__(self, webhook_url: str, token: str = ""): ...
      async def send_event(self, event: str, **fields) -> bool: ...
  ```
  `send_event` POSTs `{"event": event, **fields}` as JSON (header `X-N8N-Token: token` if `token` is set), returns `True` on 2xx, `False` otherwise, never raises.
- `N8nNotifierAdapter` in `services/trade_orchestrator/notifications/n8n.py`:
  ```python
  class N8nNotifierAdapter:
      def __init__(self, notifier=None): ...
      async def notify(self, target, message: str) -> None: ...
      async def notify_trade_event(self, event: str, **kwargs) -> None: ...
  ```
  Tasks 5-6 use only `notify` and `notify_trade_event` — the dual-TP opening/management code emits structured events (`group_opened`, `tp1_hit`, `be_applied`, `trailing_updated`, `runner_closed`, `mgmt_action_applied`, etc) via `notify_trade_event`, not a `notify_trade_opened`-shaped call, so that method is intentionally not carried over from the old adapter.
- Consumes (for later tasks): none.

- [ ] **Step 1: Write the failing test for `N8nWebhookNotifier`**

Create `services/common/test_n8n_notifier.py`:
```python
import pytest
import httpx
from services.common.n8n_notifier import N8nWebhookNotifier


class DummyResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.asyncio
async def test_send_event_posts_json_and_returns_true_on_success(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades")
    ok = await notifier.send_event("trade_opened", ticket=123, symbol="XAUUSD")

    assert ok is True
    assert captured["url"] == "https://n8n.example.com/webhook/trades"
    assert captured["json"] == {"event": "trade_opened", "ticket": 123, "symbol": "XAUUSD"}


@pytest.mark.asyncio
async def test_send_event_includes_token_header_when_set(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades", token="secret123")
    await notifier.send_event("trade_closed", ticket=456)

    assert captured["headers"]["X-N8N-Token"] == "secret123"


@pytest.mark.asyncio
async def test_send_event_returns_false_and_does_not_raise_on_error(monkeypatch):
    async def fake_post(self, url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades")
    ok = await notifier.send_event("trade_opened", ticket=789)

    assert ok is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest services/common/test_n8n_notifier.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `N8nWebhookNotifier`**

Create `services/common/n8n_notifier.py`:
```python
"""
n8n_notifier.py
Cliente HTTP minimo para enviar eventos de trading a un webhook n8n.
n8n es el unico destino de notificaciones/eventos y decide que hacer
con cada uno (reenviar a Telegram, loggear, alertar, etc).
"""
import logging

import httpx

log = logging.getLogger("n8n_notifier")


class N8nWebhookNotifier:
    """Envia eventos de trading como JSON a un webhook n8n via HTTP POST."""

    def __init__(self, webhook_url: str, token: str = ""):
        self.webhook_url = webhook_url
        self.token = token

    async def send_event(self, event: str, **fields) -> bool:
        payload = {"event": event, **fields}
        headers = {"X-N8N-Token": self.token} if self.token else None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(self.webhook_url, json=payload, headers=headers, timeout=10.0)
            if 200 <= resp.status_code < 300:
                return True
            log.warning("[N8N] webhook respondio status=%s event=%s", resp.status_code, event)
            return False
        except Exception as e:
            log.warning("[N8N] error enviando evento '%s': %s", event, e)
            return False
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
python -m pytest services/common/test_n8n_notifier.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Commit the notifier client**

```bash
git add services/common/n8n_notifier.py services/common/test_n8n_notifier.py
git commit -m "feat(common): add N8nWebhookNotifier HTTP client

Minimal client that POSTs {event, ...fields} JSON to a single n8n
webhook URL, with optional token header. Never raises.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Create the trade_orchestrator-side adapter**

Create `services/trade_orchestrator/notifications/n8n.py`:
```python
"""
notifications/n8n.py
Adaptador que traduce las llamadas de notificacion del orchestrator
(notify, notify_trade_event) a eventos enviados al webhook n8n.
Reemplaza notifications/telegram.py.
"""
import logging

log = logging.getLogger("trade_orchestrator.notifications.n8n")


class N8nNotifierAdapter:
    """Adaptador desacoplado: todas las llamadas son seguras y no bloquean la gestion principal."""

    def __init__(self, notifier=None):
        self.notifier = notifier

    async def notify(self, target, message: str) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.send_event("message", target=str(target), message=message)
        except Exception as e:
            log.warning("[N8N_ADAPTER] notify failed: %s", e)

    async def notify_trade_event(self, event: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.send_event(event, **kwargs)
        except Exception as e:
            log.warning("[N8N_ADAPTER] notify_trade_event failed: %s", e)
```

- [ ] **Step 7: Delete the Telegram notifier files**

```bash
git rm services/trade_orchestrator/notifications/telegram.py
git rm services/trade_orchestrator/common/telegram_notifier.py
git rm services/common/telegram_notifier.py
git rm services/telegram_ingestor/notify_api.py
```

(`services/trade_orchestrator/mt5_executor.py` still imports `.notifications.telegram` at its top — this is fixed in Task 5, where `mt5_executor.py` and `trade_manager.py` are rewritten together; do not attempt to patch that import here in isolation.)

- [ ] **Step 8: Confirm no other file still references the deleted Telegram notifier modules**

```bash
grep -rln "notifications.telegram\|TelegramNotifierAdapter\|RemoteTelegramNotifier\|common.telegram_notifier\|notify_api" services/ tests/
```

Expected: matches only in `services/trade_orchestrator/mt5_executor.py` and `services/trade_orchestrator/trade_manager.py` and `services/trade_orchestrator/app.py` (all rewritten in Task 5) and `tests/test_orchestrator.py` (fixed in Task 8). Record this list — Task 5 must clear every remaining match in `mt5_executor.py`/`trade_manager.py`/`app.py`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: add N8nNotifierAdapter, remove Telegram notifier modules

Deletes notifications/telegram.py, common/telegram_notifier.py,
trade_orchestrator/common/telegram_notifier.py, and
telegram_ingestor/notify_api.py. trade_orchestrator's remaining
imports of these modules are fixed in the next task, which rewrites
trade_manager.py/mt5_executor.py/app.py wholesale for the dual-TP
model.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Rewrite `trade_manager.py` for the dual-TP model

This is the largest task in the plan — it replaces the entire management
core. Read dual-TP spec §3-4, §6 before starting.

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (near-total rewrite)
- Test: `services/trade_orchestrator/test_trade_manager_dual_tp.py` (new)

**Interfaces:**
- Consumes: `N8nNotifierAdapter` (Task 4), `MT5Client` (still at its current path — moved to `services/common` in Task 7, imported here via whatever path Task 7 leaves it at; if Task 7 hasn't run yet when this task executes, import from `.mt5_client` as today and let Task 7's import-path update pick this file up too), `modify_sl`/`_client_for`/`_best_filling_order_send` from `MT5Executor` (unchanged), `calcular_sl_default`/`calcular_be_price`/`pips_to_price`/`safe_comment` from `trade_utils` (unchanged).
- Produces:
  ```python
  @dataclass
  class ManagedTrade:
      account_name: str
      ticket: int
      symbol: str
      direction: str
      group_id: int
      leg: str  # "tp1" or "runner"
      tp1_price: float
      tp2_price: float  # used only as the trailing "unit" reference, never sent to MT5 for the runner leg
      planned_sl: float
      entry_price: Optional[float] = None
      be_applied: bool = False       # runner only: True once SL has been moved to BE (TP1 confirmed hit)
      peak_multiple: float = 0.0     # runner only: highest (price - tp1_price) / unit ever observed, never decreases
      opened_ts: float = field(default_factory=lambda: time.time())

  class TradeManager:
      def __init__(self, mt5_executor, *, notifier=None, config_provider=None): ...
      async def open_group(self, account: dict, *, symbol: str, direction: str, sl: float, tp1: float, tp2: Optional[float]) -> Optional[int]:
          """Opens tp1_leg + runner_leg (dual-TP spec §3). Returns the new group_id, or None if aborted (unit<=0, no price, SL invalid)."""
      async def update_group_signal(self, group_id: int, *, sl: Optional[float], tp1: Optional[float], tp2: Optional[float]) -> None:
          """Applies a fast->full update or a signal_correction to both legs of an existing group (dual-TP spec §3, §5.2)."""
      def find_active_group_for_symbol(self, symbol: str) -> Optional[int]:
          """Returns the most recently opened group_id with at least one open leg for `symbol`, or None (dual-TP spec §5.2 'no_active_trade')."""
      async def apply_mgmt_action(self, *, action: str, symbol: str, raw_text: str, correction: Optional[dict]) -> dict:
          """Executes one /mgmt/action decision (close_now/move_sl_be_now/note_sl_hit/signal_correction/ignore) against find_active_group_for_symbol(symbol)'s group. Returns {"status": ...} per dual-TP spec §5.2."""
      async def run_forever(self) -> None:
          """Mechanical management loop: detects tp1_leg closes -> BE on runner; trailing on runner once be_applied; cleans up closed groups."""
  ```
- These four public methods (`open_group`, `update_group_signal`, `find_active_group_for_symbol`, `apply_mgmt_action`) are the surface Task 6 (`app.py`) and Task 8 (the `/mgmt/action` HTTP route) call — their signatures above are final; do not rename in later tasks.

- [ ] **Step 1: Delete the entire current content of `trade_manager.py` and note what's being discarded**

```bash
grep -n "^class \|^    def \|^    async def " services/trade_orchestrator/trade_manager.py
```

Confirm this list matches what dual-TP spec §6 says to remove: `TradingMode` enum, all `gestionar_trade*` methods, `_maybe_addon_midpoint`, `handle_torofx_management_message`, `handle_hannah_management_message`, `_tick_once` (the old unused loop — dual-TP spec §6 and the pre-existing-bugs memory note confirm `run_forever`/`_tick_once_account` is the live one), `_get_recorrido_pips`, `_move_sl_to_be`, `_calcular_sl_por_pnl`, `_move_sl`, `_valor_pip`, `_close_partial_and_be` (dead/incomplete), `torofx_provider_tag_match`. Keep only: `_ensure_account_dict`, `_safe_comment`/`_pips_to_price` wrappers (or inline `trade_utils` calls directly — see Step 3), `_client_for`-style account resolution pattern, the Prometheus `Counter`/`Gauge` declarations (reused).

- [ ] **Step 2: Write the failing tests for `open_group`**

Create `services/trade_orchestrator/test_trade_manager_dual_tp.py`:
```python
import time
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager, ManagedTrade


class DummyExecutor:
    """Minimal stand-in for MT5Executor — exposes only what TradeManager needs."""
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [{"name": "demo", "active": True, "host": "x", "port": 1}]
        self.magic = 987654
        self.default_deviation = 50
        self.comment_prefix = "TM"

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    def __init__(self):
        self.events = []

    async def notify_trade_event(self, event, **kwargs):
        self.events.append((event, kwargs))

    async def notify(self, target, message):
        pass


ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1}


@pytest.mark.asyncio
async def test_open_group_opens_two_positions_with_shared_group_id():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert group_id is not None
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    leg_names = {t.leg for t in legs}
    assert leg_names == {"tp1", "runner"}
    for t in legs:
        assert t.planned_sl == 2490.0
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0


@pytest.mark.asyncio
async def test_open_group_aborts_when_tp2_not_above_tp1_for_buy():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2505.0)

    assert group_id is None
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_open_group_without_tp2_opens_fast_guard_pair():
    """Fast signal (no real TP yet): both legs open with a temporary SL, tp1/tp2 unset."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2470.0, tp1=None, tp2=None)

    assert group_id is not None
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert t.tp1_price is None
        assert t.tp2_price is None
        assert t.planned_sl == 2470.0


@pytest.mark.asyncio
async def test_update_group_signal_fills_in_tp1_tp2_on_fast_guard_pair():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2470.0, tp1=None, tp2=None)

    await tm.update_group_signal(group_id, sl=2490.0, tp1=2510.0, tp2=2530.0)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    for t in legs:
        assert t.planned_sl == 2490.0
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
    tp1_leg = next(t for t in legs if t.leg == "tp1")
    runner_leg = next(t for t in legs if t.leg == "runner")
    tp1_pos = sim.positions_get(ticket=tp1_leg.ticket)[0]
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert tp1_pos.tp == 2510.0
    # runner never gets a real MT5 TP (dual-TP spec section 4)
    assert runner_pos.tp != 2530.0


@pytest.mark.asyncio
async def test_find_active_group_for_symbol_returns_most_recent():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    found = tm.find_active_group_for_symbol("XAUUSD")
    assert found == g1

    found_none = tm.find_active_group_for_symbol("EURUSD")
    assert found_none is None
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v
```

Expected: FAIL — `open_group`/`update_group_signal`/`find_active_group_for_symbol` don't exist yet.

- [ ] **Step 4: Write the new `trade_manager.py` header, `ManagedTrade`, and `TradeManager.__init__`/`open_group`**

Replace the top of `services/trade_orchestrator/trade_manager.py` through the old `__init__` with:
```python
from .trade_utils import pips_to_price, safe_comment, calcular_sl_default, calcular_be_price
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from prometheus_client import Counter, Gauge

log = logging.getLogger("trade_orchestrator.trade_manager")

TRADES_OPENED = Counter('trades_opened_total', 'Total trades opened')
TP1_HITS = Counter('trade_tp1_hits_total', 'TP1 hits (runner moved to BE)')
ACTIVE_TRADES = Gauge('active_trades', 'Active trades')

MAGIC = 987654


@dataclass
class ManagedTrade:
    account_name: str
    ticket: int
    symbol: str
    direction: str
    group_id: int
    leg: str  # "tp1" or "runner"
    planned_sl: float
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None
    entry_price: Optional[float] = None
    be_applied: bool = False
    peak_multiple: float = 0.0
    opened_ts: float = field(default_factory=lambda: time.time())


class TradeManager:
    def __init__(self, mt5_executor, *, notifier=None, config_provider=None):
        self.mt5 = mt5_executor
        self.notifier = notifier
        self.config_provider = config_provider
        self.trades: dict[int, ManagedTrade] = {}
        self._next_group_id = 1

    def _ensure_account_dict(self, account):
        if isinstance(account, dict):
            return account
        accounts = list(getattr(self.mt5, "accounts", []) or [])
        for acc in accounts:
            if acc.get("name") == str(account):
                return acc
        log.error("[TM][ERROR] No se encontro el dict de cuenta para: %s", account)
        return None

    async def _notify(self, event: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.notify_trade_event(event, **kwargs)
        except Exception as e:
            log.warning("[TM] notify failed for event=%s: %s", event, e)

    async def open_group(self, account: dict, *, symbol: str, direction: str, sl: float, tp1: Optional[float], tp2: Optional[float]) -> Optional[int]:
        """
        Abre dos posiciones (tp1_leg, runner_leg) con el mismo symbol/direction/SL,
        vinculadas por un group_id nuevo. Ver dual-TP spec seccion 3.
        - tp1/tp2 pueden venir None (senal fast): se abre el par con SL guard,
          sin TP fijo todavia — update_group_signal los completa despues.
        - Si tp1 y tp2 vienen ambos, valida unit=tp2-tp1 en la direccion correcta
          antes de abrir; aborta si unit<=0.
        Retorna el group_id nuevo, o None si se aborto (unit invalido, SL invalido,
        sin precio disponible).
        """
        account = self._ensure_account_dict(account)
        if not account:
            return None

        if tp1 is not None and tp2 is not None:
            unit = (tp2 - tp1) if direction.upper() == "BUY" else (tp1 - tp2)
            if unit <= 0:
                log.error("[TM][OPEN] Abortado: unit invalido (tp1=%s tp2=%s dir=%s) symbol=%s", tp1, tp2, direction, symbol)
                await self._notify("open_aborted", symbol=symbol, reason="invalid_unit", tp1=tp1, tp2=tp2)
                return None

        if sl is None or float(sl) == 0.0:
            log.error("[TM][OPEN] Abortado: SL invalido symbol=%s", symbol)
            await self._notify("open_aborted", symbol=symbol, reason="invalid_sl")
            return None

        client = self.mt5._client_for(account)
        client.symbol_select(symbol, True)
        price = client.tick_price(symbol, direction.upper())
        if not price:
            log.error("[TM][OPEN] Abortado: sin precio para %s", symbol)
            await self._notify("open_aborted", symbol=symbol, reason="no_price")
            return None

        order_type = 0 if direction.upper() == "BUY" else 1
        group_id = self._next_group_id
        self._next_group_id += 1

        tickets = {}
        for leg in ("tp1", "runner"):
            req = {
                "action": 1,
                "symbol": symbol,
                "volume": float(account.get("fixed_lot", 0.01) or 0.01),
                "type": order_type,
                "price": float(price),
                "sl": float(sl),
                "tp": float(tp1) if (leg == "tp1" and tp1 is not None) else 0.0,
                "deviation": 50,
                "magic": MAGIC,
                "comment": safe_comment(f"GRP{group_id}-{leg}", "TM"),
                "type_time": 0,
                "type_filling": 1,
            }
            res = client.order_send(req)
            if not res or getattr(res, "retcode", None) != 10009:
                log.error("[TM][OPEN] Fallo abriendo leg=%s symbol=%s retcode=%s", leg, symbol, getattr(res, "retcode", None))
                for t in tickets.values():
                    client.partial_close(account, t, 100)
                await self._notify("open_failed", symbol=symbol, leg=leg, group_id=group_id)
                return None
            tickets[leg] = int(res.order)

        for leg, ticket in tickets.items():
            self.trades[ticket] = ManagedTrade(
                account_name=account["name"],
                ticket=ticket,
                symbol=symbol,
                direction=direction.upper(),
                group_id=group_id,
                leg=leg,
                planned_sl=float(sl),
                tp1_price=float(tp1) if tp1 is not None else None,
                tp2_price=float(tp2) if tp2 is not None else None,
                entry_price=float(price),
            )
        TRADES_OPENED.inc(2)
        ACTIVE_TRADES.set(len(self.trades))
        log.info("[TM] group %s opened: tp1=%s runner=%s symbol=%s dir=%s sl=%s tp1_price=%s tp2_price=%s",
                  group_id, tickets["tp1"], tickets["runner"], symbol, direction, sl, tp1, tp2)
        await self._notify("group_opened", group_id=group_id, symbol=symbol, direction=direction,
                            tp1_ticket=tickets["tp1"], runner_ticket=tickets["runner"], sl=sl, tp1=tp1, tp2=tp2)
        return group_id
```

- [ ] **Step 5: Run the `open_group` tests to verify they pass**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py::test_open_group_opens_two_positions_with_shared_group_id services/trade_orchestrator/test_trade_manager_dual_tp.py::test_open_group_aborts_when_tp2_not_above_tp1_for_buy services/trade_orchestrator/test_trade_manager_dual_tp.py::test_open_group_without_tp2_opens_fast_guard_pair -v
```

Expected: PASS. (`SimuladorMT5.order_send` accepts `action=1` and returns a ticket per call — confirm this in `tests/test_simulador_mt5.py` if any assertion fails; it already supports opening multiple sequential positions since `last_ticket` increments each call.)

- [ ] **Step 6: Add `update_group_signal`**

Append to the `TradeManager` class:
```python
    async def update_group_signal(self, group_id: int, *, sl: Optional[float], tp1: Optional[float], tp2: Optional[float]) -> None:
        """
        Aplica valores nuevos de SL/TP1/TP2 a ambas piernas de un grupo existente.
        Usado tanto para el update fast->full (dual-TP spec seccion 3) como para
        signal_correction via /mgmt/action (dual-TP spec seccion 5.2) — una
        correccion de tp2 solo actualiza la referencia usada por el trailing,
        nunca toca MT5 directamente para la pierna runner.
        """
        legs = [t for t in self.trades.values() if t.group_id == group_id]
        if not legs:
            log.warning("[TM][UPDATE] group_id=%s no tiene piernas activas", group_id)
            return
        account = self._ensure_account_dict(legs[0].account_name)
        if not account:
            return
        client = self.mt5._client_for(account)

        for t in legs:
            if sl is not None:
                t.planned_sl = float(sl)
            if tp1 is not None:
                t.tp1_price = float(tp1)
            if tp2 is not None:
                t.tp2_price = float(tp2)

            new_sl = t.planned_sl
            new_tp = t.tp1_price if (t.leg == "tp1" and t.tp1_price is not None) else 0.0
            req = {
                "action": 6,
                "position": t.ticket,
                "sl": float(new_sl),
                "tp": float(new_tp),
            }
            res = client.order_send(req)
            ok = bool(res and getattr(res, "retcode", None) == 10009)
            if not ok:
                log.error("[TM][UPDATE] fallo actualizando ticket=%s leg=%s", t.ticket, t.leg)

        log.info("[TM] group %s actualizado: sl=%s tp1=%s tp2=%s", group_id, sl, tp1, tp2)
        await self._notify("group_updated", group_id=group_id, sl=sl, tp1=tp1, tp2=tp2)
```

- [ ] **Step 7: Run the `update_group_signal` test**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py::test_update_group_signal_fills_in_tp1_tp2_on_fast_guard_pair -v
```

Expected: PASS.

- [ ] **Step 8: Add `find_active_group_for_symbol`**

Append to the `TradeManager` class:
```python
    def find_active_group_for_symbol(self, symbol: str) -> Optional[int]:
        """
        Devuelve el group_id mas reciente con al menos una pierna abierta para
        `symbol`, o None (dual-TP spec seccion 5.2 — respuesta 'no_active_trade').
        """
        candidates = [t for t in self.trades.values() if t.symbol == symbol]
        if not candidates:
            return None
        newest = max(candidates, key=lambda t: t.opened_ts)
        return newest.group_id
```

- [ ] **Step 9: Run the `find_active_group_for_symbol` test**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py::test_find_active_group_for_symbol_returns_most_recent -v
```

Expected: PASS.

- [ ] **Step 10: Write the failing tests for the mechanical management loop (BE-on-TP1 + trailing)**

Append to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:
```python
@pytest.mark.asyncio
async def test_tick_moves_runner_sl_to_be_when_tp1_leg_closes():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    # Simulate TP1 leg having closed (no longer in MT5 positions)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    del sim.positions[tp1_leg.ticket]

    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6  # moved to entry price (BE)
    assert tm.trades[runner_leg.ticket].be_applied is True
    assert tp1_leg.ticket not in tm.trades


@pytest.mark.asyncio
async def test_trailing_raises_runner_sl_proportionally_to_peak():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE

    # unit = tp2 - tp1 = 20. Move price to 60% of unit past tp1 = 2510 + 12 = 2522
    sim.positions[runner_leg.ticket]["price_current"] = 2522.0
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)

    # peak_multiple = 0.6, SL = tp1 + (0.6 * 20)/3 = 2510 + 4 = 2514
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2514.0) < 1e-6
    assert abs(tm.trades[runner_leg.ticket].peak_multiple - 0.6) < 1e-9


@pytest.mark.asyncio
async def test_trailing_sl_never_decreases_on_price_pullback():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    sim.positions[runner_leg.ticket]["price_current"] = 2522.0  # peak 60%
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)
    sl_at_peak = sim.positions_get(ticket=runner_leg.ticket)[0].sl

    sim.positions[runner_leg.ticket]["price_current"] = 2515.0  # pulls back to 25%
    sim.price = 2515.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_pullback = sim.positions_get(ticket=runner_leg.ticket)[0].sl

    assert sl_after_pullback == sl_at_peak  # never decreases
    assert tm.trades[runner_leg.ticket].peak_multiple == 0.6  # peak retained


@pytest.mark.asyncio
async def test_trailing_extrapolates_unit_beyond_tp2():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    # price at 150% of unit past tp1 = 2510 + 30 = 2540 (beyond tp2=2530)
    sim.positions[runner_leg.ticket]["price_current"] = 2540.0
    sim.price = 2540.0
    await tm._tick_once_account(ACCOUNT)

    # SL = tp1 + (1.5 * 20)/3 = 2510 + 10 = 2520
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2520.0) < 1e-6
```

- [ ] **Step 11: Run the mechanical management tests to verify they fail**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "tick_moves or trailing" -v
```

Expected: FAIL — `_tick_once_account`/`run_forever` don't exist in the rewritten file yet.

- [ ] **Step 12: Add the mechanical management loop**

Append to the `TradeManager` class:
```python
    async def run_forever(self) -> None:
        LOOP_INTERVAL = 0.1
        log.info("[TM] run_forever iniciado")
        while True:
            loop_start = asyncio.get_event_loop().time()
            accounts = self.config_provider.get_accounts() if self.config_provider else self.mt5.accounts
            accounts = [a for a in accounts if a.get("active")]
            if accounts:
                await asyncio.gather(*(self._tick_once_account(a) for a in accounts))
            elapsed = asyncio.get_event_loop().time() - loop_start
            remaining = LOOP_INTERVAL - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _tick_once_account(self, account) -> None:
        account = self._ensure_account_dict(account)
        if not account:
            return
        try:
            client = self.mt5._client_for(account)
            positions = client.positions_get() or []
            pos_by_ticket = {p.ticket: p for p in positions}

            # Detect closed tickets for this account (TP1 hit, SL hit, or manual close)
            for ticket in [t for t, mt in self.trades.items() if mt.account_name == account["name"]]:
                if ticket in pos_by_ticket:
                    continue
                closed_trade = self.trades.pop(ticket)
                if closed_trade.leg == "tp1":
                    await self._on_tp1_leg_closed(account, client, closed_trade)
                else:
                    await self._notify("runner_closed", group_id=closed_trade.group_id, ticket=ticket, symbol=closed_trade.symbol)

            ACTIVE_TRADES.set(len(self.trades))

            for ticket, t in [(tk, mt) for tk, mt in self.trades.items() if mt.account_name == account["name"]]:
                pos = pos_by_ticket.get(ticket)
                if not pos or t.leg != "runner" or not t.be_applied:
                    continue
                await self._apply_trailing(account, client, t, pos)

        except Exception as e:
            log.error("[TM] error gestionando cuenta %s: %s", account.get("name"), e)

    async def _on_tp1_leg_closed(self, account, client, tp1_leg: ManagedTrade) -> None:
        """TP1 hit -> mueve el runner del mismo group_id a BE (dual-TP spec seccion 4)."""
        TP1_HITS.inc()
        runner = next((t for t in self.trades.values() if t.group_id == tp1_leg.group_id and t.leg == "runner"), None)
        if not runner:
            return
        await self._force_runner_sl(account, client, runner, runner.entry_price, reason="TP1-BE")
        runner.be_applied = True
        await self._notify("tp1_hit", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, runner_ticket=runner.ticket)

    async def _force_runner_sl(self, account, client, runner: ManagedTrade, new_sl: float, *, reason: str) -> bool:
        req = {"action": 6, "position": runner.ticket, "sl": float(new_sl)}
        res = client.order_send(req)
        ok = bool(res and getattr(res, "retcode", None) == 10009)
        if not ok:
            log.error("[TM] fallo moviendo SL runner=%s reason=%s", runner.ticket, reason)
        return ok

    async def _apply_trailing(self, account, client, runner: ManagedTrade, pos) -> None:
        """
        Trailing proporcional sin techo (dual-TP spec seccion 4):
        unit = tp2_price - tp1_price (constante); peak = maximo multiplo de unit
        alcanzado desde tp1_price (nunca decrece); SL = tp1_price + (peak*unit)/3.
        """
        if runner.tp1_price is None or runner.tp2_price is None:
            return
        is_buy = runner.direction == "BUY"
        unit = (runner.tp2_price - runner.tp1_price) if is_buy else (runner.tp1_price - runner.tp2_price)
        if unit <= 0:
            return
        current = float(pos.price_current)
        advance = (current - runner.tp1_price) if is_buy else (runner.tp1_price - current)
        multiple = advance / unit
        if multiple <= runner.peak_multiple:
            return  # never decreases
        runner.peak_multiple = multiple
        sl_offset = (multiple * unit) / 3.0
        new_sl = runner.tp1_price + sl_offset if is_buy else runner.tp1_price - sl_offset
        ok = await self._force_runner_sl(account, client, runner, new_sl, reason="trailing")
        if ok:
            await self._notify("trailing_updated", group_id=runner.group_id, ticket=runner.ticket, peak_multiple=multiple, new_sl=new_sl)
```

- [ ] **Step 13: Run the mechanical management tests to verify they pass**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v
```

Expected: all PASS (9 tests total across Steps 5, 7, 9, 13).

- [ ] **Step 14: Write the failing tests for `apply_mgmt_action`**

Append to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:
```python
@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_closes_both_legs_before_tp1():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    result = await tm.apply_mgmt_action(action="close_now", symbol="XAUUSD", raw_text="Close now", correction=None)

    assert result["status"] == "closed"
    remaining = [t for t in tm.trades.values() if t.group_id == group_id]
    assert remaining == []


@pytest.mark.asyncio
async def test_apply_mgmt_action_no_active_trade_returns_no_active_trade():
    sim = SimuladorMT5()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    result = await tm.apply_mgmt_action(action="close_now", symbol="EURUSD", raw_text="Close now", correction=None)

    assert result["status"] == "no_active_trade"


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_forces_be_when_worse():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    # SL still at original 2490 (TP1 not hit yet) — worse than BE (2500 entry)

    result = await tm.apply_mgmt_action(action="move_sl_be_now", symbol="XAUUSD", raw_text="adjust sl to entry", correction=None)

    assert result["status"] == "applied"
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6
    assert tm.trades[runner_leg.ticket].be_applied is True


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_noop_when_already_better():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500
    sim.positions[runner_leg.ticket]["price_current"] = 2522.0
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)  # trailing raises SL above BE
    sl_before = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_before > 2500.0

    result = await tm.apply_mgmt_action(action="move_sl_be_now", symbol="XAUUSD", raw_text="secure be", correction=None)

    assert result["status"] == "already_satisfied"
    sl_after = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after == sl_before  # unchanged, never reduced


@pytest.mark.asyncio
async def test_apply_mgmt_action_note_sl_hit_does_not_touch_mt5():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    legs_before = {t.ticket: (sim.positions_get(ticket=t.ticket)[0].sl) for t in tm.trades.values() if t.group_id == group_id}

    result = await tm.apply_mgmt_action(action="note_sl_hit", symbol="XAUUSD", raw_text="HIT SL, recovery incoming", correction=None)

    assert result["status"] == "noted"
    for ticket, sl_before in legs_before.items():
        assert sim.positions_get(ticket=ticket)[0].sl == sl_before


@pytest.mark.asyncio
async def test_apply_mgmt_action_signal_correction_updates_tp1_on_mt5_and_tp2_reference_only():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    result = await tm.apply_mgmt_action(action="signal_correction", symbol="XAUUSD", raw_text="TP 2 IS 4687 Correction", correction={"field": "tp2", "value": 4687.0})

    assert result["status"] == "applied"
    assert tm.trades[runner_leg.ticket].tp2_price == 4687.0
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.tp != 4687.0  # never sent to MT5 for the runner leg


@pytest.mark.asyncio
async def test_apply_mgmt_action_ignore_is_a_noop():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    result = await tm.apply_mgmt_action(action="ignore", symbol="XAUUSD", raw_text="spam your feedbacks", correction=None)

    assert result["status"] == "ignored"
```

- [ ] **Step 15: Run the `apply_mgmt_action` tests to verify they fail**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "mgmt_action" -v
```

Expected: FAIL — `apply_mgmt_action` doesn't exist yet.

- [ ] **Step 16: Add `apply_mgmt_action`**

Append to the `TradeManager` class:
```python
    async def apply_mgmt_action(self, *, action: str, symbol: str, raw_text: str, correction: Optional[dict]) -> dict:
        """
        Ejecuta una decision de /mgmt/action (dual-TP spec seccion 5.2).
        Resuelve el grupo activo mas reciente para `symbol` y aplica la accion.
        """
        group_id = self.find_active_group_for_symbol(symbol)
        if group_id is None:
            log.info("[TM][MGMT] no_active_trade symbol=%s action=%s text=%r", symbol, action, raw_text[:80])
            return {"status": "no_active_trade"}

        legs = [t for t in self.trades.values() if t.group_id == group_id]
        account = self._ensure_account_dict(legs[0].account_name)
        client = self.mt5._client_for(account)

        if action == "close_now":
            for t in list(legs):
                client.partial_close(account, t.ticket, 100)
                self.trades.pop(t.ticket, None)
            await self._notify("mgmt_close_now", group_id=group_id, symbol=symbol, raw_text=raw_text)
            return {"status": "closed", "group_id": group_id}

        if action == "move_sl_be_now":
            runner = next((t for t in legs if t.leg == "runner"), None)
            if not runner:
                return {"status": "no_active_trade"}
            pos_list = client.positions_get(ticket=runner.ticket)
            current_sl = float(pos_list[0].sl) if pos_list else None
            be_price = runner.entry_price
            is_buy = runner.direction == "BUY"
            worse_than_be = current_sl is None or (current_sl < be_price if is_buy else current_sl > be_price)
            if not worse_than_be:
                await self._notify("mgmt_move_sl_be_already_satisfied", group_id=group_id, symbol=symbol)
                return {"status": "already_satisfied", "group_id": group_id}
            ok = await self._force_runner_sl(account, client, runner, be_price, reason="mgmt-fallback-BE")
            if ok:
                runner.be_applied = True
                await self._notify("mgmt_move_sl_be_applied", group_id=group_id, symbol=symbol, raw_text=raw_text)
                return {"status": "applied", "group_id": group_id}
            return {"status": "failed", "group_id": group_id}

        if action == "note_sl_hit":
            await self._notify("mgmt_note_sl_hit", group_id=group_id, symbol=symbol, raw_text=raw_text)
            return {"status": "noted", "group_id": group_id}

        if action == "signal_correction":
            if not correction or correction.get("field") not in ("sl", "tp1", "tp2"):
                return {"status": "invalid_correction"}
            field = correction["field"]
            value = float(correction["value"])
            kwargs = {"sl": None, "tp1": None, "tp2": None}
            kwargs[field] = value
            await self.update_group_signal(group_id, **kwargs)
            return {"status": "applied", "group_id": group_id}

        if action == "ignore":
            return {"status": "ignored"}

        return {"status": "unknown_action"}
```

- [ ] **Step 17: Run all `test_trade_manager_dual_tp.py` tests**

```bash
python -m pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v
```

Expected: PASS (15 tests total: 3 open_group, 1 update_group_signal, 1 find_active_group, 3 mechanical loop, 7 mgmt_action).

- [ ] **Step 18: Commit**

```bash
git add -A
git commit -m "refactor(trade_orchestrator): rewrite trade_manager.py for the dual-TP model

Replaces TradingMode/gestionar_trade*/addon/Hannah/TOROFX handlers
entirely with: open_group (opens tp1_leg+runner_leg per signal),
update_group_signal (fast->full update and signal_correction target),
the mechanical management loop (BE on TP1 hit, proportional
uncapped trailing on the runner via unit=tp2-tp1, peak/3 formula),
find_active_group_for_symbol, and apply_mgmt_action (the five
n8n/Ollama exception actions). See dual-TP spec sections 3-6.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Rewrite `trade_orchestrator/app.py` — dual-TP signal handling, drop Redis MGMT consumer

**Files:**
- Modify: `services/trade_orchestrator/app.py`
- Test: `services/trade_orchestrator/test_app_signal_handling.py` (new)

**Interfaces:**
- Consumes: `TradeManager.open_group`/`update_group_signal` (Task 5), `N8nWebhookNotifier`/`N8nNotifierAdapter` (Task 4).
- Produces: `handle_signal(fields: dict) -> None` — parses a `Streams.SIGNALS` message and calls `tradeManager.open_group(...)` (no prior group for this symbol/direction) or `tradeManager.update_group_signal(...)` (an open group from a prior fast signal exists). No more `handle_mgmt`, no more `Streams.MGMT` consumption, no more `loop_mgmt`.

- [ ] **Step 1: Confirm what currently imports/constructs `MT5Executor` and how `handle_signal` is wired, before rewriting**

```bash
grep -n "MT5Executor(\|tradeManager = \|loop_mgmt\|handle_mgmt\|Streams.MGMT" services/trade_orchestrator/app.py
```

- [ ] **Step 2: Write the failing test for the new `handle_signal`**

Create `services/trade_orchestrator/test_app_signal_handling.py`:
```python
import json
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager


class DummyExecutor:
    def __init__(self, sim, accounts):
        self.sim = sim
        self.accounts = accounts
        self.magic = 987654

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    async def notify_trade_event(self, event, **kwargs):
        pass

    async def notify(self, target, message):
        pass


ACCOUNTS = [{"name": "demo", "active": True, "host": "x", "port": 1, "fixed_lot": 0.05}]


@pytest.mark.asyncio
async def test_fast_signal_opens_guard_pair_then_full_signal_completes_it():
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    fast_fields = {"symbol": "XAUUSD", "direction": "BUY", "fast": "true", "sl": "", "tps": "[]", "entry_range": ""}
    await handle_signal_fields(fast_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2
    group_id = next(iter(tm.trades.values())).group_id
    for t in tm.trades.values():
        assert t.tp1_price is None  # guard pair, no real TPs yet

    full_fields = {
        "symbol": "XAUUSD", "direction": "BUY", "fast": "false",
        "sl": "2490.0", "tps": json.dumps([2510.0, 2530.0]), "entry_range": "",
    }
    await handle_signal_fields(full_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2  # same two legs, updated in place — not 4
    for t in tm.trades.values():
        assert t.group_id == group_id
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
        assert t.planned_sl == 2490.0


@pytest.mark.asyncio
async def test_full_signal_without_prior_fast_opens_group_directly():
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    full_fields = {
        "symbol": "XAUUSD", "direction": "BUY", "fast": "false",
        "sl": "2490.0", "tps": json.dumps([2510.0, 2530.0]), "entry_range": "",
    }
    await handle_signal_fields(full_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2
    for t in tm.trades.values():
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
python -m pytest services/trade_orchestrator/test_app_signal_handling.py -v
```

Expected: FAIL — `handle_signal_fields` doesn't exist yet (the current `handle_signal` is a closure inside `main()`, not independently importable/testable).

- [ ] **Step 4: Rewrite `app.py`**

Replace the entire content of `services/trade_orchestrator/app.py`:
```python
import os
import json
import asyncio
import logging
import uuid
from typing import Optional

from services.common.config import Settings
from services.common.redis_streams import redis_client, xread_loop, xadd, Streams
from services.common.timewindow import parse_windows, in_windows

from .trade_manager import TradeManager
from .mt5_executor import MT5Executor

container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "trade_orchestrator"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=log_fmt)
log = logging.getLogger("trade_orchestrator")


async def handle_signal_fields(fields: dict, tradeManager: TradeManager, accounts: list[dict]) -> None:
    """
    Procesa un mensaje de Streams.SIGNALS (senal fast o completa de TradePulse)
    y lo traduce a open_group/update_group_signal en el TradeManager
    (dual-TP spec seccion 3).
    """
    symbol = fields.get("symbol")
    direction = fields.get("direction")
    is_fast = fields.get("fast", "false").lower() == "true"
    sl_raw = fields.get("sl", "")
    tps = json.loads(fields.get("tps", "[]") or "[]")

    account = next((a for a in accounts if a.get("active")), None)
    if not account:
        log.error("[SIGNAL] No hay cuenta activa configurada. Abortando.")
        return

    existing_group_id = tradeManager.find_active_group_for_symbol(symbol)

    if is_fast:
        if existing_group_id is not None:
            log.info("[SIGNAL][FAST] Ya existe un grupo activo para %s, ignorando fast duplicado.", symbol)
            return
        sl = float(sl_raw) if sl_raw else None
        if sl is None:
            client = tradeManager.mt5._client_for(account)
            price = client.tick_price(symbol, direction)
            from services.common.config import config as _config
            default_sl_pips = float(_config.get("DEFAULT_SL_XAUUSD_PIPS", 300)) if symbol.upper().startswith("XAU") else float(_config.get("DEFAULT_SL_PIPS", 100))
            point = 0.1 if symbol.upper().startswith("XAU") else 0.00001
            from .trade_utils import calcular_sl_default
            sl = calcular_sl_default(symbol, direction, price, point, default_sl_pips)
        await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=None, tp2=None)
        return

    sl = float(sl_raw) if sl_raw else None
    tp1 = float(tps[0]) if len(tps) > 0 else None
    tp2 = float(tps[1]) if len(tps) > 1 else None

    if existing_group_id is not None:
        await tradeManager.update_group_signal(existing_group_id, sl=sl, tp1=tp1, tp2=tp2)
        return

    if sl is None or tp1 is None or tp2 is None:
        log.error("[SIGNAL] Senal completa incompleta (sl=%s tp1=%s tp2=%s), abortando.", sl, tp1, tp2)
        return
    await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=tp1, tp2=tp2)


async def main():
    from services.common.env_validator import validate_trade_orchestrator
    validate_trade_orchestrator()

    from services.common.config import config as _config
    s = Settings.load()
    r = await redis_client(s["redis_url"])
    accounts = Settings.accounts()

    from services.common.n8n_notifier import N8nWebhookNotifier
    from .notifications.n8n import N8nNotifierAdapter

    notifier_adapter = None
    webhook_url = _config.get("N8N_WEBHOOK_URL", "")
    if webhook_url:
        n8n_notifier = N8nWebhookNotifier(webhook_url, token=_config.get("N8N_WEBHOOK_TOKEN", ""))
        notifier_adapter = N8nNotifierAdapter(n8n_notifier)
        log.info("N8nNotifierAdapter initialized (webhook_url=%s)", webhook_url)
    else:
        log.warning("N8N_WEBHOOK_URL not configured — trade notifications disabled")

    tradeExecutor = MT5Executor(
        accounts,
        magic=987654,
        notifier=notifier_adapter,
        trading_windows=s["trading_windows"],
        entry_wait_seconds=int(s["entry_wait_seconds"]),
        entry_poll_ms=int(s["entry_poll_ms"]),
        entry_buffer_points=float(s["entry_buffer_points"]),
        config_provider=_config,
    )
    tradeManager = TradeManager(tradeExecutor, notifier=notifier_adapter, config_provider=_config)

    async def loop_signals():
        async for msg_id, fields in xread_loop(r, Streams.SIGNALS, last_id="$"):
            if not in_windows(parse_windows(s["trading_windows"])):
                log.info("[SKIP] signal outside windows")
                continue
            try:
                await handle_signal_fields(fields, tradeManager, accounts)
            except Exception:
                log.exception("[SIGNAL] error procesando senal: %s", fields)

    asyncio.create_task(tradeManager.run_forever())
    await loop_signals()


if __name__ == "__main__":
    asyncio.run(main())
```

Notes on this rewrite: `handle_mgmt`, `loop_mgmt`, the `Streams.MGMT` consumption, `ConfigProvider`/`get_last_id`/`set_last_id` offset tracking (the old `REDIS_OFFSET_KEY` mechanism), and the `allowed_channels` filtering are all removed — `find_active_group_for_symbol`/`update_group_signal` replace the old `provider_tag == "GB_FAST"` lookup loop, and single-account operation makes the old multi-account `filtered_accounts` step unnecessary (Global Constraints: single active account).

- [ ] **Step 5: Run the tests to verify they pass**

```bash
python -m pytest services/trade_orchestrator/test_app_signal_handling.py -v
```

Expected: PASS.

- [ ] **Step 6: Confirm the file parses and has no leftover references to removed modules**

```bash
python -c "import ast; ast.parse(open('services/trade_orchestrator/app.py').read())"
grep -n "handle_mgmt\|Streams.MGMT\|ConfigProvider\|ADDON\|TradingMode" services/trade_orchestrator/app.py
```

Expected: no matches for the grep.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(trade_orchestrator): rewrite app.py for dual-TP signal handling

handle_signal_fields (new, independently testable) replaces the old
closure-based handle_signal: fast signals open a tp1_leg+runner_leg
guard pair via TradeManager.open_group, full signals either complete
an existing guard pair (update_group_signal) or open a fresh group.
handle_mgmt/loop_mgmt/Streams.MGMT consumption are removed —
management text now flows through router_parser -> n8n -> /mgmt/action
(Task 8), not through this Redis stream.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Simplify `services/common/config.py` and `config_db.py` to env-only

**Files:**
- Modify: `services/common/config_db.py` (strip all `psycopg2`/Postgres code, keep only env-var reads)
- Modify: `services/common/config.py` (strip `db_url`/`psycopg2` branches from `accounts()`/`signal_providers()`/`channel_providers()`; remove `CHANNELS_CONFIG_JSON` module-level constant)
- Delete: `services/common/config_db_loader.py`
- Delete: `services/common/config_db_migration.py`
- Delete: `services/common/config_db_schema.sql`
- Delete: `services/common/config_db_schema_full.sql`
- Test: `services/common/test_config.py`

**Interfaces:**
- Produces: `ConfigProvider.get(key, default=None)`, `.set(key, value)` — env-only, no `psycopg2` import anywhere in the module. `Settings.accounts()` reads only `json.loads(config.get("ACCOUNTS_JSON", "[]"))`.

- [ ] **Step 1: Write the failing test for env-only `ConfigProvider`**

Create `services/common/test_config.py`:
```python
import pytest
from services.common.config_db import ConfigProvider


def test_get_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SOME_TEST_KEY", "hello")
    provider = ConfigProvider()
    assert provider.get("SOME_TEST_KEY") == "hello"


def test_get_returns_default_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)
    provider = ConfigProvider()
    assert provider.get("MISSING_TEST_KEY", "fallback") == "fallback"


def test_set_writes_to_environment(monkeypatch):
    provider = ConfigProvider()
    provider.set("ANOTHER_TEST_KEY", "written")
    import os
    assert os.environ["ANOTHER_TEST_KEY"] == "written"


def test_get_accounts_reads_accounts_json_env(monkeypatch):
    monkeypatch.setenv("ACCOUNTS_JSON", '[{"name": "acct1", "active": true}]')
    provider = ConfigProvider()
    accounts = provider.get_accounts()
    assert accounts == [{"name": "acct1", "active": True}]


def test_config_provider_has_no_psycopg2_dependency():
    import services.common.config_db as mod
    import inspect
    source = inspect.getsource(mod)
    assert "psycopg2" not in source
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest services/common/test_config.py -v
```

Expected: FAIL on `test_config_provider_has_no_psycopg2_dependency`.

- [ ] **Step 3: Rewrite `config_db.py` as env-only**

Replace the entire content of `services/common/config_db.py`:
```python
"""
config_db.py
Proveedor de configuracion basado unicamente en variables de entorno.
Sustituye la version anterior que soportaba un backend Postgres —
eliminado junto con backend_admin.
"""
import json
import logging
import os
from typing import Any

log = logging.getLogger("config_db")


class ConfigProvider:
    """Lee/escribe configuracion desde variables de entorno."""

    def get(self, key: str, default: Any = None) -> Any:
        return os.environ.get(key, default)

    def set(self, key: str, value: str) -> None:
        os.environ[key] = value

    def get_accounts(self) -> list[dict]:
        return json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

    def get_signal_providers(self) -> list[dict]:
        return []

    def get_account_channels(self, account_id: int) -> list[int]:
        for acc in self.get_accounts():
            if acc.get("id") == account_id:
                return acc.get("allowed_channels", [])
        return []

    def get_channel_providers(self) -> dict[int, list]:
        return {}

    def close(self) -> None:
        pass
```

- [ ] **Step 4: Simplify `config.py`'s `accounts()`/`signal_providers()`/`channel_providers()` and drop `CHANNELS_CONFIG_JSON`**

```bash
grep -n "db_url\|CHANNELS_CONFIG_JSON" services/common/config.py
```

Change:
```python
config = ConfigProvider()
FAST_UPDATE_WINDOW_SECONDS: float = float(config.get("FAST_UPDATE_WINDOW_SECONDS", 30))
CHANNELS_CONFIG_JSON: str = config.get("CHANNELS_CONFIG_JSON", "{}")
```
to:
```python
config = ConfigProvider()
FAST_UPDATE_WINDOW_SECONDS: float = float(config.get("FAST_UPDATE_WINDOW_SECONDS", 30))
```

Change:
```python
    @staticmethod
    def accounts() -> list[dict]:
        db_url = config.db_url
        try:
            import psycopg2
            if db_url:
                conn = psycopg2.connect(db_url)
                from services.common.config_db_loader import load_accounts
                return load_accounts(conn)
        except ImportError:
            pass
        return json.loads(config.get("ACCOUNTS_JSON", "[]"))

    @staticmethod
    def signal_providers() -> list[dict]:
        db_url = config.db_url
        try:
            import psycopg2
            if db_url:
                conn = psycopg2.connect(db_url)
                from services.common.config_db_loader import load_signal_providers
                return load_signal_providers(conn)
        except ImportError:
            pass
        return []

    @staticmethod
    def channel_providers() -> dict:
        db_url = config.db_url
        try:
            import psycopg2
            if db_url:
                conn = psycopg2.connect(db_url)
                from services.common.config_db_loader import load_channel_providers
                return load_channel_providers(conn)
        except ImportError:
            pass
        return {}
```
to:
```python
    @staticmethod
    def accounts() -> list[dict]:
        return json.loads(config.get("ACCOUNTS_JSON", "[]"))

    @staticmethod
    def signal_providers() -> list[dict]:
        return []

    @staticmethod
    def channel_providers() -> dict:
        return {}
```

- [ ] **Step 5: Delete the now-unused Postgres-backed config modules and schema files**

```bash
git rm services/common/config_db_loader.py services/common/config_db_migration.py services/common/config_db_schema.sql services/common/config_db_schema_full.sql
```

- [ ] **Step 6: Confirm no remaining references to the deleted modules**

```bash
grep -rn "config_db_loader\|config_db_migration\|config_db_schema" services/ tests/
grep -rn "\.db_url" services/
```

Expected: no output.

- [ ] **Step 7: Run the test to verify it passes**

```bash
python -m pytest services/common/test_config.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 8: Run the full test suite**

```bash
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task7_pytest.txt
```

Expected: no new failures relative to Task 6's state.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(common): simplify ConfigProvider to env-only, drop Postgres support

config_db.py no longer imports psycopg2. Removes config_db_loader.py,
config_db_migration.py, and the .sql schema files. Settings.accounts()/
signal_providers()/channel_providers() simplified to match. Drops
CHANNELS_CONFIG_JSON.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 8: Fix `mt5_executor.py`'s remaining imports, fix `tests/test_orchestrator.py`, remove dead code

**Files:**
- Modify: `services/trade_orchestrator/mt5_executor.py`
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `N8nNotifierAdapter` (Task 4).
- Produces: `mt5_executor.py` imports `.notifications.n8n` instead of `.notifications.telegram`; `MT5Executor._apply_be` is fixed to actually send its order (was silently broken — see the pre-existing-bugs memory note) or removed if nothing in the rewritten `trade_manager.py` calls it (confirm with Step 1 before choosing).

- [ ] **Step 1: Confirm whether anything in the rewritten codebase still calls `MT5Executor._apply_be`, `early_partial_close`, or `open_runner_trade`**

```bash
grep -rn "_apply_be\|early_partial_close\|open_runner_trade\|find_recent_fast_trade" services/trade_orchestrator/trade_manager.py services/trade_orchestrator/app.py
```

Expected: no matches (Task 5/6 replaced all of this with `TradeManager.open_group`/`_force_runner_sl`/`_apply_trailing`, which call `order_send` directly, not through `MT5Executor`). If confirmed unused, these three methods plus `find_recent_fast_trade` become dead code — delete them from `mt5_executor.py` in Step 3 rather than fixing `_apply_be`'s bug for code nothing calls.

- [ ] **Step 2: Fix the Telegram import**

```bash
grep -n "notifications.telegram\|TelegramNotifierAdapter" services/trade_orchestrator/mt5_executor.py
```

Change:
```python
from .notifications.telegram import TelegramNotifierAdapter
```
to:
```python
from .notifications.n8n import N8nNotifierAdapter
```
(If `TelegramNotifierAdapter` is referenced elsewhere in the file, e.g. as a type hint or default, replace with `N8nNotifierAdapter`.)

- [ ] **Step 3: Remove dead methods confirmed unused in Step 1**

Delete `open_runner_trade`, `early_partial_close`, `_apply_be`, `find_recent_fast_trade` from `services/trade_orchestrator/mt5_executor.py` if Step 1 confirmed nothing calls them. Keep `modify_sl`, `_client_for`, `_best_filling_order_send`, `_safe_comment`, `_notify_bg`, `open_for_accounts`/`open_complete_trade` (still constructible/importable even if `app.py` no longer calls them directly, unless Step 1's grep also shows them unused — if so, remove those too and note it in the commit message).

- [ ] **Step 4: Fix `tests/test_orchestrator.py`'s broken import and stale references**

Read the current file first:
```bash
cat tests/test_orchestrator.py
```

Change:
```python
from services.trade_orchestrator.app import TradeManager, NotifierAdapter
```
to:
```python
from services.trade_orchestrator.trade_manager import TradeManager
```
Update the rest of the test body to match the dual-TP `TradeManager` constructor/API (`TradeManager(mt5_executor, notifier=..., config_provider=...)`, `open_group`/`register_trade` no longer exists as a standalone public entry point outside `open_group`) — rewrite the test's body to open a group via `open_group` and assert on `tm.trades`, following the same pattern as `services/trade_orchestrator/test_trade_manager_dual_tp.py`'s `test_open_group_opens_two_positions_with_shared_group_id`. Remove the Hannah-handler call entirely (the method no longer exists).

- [ ] **Step 5: Run the full test suite**

```bash
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task8_pytest.txt
python -m pytest tests/test_orchestrator.py services/trade_orchestrator/ services/common/ services/router_parser/ -v
```

Expected: `tests/test_orchestrator.py` now collects and passes. No new failures relative to Task 7's state.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "fix(trade_orchestrator): fix mt5_executor Telegram import, remove dead code, fix test_orchestrator.py

mt5_executor.py now imports N8nNotifierAdapter instead of the deleted
TelegramNotifierAdapter. open_runner_trade/early_partial_close/
_apply_be/find_recent_fast_trade are removed as dead code superseded
by TradeManager's dual-TP open/BE/trailing logic (Task 5) — notably
_apply_be never actually sent its order_send call, a pre-existing
bug now moot since nothing calls it. Fixes tests/test_orchestrator.py's
long-broken NotifierAdapter import and updates it for the new
TradeManager API.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 9: Add the `/mgmt/action` HTTP endpoint to `trade_orchestrator`

**Files:**
- Modify: `services/trade_orchestrator/app.py` (mount a FastAPI app alongside the Redis consumer loop)
- Modify: `services/trade_orchestrator/requirements.txt` (add `fastapi`, `uvicorn[standard]` if missing)
- Modify: `services/trade_orchestrator/Dockerfile` (expose the HTTP port, run via uvicorn or a combined entrypoint)
- Modify: `docker-compose.yml` (expose `trade_orchestrator`'s new HTTP port)
- Test: `services/trade_orchestrator/test_mgmt_action_endpoint.py` (new)

**Interfaces:**
- Consumes: `TradeManager.apply_mgmt_action` (Task 5).
- Produces: `POST /mgmt/action` on `trade_orchestrator`, authenticated via `X-N8N-Action-Key` header against `N8N_ACTION_API_KEY` env var (dual-TP spec §7). Request/response bodies exactly as dual-TP spec §5.2.

- [ ] **Step 1: Write the failing test for the endpoint**

Create `services/trade_orchestrator/test_mgmt_action_endpoint.py`:
```python
import os
import pytest
from fastapi.testclient import TestClient

os.environ["N8N_ACTION_API_KEY"] = "test-action-key"

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.mgmt_api import create_mgmt_app

HEADERS = {"X-N8N-Action-Key": "test-action-key"}
ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1}


class DummyExecutor:
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [ACCOUNT]

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    async def notify_trade_event(self, event, **kwargs):
        pass

    async def notify(self, target, message):
        pass


@pytest.fixture
def tm_and_client():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    app = create_mgmt_app(tm)
    return tm, TestClient(app)


def test_mgmt_action_requires_api_key(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})
    assert resp.status_code == 401


def test_mgmt_action_no_active_trade_returns_200(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_active_trade"


@pytest.mark.asyncio
async def test_mgmt_action_close_now_closes_group(tm_and_client):
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})

    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"
    assert len(tm.trades) == 0
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -v
```

Expected: FAIL — `services/trade_orchestrator/mgmt_api.py` doesn't exist yet.

- [ ] **Step 3: Implement `mgmt_api.py`**

Create `services/trade_orchestrator/mgmt_api.py`:
```python
"""
mgmt_api.py
Endpoint HTTP /mgmt/action que recibe decisiones de gestion desde un
flujo n8n/Ollama externo, para mensajes del canal que el parser de
senales no reconoce (dual-TP spec seccion 5.2). Se monta junto al
consumer de Redis Streams de trade_orchestrator, en el mismo proceso,
porque necesita el TradeManager en memoria para resolver el grupo
activo por simbolo.
"""
import os
import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

log = logging.getLogger("trade_orchestrator.mgmt_api")

_action_key_header = APIKeyHeader(name="X-N8N-Action-Key", auto_error=False)


class Correction(BaseModel):
    field: str
    value: float


class MgmtActionRequest(BaseModel):
    action: str
    symbol: str
    raw_text: str
    correction: Optional[Correction] = None


def create_mgmt_app(trade_manager) -> FastAPI:
    app = FastAPI(title="trade_orchestrator-mgmt")
    action_api_key = os.getenv("N8N_ACTION_API_KEY", "")
    if not action_api_key:
        log.warning("[MGMT_API] N8N_ACTION_API_KEY no configurada - endpoint desprotegido")

    def _check_key(api_key: str | None = Depends(_action_key_header)) -> None:
        if action_api_key and api_key != action_api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key invalida o ausente.")

    @app.post("/mgmt/action", dependencies=[Depends(_check_key)])
    async def mgmt_action(req: MgmtActionRequest) -> dict:
        correction = req.correction.model_dump() if req.correction else None
        result = await trade_manager.apply_mgmt_action(
            action=req.action, symbol=req.symbol, raw_text=req.raw_text, correction=correction,
        )
        return result

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
python -m pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Mount the mgmt app alongside the Redis consumer loop in `app.py`**

In `services/trade_orchestrator/app.py`'s `main()`, after `tradeManager = TradeManager(...)`, add:
```python
    from .mgmt_api import create_mgmt_app
    import uvicorn

    mgmt_app = create_mgmt_app(tradeManager)
    mgmt_port = int(_config.get("MGMT_API_PORT", 8200))
    uvicorn_config = uvicorn.Config(mgmt_app, host="0.0.0.0", port=mgmt_port, log_level="warning")
    uvicorn_server = uvicorn.Server(uvicorn_config)
```
and change the final line of `main()` from:
```python
    asyncio.create_task(tradeManager.run_forever())
    await loop_signals()
```
to:
```python
    asyncio.create_task(tradeManager.run_forever())
    await asyncio.gather(loop_signals(), uvicorn_server.serve())
```

- [ ] **Step 6: Add `fastapi`/`uvicorn` to `trade_orchestrator/requirements.txt` if missing**

```bash
grep -n "fastapi\|uvicorn" services/trade_orchestrator/requirements.txt
```

Append `fastapi==0.111.0` and `uvicorn[standard]==0.30.1` if absent.

- [ ] **Step 7: Expose the mgmt port in `docker-compose.yml`**

In the `trade_orchestrator` service block, add:
```yaml
    ports:
      - "8200:8200"
```

- [ ] **Step 8: Run the full test suite**

```bash
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task9_pytest.txt
python -c "import ast; ast.parse(open('services/trade_orchestrator/app.py').read())"
```

Expected: no new failures relative to Task 8's state.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(trade_orchestrator): add /mgmt/action endpoint for n8n/Ollama decisions

Mounts a FastAPI app (mgmt_api.create_mgmt_app) alongside the Redis
consumer loop in the same process, so it shares TradeManager's
in-memory group state. Authenticated via X-N8N-Action-Key
(N8N_ACTION_API_KEY). Implements the five actions from dual-TP spec
section 5.2: close_now, move_sl_be_now, note_sl_hit, signal_correction,
ignore, plus the no_active_trade no-op response.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 10: Remove `backend_admin`, `market_data`, `monitoring`, and Postgres from the project

**Files:**
- Delete: `services/backend_admin/` (entire directory)
- Delete: `services/market_data/` (entire directory)
- Delete: `monitoring/` (entire directory)
- Delete: `prometheus.yml` (root)
- Delete: `promtail-config.yml` (root)
- Delete: `tests/test_backend_endpoints.py`
- Modify: `docker-compose.yml` (remove `postgres`, `backend_admin`, `market_data` services and the `pgdata` volume; remove `mt5_acct2` reference from `trade_orchestrator.depends_on`)
- Modify: `services/common/env_validator.py` (remove `validate_backend_admin`, `validate_market_data`)
- Delete: `init_config_db.sh`
- Delete/keep `validate_accounts_json.py` / `validate_accounts_json_local.py` per Step 1's inspection

**Interfaces:**
- Consumes: Task 7's env-only `config.py`.
- Produces: `docker-compose.yml` services reduced to `redis`, `mt5_acct1`, `telegram_ingestor`, `router_parser`, `trade_orchestrator` (Task 11 adds `trade_api`).

- [ ] **Step 1: Inspect `validate_accounts_json*.py` before deciding whether to delete**

```bash
grep -n "psycopg2\|CONFIG_DB_URL\|backend_admin" validate_accounts_json.py validate_accounts_json_local.py
```

Keep if env-only; delete if Postgres-dependent.

- [ ] **Step 2: Delete the three service directories and root monitoring config files**

```bash
git rm -r services/backend_admin services/market_data monitoring
git rm prometheus.yml promtail-config.yml init_config_db.sh tests/test_backend_endpoints.py
```

- [ ] **Step 3: Update `docker-compose.yml`**

Remove the `postgres`, `backend_admin`, and `market_data` service blocks entirely. In the `trade_orchestrator` service block, remove the dangling `mt5_acct2` dependency:
```yaml
    depends_on:
      mt5_acct1:
        condition: service_started
      mt5_acct2:
        condition: service_started
      redis:
        condition: service_healthy
```
becomes:
```yaml
    depends_on:
      mt5_acct1:
        condition: service_started
      redis:
        condition: service_healthy
```
Remove the `pgdata` volume from the `volumes:` section at the bottom.

- [ ] **Step 4: Validate the compose file syntax**

```bash
docker compose config --quiet
```
If Docker isn't available:
```bash
python -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"
```

- [ ] **Step 5: Remove `validate_backend_admin`/`validate_market_data` from `env_validator.py`**

Delete both functions entirely from `services/common/env_validator.py`.

- [ ] **Step 6: Confirm no remaining references to removed services**

```bash
grep -rln "backend_admin\|market_data\|CONFIG_DB_URL\|postgres\|psycopg2" services/ docker-compose.yml .env.example 2>&1
```

- [ ] **Step 7: Update `.env.example` — remove Postgres/admin/legacy blocks**

Remove:
```
# --- Admin API ---
ADMIN_USER=CHANGE_ME
ADMIN_PASS=CHANGE_ME_use_a_strong_password

# --- Base de datos de configuracion ---
CONFIG_DB_URL=postgresql://trading_user:CHANGE_ME@postgres:5432/trading_config
```
Remove:
```
# --- Configuracion de canales y parsers (JSON) ---
# CHANNELS_CONFIG_JSON={}
```
Remove:
```
# API key para el endpoint /notify (generar con: openssl rand -hex 32)
NOTIFY_API_KEY=CHANGE_ME_generate_with_openssl_rand_hex_32
```
(Task 12 adds the new n8n/trade_api env vars.)

- [ ] **Step 8: Run the full test suite**

```bash
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task10_pytest.txt
```

Expected: no new failures relative to Task 9's state.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: remove backend_admin, market_data, monitoring, and Postgres

Deletes the config-CRUD admin service, market_data service, and the
Prometheus/Alertmanager/Promtail monitoring stack. Removes postgres
and its pgdata volume from docker-compose.yml, plus the dangling
mt5_acct2 depends_on reference. env_validator.py drops
validate_backend_admin/validate_market_data.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 11: Move `MT5Client` to `services/common` and build the `trade_api` service

**Files:**
- Modify: `services/trade_orchestrator/mt5_client.py` → **delete**, replaced by `services/common/mt5_client.py`
- Modify: `services/trade_orchestrator/mt5_executor.py` (update import path)
- Modify: `services/trade_orchestrator/trade_manager.py` (update import path if it imports `mt5_client` directly)
- Create: `services/trade_api/__init__.py`, `app.py`, `Dockerfile`, `requirements.txt`
- Modify: `docker-compose.yml` (add `trade_api` service)
- Test: `services/trade_api/test_app.py`

**Interfaces:**
- Consumes: `services/common/mt5_client.py`'s `MT5Client(host: str, port: int)` — `get_pip_size`, `symbol_info_tick`, `partial_close`, `tick_price`, `positions_get`, `order_send`, `symbol_info`, `symbol_select`.
- Produces: FastAPI app in `services/trade_api/app.py` exposing `POST /trades`, `PATCH /trades/{ticket}`, `DELETE /trades/{ticket}`, `GET /trades`, `GET /trades/{ticket}`, `GET /health`, behind `X-API-Key`/`TRADE_API_KEY` auth, reading account host/port from `ACCOUNTS_JSON`'s first active entry.

- [ ] **Step 1: Verify `mt5_client.py`'s only cross-file dependency before moving it**

```bash
grep -n "^import\|^from" services/trade_orchestrator/mt5_client.py
grep -rln "trade_orchestrator.mt5_client\|from \.mt5_client\|from mt5_client" services/
```

- [ ] **Step 2: Move the file**

```bash
git mv services/trade_orchestrator/mt5_client.py services/common/mt5_client.py
```

- [ ] **Step 3: Update the import in `mt5_executor.py`**

```bash
grep -n "mt5_client" services/trade_orchestrator/mt5_executor.py
```
Change to `from services.common.mt5_client import MT5Client`.

- [ ] **Step 4: Update the import in `trade_manager.py` if present**

```bash
grep -n "mt5_client" services/trade_orchestrator/trade_manager.py
```
Apply the same change if found (Task 5's rewrite may already reference `MT5Client` — confirm and update the import path if so).

- [ ] **Step 5: Run the orchestrator tests to confirm the move didn't break anything**

```bash
python -m pytest services/trade_orchestrator/ -v
python -c "import ast; ast.parse(open('services/trade_orchestrator/mt5_executor.py').read())"
```

- [ ] **Step 6: Commit the move**

```bash
git add -A
git commit -m "refactor: move MT5Client to services/common

MT5Client is pure MT5/RPyC execution logic with no dependency on
trade_orchestrator's streams/state handling, so it belongs in
services/common where the new trade_api service can import it too.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Write the failing test for the new `trade_api` service**

Create `services/trade_api/test_app.py`:
```python
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from fastapi.testclient import TestClient

os.environ["TRADE_API_KEY"] = "test-key-123"
os.environ["ACCOUNTS_JSON"] = '[{"name": "acct1", "host": "mt5_acct1", "port": 8001, "active": true}]'

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_api.app import app, get_mt5_client

HEADERS = {"X-API-Key": "test-key-123"}


@pytest.fixture
def client():
    sim = SimuladorMT5()
    sim.price = 2500.0
    app.dependency_overrides[get_mt5_client] = lambda: sim
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_does_not_require_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_open_trade_requires_api_key(client):
    resp = client.post("/trades", json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    assert resp.status_code == 401


def test_open_trade_creates_position(client):
    resp = client.post(
        "/trades", headers=HEADERS,
        json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticket"] > 0
    assert body["symbol"] == "XAUUSD"


def test_get_trades_lists_open_positions(client):
    client.post("/trades", headers=HEADERS, json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    resp = client.get("/trades", headers=HEADERS)
    assert resp.status_code == 200
    trades = resp.json()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "XAUUSD"


def test_modify_trade_updates_sl_tp(client):
    open_resp = client.post("/trades", headers=HEADERS, json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    ticket = open_resp.json()["ticket"]
    resp = client.patch(f"/trades/{ticket}", headers=HEADERS, json={"sl": 2495.0})
    assert resp.status_code == 200
    assert resp.json()["sl"] == 2495.0


def test_close_trade_returns_ok(client):
    open_resp = client.post("/trades", headers=HEADERS, json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    ticket = open_resp.json()["ticket"]
    resp = client.delete(f"/trades/{ticket}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


def test_close_unknown_ticket_returns_404(client):
    resp = client.delete("/trades/999999", headers=HEADERS)
    assert resp.status_code == 404
```

- [ ] **Step 8: Run the test to verify it fails**

```bash
python -m pytest services/trade_api/test_app.py -v
```

Expected: FAIL — `services.trade_api` doesn't exist yet.

- [ ] **Step 9: Implement `services/trade_api/app.py`**

Create `services/trade_api/__init__.py` (empty).

Create `services/trade_api/app.py`:
```python
"""
trade_api/app.py
Servicio HTTP independiente para abrir, modificar, cerrar y consultar
trades en MT5 desde aplicaciones externas. No depende del estado en
memoria de trade_orchestrator: opera MT5 directamente via MT5Client.
Ver docs/superpowers/specs/2026-09-03-tradepulse-only-simplification-design.md
seccion 6.
"""
import logging
import os

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from services.common.config import Settings
from services.common.mt5_client import MT5Client

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("trade_api")

app = FastAPI(title="trade_api")

TRADE_API_KEY = os.getenv("TRADE_API_KEY", "")
if not TRADE_API_KEY:
    log.warning("[TRADE_API] TRADE_API_KEY no configurada - endpoints desprotegidos")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
MAGIC = 987654


def _check_api_key(api_key: str | None = Depends(_api_key_header)) -> None:
    if TRADE_API_KEY and api_key != TRADE_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key invalida o ausente. Incluir header X-API-Key.")


_client_singleton: MT5Client | None = None


def get_mt5_client() -> MT5Client:
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton
    accounts = Settings.accounts()
    account = next((a for a in accounts if a.get("active")), None)
    if not account:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No hay cuenta MT5 activa configurada")
    _client_singleton = MT5Client(host=account["host"], port=int(account["port"]))
    return _client_singleton


class OpenTradeRequest(BaseModel):
    symbol: str
    direction: str
    volume: float
    sl: float
    tp: float | None = None


class ModifyTradeRequest(BaseModel):
    sl: float | None = None
    tp: float | None = None


class TradeResponse(BaseModel):
    ticket: int
    symbol: str
    direction: str
    volume: float
    sl: float
    tp: float


def _position_to_response(pos) -> TradeResponse:
    direction = "BUY" if getattr(pos, "type", 0) == 0 else "SELL"
    return TradeResponse(
        ticket=int(pos.ticket), symbol=pos.symbol, direction=direction,
        volume=float(pos.volume), sl=float(getattr(pos, "sl", 0.0)), tp=float(getattr(pos, "tp", 0.0)),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/trades", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_check_api_key)])
async def open_trade(req: OpenTradeRequest, client: MT5Client = Depends(get_mt5_client)) -> TradeResponse:
    order_type = 0 if req.direction.upper() == "BUY" else 1
    price = client.tick_price(req.symbol, req.direction.upper())
    if not price:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"No se pudo obtener precio para {req.symbol}")
    request_payload = {
        "action": 1, "symbol": req.symbol, "volume": float(req.volume), "type": order_type,
        "price": float(price), "sl": float(req.sl), "tp": float(req.tp) if req.tp is not None else 0.0,
        "deviation": 50, "magic": MAGIC, "comment": "trade_api", "type_time": 0, "type_filling": 1,
    }
    res = client.order_send(request_payload)
    if not res or getattr(res, "retcode", None) != 10009:
        detail = getattr(res, "comment", "order_send failed") if res else "no response from MT5"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    ticket = int(res.order)
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Orden ejecutada pero posicion no encontrada")
    return _position_to_response(pos_list[0])


@app.get("/trades", dependencies=[Depends(_check_api_key)])
async def list_trades(client: MT5Client = Depends(get_mt5_client)) -> list[TradeResponse]:
    positions = client.positions_get() or []
    return [_position_to_response(p) for p in positions]


@app.get("/trades/{ticket}", dependencies=[Depends(_check_api_key)])
async def get_trade(ticket: int, client: MT5Client = Depends(get_mt5_client)) -> TradeResponse:
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket} no encontrado")
    return _position_to_response(pos_list[0])


@app.patch("/trades/{ticket}", dependencies=[Depends(_check_api_key)])
async def modify_trade(ticket: int, req: ModifyTradeRequest, client: MT5Client = Depends(get_mt5_client)) -> TradeResponse:
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket} no encontrado")
    pos = pos_list[0]
    new_sl = req.sl if req.sl is not None else float(getattr(pos, "sl", 0.0))
    new_tp = req.tp if req.tp is not None else float(getattr(pos, "tp", 0.0))
    request_payload = {"action": 6, "position": ticket, "sl": float(new_sl), "tp": float(new_tp)}
    res = client.order_send(request_payload)
    if not res or getattr(res, "retcode", None) != 10009:
        detail = getattr(res, "comment", "order_send failed") if res else "no response from MT5"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    pos_list = client.positions_get(ticket=ticket)
    return _position_to_response(pos_list[0])


@app.delete("/trades/{ticket}", dependencies=[Depends(_check_api_key)])
async def close_trade(ticket: int, client: MT5Client = Depends(get_mt5_client)) -> dict:
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket} no encontrado")
    ok = client.partial_close({}, ticket, 100)
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="No se pudo cerrar la posicion")
    return {"status": "closed", "ticket": ticket}
```

Create `services/trade_api/requirements.txt`:
```
fastapi==0.111.0
uvicorn[standard]==0.30.1
mt5linux==0.1.9
```

Create `services/trade_api/Dockerfile`:
```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY services ./services

RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential
RUN pip install --no-cache-dir -r services/trade_api/requirements.txt

CMD ["uvicorn", "services.trade_api.app:app", "--host", "0.0.0.0", "--port", "8100"]
```

- [ ] **Step 10: Run the test to verify it passes**

```bash
python -m pytest services/trade_api/test_app.py -v
```

Expected: PASS (7 tests). If `SimuladorMT5` lacks a method the app calls, check `tests/test_simulador_mt5.py` first and extend the test fixture locally rather than the shared simulator.

- [ ] **Step 11: Add `trade_api` to `docker-compose.yml`**

```yaml
  trade_api:
    build:
      context: .
      dockerfile: services/trade_api/Dockerfile
    container_name: atp-trade-api
    env_file:
      - .env
    ports:
      - "8100:8100"
    depends_on:
      mt5_acct1:
        condition: service_healthy
    restart: unless-stopped
```

- [ ] **Step 12: Validate compose syntax and run the full suite**

```bash
docker compose config --quiet
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task11_pytest.txt
```

- [ ] **Step 13: Commit**

```bash
git add -A
git commit -m "feat: add trade_api service for external trade control

New standalone FastAPI service exposing POST/GET/PATCH/DELETE
/trades, authenticated via X-API-Key (TRADE_API_KEY). Operates MT5
directly through the now-shared services/common/mt5_client.py,
independent of trade_orchestrator's dual-TP group state — trades
opened here are plain MT5 positions without group_id/leg tracking,
by design.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 12: Update env vars, README, and remaining docs

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `DEPLOYMENT.md`, `IMPLEMENTATION_SUMMARY.md` (if they reference removed services/behavior)

**Interfaces:**
- Consumes: nothing new — documentation and env template only.

- [ ] **Step 1: Add the new env vars to `.env.example`**

```
# --- Notificaciones (webhook n8n) ---
N8N_WEBHOOK_URL=https://your-n8n-instance.example.com/webhook/trades
N8N_WEBHOOK_TOKEN=

# --- Mensajes no reconocidos como senal -> n8n/Ollama ---
N8N_INBOUND_WEBHOOK_URL=https://your-n8n-instance.example.com/webhook/inbound

# --- /mgmt/action (trade_orchestrator recibe decisiones de n8n/Ollama) ---
N8N_ACTION_API_KEY=CHANGE_ME_generate_with_openssl_rand_hex_32
MGMT_API_PORT=8200

# --- trade_api (control externo de trades) ---
TRADE_API_KEY=CHANGE_ME_generate_with_openssl_rand_hex_32
```

Remove `TELEGRAM_INGESTOR_URL` if `grep -rn "TELEGRAM_INGESTOR_URL" services/` shows no remaining uses.

Update the `ACCOUNTS_JSON` comment to clarify single-account-for-now.

- [ ] **Step 2: Rewrite the relevant sections of `README.md`**

Read the current file first, then update it to describe: single provider (TradePulse), single active MT5 account, the dual-TP opening model (two positions per signal, mechanical BE-on-TP1 + uncapped proportional trailing on the runner), the `/mgmt/action` endpoint and its relationship to the external n8n/Ollama flow, notifications via n8n webhook (not Telegram), no Postgres/backend_admin, and `trade_api`'s endpoints with a curl example. Remove or rewrite the "Modalidad de trading por cuenta" section (`general`/`be_pips`/`be_pnl` no longer exist — dual-TP is now the only mode) and any references to `allowed_channels` implying multiple concurrent providers.

- [ ] **Step 3: Check and update `DEPLOYMENT.md` and `IMPLEMENTATION_SUMMARY.md`**

```bash
grep -iln "backend_admin\|market_data\|postgres\|prometheus\|alertmanager\|promtail\|hannah\|torofx\|goldbro\|limitless\|be_pips\|be_pnl\|gestionar_trade" DEPLOYMENT.md IMPLEMENTATION_SUMMARY.md
```

Update or remove matching sections.

- [ ] **Step 4: Confirm no stale references remain across the repo**

```bash
grep -rln "backend_admin\|market_data\|CONFIG_DB_URL\|ADMIN_USER\|ADMIN_PASS\|CHANNELS_CONFIG_JSON\|TelegramNotifierAdapter\|RemoteTelegramNotifier\|GB_FAST\|handle_hannah\|handle_torofx\|torofx_provider_tag_match\|TradingMode\|gestionar_trade\|Streams.MGMT" --include="*.py" --include="*.md" --include="*.yml" --include="*.example" .
```

Expected: no output.

- [ ] **Step 5: Run the full test suite one last time**

```bash
python -m pytest -m "not integration" -q 2>&1 | tee /tmp/task12_pytest.txt
```

Expected: zero failures for `not integration` tests.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: update README, .env.example, deployment docs for the dual-TP TradePulse setup

Documents the dual-TP opening/management model, the /mgmt/action
endpoint and its n8n/Ollama contract, the n8n webhook notification
flow, and trade_api's endpoints. Removes references to backend_admin,
market_data, Postgres, non-TradePulse providers, and the retired
trading_mode system.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** tradepulse-only-simplification spec §2 (scope) → Tasks 1, 10; §3 (architecture) → Task 10 (compose), Task 11 (trade_api); §4 (router_parser) → Tasks 2, 3; §6 (trade_api) → Task 11; §7 (config/env) → Tasks 7, 12; §8 (testing) → woven into every task; §9 (docs) → Task 12. dual-tp-management spec §3 (opening) → Task 5 (`open_group`/`update_group_signal`) + Task 6 (`handle_signal_fields`); §4 (mechanical management) → Task 5 (`run_forever`/`_tick_once_account`/`_apply_trailing`); §5 (n8n/Ollama exceptions) → Task 3 (outbound forward) + Task 9 (`/mgmt/action`); §6 (final trade_manager.py scope) → Task 5 Step 1's deletion checklist; §7 (risks: unit<=0 guard, `/mgmt/action` auth) → Task 5's `open_group` abort logic, Task 9's `N8N_ACTION_API_KEY` auth.
- **Pre-existing bugs surfaced during planning research, addressed rather than silently inherited:** `MT5Executor._apply_be` never sent its `order_send` call (Task 8 removes it as dead code once confirmed unused, rather than reusing it for the new BE mechanic); `trade_utils.calcular_trailing_retroceso` hardcodes pip conversion ignoring its `point` parameter (Task 5's new `_apply_trailing` does not call it — the new proportional formula is implemented directly); `tests/test_orchestrator.py`'s `NotifierAdapter` import never resolved to a real symbol (Task 8 fixes it); two parallel management loops existed in the old `trade_manager.py` (`run_forever`/`_tick_once_account` vs. unused `_tick_once` with an extra "250 pips" rule) — Task 5 keeps only the live one's name/shape, discarding the unused loop entirely rather than preserving its extra rule as hidden behavior.
- **Type/interface consistency:** `TradeManager.open_group`/`update_group_signal`/`find_active_group_for_symbol`/`apply_mgmt_action` signatures are defined once in Task 5 and used identically by Task 6 (`handle_signal_fields`) and Task 9 (`mgmt_api.py`) — no drift. `N8nWebhookNotifier.send_event`/`N8nNotifierAdapter.notify_trade_event` (Task 4) are the only notifier methods called anywhere in Tasks 5-6 — confirmed no code calls a `notify_trade_opened`-shaped method that doesn't exist on the new adapter.
- **No placeholders:** every task has literal file contents, exact test code, exact commands, and expected outputs.
