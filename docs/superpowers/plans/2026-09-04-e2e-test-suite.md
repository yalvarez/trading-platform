# E2E Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `tests/e2e/` suite that sends real Telegram messages to a
dedicated test channel and verifies the full pipeline (Telegram →
`telegram_ingestor` → Redis → `router_parser` → `trade_orchestrator` → MT5
demo account) behaves correctly, across 15 scenarios in 3 families, runnable
on the VPS via `docker compose --profile e2e run --rm e2e_runner`.

**Architecture:** Four reusable async helpers (`price_reader`,
`telegram_sender`, `vps_observer`, plus a `runner.py` CLI) live in
`tests/e2e/`, imported by one file per scenario under `tests/e2e/scenarios/`.
Helpers are unit-tested locally with mocks (RPyC/Telethon/Redis), following
the existing `DummyExecutor`/`DummyNotifier` pattern from
`services/trade_orchestrator/test_trade_manager_dual_tp.py`. Scenarios
themselves are only exercised for real, against the VPS. A small,
independent fix lands first in `trade_manager.py` so every `_notify(...)`
call also emits a `log.info` line the log-based parts of `vps_observer` can
grep for reliably.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio (existing), Telethon
(existing dependency, new session), `redis.asyncio` (existing), the existing
RPyC pool client pattern (`services/trade_orchestrator/mt5_pool.py`), FastAPI
test patterns already used in `test_mgmt_action_endpoint.py` are NOT needed
here (no new HTTP endpoint — the suite is a client, not a server).

**Spec:** [docs/superpowers/specs/2026-09-04-e2e-test-suite-design.md](../specs/2026-09-04-e2e-test-suite-design.md)

## Global Constraints

- No new ports are published to the host — `e2e_runner` shares the existing
  Docker internal network only (spec §3.2).
- `TG_TEST_CHAT_ID` must be the `chat_id` of a **dedicated test channel**
  added to `allowed_channels` in `ACCOUNTS_JSON`, never the real TradePulse
  channel (spec §3.1, §4).
- XAUUSD's entry-range wait is a hard **5 seconds** in `trade_manager.py`
  (`entry_wait_max = 5.0 if is_gold else entry_wait_seconds`) — every
  scenario that sends a full `SIGNAL ALERT` must read the price immediately
  before sending and use a wide entry range (spec §5, Familia A note).
- Real market price cannot be forced to hit TP1 — Family A scenarios use a
  TP1 close to the read price and report **inconclusive** (not fail) on
  timeout, never assume determinism (spec §5, §7).
- n8n/Ollama is real, not mocked, for Family B (spec §2 "Fuera de alcance").
  Its inbound webhook and `/mgmt/action` callback must point at this VPS,
  not production (spec §4) — this is an operator precondition, not code this
  plan builds.
- Redis stream names are `raw_messages` (`Streams.RAW`) and `parsed_signals`
  (`Streams.SIGNALS`) — there is no "management" stream (spec §3.1).
- Every scenario that opens a position must close it — via its own assertion
  flow or an emergency cleanup keyed by `group_id` (spec §6).

---

## File Structure

```
services/trade_orchestrator/trade_manager.py     # MODIFY: log.info in _notify

tests/e2e/
  __init__.py
  requirements.txt          # telethon, redis, httpx (rpyc via existing mt5 pool import)
  Dockerfile
  config.py                 # env var loading (TG_TEST_CHAT_ID, ACCOUNTS_JSON, etc.)
  price_reader.py           # PriceReader: reads XAUUSD tick from mt5_acct1 via RPyC
  telegram_sender.py        # TelegramSender: sends messages via Telethon
  vps_observer.py           # VpsObserver: docker logs + Redis streams + MT5 positions
  preflight.py              # pre-flight checklist (spec §7)
  runner.py                 # CLI entrypoint: --scenario NAME | --all
  scenarios/
    __init__.py
    base.py                 # ScenarioContext, ScenarioResult, ScenarioOutcome types
    a1_fast_only.py
    a2_fast_then_full_early.py
    a3_fast_then_full_late.py
    a4_full_only.py
    b1_be_variant1.py
    b2_be_variant2.py
    b3_be_variant3.py
    b4_forced_close.py
    b5_signal_correction.py
    b6_milestone_noop.py
    b7_sl_hit_note.py
    b8_spam_noop.py
    c1_dedup.py
    c2_unrecognized_to_n8n.py
    c3_entry_range_dash_variants.py

tests/e2e/test_price_reader.py       # unit tests (mocked RPyC)
tests/e2e/test_telegram_sender.py    # unit tests (mocked Telethon)
tests/e2e/test_vps_observer.py       # unit tests (mocked Redis + subprocess)
tests/e2e/test_preflight.py          # unit tests (mocked checks)

docker-compose.yml                   # MODIFY: add e2e_runner service
```

**Responsibility boundaries:**
- `price_reader.py`: only reads price. No message construction.
- `telegram_sender.py`: only sends text to a chat_id. No message construction.
- `vps_observer.py`: only reads state (logs/Redis/MT5). No assertions — returns data, scenarios assert on it.
- `scenarios/*.py`: own message construction, orchestration order, and assertions for their one scenario.
- `runner.py`: only wires the above together per the CLI args and prints the report — no scenario-specific logic.

---

## Task 1: Reliable log line for every trade-manager notification

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py:53-59`
- Test: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: nothing new — `TradeManager._notify(self, event: str, **kwargs) -> None` already exists.
- Produces: every call to `_notify` now also emits `log.info("[TM][EVENT] %s %s", event, kwargs)` — later tasks' `vps_observer` greps container logs for the literal substring `[TM][EVENT] <event_name>` (e.g. `[TM][EVENT] open_aborted`).

- [ ] **Step 1: Write the failing test**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:

```python
@pytest.mark.asyncio
async def test_notify_emits_log_line_for_every_event(caplog):
    import logging
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    with caplog.at_level(logging.INFO, logger="trade_orchestrator.trade_manager"):
        await tm._notify("group_opened", group_id=42, symbol="XAUUSD")

    assert any(
        "[TM][EVENT]" in r.message and "group_opened" in r.message
        for r in caplog.records
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py::test_notify_emits_log_line_for_every_event -v`
Expected: FAIL — no `[TM][EVENT]` substring in any log record yet.

- [ ] **Step 3: Write minimal implementation**

Replace in `services/trade_orchestrator/trade_manager.py`:

```python
    async def _notify(self, event: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.notify_trade_event(event, **kwargs)
        except Exception as e:
            log.warning("[TM] notify failed for event=%s: %s", event, e)
```

with:

```python
    async def _notify(self, event: str, **kwargs) -> None:
        log.info("[TM][EVENT] %s %s", event, kwargs)
        if not self.notifier:
            return
        try:
            await self.notifier.notify_trade_event(event, **kwargs)
        except Exception as e:
            log.warning("[TM] notify failed for event=%s: %s", event, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py::test_notify_emits_log_line_for_every_event -v`
Expected: PASS

- [ ] **Step 5: Run the full existing orchestrator test suite to check for regressions**

Run: `pytest services/trade_orchestrator/ -v`
Expected: all PASS (this change only adds a log line, no behavior change)

- [ ] **Step 6: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat(trade_orchestrator): log every _notify event for e2e observability

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: `tests/e2e/` package scaffolding + config loader

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/config.py`
- Create: `tests/e2e/requirements.txt`
- Test: `tests/e2e/test_config.py`

**Interfaces:**
- Produces:
  - `E2EConfig` dataclass with fields: `redis_url: str`, `tg_test_chat_id: int`, `tg_api_id: str`, `tg_api_hash: str`, `tg_phone: str`, `mt5_host: str`, `mt5_port: int`, `n8n_action_api_key: str`, `mgmt_api_port: int`, `trade_orchestrator_host: str`.
  - `load_config() -> E2EConfig` — reads from `os.environ`, raises `RuntimeError` with a clear message naming every missing required variable (collect all missing before raising, don't fail on the first one).

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_config.py
import pytest
from tests.e2e.config import load_config, E2EConfig


def test_load_config_reads_all_fields(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("TG_TEST_CHAT_ID", "-1009999999999")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.setenv("TG_PHONE", "+10000000000")
    monkeypatch.setenv("MT5_HOST", "mt5_acct1")
    monkeypatch.setenv("MT5_PORT", "8001")
    monkeypatch.setenv("N8N_ACTION_API_KEY", "key")
    monkeypatch.setenv("MGMT_API_PORT", "8200")
    monkeypatch.setenv("TRADE_ORCHESTRATOR_HOST", "trade_orchestrator")

    cfg = load_config()

    assert cfg == E2EConfig(
        redis_url="redis://redis:6379/0",
        tg_test_chat_id=-1009999999999,
        tg_api_id="123",
        tg_api_hash="abc",
        tg_phone="+10000000000",
        mt5_host="mt5_acct1",
        mt5_port=8001,
        n8n_action_api_key="key",
        mgmt_api_port=8200,
        trade_orchestrator_host="trade_orchestrator",
    )


def test_load_config_raises_with_all_missing_vars_named(monkeypatch):
    for var in ("REDIS_URL", "TG_TEST_CHAT_ID", "TG_API_ID", "TG_API_HASH",
                "TG_PHONE", "MT5_HOST", "MT5_PORT", "N8N_ACTION_API_KEY",
                "MGMT_API_PORT", "TRADE_ORCHESTRATOR_HOST"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(RuntimeError) as exc:
        load_config()

    assert "REDIS_URL" in str(exc.value)
    assert "TG_TEST_CHAT_ID" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/__init__.py
```

```python
# tests/e2e/config.py
"""
Environment configuration for the e2e test suite. All values come from the
same .env the rest of the platform uses (docker-compose env_file), plus a
few e2e-only vars for reaching mt5_acct1/trade_orchestrator by service name
inside the docker-compose network.
"""
import os
from dataclasses import dataclass


REQUIRED_VARS = (
    "REDIS_URL", "TG_TEST_CHAT_ID", "TG_API_ID", "TG_API_HASH", "TG_PHONE",
    "MT5_HOST", "MT5_PORT", "N8N_ACTION_API_KEY", "MGMT_API_PORT",
    "TRADE_ORCHESTRATOR_HOST",
)


@dataclass(frozen=True)
class E2EConfig:
    redis_url: str
    tg_test_chat_id: int
    tg_api_id: str
    tg_api_hash: str
    tg_phone: str
    mt5_host: str
    mt5_port: int
    n8n_action_api_key: str
    mgmt_api_port: int
    trade_orchestrator_host: str


def load_config() -> E2EConfig:
    missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(
            f"Missing required e2e env vars: {', '.join(missing)}"
        )
    return E2EConfig(
        redis_url=os.environ["REDIS_URL"],
        tg_test_chat_id=int(os.environ["TG_TEST_CHAT_ID"]),
        tg_api_id=os.environ["TG_API_ID"],
        tg_api_hash=os.environ["TG_API_HASH"],
        tg_phone=os.environ["TG_PHONE"],
        mt5_host=os.environ["MT5_HOST"],
        mt5_port=int(os.environ["MT5_PORT"]),
        n8n_action_api_key=os.environ["N8N_ACTION_API_KEY"],
        mgmt_api_port=int(os.environ["MGMT_API_PORT"]),
        trade_orchestrator_host=os.environ["TRADE_ORCHESTRATOR_HOST"],
    )
```

```
# tests/e2e/requirements.txt
telethon>=1.36,<2
redis>=5,<6
httpx>=0.27,<1
rpyc>=6,<7
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/__init__.py tests/e2e/config.py tests/e2e/requirements.txt tests/e2e/test_config.py
git commit -m "feat(e2e): add tests/e2e package scaffolding and env config loader

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: `price_reader.py`

**Files:**
- Create: `tests/e2e/price_reader.py`
- Test: `tests/e2e/test_price_reader.py`

**Interfaces:**
- Consumes: `E2EConfig` from Task 2 (`mt5_host`, `mt5_port`).
- Produces:
  - `PriceReader(host: str, port: int)` class.
  - `async def read_price(self, symbol: str = "XAUUSD") -> float` — returns the mid price (`(bid+ask)/2`), raising `RuntimeError` if the tick is empty/unavailable after retries.
  - Retry behavior mirrors `trade_manager._get_price_with_retry` (`services/trade_orchestrator/trade_manager.py:72`): 3 attempts, 0.15s delay, same "survive transient empty tick" logic (memory: `cb227ad fix: retry initial price read in open_group`).

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_price_reader.py
import pytest
from unittest.mock import MagicMock
from tests.e2e.price_reader import PriceReader


class FakeTick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


def _rpyc_client_returning(tick):
    client = MagicMock()
    client.root.symbol_info_tick.return_value = tick
    return client


@pytest.mark.asyncio
async def test_read_price_returns_mid_price(monkeypatch):
    fake_client = _rpyc_client_returning(FakeTick(bid=2499.5, ask=2500.5))
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0


@pytest.mark.asyncio
async def test_read_price_retries_on_empty_tick_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_tick(symbol):
        calls["n"] += 1
        if calls["n"] < 3:
            return None
        return FakeTick(bid=2499.0, ask=2501.0)

    fake_client = MagicMock()
    fake_client.root.symbol_info_tick.side_effect = fake_tick
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_read_price_raises_after_exhausting_retries(monkeypatch):
    fake_client = MagicMock()
    fake_client.root.symbol_info_tick.return_value = None
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    with pytest.raises(RuntimeError):
        await reader.read_price("XAUUSD")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_price_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.price_reader'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/price_reader.py
"""
Reads the current XAUUSD tick directly from mt5_acct1 over RPyC — the same
source of truth trade_orchestrator uses (services/trade_orchestrator/mt5_pool.py),
not an external price API. Used to build realistic ENTRY PRICE values for
full-signal test messages and to sanity-check the opening price the bot
recorded.
"""
import asyncio
import rpyc


class PriceReader:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    async def read_price(self, symbol: str = "XAUUSD", attempts: int = 3, delay_seconds: float = 0.15) -> float:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                client = rpyc.connect(self.host, self.port)
                tick = client.root.symbol_info_tick(symbol)
                if tick is not None and tick.bid and tick.ask:
                    return (float(tick.bid) + float(tick.ask)) / 2.0
                last_error = RuntimeError(f"empty tick for {symbol}")
            except Exception as e:
                last_error = e
            if attempt < attempts - 1:
                await asyncio.sleep(delay_seconds)
        raise RuntimeError(f"could not read price for {symbol} after {attempts} attempts: {last_error}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_price_reader.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/price_reader.py tests/e2e/test_price_reader.py
git commit -m "feat(e2e): add PriceReader for reading live XAUUSD price via RPyC

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: `telegram_sender.py`

**Files:**
- Create: `tests/e2e/telegram_sender.py`
- Test: `tests/e2e/test_telegram_sender.py`

**Interfaces:**
- Consumes: `E2EConfig` (`tg_api_id`, `tg_api_hash`, `tg_phone`, `tg_test_chat_id`).
- Produces:
  - `TelegramSender(api_id: str, api_hash: str, phone: str, session_name: str = "e2e_test_session")`.
  - `async def send(self, chat_id: int, text: str) -> int` — sends `text` to `chat_id`, returns the sent message id (used later for correlating log lines / cleanup, not required by every scenario).
  - `async def close(self) -> None` — disconnects the Telethon client cleanly; every scenario must call this in a `finally`.

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_telegram_sender.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.telegram_sender import TelegramSender


@pytest.mark.asyncio
async def test_send_calls_telethon_send_message_and_returns_id(monkeypatch):
    fake_sent_message = MagicMock(id=4242)
    fake_client = MagicMock()
    fake_client.start = AsyncMock()
    fake_client.send_message = AsyncMock(return_value=fake_sent_message)
    fake_client.disconnect = AsyncMock()

    monkeypatch.setattr(
        "tests.e2e.telegram_sender.TelegramClient",
        lambda session, api_id, api_hash: fake_client,
    )

    sender = TelegramSender(api_id="1", api_hash="h", phone="+1000000000")
    msg_id = await sender.send(chat_id=-100123, text="XAUUSD BUY NOW")

    assert msg_id == 4242
    fake_client.send_message.assert_awaited_once_with(-100123, "XAUUSD BUY NOW")
    await sender.close()
    fake_client.disconnect.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_telegram_sender.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.telegram_sender'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/telegram_sender.py
"""
Sends real Telegram messages to a dedicated e2e test channel via Telethon,
using a session separate from the bot's own telegram_ingestor session.
TG_TEST_CHAT_ID must be a channel/group already present in allowed_channels
of ACCOUNTS_JSON — see docs/superpowers/specs/2026-09-04-e2e-test-suite-design.md
section 3.1 for why plain TG_TEST_CHAT_ID alone does not make telegram_ingestor
process the message.
"""
from telethon import TelegramClient


class TelegramSender:
    def __init__(self, api_id: str, api_hash: str, phone: str, session_name: str = "e2e_test_session"):
        self.phone = phone
        self._client = TelegramClient(session_name, api_id, api_hash)
        self._started = False

    async def _ensure_started(self) -> None:
        if not self._started:
            await self._client.start(phone=self.phone)
            self._started = True

    async def send(self, chat_id: int, text: str) -> int:
        await self._ensure_started()
        sent = await self._client.send_message(chat_id, text)
        return sent.id

    async def close(self) -> None:
        if self._started:
            await self._client.disconnect()
            self._started = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_telegram_sender.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/telegram_sender.py tests/e2e/test_telegram_sender.py
git commit -m "feat(e2e): add TelegramSender for sending test messages via Telethon

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: `vps_observer.py`

**Files:**
- Create: `tests/e2e/vps_observer.py`
- Test: `tests/e2e/test_vps_observer.py`

**Interfaces:**
- Consumes: `E2EConfig` (`redis_url`, `mt5_host`, `mt5_port`); `rpyc` connection pattern from Task 3.
- Produces:
  - `VpsObserver(redis_client, mt5_host: str, mt5_port: int)`.
  - `async def read_raw_messages(self, count: int = 20) -> list[dict]` — `XRANGE raw_messages - + COUNT count` via `redis.asyncio`, returns list of `{"chat_id": str, "text": str}`.
  - `async def read_parsed_signals(self, count: int = 20) -> list[dict]` — same for `parsed_signals` (`Streams.SIGNALS`), returns the raw field dict per entry.
  - `def grep_container_logs(self, container: str, pattern: str, since: str = "5m") -> list[str]` — runs `docker logs --since <since> <container>` as a subprocess, returns matching lines containing `pattern`. Used for `[TM][EVENT] <event>` lines from Task 1.
  - `async def positions_for_symbol(self, symbol: str) -> list[dict]` — connects via `rpyc.connect(mt5_host, mt5_port)`, calls `client.root.positions_get(symbol=symbol)`, returns list of `{"ticket": int, "sl": float, "tp": float, "volume": float}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_vps_observer.py
import pytest
import subprocess
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.vps_observer import VpsObserver


@pytest.mark.asyncio
async def test_read_raw_messages_calls_xrange_on_raw_messages_stream():
    fake_redis = MagicMock()
    fake_redis.xrange = AsyncMock(return_value=[
        ("1-0", {"chat_id": "-100123", "text": "XAUUSD BUY NOW"}),
    ])
    observer = VpsObserver(redis_client=fake_redis, mt5_host="mt5_acct1", mt5_port=8001)

    messages = await observer.read_raw_messages(count=10)

    fake_redis.xrange.assert_awaited_once_with("raw_messages", "-", "+", count=10)
    assert messages == [{"chat_id": "-100123", "text": "XAUUSD BUY NOW"}]


@pytest.mark.asyncio
async def test_read_parsed_signals_calls_xrange_on_parsed_signals_stream():
    fake_redis = MagicMock()
    fake_redis.xrange = AsyncMock(return_value=[
        ("2-0", {"symbol": "XAUUSD", "direction": "BUY", "fast": "true"}),
    ])
    observer = VpsObserver(redis_client=fake_redis, mt5_host="mt5_acct1", mt5_port=8001)

    signals = await observer.read_parsed_signals(count=10)

    fake_redis.xrange.assert_awaited_once_with("parsed_signals", "-", "+", count=10)
    assert signals == [{"symbol": "XAUUSD", "direction": "BUY", "fast": "true"}]


def test_grep_container_logs_filters_matching_lines(monkeypatch):
    fake_output = (
        "2026-09-04 INFO [TM][EVENT] group_opened {'group_id': 1}\n"
        "2026-09-04 INFO some other line\n"
        "2026-09-04 INFO [TM][EVENT] open_aborted {'reason': 'no_price'}\n"
    )
    monkeypatch.setattr(
        "tests.e2e.vps_observer.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout=fake_output, stderr=""),
    )
    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)

    lines = observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")

    assert len(lines) == 2
    assert "open_aborted" in lines[1]


@pytest.mark.asyncio
async def test_positions_for_symbol_returns_position_dicts(monkeypatch):
    fake_pos = MagicMock(ticket=555, sl=2490.0, tp=0.0, volume=0.01)
    fake_client = MagicMock()
    fake_client.root.positions_get.return_value = [fake_pos]
    monkeypatch.setattr(
        "tests.e2e.vps_observer.rpyc.connect",
        lambda host, port: fake_client,
    )
    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)

    positions = await observer.positions_for_symbol("XAUUSD")

    assert positions == [{"ticket": 555, "sl": 2490.0, "tp": 0.0, "volume": 0.01}]
    fake_client.root.positions_get.assert_called_once_with(symbol="XAUUSD")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_vps_observer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.vps_observer'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/vps_observer.py
"""
Reads observable state across the three layers the e2e suite verifies:
docker container logs, Redis streams (raw_messages, parsed_signals — there
is no Redis stream for management messages, see spec section 3.1), and
live MT5 positions via the same RPyC pattern as price_reader. Returns raw
data only — scenarios own the assertions.
"""
import subprocess
import rpyc


class VpsObserver:
    def __init__(self, redis_client, mt5_host: str, mt5_port: int):
        self.redis = redis_client
        self.mt5_host = mt5_host
        self.mt5_port = mt5_port

    async def read_raw_messages(self, count: int = 20) -> list[dict]:
        entries = await self.redis.xrange("raw_messages", "-", "+", count=count)
        return [fields for _msg_id, fields in entries]

    async def read_parsed_signals(self, count: int = 20) -> list[dict]:
        entries = await self.redis.xrange("parsed_signals", "-", "+", count=count)
        return [fields for _msg_id, fields in entries]

    def grep_container_logs(self, container: str, pattern: str, since: str = "5m") -> list[str]:
        result = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True, text=True, check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return [line for line in combined.splitlines() if pattern in line]

    async def positions_for_symbol(self, symbol: str) -> list[dict]:
        client = rpyc.connect(self.mt5_host, self.mt5_port)
        positions = client.root.positions_get(symbol=symbol) or []
        return [
            {"ticket": p.ticket, "sl": p.sl, "tp": p.tp, "volume": p.volume}
            for p in positions
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_vps_observer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/vps_observer.py tests/e2e/test_vps_observer.py
git commit -m "feat(e2e): add VpsObserver for reading logs, Redis streams, and MT5 positions

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Pre-flight checklist

**Files:**
- Create: `tests/e2e/preflight.py`
- Test: `tests/e2e/test_preflight.py`

**Interfaces:**
- Consumes: `E2EConfig` (Task 2); `httpx.AsyncClient` for reaching `trade_orchestrator`'s `/health`.
- Produces:
  - `@dataclass PreflightResult: ok: bool; problems: list[str]`.
  - `async def run_preflight(cfg: E2EConfig, accounts_json: list[dict], http_client) -> PreflightResult` — checks (spec §7):
    1. `cfg.tg_test_chat_id` (as `str`) is present in the union of `allowed_channels` across `accounts_json` entries — reuse the exact matching rule from `services/telegram_ingestor/app.py::build_channel_filter` (union of all accounts' `allowed_channels`, string comparison).
    2. `GET http://{trade_orchestrator_host}:{mgmt_api_port}/health` returns 200 (confirms `trade_orchestrator`'s mgmt API, which n8n calls back into, is up — this cannot confirm n8n's own webhook config, so the check only asserts reachability and the result message says so explicitly per spec §7).
  - Any failed check appends a human-readable string to `problems`; `ok` is `len(problems) == 0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_preflight.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.config import E2EConfig
from tests.e2e.preflight import run_preflight


def _cfg(chat_id=-1009999999999):
    return E2EConfig(
        redis_url="redis://redis:6379/0", tg_test_chat_id=chat_id,
        tg_api_id="1", tg_api_hash="h", tg_phone="+1",
        mt5_host="mt5_acct1", mt5_port=8001,
        n8n_action_api_key="key", mgmt_api_port=8200,
        trade_orchestrator_host="trade_orchestrator",
    )


@pytest.mark.asyncio
async def test_preflight_ok_when_channel_allowed_and_health_up():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is True
    assert result.problems == []


@pytest.mark.asyncio
async def test_preflight_fails_when_test_chat_id_not_in_allowed_channels():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1002293184715]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("allowed_channels" in p for p in result.problems)


@pytest.mark.asyncio
async def test_preflight_fails_when_trade_orchestrator_unreachable():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("trade_orchestrator" in p for p in result.problems)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_preflight.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.preflight'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/preflight.py
"""
Pre-flight checks the e2e runner performs before executing scenarios
(spec section 7): confirm the test channel is actually reachable by the
pipeline, and that trade_orchestrator's mgmt API (n8n's callback target)
is up. This cannot confirm n8n's own webhook configuration points at this
VPS — that stays a documented operator precondition (spec section 4).
"""
from dataclasses import dataclass, field

from tests.e2e.config import E2EConfig


@dataclass
class PreflightResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


def _build_allowed_channels(accounts_json: list[dict]) -> set[str]:
    allowed: set[str] = set()
    for acct in accounts_json:
        for ch in acct.get("allowed_channels") or []:
            allowed.add(str(ch))
    return allowed


async def run_preflight(cfg: E2EConfig, accounts_json: list[dict], http_client) -> PreflightResult:
    problems: list[str] = []

    allowed = _build_allowed_channels(accounts_json)
    if allowed and str(cfg.tg_test_chat_id) not in allowed:
        problems.append(
            f"TG_TEST_CHAT_ID={cfg.tg_test_chat_id} is not in any account's "
            f"allowed_channels ({sorted(allowed)}) — telegram_ingestor will "
            f"silently drop test messages. Add it to ACCOUNTS_JSON."
        )

    try:
        resp = await http_client.get(
            f"http://{cfg.trade_orchestrator_host}:{cfg.mgmt_api_port}/health"
        )
        if resp.status_code != 200:
            problems.append(
                f"trade_orchestrator /health returned {resp.status_code}, expected 200"
            )
    except Exception as e:
        problems.append(
            f"trade_orchestrator unreachable at {cfg.trade_orchestrator_host}:{cfg.mgmt_api_port}: {e}"
        )

    return PreflightResult(ok=len(problems) == 0, problems=problems)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_preflight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/preflight.py tests/e2e/test_preflight.py
git commit -m "feat(e2e): add pre-flight checklist for test channel and trade_orchestrator reachability

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Scenario base types

**Files:**
- Create: `tests/e2e/scenarios/__init__.py`
- Create: `tests/e2e/scenarios/base.py`
- Test: `tests/e2e/scenarios/test_base.py`

**Interfaces:**
- Produces (used by every scenario file and by `runner.py`):
  - `class ScenarioOutcome(str, Enum)`: `PASS`, `FAIL`, `INCONCLUSIVE_TP1_NOT_REACHED`, `INCONCLUSIVE_ENTRY_RANGE_TIMEOUT`, `EXTERNAL_DEPENDENCY_FAILURE`.
  - `@dataclass ScenarioContext`: `cfg: E2EConfig`, `price_reader: PriceReader`, `sender: TelegramSender`, `observer: VpsObserver`.
  - `@dataclass ScenarioResult`: `name: str`, `outcome: ScenarioOutcome`, `evidence: dict` (free-form: log lines, stream entries, position snapshots — whatever the scenario collected), `detail: str` (one-line human summary).
  - `async def cleanup_group(ctx: ScenarioContext, symbol: str) -> None` — closes any open position for `symbol` still present via `ctx.observer.positions_for_symbol`, using a direct RPyC `order_send` close (best-effort, swallows exceptions, logs a warning) — the emergency cleanup path from spec §6.

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/scenarios/test_base.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group


@pytest.mark.asyncio
async def test_cleanup_group_closes_open_positions():
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(return_value=[{"ticket": 111, "sl": 0, "tp": 0, "volume": 0.01}])

    fake_mt5_client = MagicMock()
    ctx = ScenarioContext(cfg=MagicMock(), price_reader=MagicMock(), sender=MagicMock(), observer=observer)

    closed_tickets = []

    async def fake_close(ticket, volume):
        closed_tickets.append(ticket)

    await cleanup_group(ctx, "XAUUSD", close_fn=fake_close)

    assert closed_tickets == [111]


def test_scenario_result_holds_outcome_and_evidence():
    result = ScenarioResult(
        name="a1_fast_only",
        outcome=ScenarioOutcome.PASS,
        evidence={"raw_messages": [], "positions": []},
        detail="opened and closed cleanly",
    )
    assert result.outcome == ScenarioOutcome.PASS
    assert result.name == "a1_fast_only"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/scenarios/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.scenarios.base'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/scenarios/__init__.py
```

```python
# tests/e2e/scenarios/base.py
"""
Shared types for every e2e scenario: the context each scenario receives,
the result it must return, and the emergency cleanup helper (spec section 6)
scenarios call from a `finally` block or the runner calls on unexpected
failure.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from tests.e2e.config import E2EConfig
from tests.e2e.price_reader import PriceReader
from tests.e2e.telegram_sender import TelegramSender
from tests.e2e.vps_observer import VpsObserver


class ScenarioOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE_TP1_NOT_REACHED = "inconclusive_tp1_not_reached"
    INCONCLUSIVE_ENTRY_RANGE_TIMEOUT = "inconclusive_entry_range_timeout"
    EXTERNAL_DEPENDENCY_FAILURE = "external_dependency_failure"


@dataclass
class ScenarioContext:
    cfg: E2EConfig
    price_reader: PriceReader
    sender: TelegramSender
    observer: VpsObserver


@dataclass
class ScenarioResult:
    name: str
    outcome: ScenarioOutcome
    evidence: dict = field(default_factory=dict)
    detail: str = ""


async def cleanup_group(ctx: ScenarioContext, symbol: str, close_fn: Optional[Callable] = None) -> None:
    """
    Best-effort emergency cleanup: closes any position still open for
    `symbol`. `close_fn(ticket, volume)` defaults to a direct RPyC
    order_send close against ctx.observer's MT5 connection; a scenario's
    unit test injects a fake to avoid touching a real MT5 connection.
    Never raises — a cleanup failure is logged, not propagated, so it
    never masks the scenario's own result.
    """
    import logging
    log = logging.getLogger("e2e.cleanup")
    try:
        positions = await ctx.observer.positions_for_symbol(symbol)
    except Exception as e:
        log.warning("cleanup_group: could not read positions for %s: %s", symbol, e)
        return
    for pos in positions:
        try:
            if close_fn is not None:
                await close_fn(pos["ticket"], pos["volume"])
            else:
                import rpyc
                client = rpyc.connect(ctx.observer.mt5_host, ctx.observer.mt5_port)
                client.root.close_position(pos["ticket"])
        except Exception as e:
            log.warning("cleanup_group: failed to close ticket=%s: %s", pos["ticket"], e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/scenarios/test_base.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/scenarios/__init__.py tests/e2e/scenarios/base.py tests/e2e/scenarios/test_base.py
git commit -m "feat(e2e): add scenario base types (ScenarioContext, ScenarioResult, cleanup_group)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 8: Family A scenarios (signal lifecycle)

**Files:**
- Create: `tests/e2e/scenarios/a1_fast_only.py`
- Create: `tests/e2e/scenarios/a2_fast_then_full_early.py`
- Create: `tests/e2e/scenarios/a3_fast_then_full_late.py`
- Create: `tests/e2e/scenarios/a4_full_only.py`
- Test: `tests/e2e/scenarios/test_a1_fast_only.py` (pattern repeats per scenario file — see step 1)

**Interfaces:**
- Consumes: `ScenarioContext`, `ScenarioResult`, `ScenarioOutcome`, `cleanup_group` (Task 7); `ctx.price_reader.read_price("XAUUSD")` (Task 3); `ctx.sender.send(chat_id, text)` (Task 4); `ctx.observer.read_raw_messages/read_parsed_signals/positions_for_symbol/grep_container_logs` (Task 5).
- Produces: each file exports `async def run(ctx: ScenarioContext) -> ScenarioResult`, the contract `runner.py` (Task 10) dispatches against by scenario name.

Every scenario in this family follows the same skeleton: read price → build message(s) with a TP1 near that price (per the spec §5 determinism note) → send via `ctx.sender` → poll `ctx.observer` for the expected Redis/log/MT5 evidence within a timeout → assert → clean up.

- [ ] **Step 1: Write the failing test for A1 (pattern to replicate for A2-A4)**

```python
# tests/e2e/scenarios/test_a1_fast_only.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import a1_fast_only


def _ctx(price=2500.0, parsed_signals=None, positions=None):
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=price)

    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)

    observer = MagicMock()
    observer.read_parsed_signals = AsyncMock(return_value=parsed_signals or [])
    observer.positions_for_symbol = AsyncMock(return_value=positions or [])
    observer.grep_container_logs = MagicMock(return_value=[])

    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_a1_sends_fast_signal_text():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # before send: nothing open
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after: two legs opened
        ]
    )

    result = await a1_fast_only.run(ctx)

    ctx.sender.send.assert_awaited_once_with(-1009999999999, "XAUUSD BUY NOW")
    assert result.outcome in (ScenarioOutcome.PASS, ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED)


@pytest.mark.asyncio
async def test_a1_fails_when_no_positions_open_after_signal():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens

    result = await a1_fast_only.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/scenarios/test_a1_fast_only.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.scenarios.a1_fast_only'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/scenarios/a1_fast_only.py
"""
A1 (spec section 5, Familia A): "XAUUSD BUY NOW" with no follow-up full
signal. Opens with DEFAULT_SL_XAUUSD_PIPS/DEFAULT_TP_XAUUSD_PIPS. Verifies
two legs (tp1 + runner) open, then polls for TP1 closing the tp1 leg within
a timeout — reporting INCONCLUSIVE_TP1_NOT_REACHED (not FAIL) if the real
market never gets there in time (spec section 5/7 determinism note).
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group

SYMBOL = "XAUUSD"
OPEN_POLL_TIMEOUT_SECONDS = 30
OPEN_POLL_INTERVAL_SECONDS = 2
TP1_POLL_TIMEOUT_SECONDS = 600
TP1_POLL_INTERVAL_SECONDS = 10


async def _poll_until(condition_fn, timeout_seconds: float, interval_seconds: float):
    elapsed = 0.0
    while elapsed < timeout_seconds:
        value = await condition_fn()
        if value:
            return value
        await asyncio.sleep(interval_seconds)
        elapsed += interval_seconds
    return None


async def run(ctx: ScenarioContext) -> ScenarioResult:
    await ctx.price_reader.read_price(SYMBOL)  # sanity read; fast signal carries no price itself
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="a1_fast_only", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions or []},
            detail="two legs (tp1+runner) did not appear within timeout after fast signal",
        )

    try:
        async def check_tp1_closed():
            remaining = await ctx.observer.positions_for_symbol(SYMBOL)
            return remaining if len(remaining) == 1 else None

        remaining = await _poll_until(check_tp1_closed, TP1_POLL_TIMEOUT_SECONDS, TP1_POLL_INTERVAL_SECONDS)
        if not remaining:
            return ScenarioResult(
                name="a1_fast_only", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions": positions},
                detail="opened correctly; TP1 not reached by real market within timeout",
            )
        return ScenarioResult(
            name="a1_fast_only", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_open": positions, "positions_after_tp1": remaining},
            detail="opened two legs, TP1 leg closed, runner remains under BE/trailing",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

Repeat the same skeleton for A2/A3/A4 (`tests/e2e/scenarios/a2_fast_then_full_early.py`, `a3_fast_then_full_late.py`, `a4_full_only.py`), each adjusting the message(s) sent and assertions per spec §5:

```python
# tests/e2e/scenarios/a2_fast_then_full_early.py
"""
A2 (spec section 5): fast signal followed by a full SIGNAL ALERT before TP1
closes. update_group_signal (trade_manager.py) must replace SL/TP1/TP2 on
the already-open group rather than opening a second one. TP1 in the full
signal is set close to the read price (spec section 5 determinism note),
and ENTRY PRICE is built wide around the read price to survive the 5s gold
entry-range window (spec section 5 gold entry-range note).
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS

SYMBOL = "XAUUSD"
TP1_POLL_TIMEOUT_SECONDS = 600
TP1_POLL_INTERVAL_SECONDS = 10
ENTRY_RANGE_HALF_WIDTH_PIPS = 3.0  # wide relative to expected spread — spec section 5 gold note


def _build_full_signal_text(direction: str, price: float, sl_pips: float, tp1_pips: float, tp2_pips: float) -> str:
    sign = 1 if direction == "BUY" else -1
    entry_lo = price - ENTRY_RANGE_HALF_WIDTH_PIPS
    entry_hi = price + ENTRY_RANGE_HALF_WIDTH_PIPS
    sl = price - sign * sl_pips
    tp1 = price + sign * tp1_pips
    tp2 = price + sign * tp2_pips
    return (
        "‼SIGNAL ALERT‼\n\n"
        f"PAIR: {SYMBOL}\n"
        f"ORDER TYPE: {direction}\n"
        f"ENTRY PRICE: {entry_lo:.2f} -{entry_hi:.2f}\n\n"
        f"❌STOP LOSS: {sl:.2f}\n\n"
        f"✅TAKE PROFIT 1:{tp1:.2f}\n"
        f"✅TAKE PROFIT 2:{tp2:.2f}\n"
    )


async def run(ctx: ScenarioContext) -> ScenarioResult:
    price = await ctx.price_reader.read_price(SYMBOL)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="a2_fast_then_full_early", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="fast signal did not open two legs",
        )

    # Re-read price right before sending the full signal — minimizes the gap
    # against the 5s gold entry-range window (spec section 5).
    price_for_full = await ctx.price_reader.read_price(SYMBOL)
    full_text = _build_full_signal_text("BUY", price_for_full, sl_pips=6, tp1_pips=1.5, tp2_pips=3)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, full_text)

    try:
        async def check_updated_sl():
            updated = await ctx.observer.positions_for_symbol(SYMBOL)
            expected_sl = price_for_full - 6
            if updated and all(abs(p["sl"] - expected_sl) < 0.5 for p in updated):
                return updated
            return None

        updated_positions = await _poll_until(check_updated_sl, 30, 2)
        aborted_logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] open_aborted")
        if not updated_positions:
            if any("entry_range" in line for line in aborted_logs):
                return ScenarioResult(
                    name="a2_fast_then_full_early", outcome=ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                    evidence={"aborted_logs": aborted_logs},
                    detail="full signal aborted on the 5s gold entry-range window, not a bot defect",
                )
            return ScenarioResult(
                name="a2_fast_then_full_early", outcome=ScenarioOutcome.FAIL,
                evidence={"positions": positions}, detail="SL was not updated to the full signal's value",
            )

        async def check_tp1_closed():
            remaining = await ctx.observer.positions_for_symbol(SYMBOL)
            return remaining if len(remaining) == 1 else None

        remaining = await _poll_until(check_tp1_closed, TP1_POLL_TIMEOUT_SECONDS, TP1_POLL_INTERVAL_SECONDS)
        if not remaining:
            return ScenarioResult(
                name="a2_fast_then_full_early", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions_after_update": updated_positions},
                detail="SL/TP updated correctly; TP1 not reached by real market within timeout",
            )
        return ScenarioResult(
            name="a2_fast_then_full_early", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_update": updated_positions, "positions_after_tp1": remaining},
            detail="fast opened, full signal updated SL/TP1/TP2 on same group, TP1 closed",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/a3_fast_then_full_late.py
"""
A3 (spec section 5, edge case): full signal arrives AFTER tp1_leg already
closed (BE applied) or the runner is already trailing. Asserts the explicit
guarantees already present in trade_manager.update_group_signal (lines
~312-352): the runner's SL never regresses past what BE/trailing already
achieved, and peak_multiple is rescaled to the new tp1/tp2 without losing
progress. To reach that state quickly and reliably, this scenario forces
TP1 very close to the opening price (tighter than A2) so the tp1 leg closes
fast, then sends the full signal.
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS
from tests.e2e.scenarios.a2_fast_then_full_early import _build_full_signal_text

SYMBOL = "XAUUSD"
TP1_CLOSE_TIMEOUT_SECONDS = 900
TP1_CLOSE_POLL_INTERVAL_SECONDS = 10


async def run(ctx: ScenarioContext) -> ScenarioResult:
    price = await ctx.price_reader.read_price(SYMBOL)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="a3_fast_then_full_late", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="fast signal did not open two legs",
        )

    try:
        # Wait for the default-TP1 tp1 leg to close on its own (default TP is
        # DEFAULT_TP_XAUUSD_PIPS — small enough to close within the timeout
        # in normal XAUUSD movement; if it never does, report inconclusive
        # rather than a false FAIL, consistent with A1/A2).
        async def check_tp1_closed():
            remaining = await ctx.observer.positions_for_symbol(SYMBOL)
            return remaining if len(remaining) == 1 else None

        remaining_before_full = await _poll_until(check_tp1_closed, TP1_CLOSE_TIMEOUT_SECONDS, TP1_CLOSE_POLL_INTERVAL_SECONDS)
        if not remaining_before_full:
            return ScenarioResult(
                name="a3_fast_then_full_late", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions": positions},
                detail="default TP1 not reached within timeout; cannot reach the late-update state to test",
            )

        sl_before_full = remaining_before_full[0]["sl"]

        price_for_full = await ctx.price_reader.read_price(SYMBOL)
        # Deliberately worse SL than what BE/trailing already achieved, to
        # exercise the "never regress" guarantee under test.
        full_text = _build_full_signal_text("BUY", price_for_full, sl_pips=20, tp1_pips=1.5, tp2_pips=3)
        await ctx.sender.send(ctx.cfg.tg_test_chat_id, full_text)

        async def check_sl_after_update():
            after = await ctx.observer.positions_for_symbol(SYMBOL)
            return after if after else None

        positions_after_update = await _poll_until(check_sl_after_update, 30, 2)
        if not positions_after_update:
            return ScenarioResult(
                name="a3_fast_then_full_late", outcome=ScenarioOutcome.FAIL,
                evidence={}, detail="runner position disappeared unexpectedly after late full signal",
            )

        sl_after_full = positions_after_update[0]["sl"]
        if sl_after_full < sl_before_full:  # BUY: SL regressing means it got worse
            return ScenarioResult(
                name="a3_fast_then_full_late", outcome=ScenarioOutcome.FAIL,
                evidence={"sl_before_full": sl_before_full, "sl_after_full": sl_after_full},
                detail="SL regressed after late full signal update — violates update_group_signal's never-regress guarantee",
            )
        return ScenarioResult(
            name="a3_fast_then_full_late", outcome=ScenarioOutcome.PASS,
            evidence={"sl_before_full": sl_before_full, "sl_after_full": sl_after_full},
            detail="late full signal did not regress an already-improved SL",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/a4_full_only.py
"""
A4 (spec section 5): a full SIGNAL ALERT with no preceding fast signal.
Opens directly with the full signal's SL/TP1/TP2 (not defaults).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios.a2_fast_then_full_early import _build_full_signal_text

SYMBOL = "XAUUSD"
OPEN_POLL_TIMEOUT_SECONDS = 30
OPEN_POLL_INTERVAL_SECONDS = 2
TP1_POLL_TIMEOUT_SECONDS = 600
TP1_POLL_INTERVAL_SECONDS = 10


async def run(ctx: ScenarioContext) -> ScenarioResult:
    price = await ctx.price_reader.read_price(SYMBOL)
    full_text = _build_full_signal_text("BUY", price, sl_pips=6, tp1_pips=1.5, tp2_pips=3)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, full_text)

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        aborted_logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] open_aborted")
        if any("entry_range" in line for line in aborted_logs):
            return ScenarioResult(
                name="a4_full_only", outcome=ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                evidence={"aborted_logs": aborted_logs},
                detail="full signal aborted on the 5s gold entry-range window, not a bot defect",
            )
        return ScenarioResult(
            name="a4_full_only", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="full signal did not open two legs",
        )

    try:
        expected_sl = price - 6
        if not all(abs(p["sl"] - expected_sl) < 0.5 for p in positions):
            return ScenarioResult(
                name="a4_full_only", outcome=ScenarioOutcome.FAIL,
                evidence={"positions": positions},
                detail="opened SL does not match the full signal's SL (not defaults, not the sent value)",
            )

        async def check_tp1_closed():
            remaining = await ctx.observer.positions_for_symbol(SYMBOL)
            return remaining if len(remaining) == 1 else None

        remaining = await _poll_until(check_tp1_closed, TP1_POLL_TIMEOUT_SECONDS, TP1_POLL_INTERVAL_SECONDS)
        if not remaining:
            return ScenarioResult(
                name="a4_full_only", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions": positions},
                detail="opened correctly with full signal's values; TP1 not reached within timeout",
            )
        return ScenarioResult(
            name="a4_full_only", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_open": positions, "positions_after_tp1": remaining},
            detail="full signal alone opened two legs with its own SL/TP1/TP2, TP1 closed",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/e2e/scenarios/test_a1_fast_only.py -v`
Expected: PASS (write and run equivalent `test_a2_*.py`/`test_a3_*.py`/`test_a4_*.py` using the same mocking pattern as step 1, adjusted per scenario's assertions — same fixture shape, different expected calls)

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/scenarios/a1_fast_only.py tests/e2e/scenarios/a2_fast_then_full_early.py \
        tests/e2e/scenarios/a3_fast_then_full_late.py tests/e2e/scenarios/a4_full_only.py \
        tests/e2e/scenarios/test_a1_fast_only.py tests/e2e/scenarios/test_a2_fast_then_full_early.py \
        tests/e2e/scenarios/test_a3_fast_then_full_late.py tests/e2e/scenarios/test_a4_full_only.py
git commit -m "feat(e2e): add Family A scenarios (signal lifecycle: fast/full/update-timing)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 9: Family B scenarios (management via n8n/Ollama)

**Files:**
- Create: `tests/e2e/scenarios/b1_be_variant1.py`, `b2_be_variant2.py`, `b3_be_variant3.py`, `b4_forced_close.py`, `b5_signal_correction.py`, `b6_milestone_noop.py`, `b7_sl_hit_note.py`, `b8_spam_noop.py`
- Test: one test file per scenario, same pattern as Task 8.

**Interfaces:**
- Consumes: same `ScenarioContext`/`cleanup_group` as Task 8, plus a shared setup helper `open_position_for_management_test(ctx) -> list[dict]` (opens a fast signal and waits for two legs, reused by every Family B scenario that needs an existing position — B1-B5, B7).
- Produces: `run(ctx) -> ScenarioResult` per file, same contract as Task 8.

All Family B scenarios verify through `ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] <event>")` (Task 1's log line) plus `positions_for_symbol` — never a Redis stream, per spec §3.1.

- [ ] **Step 1: Write the failing test for B1 (pattern to replicate for B2-B8)**

```python
# tests/e2e/scenarios/test_b1_be_variant1.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b1_be_variant1


def _ctx_with_open_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after fast open
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01}],  # after BE applied
        ]
    )
    observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b1_sends_be_message_and_confirms_sl_moved_to_entry():
    ctx = _ctx_with_open_position()

    result = await b1_be_variant1.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert "Set BE for zero risk" in sent_texts
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b1_reports_external_dependency_failure_on_timeout_without_error():
    ctx = _ctx_with_open_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
        ] + [[{"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]] * 20  # SL never moves
    )
    ctx.observer.grep_container_logs = MagicMock(return_value=[])  # no mgmt event logged at all

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/scenarios/test_b1_be_variant1.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.scenarios.b1_be_variant1'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/scenarios/_management_common.py
"""
Shared setup for Family B scenarios (spec section 5, Familia B): open a
position via a fast signal and wait for both legs, so the management
message under test has something real to act on.
"""
from tests.e2e.scenarios.base import ScenarioContext
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS

SYMBOL = "XAUUSD"


async def open_position_for_management_test(ctx: ScenarioContext) -> list[dict]:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    return await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS) or []
```

```python
# tests/e2e/scenarios/b1_be_variant1.py
"""
B1 (spec section 5, Familia B): "Set BE for zero risk" -> n8n/Ollama should
classify this as move_sl_be_now and call POST /mgmt/action on
trade_orchestrator, which moves the runner leg's SL to its entry price
(trade_manager.apply_mgmt_action, action == "move_sl_be_now").
Verification is via the [TM][EVENT] log line (Task 1) and the runner
position's SL in MT5 -- there is no Redis stream for management (spec
section 3.1). n8n/Ollama is the real test instance, not a mock (spec
section 2) -- a timeout with no mgmt event logged is reported as an
external dependency failure, not a bot FAIL (spec section 7).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE = "Set BE for zero risk"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )
    runner_sl_before = next(p["sl"] for p in positions)

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    try:
        async def check_be_applied():
            current = await ctx.observer.positions_for_symbol(SYMBOL)
            runner = next(iter(current), None)
            if runner and runner["sl"] != runner_sl_before:
                return runner
            return None

        runner_after = await _poll_until(check_be_applied, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] mgmt_move_sl_be_applied")

        if not runner_after:
            if not logs:
                return ScenarioResult(
                    name="b1_be_variant1", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="no mgmt_move_sl_be_applied event logged — n8n/Ollama likely did not act",
                )
            return ScenarioResult(
                name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": logs}, detail="event was logged but SL did not change in MT5",
            )
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs, "runner_after": runner_after},
            detail="'Set BE for zero risk' correctly moved runner SL to entry",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/b2_be_variant2.py
"""B2 (spec section 5): same as B1 but with different phrasing, to confirm
Ollama generalizes intent rather than matching a fixed keyword."""
from tests.e2e.scenarios import b1_be_variant1
from tests.e2e.scenarios.base import ScenarioContext, ScenarioResult

MESSAGE = "Make sure you adjust your sl to Entry for zero risk"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    b1_be_variant1.MESSAGE = MESSAGE
    result = await b1_be_variant1.run(ctx)
    result.name = "b2_be_variant2"
    return result
```

```python
# tests/e2e/scenarios/b3_be_variant3.py
"""B3 (spec section 5): same as B1/B2 with a third phrasing."""
from tests.e2e.scenarios import b1_be_variant1
from tests.e2e.scenarios.base import ScenarioContext, ScenarioResult

MESSAGE = "lock all your trades in Break Even"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    b1_be_variant1.MESSAGE = MESSAGE
    result = await b1_be_variant1.run(ctx)
    result.name = "b3_be_variant3"
    return result
```

```python
# tests/e2e/scenarios/b4_forced_close.py
"""
B4 (spec section 5): "MARKET STRUCTURE SHIFTED! DON'T HOLD SELL. Close now"
-> action close_now (trade_manager.apply_mgmt_action) closes both legs.
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE = "MARKET STRUCTURE SHIFTED! DON'T HOLD SELL. Close now"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="b4_forced_close", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    try:
        async def check_all_closed():
            current = await ctx.observer.positions_for_symbol(SYMBOL)
            return [] if len(current) == 0 else None

        closed = await _poll_until(check_all_closed, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] mgmt_close_now")

        if closed is None:
            if not logs:
                return ScenarioResult(
                    name="b4_forced_close", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="no mgmt_close_now event logged — n8n/Ollama likely did not act",
                )
            return ScenarioResult(
                name="b4_forced_close", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": logs}, detail="event was logged but positions remain open in MT5",
            )
        return ScenarioResult(
            name="b4_forced_close", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs}, detail="forced-close message correctly closed both legs",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)  # no-op if already closed
```

```python
# tests/e2e/scenarios/b5_signal_correction.py
"""
B5 (spec section 5): "SIGNAL UPDATED" / "TP 2 IS 4687 Correction" -- not a
recognized TradePulseParser format (memory: tradepulse-channel-message-patterns
notes this exact message is unhandled by any parser), so it must go through
n8n/Ollama classifying it as signal_correction, which only updates the
tp2 reference used by trailing (trade_manager.apply_mgmt_action, action ==
"signal_correction") -- it does not touch MT5 directly for the runner leg,
so there is no SL/TP change to observe in MT5, only the log line.
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE_1 = "SIGNAL UPDATED"
MESSAGE_2 = "TP 2 IS 4687 Correction"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="b5_signal_correction", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE_1)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE_2)

    try:
        async def check_correction_logged():
            logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] group_updated")
            return logs if logs else None

        logs = await _poll_until(check_correction_logged, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        if not logs:
            return ScenarioResult(
                name="b5_signal_correction", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                evidence={}, detail="no group_updated event logged for the correction — n8n/Ollama likely did not act",
            )
        return ScenarioResult(
            name="b5_signal_correction", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs}, detail="free-text TP2 correction was classified and applied via signal_correction",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/b6_milestone_noop.py
"""
B6 (spec section 5): progress/milestone messages ("+240 PIPS SKYROCKETING",
"TP 1 DONE", "Road to TP ONE") must not trigger any mgmt action -- a false
positive here is the failure mode under test.
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

QUIET_WINDOW_SECONDS = 60
MESSAGES = ["+240 PIPS SKYROCKETING", "TP 1 DONE", "Road to TP ONE"]


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions_before = await open_position_for_management_test(ctx)
    if len(positions_before) < 2:
        return ScenarioResult(
            name="b6_milestone_noop", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    import asyncio
    for text in MESSAGES:
        await ctx.sender.send(ctx.cfg.tg_test_chat_id, text)
    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    try:
        positions_after = await ctx.observer.positions_for_symbol(SYMBOL)
        mutating_logs = [
            line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
            if any(ev in line for ev in ("mgmt_close_now", "mgmt_move_sl_be_applied", "group_updated"))
        ]
        if mutating_logs or positions_after != positions_before:
            return ScenarioResult(
                name="b6_milestone_noop", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": mutating_logs, "before": positions_before, "after": positions_after},
                detail="a milestone/progress message triggered a mutating mgmt action (false positive)",
            )
        return ScenarioResult(
            name="b6_milestone_noop", outcome=ScenarioOutcome.PASS,
            evidence={"before": positions_before, "after": positions_after},
            detail="milestone messages correctly produced no mgmt action",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/b7_sl_hit_note.py
"""
B7 (spec section 5, corrected): "HIT SL. GET READY FOR RECOVERY" maps to
the real note_sl_hit action (trade_manager.apply_mgmt_action) -- it DOES
log an event and notify, but must NOT change SL/TP or close the position.
Assert "no mutation", not "no action" (spec section 5 correction).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

QUIET_WINDOW_SECONDS = 60
MESSAGE = "HIT SL ❌. GET READY FOR RECOVERY \U0001f91d"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions_before = await open_position_for_management_test(ctx)
    if len(positions_before) < 2:
        return ScenarioResult(
            name="b7_sl_hit_note", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    import asyncio
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)
    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    try:
        positions_after = await ctx.observer.positions_for_symbol(SYMBOL)
        mutating_logs = [
            line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
            if any(ev in line for ev in ("mgmt_close_now", "mgmt_move_sl_be_applied", "group_updated"))
        ]
        if mutating_logs or positions_after != positions_before:
            return ScenarioResult(
                name="b7_sl_hit_note", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": mutating_logs, "before": positions_before, "after": positions_after},
                detail="SL-hit/recovery message mutated the position — it should only be noted (note_sl_hit)",
            )
        return ScenarioResult(
            name="b7_sl_hit_note", outcome=ScenarioOutcome.PASS,
            evidence={"before": positions_before, "after": positions_after},
            detail="SL-hit/recovery message correctly left the position unmutated",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/b8_spam_noop.py
"""B8 (spec section 5): promotional spam must produce zero effects — no
trade, no mgmt action."""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult

QUIET_WINDOW_SECONDS = 60
MESSAGE = (
    "\U0001f680 JOIN OUR VIP POOL TRADING PROGRAM TODAY! \U0001f680\n"
    "Limited spots left — DM now to secure your spot and 10x your account!"
)


async def run(ctx: ScenarioContext) -> ScenarioResult:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    import asyncio
    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    positions = await ctx.observer.positions_for_symbol("XAUUSD")
    mutating_logs = [
        line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
        if any(ev in line for ev in ("group_opened", "mgmt_close_now", "mgmt_move_sl_be_applied", "group_updated"))
    ]
    if positions or mutating_logs:
        return ScenarioResult(
            name="b8_spam_noop", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions, "logs": mutating_logs},
            detail="promotional spam produced a trade or mgmt action (false positive)",
        )
    return ScenarioResult(
        name="b8_spam_noop", outcome=ScenarioOutcome.PASS,
        evidence={}, detail="promotional spam correctly produced no effects",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/e2e/scenarios/test_b1_be_variant1.py -v`
Expected: PASS (write equivalent tests for B2-B8 following the same mock/assert pattern as B1/A1)

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/scenarios/_management_common.py tests/e2e/scenarios/b*.py tests/e2e/scenarios/test_b*.py
git commit -m "feat(e2e): add Family B scenarios (free-text management via n8n/Ollama)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 10: Family C scenarios (pipeline robustness)

**Files:**
- Create: `tests/e2e/scenarios/c1_dedup.py`, `c2_unrecognized_to_n8n.py`, `c3_entry_range_dash_variants.py`
- Test: one test file per scenario.

**Interfaces:**
- Consumes: same `ScenarioContext` as Task 8/9; `ctx.observer.read_raw_messages`/`read_parsed_signals` (Task 5) — these are the only scenarios that read the Redis streams directly, since C1/C2 are about the parsing/dedup layer, not management.
- Produces: `run(ctx) -> ScenarioResult`, same contract.

- [ ] **Step 1: Write the failing test for C1 (pattern to replicate for C2/C3)**

```python
# tests/e2e/scenarios/test_c1_dedup.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import c1_dedup


@pytest.mark.asyncio
async def test_c1_second_identical_signal_does_not_open_a_second_group():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after 1st send
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after 2nd send: unchanged
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1_dedup.run(ctx)

    assert ctx.sender.send.await_count == 2
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_c1_fails_when_second_signal_opens_a_second_group():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 3, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 4, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # duplicate opened a second group!
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1_dedup.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/scenarios/test_c1_dedup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.scenarios.c1_dedup'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/scenarios/c1_dedup.py
"""
C1 (spec section 5, Familia C): the same fast signal sent twice in a row.
SignalDeduplicator (services/common/signal_dedup.py) must discard the
second within DEDUP_TTL_SECONDS -- no second group should open.
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS

SYMBOL = "XAUUSD"
BETWEEN_SENDS_SECONDS = 3
SETTLE_SECONDS = 10


async def run(ctx: ScenarioContext) -> ScenarioResult:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="c1_dedup", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="first fast signal did not open two legs",
        )

    try:
        await asyncio.sleep(BETWEEN_SENDS_SECONDS)
        await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")
        await asyncio.sleep(SETTLE_SECONDS)

        positions_after = await ctx.observer.positions_for_symbol(SYMBOL)
        if len(positions_after) != 2:
            return ScenarioResult(
                name="c1_dedup", outcome=ScenarioOutcome.FAIL,
                evidence={"positions_after": positions_after},
                detail=f"expected 2 positions (dedup held), found {len(positions_after)} — duplicate was not discarded",
            )
        return ScenarioResult(
            name="c1_dedup", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after": positions_after},
            detail="second identical fast signal within dedup TTL correctly discarded",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

```python
# tests/e2e/scenarios/c2_unrecognized_to_n8n.py
"""
C2 (spec section 5): text that is neither a signal nor recognizable
management text. router_parser forwards it to N8N_INBOUND_WEBHOOK_URL
(app.py::forward_to_n8n) -- this suite cannot observe that outbound POST
directly (it targets n8n, external to this VPS' own logs/Redis), so it
asserts the two things it CAN observe: the text reached raw_messages, and
it produced no trade and no mgmt action.
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult

SETTLE_SECONDS = 30
MESSAGE = "Anyone else watching the Fed announcement today? Curious how gold reacts."


async def run(ctx: ScenarioContext) -> ScenarioResult:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)
    await asyncio.sleep(SETTLE_SECONDS)

    raw_messages = await ctx.observer.read_raw_messages(count=20)
    reached_raw = any(MESSAGE in m.get("text", "") for m in raw_messages)

    positions = await ctx.observer.positions_for_symbol("XAUUSD")
    mutating_logs = [
        line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
        if any(ev in line for ev in ("group_opened", "mgmt_close_now", "mgmt_move_sl_be_applied", "group_updated"))
    ]

    if not reached_raw:
        return ScenarioResult(
            name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.FAIL,
            evidence={"raw_messages": raw_messages},
            detail="message never reached raw_messages — ingestor/filter issue, not an n8n issue",
        )
    if positions or mutating_logs:
        return ScenarioResult(
            name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions, "logs": mutating_logs},
            detail="unrecognized text incorrectly produced a trade or mgmt action",
        )
    return ScenarioResult(
        name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.PASS,
        evidence={"raw_messages_matched": reached_raw},
        detail="unrecognized text reached the pipeline and produced no trade/mgmt action "
                "(the outbound POST to n8n itself is not directly observable from this VPS)",
    )
```

```python
# tests/e2e/scenarios/c3_entry_range_dash_variants.py
"""
C3 (spec section 5): irregular dash spacing in ENTRY PRICE ("4600- 4590",
"4325 - 4335"), as seen in real channel messages (memory:
tradepulse-channel-message-patterns). TradePulseParser.ENTRY_RE
(services/router_parser/parsers_tradepulse.py) requires a dash/en-dash but
tolerates surrounding whitespace — this is a regression check that it still
parses and opens correctly. Subject to the same 5s gold entry-range window
as A2/A4 (spec section 5 gold note).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS

SYMBOL = "XAUUSD"
ENTRY_RANGE_HALF_WIDTH_PIPS = 3.0


def _build_signal_with_dash_variant(price: float, dash: str) -> str:
    lo = price - ENTRY_RANGE_HALF_WIDTH_PIPS
    hi = price + ENTRY_RANGE_HALF_WIDTH_PIPS
    return (
        "‼SIGNAL ALERT‼\n\n"
        f"PAIR: {SYMBOL}\n"
        "ORDER TYPE: BUY\n"
        f"ENTRY PRICE: {lo:.2f}{dash}{hi:.2f}\n\n"
        f"❌STOP LOSS: {price - 6:.2f}\n\n"
        f"✅TAKE PROFIT 1:{price + 1.5:.2f}\n"
        f"✅TAKE PROFIT 2:{price + 3:.2f}\n"
    )


async def run(ctx: ScenarioContext) -> ScenarioResult:
    price = await ctx.price_reader.read_price(SYMBOL)
    text = _build_signal_with_dash_variant(price, dash="- ")  # e.g. "4600- 4590" style spacing
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, text)

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        aborted_logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] open_aborted")
        if any("entry_range" in line for line in aborted_logs):
            return ScenarioResult(
                name="c3_entry_range_dash_variants", outcome=ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                evidence={"aborted_logs": aborted_logs},
                detail="aborted on the 5s gold entry-range window, not a parsing defect",
            )
        return ScenarioResult(
            name="c3_entry_range_dash_variants", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="irregular-dash ENTRY PRICE was not parsed/opened correctly",
        )

    try:
        return ScenarioResult(
            name="c3_entry_range_dash_variants", outcome=ScenarioOutcome.PASS,
            evidence={"positions": positions},
            detail="irregular dash spacing in ENTRY PRICE parsed and opened correctly",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/e2e/scenarios/test_c1_dedup.py -v`
Expected: PASS (write equivalent tests for C2/C3 following the same pattern)

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/scenarios/c1_dedup.py tests/e2e/scenarios/c2_unrecognized_to_n8n.py \
        tests/e2e/scenarios/c3_entry_range_dash_variants.py tests/e2e/scenarios/test_c1_dedup.py \
        tests/e2e/scenarios/test_c2_unrecognized_to_n8n.py tests/e2e/scenarios/test_c3_entry_range_dash_variants.py
git commit -m "feat(e2e): add Family C scenarios (dedup, unrecognized text, entry-range dash variants)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 11: `runner.py` CLI

**Files:**
- Create: `tests/e2e/runner.py`
- Test: `tests/e2e/test_runner.py`

**Interfaces:**
- Consumes: `E2EConfig`/`load_config` (Task 2), `run_preflight` (Task 6), every scenario's `run(ctx) -> ScenarioResult` (Tasks 8-10), `ScenarioContext`/`ScenarioResult`/`ScenarioOutcome` (Task 7).
- Produces:
  - `SCENARIOS: dict[str, Callable]` — the name → `run` function registry, one entry per scenario module.
  - `async def run_scenario(name: str, ctx: ScenarioContext) -> ScenarioResult` — looks up and calls the scenario's `run`.
  - `def format_report(results: list[ScenarioResult]) -> str` — human-readable summary grouped by outcome (spec §7).
  - `def main(argv: list[str] | None = None) -> int` — argparse CLI with `--scenario NAME` and `--all`; builds `ScenarioContext` from config, runs pre-flight first (aborts printing `problems` if not `ok`), runs the requested scenario(s) in serial, prints the report, returns `0` if every scenario is `PASS`/`INCONCLUSIVE_*`, `1` if any is `FAIL`, `2` if any is `EXTERNAL_DEPENDENCY_FAILURE` and none `FAIL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_runner.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.runner import run_scenario, format_report, SCENARIOS
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult


def test_scenarios_registry_has_all_15_entries():
    expected = {
        "a1_fast_only", "a2_fast_then_full_early", "a3_fast_then_full_late", "a4_full_only",
        "b1_be_variant1", "b2_be_variant2", "b3_be_variant3", "b4_forced_close",
        "b5_signal_correction", "b6_milestone_noop", "b7_sl_hit_note", "b8_spam_noop",
        "c1_dedup", "c2_unrecognized_to_n8n", "c3_entry_range_dash_variants",
    }
    assert set(SCENARIOS.keys()) == expected


@pytest.mark.asyncio
async def test_run_scenario_dispatches_to_registered_function(monkeypatch):
    fake_result = ScenarioResult(name="a1_fast_only", outcome=ScenarioOutcome.PASS, detail="ok")

    async def fake_run(ctx):
        return fake_result

    monkeypatch.setitem(SCENARIOS, "a1_fast_only", fake_run)
    ctx = ScenarioContext(cfg=MagicMock(), price_reader=MagicMock(), sender=MagicMock(), observer=MagicMock())

    result = await run_scenario("a1_fast_only", ctx)

    assert result is fake_result


def test_format_report_groups_by_outcome():
    results = [
        ScenarioResult(name="a1_fast_only", outcome=ScenarioOutcome.PASS, detail="ok"),
        ScenarioResult(name="b8_spam_noop", outcome=ScenarioOutcome.FAIL, detail="broke"),
        ScenarioResult(name="a4_full_only", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED, detail="market quiet"),
    ]

    report = format_report(results)

    assert "PASS" in report and "a1_fast_only" in report
    assert "FAIL" in report and "b8_spam_noop" in report
    assert "INCONCLUSIVE" in report and "a4_full_only" in report
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.e2e.runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/e2e/runner.py
"""
CLI entrypoint for the e2e suite (spec section 3.1/3.2): wires config,
pre-flight, and scenarios together, and prints a report classifying each
result as PASS / FAIL / INCONCLUSIVE_* / EXTERNAL_DEPENDENCY_FAILURE
(spec section 7). Run via:
  docker compose --profile e2e run --rm e2e_runner --scenario a1_fast_only
  docker compose --profile e2e run --rm e2e_runner --all
"""
import argparse
import asyncio
import json
import sys

import httpx

from tests.e2e.config import load_config, E2EConfig
from tests.e2e.preflight import run_preflight
from tests.e2e.price_reader import PriceReader
from tests.e2e.telegram_sender import TelegramSender
from tests.e2e.vps_observer import VpsObserver
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult

from tests.e2e.scenarios import (
    a1_fast_only, a2_fast_then_full_early, a3_fast_then_full_late, a4_full_only,
    b1_be_variant1, b2_be_variant2, b3_be_variant3, b4_forced_close,
    b5_signal_correction, b6_milestone_noop, b7_sl_hit_note, b8_spam_noop,
    c1_dedup, c2_unrecognized_to_n8n, c3_entry_range_dash_variants,
)

SCENARIOS = {
    "a1_fast_only": a1_fast_only.run,
    "a2_fast_then_full_early": a2_fast_then_full_early.run,
    "a3_fast_then_full_late": a3_fast_then_full_late.run,
    "a4_full_only": a4_full_only.run,
    "b1_be_variant1": b1_be_variant1.run,
    "b2_be_variant2": b2_be_variant2.run,
    "b3_be_variant3": b3_be_variant3.run,
    "b4_forced_close": b4_forced_close.run,
    "b5_signal_correction": b5_signal_correction.run,
    "b6_milestone_noop": b6_milestone_noop.run,
    "b7_sl_hit_note": b7_sl_hit_note.run,
    "b8_spam_noop": b8_spam_noop.run,
    "c1_dedup": c1_dedup.run,
    "c2_unrecognized_to_n8n": c2_unrecognized_to_n8n.run,
    "c3_entry_range_dash_variants": c3_entry_range_dash_variants.run,
}


async def run_scenario(name: str, ctx: ScenarioContext) -> ScenarioResult:
    return await SCENARIOS[name](ctx)


def format_report(results: list) -> str:
    lines = ["=== e2e suite report ==="]
    for outcome in (ScenarioOutcome.PASS, ScenarioOutcome.FAIL,
                    ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                    ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                    ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE):
        matching = [r for r in results if r.outcome == outcome]
        if not matching:
            continue
        label = "INCONCLUSIVE" if "INCONCLUSIVE" in outcome.value.upper() else outcome.value.upper()
        lines.append(f"\n{label}:")
        for r in matching:
            lines.append(f"  - {r.name}: {r.detail}")
    return "\n".join(lines)


async def _build_context(cfg: E2EConfig) -> ScenarioContext:
    import redis.asyncio as redis_asyncio
    redis_client = redis_asyncio.from_url(cfg.redis_url, decode_responses=True)
    return ScenarioContext(
        cfg=cfg,
        price_reader=PriceReader(host=cfg.mt5_host, port=cfg.mt5_port),
        sender=TelegramSender(api_id=cfg.tg_api_id, api_hash=cfg.tg_api_hash, phone=cfg.tg_phone),
        observer=VpsObserver(redis_client=redis_client, mt5_host=cfg.mt5_host, mt5_port=cfg.mt5_port),
    )


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the trading-platform e2e test suite against a live VPS.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", choices=sorted(SCENARIOS.keys()), help="run a single scenario")
    group.add_argument("--all", action="store_true", help="run every scenario in serial")
    args = parser.parse_args(argv)

    cfg = load_config()

    accounts_json = []  # loaded from ACCOUNTS_JSON env the same way services/common/config.py does
    import os
    accounts_json = json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

    async with httpx.AsyncClient() as http_client:
        preflight = await run_preflight(cfg, accounts_json, http_client)
        if not preflight.ok:
            print("Pre-flight checks failed:")
            for p in preflight.problems:
                print(f"  - {p}")
            return 2

    ctx = await _build_context(cfg)
    names = [args.scenario] if args.scenario else sorted(SCENARIOS.keys())

    results = []
    try:
        for name in names:
            print(f"--- running {name} ---")
            result = await run_scenario(name, ctx)
            results.append(result)
            print(f"{result.outcome.value}: {result.detail}")
    finally:
        await ctx.sender.close()

    print(format_report(results))

    if any(r.outcome == ScenarioOutcome.FAIL for r in results):
        return 1
    if any(r.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE for r in results):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/runner.py tests/e2e/test_runner.py
git commit -m "feat(e2e): add runner CLI wiring config, preflight, and all scenarios

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 12: Dockerfile + docker-compose `e2e_runner` service

**Files:**
- Create: `tests/e2e/Dockerfile`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `tests/e2e/requirements.txt` (Task 2), the full `tests/e2e/` package (Tasks 2-11).
- Produces: a runnable `docker compose --profile e2e run --rm e2e_runner --scenario <name>` command against the VPS.

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
# tests/e2e/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Docker CLI is required for vps_observer.grep_container_logs to call
# `docker logs` on sibling containers — mount the host's docker socket at
# run time (see docker-compose.yml e2e_runner service).
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY services/ ./services/
COPY tests/ ./tests/
RUN pip install --no-cache-dir -r ./tests/e2e/requirements.txt

ENV PYTHONPATH="/app"

WORKDIR /app

ENTRYPOINT ["python", "-m", "tests.e2e.runner"]
```

- [ ] **Step 2: Add the `e2e_runner` service to `docker-compose.yml`**

Add at the end of the `services:` block in `docker-compose.yml` (before the trailing `volumes:` section), matching indentation of the existing services:

```yaml
  e2e_runner:
    build:
      context: .
      dockerfile: tests/e2e/Dockerfile
    container_name: atp-e2e-runner
    profiles: ["e2e"]
    env_file:
      - .env
    environment:
      - MT5_HOST=mt5_acct1
      - MT5_PORT=8001
      - TRADE_ORCHESTRATOR_HOST=trade_orchestrator
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./tests/e2e/e2e_test_session.session:/app/tests/e2e/e2e_test_session.session:rw
    depends_on:
      redis:
        condition: service_healthy
      mt5_acct1:
        condition: service_healthy
      telegram_ingestor:
        condition: service_started
      router_parser:
        condition: service_started
      trade_orchestrator:
        condition: service_started
```

Note on the Docker-socket mount: `vps_observer.grep_container_logs` shells out to `docker logs <container>` (Task 5) — this requires access to the host's Docker socket, mounted read-only here. This is a deliberate, confirmed exception to "shares the internal network, no new ports" (spec §3.2 now documents it explicitly): the container can read logs of any container on the host, not just this compose project's own services. Accepted as an explicit operator decision — read-only, on an already-trusted VPS — in preference to the alternative (running the runner outside compose on the host, which would in turn require exposing Redis/RPyC to the host, same trade-off as the "run locally" option already rejected in spec §4).

- [ ] **Step 3: Verify the compose file parses**

Run: `docker compose config --profile e2e`
Expected: prints the fully resolved config for all services including `e2e_runner`, no YAML errors.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/Dockerfile docker-compose.yml
git commit -m "feat(e2e): add e2e_runner Dockerfile and docker-compose service (profile e2e)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 13: Full local unit-test pass + `.env.example` documentation

**Files:**
- Modify: `.env.example`
- Test: entire `tests/e2e/` unit test suite (no VPS required)

**Interfaces:** none new — this task verifies Tasks 1-12 are wired together and documents the new required variables for whoever sets up the VPS `.env`.

- [ ] **Step 1: Run every e2e unit test together**

Run: `pytest tests/e2e/ -v`
Expected: all PASS — this exercises `test_config.py`, `test_price_reader.py`, `test_telegram_sender.py`, `test_vps_observer.py`, `test_preflight.py`, `tests/e2e/scenarios/test_*.py`, and `test_runner.py` together, catching import/naming mismatches between tasks (e.g. a scenario file the registry references but that has a typo'd `run` signature).

- [ ] **Step 2: Run the full existing repo test suite to confirm nothing broke**

Run: `pytest -m "not integration" -v`
Expected: all PASS (unchanged behavior outside the new `tests/e2e/` package and the Task 1 logging addition)

- [ ] **Step 3: Document new env vars in `.env.example`**

Add a new section to `.env.example` (after the existing `--- MT5 web UI (para VNC) ---` block):

```
# --- Suite de pruebas e2e (tests/e2e/, docker compose --profile e2e) ---
# Host/puerto de mt5_acct1 dentro de la red interna de docker compose
MT5_HOST=mt5_acct1
MT5_PORT=8001
# Host de trade_orchestrator dentro de la red interna (para /health y para
# que el checklist de pre-vuelo confirme que esta arriba)
TRADE_ORCHESTRATOR_HOST=trade_orchestrator
# TG_TEST_CHAT_ID (definido arriba) DEBE ser el chat_id de un canal/grupo de
# Telegram dedicado a pruebas, agregado a allowed_channels en ACCOUNTS_JSON
# -- ver docs/superpowers/specs/2026-09-04-e2e-test-suite-design.md seccion 3.1.
# El n8n/Ollama de pruebas debe apuntar su webhook de entrada y su callback
# de /mgmt/action a ESTE VPS, no a produccion -- ver seccion 4 del mismo spec.
```

- [ ] **Step 4: Commit**

```bash
git add .env.example
git commit -m "docs: document e2e suite env vars and test-channel/n8n preconditions in .env.example

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

**Spec coverage:**
- §3.1 four modules → Tasks 3, 4, 5, 11 (`price_reader`, `telegram_sender`, `vps_observer`, `runner`). ✓
- §3.1 scenarios as files with arrange/act/assert + cleanup → Tasks 7-10. ✓
- §3.2 `e2e_runner` service, `profile: e2e`, no new ports → Task 12 (plus the Docker-socket mount called out explicitly as a deviation the operator should confirm). ✓
- §4 preconditions (test channel in `allowed_channels`, n8n pointing at this VPS) → Task 6 (`preflight.py` checks the channel automatically; the n8n-pointing-at-this-VPS check is inherently unverifiable from inside this repo, so it's documented as an operator precondition in Task 13's `.env.example` addition and in every Family B scenario's docstring, consistent with spec §7's own admission that this can only be a partial check).
- §5 Family A (4 scenarios, determinism note, 5s gold entry-range note) → Task 8. ✓
- §5 Family B (8 scenarios, corrected B7) → Task 9. ✓
- §5 Family C (3 scenarios) → Task 10. ✓
- §6 cleanup → `cleanup_group` (Task 7), called from every scenario's `finally`. ✓
- §7 report + 3-way outcome classification + pre-flight → Task 11 (`format_report`), Task 6 (`preflight`). ✓
- §8 unit tests for helpers, scenarios only exercised for real → every task pairs a helper with unit tests; scenario unit tests mock `ScenarioContext` entirely (never touch real Telethon/RPyC/Redis), consistent with "scenarios are the test, not tested" — the mocking in Tasks 8-10 verifies orchestration logic (which message is sent, how results are classified), not real infrastructure behavior.

**Type consistency check:** `ScenarioContext`, `ScenarioResult`, `ScenarioOutcome`, `cleanup_group` (Task 7) are used identically across Tasks 8, 9, 10, 11 — same field names (`cfg`, `price_reader`, `sender`, `observer`), same `run(ctx) -> ScenarioResult` signature everywhere, same `SCENARIOS` dict keys used consistently between Task 11's registry and every scenario module's filename.

**Docker-socket mount:** confirmed with the user as an explicit operator decision (read-only, on an already-trusted VPS) in preference to the more complex host-exposure alternative — spec §3.2 documents it, Task 12 implements it. Not an open item.
