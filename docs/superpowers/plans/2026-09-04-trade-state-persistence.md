# Trade State Persistence + Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A restart of the `trade_orchestrator` container must never again leave an open dual-TP group without mechanical management (BE/trailing), because its in-memory state was lost.

**Architecture:** A new `TradeStateStore` component persists one JSON document per `group_id` to Redis (primary) and an append-only JSON Lines file (backup, bind-mounted outside the container). `TradeManager` writes to it at every point where `self.trades` changes in a way that matters for mechanical management, and reads from it exactly once at startup via a new `reconcile_from_mt5()` method that rebuilds `self.trades` and `self._next_group_id` from MT5's live positions plus whichever of Redis/the file still has each group's management memory (`tp1_price`, `tp2_price`, `be_applied`, `peak_multiple` — none of which exist in MT5 itself).

**Tech Stack:** Python 3.10, `redis.asyncio` (already in use via `services/common/redis_streams.py`), `pytest`/`pytest-asyncio`, `SimuladorMT5` test double.

**Spec:** `docs/superpowers/specs/2026-09-04-trade-state-persistence-design.md`

## Global Constraints

- Redis key format: `trade_groups:{group_id}` → `SET` with the full JSON document as a string (no `HSET` — always read/write the whole document). No TTL.
- Backup file: `data/trade_state.jsonl`, bind-mounted into the `trade_orchestrator` container at `/app/data/trade_state.jsonl` (container `WORKDIR` is `/app`). Append-only writes; "last line wins" on read; compacted (atomic tmp-file + rename) after a successful startup reconciliation.
- A group-close entry is `{"group_id": N, "closed": true}` — written to both Redis (`DEL`) and the file (appended) whenever both legs of a group leave `self.trades`, whether detected passively (`_tick_once_account`) or actively (`apply_mgmt_action`'s `close_now`).
- Comment format (already in production, unchanged): `TM-GRP{group_id}-{leg}` via `safe_comment()`, `leg` is `"tp1"` or `"runner"`.
- MAGIC = `987654` (already defined in `trade_manager.py`).
- All store writes are fire-and-forget with respect to the trading critical path: a write failure logs a warning and never raises into the caller.
- `reconcile_from_mt5()` never sends `order_send` for opening/closing positions — its only MT5 calls are read-only (`positions_get`), except for the one documented exception: applying BE to a runner whose `tp1_leg` is confirmed closed during the downtime (same `_force_runner_sl` call `_on_tp1_leg_closed` already makes in production).
- `_next_group_id` after reconciliation must be `max(group_id seen across MT5 comments, Redis documents, file documents) + 1`, defaulting to `1` if nothing is found anywhere.

---

### Task 1: `TradeStateStore` — persistence in isolation

**Files:**
- Create: `services/trade_orchestrator/trade_state_store.py`
- Create: `services/trade_orchestrator/test_trade_state_store.py`

**Interfaces:**
- Produces:
  - `class TradeStateStore.__init__(self, redis_client, file_path: str)` — `redis_client` is a `redis.asyncio.Redis` (or any object exposing `async def set(key, value)`, `async def get(key)`, `async def delete(key)` — matches the real client's API).
  - `async def save_group(self, doc: dict) -> None` — `doc` must contain `group_id` (int). Writes to Redis (`SET trade_groups:{group_id}`) and appends one JSON line to the file. Never raises — logs a warning on any failure in either backend and returns.
  - `async def close_group(self, group_id: int) -> None` — deletes the Redis key and appends `{"group_id": group_id, "closed": true}` to the file. Never raises.
  - `async def load_group(self, group_id: int) -> tuple[Optional[dict], str]` — tries Redis first; if not found (or Redis errors), falls back to scanning the file for the last non-close entry for that `group_id`. Returns `(doc, "redis")`, `(doc, "file")`, or `(None, "none")` — the source string is what `TradeManager.reconcile_from_mt5` uses to build its `recovered_from_redis`/`recovered_from_file` summary counts (a plain `Optional[dict]` return can't distinguish those two cases, and getting this wrong silently breaks that reporting — this exact bug was caught and fixed during this plan's self-review, see Task 4's summary-counting logic).
  - `async def load_all_group_ids(self) -> set[int]` — every `group_id` that has ANY entry (open or close) in the file, unioned with every `group_id` currently present as a Redis key (`trade_groups:*` via `KEYS`, acceptable here since this only runs once at startup, not in the hot path). Used for `_next_group_id` reconciliation — a closed group's `group_id` still counts, since MT5 might have an old comment referencing it.
  - `async def compact(self, active_group_ids: set[int]) -> None` — atomically rewrites the file (write to `{file_path}.tmp`, then `os.replace`) keeping only the latest entry for each `group_id` in `active_group_ids` (closed/stale groups are dropped entirely — no need to keep their close markers once compacted, since MT5 won't reference them and `load_all_group_ids`'s closed-group counting only matters before the first compaction of this session).

**Step-by-step:**

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_trade_state_store.py
import asyncio
import json
import os
import tempfile

import pytest

from services.trade_orchestrator.trade_state_store import TradeStateStore


class FakeRedis:
    """Minimal redis.asyncio.Redis stand-in — just the three methods TradeStateStore uses."""
    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    async def set(self, key, value):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        self.store[key] = value

    async def get(self, key):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        return self.store.get(key)

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        self.store.pop(key, None)

    async def keys(self, pattern):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture
def tmp_file():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)  # store must create it on first write, like open(path, "a")
    yield path
    if os.path.exists(path):
        os.remove(path)
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.mark.asyncio
async def test_save_and_load_round_trip(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    doc = {"group_id": 1, "symbol": "XAUUSD", "direction": "BUY", "tp1_price": 4439.8}

    await store.save_group(doc)
    loaded, source = await store.load_group(1)

    assert loaded == doc
    assert source == "redis"


@pytest.mark.asyncio
async def test_close_group_removes_it_from_redis_and_load(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})

    await store.close_group(1)

    loaded, source = await store.load_group(1)
    assert loaded is None
    assert source == "none"
    assert "trade_groups:1" not in redis.store


@pytest.mark.asyncio
async def test_load_falls_back_to_file_when_redis_has_nothing(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 5, "symbol": "EURUSD"})
    # Simulate Redis losing the key (e.g. flushed) but the file surviving.
    redis.store.clear()

    loaded, source = await store.load_group(5)

    assert loaded == {"group_id": 5, "symbol": "EURUSD"}
    assert source == "file"


@pytest.mark.asyncio
async def test_load_falls_back_to_file_when_redis_errors(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 5, "symbol": "EURUSD"})
    redis.fail = True  # Redis reachable at write time, now erroring

    loaded, source = await store.load_group(5)

    assert loaded == {"group_id": 5, "symbol": "EURUSD"}
    assert source == "file"


@pytest.mark.asyncio
async def test_file_last_entry_wins_across_multiple_writes(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "peak_multiple": 0.1})
    await store.save_group({"group_id": 1, "peak_multiple": 0.5})
    await store.save_group({"group_id": 1, "peak_multiple": 0.9})
    redis.store.clear()  # force reading from the file

    loaded, source = await store.load_group(1)

    assert loaded["peak_multiple"] == 0.9
    assert source == "file"


@pytest.mark.asyncio
async def test_file_close_marker_wins_over_earlier_save(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})
    await store.close_group(1)
    redis.store.clear()

    loaded, source = await store.load_group(1)

    assert loaded is None
    assert source == "none"


@pytest.mark.asyncio
async def test_save_group_does_not_raise_when_redis_fails(tmp_file):
    redis = FakeRedis()
    redis.fail = True
    store = TradeStateStore(redis, tmp_file)

    # Must not raise -- the file write still succeeds even if Redis is down.
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})

    redis.fail = False
    loaded, source = await store.load_group(1)  # comes from the file since Redis never had it
    assert loaded == {"group_id": 1, "symbol": "XAUUSD"}
    assert source == "file"


@pytest.mark.asyncio
async def test_load_all_group_ids_unions_redis_and_file(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})
    await store.save_group({"group_id": 2, "symbol": "EURUSD"})
    await store.close_group(2)  # closed groups still count -- MT5 might still reference the id
    # Simulate group 3 existing only in Redis (e.g. file write raced/failed once)
    redis.store["trade_groups:3"] = json.dumps({"group_id": 3, "symbol": "GBPUSD"})

    ids = await store.load_all_group_ids()

    assert ids == {1, 2, 3}


@pytest.mark.asyncio
async def test_compact_keeps_only_latest_entry_for_active_groups(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "peak_multiple": 0.1})
    await store.save_group({"group_id": 1, "peak_multiple": 0.5})
    await store.save_group({"group_id": 2, "symbol": "EURUSD"})
    await store.close_group(2)

    await store.compact(active_group_ids={1})

    with open(tmp_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    assert lines[0] == {"group_id": 1, "peak_multiple": 0.5}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_state_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.trade_orchestrator.trade_state_store'`

- [ ] **Step 3: Write the implementation**

```python
# services/trade_orchestrator/trade_state_store.py
"""
trade_state_store.py
Persiste el estado de gestion de TradeManager (memoria que no existe en
MT5: tp1_price, tp2_price, be_applied, peak_multiple) para que un reinicio
del contenedor pueda reconstruirlo, en vez de dejar grupos huerfanos.

Dos capas de persistencia:
- Redis (primaria, rapida, ya en el stack del proyecto).
- Archivo JSON Lines (respaldo, bind-mounted fuera del contenedor —
  sobrevive aunque el volumen de Redis se pierda por completo).

Ver docs/superpowers/specs/2026-09-04-trade-state-persistence-design.md.
"""
import asyncio
import json
import logging
import os
from typing import Optional

log = logging.getLogger("trade_orchestrator.trade_state_store")

REDIS_KEY_PREFIX = "trade_groups:"


class TradeStateStore:
    def __init__(self, redis_client, file_path: str):
        self.redis = redis_client
        self.file_path = file_path

    def _redis_key(self, group_id: int) -> str:
        return f"{REDIS_KEY_PREFIX}{group_id}"

    async def save_group(self, doc: dict) -> None:
        """
        Persiste el documento completo de un grupo (Redis + archivo). Nunca
        lanza: un fallo aqui no debe interrumpir la operacion de trading en
        curso — se loguea como warning y se continua.
        """
        group_id = doc["group_id"]
        line = json.dumps(doc)
        try:
            await self.redis.set(self._redis_key(group_id), line)
        except Exception as e:
            log.warning("[STORE] fallo escribiendo group_id=%s en Redis: %s", group_id, e)
        try:
            await asyncio.to_thread(self._append_line, line)
        except Exception as e:
            log.warning("[STORE] fallo escribiendo group_id=%s en archivo: %s", group_id, e)

    async def close_group(self, group_id: int) -> None:
        """Marca un grupo como cerrado: borra de Redis, agrega marcador de cierre al archivo."""
        try:
            await self.redis.delete(self._redis_key(group_id))
        except Exception as e:
            log.warning("[STORE] fallo borrando group_id=%s de Redis: %s", group_id, e)
        try:
            line = json.dumps({"group_id": group_id, "closed": True})
            await asyncio.to_thread(self._append_line, line)
        except Exception as e:
            log.warning("[STORE] fallo escribiendo cierre de group_id=%s en archivo: %s", group_id, e)

    async def load_group(self, group_id: int) -> tuple[Optional[dict], str]:
        """
        Redis primero; si no esta (o Redis falla), cae al archivo (ultima
        entrada no-cierre). Retorna (doc, "redis"), (doc, "file"), o
        (None, "none"). El string de fuente es lo que
        TradeManager.reconcile_from_mt5 usa para reportar
        recovered_from_redis vs recovered_from_file por separado.
        """
        try:
            raw = await self.redis.get(self._redis_key(group_id))
            if raw:
                return json.loads(raw), "redis"
        except Exception as e:
            log.warning("[STORE] fallo leyendo group_id=%s de Redis, probando archivo: %s", group_id, e)

        try:
            doc = await asyncio.to_thread(self._load_from_file, group_id)
            return (doc, "file") if doc is not None else (None, "none")
        except Exception as e:
            log.warning("[STORE] fallo leyendo group_id=%s de archivo: %s", group_id, e)
            return None, "none"

    async def load_all_group_ids(self) -> set[int]:
        """
        Todo group_id con CUALQUIER entrada (abierta o de cierre) en el
        archivo, unido con todo group_id presente como key en Redis. Usado
        para reconciliar _next_group_id — un grupo cerrado sigue contando,
        porque MT5 podria todavia tener un comment viejo que lo referencia.
        """
        ids: set[int] = set()
        try:
            keys = await self.redis.keys(f"{REDIS_KEY_PREFIX}*")
            for k in keys:
                try:
                    ids.add(int(k[len(REDIS_KEY_PREFIX):]))
                except ValueError:
                    continue
        except Exception as e:
            log.warning("[STORE] fallo listando keys de Redis: %s", e)

        try:
            file_ids = await asyncio.to_thread(self._all_group_ids_from_file)
            ids |= file_ids
        except Exception as e:
            log.warning("[STORE] fallo leyendo group_ids del archivo: %s", e)

        return ids

    async def compact(self, active_group_ids: set[int]) -> None:
        """
        Reescribe el archivo de forma atomica (tmp + rename), conservando
        solo la ultima entrada de cada group_id en active_group_ids. Grupos
        cerrados/inactivos se descartan por completo.
        """
        try:
            await asyncio.to_thread(self._compact_file, active_group_ids)
        except Exception as e:
            log.warning("[STORE] fallo compactando archivo: %s", e)

    # ---- Helpers sincronos (corren en threads via asyncio.to_thread) ----

    def _append_line(self, line: str) -> None:
        with open(self.file_path, "a") as f:
            f.write(line + "\n")

    def _read_last_entries(self) -> dict[int, dict]:
        """Recorre el archivo y retorna {group_id: ultima_entrada} (incluye cierres)."""
        if not os.path.exists(self.file_path):
            return {}
        last: dict[int, dict] = {}
        with open(self.file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                gid = doc.get("group_id")
                if gid is None:
                    continue
                last[gid] = doc
        return last

    def _load_from_file(self, group_id: int) -> Optional[dict]:
        last = self._read_last_entries()
        doc = last.get(group_id)
        if doc is None or doc.get("closed"):
            return None
        return doc

    def _all_group_ids_from_file(self) -> set[int]:
        return set(self._read_last_entries().keys())

    def _compact_file(self, active_group_ids: set[int]) -> None:
        last = self._read_last_entries()
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w") as f:
            for gid in active_group_ids:
                doc = last.get(gid)
                if doc and not doc.get("closed"):
                    f.write(json.dumps(doc) + "\n")
        os.replace(tmp_path, self.file_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_state_store.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_state_store.py services/trade_orchestrator/test_trade_state_store.py
git commit -m "feat: add TradeStateStore for trade group persistence

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Comment parsing + `SimuladorMT5` support for `comment`/`magic`

**Files:**
- Modify: `services/trade_orchestrator/trade_utils.py`
- Modify: `tests/test_simulador_mt5.py`
- Create: `services/trade_orchestrator/test_trade_utils_parse_comment.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `parse_group_comment(comment: str) -> Optional[tuple[int, str]]` in `trade_utils.py` — returns `(group_id, leg)` if `comment` matches `TM-GRP{digits}-{tp1|runner}` exactly, `None` otherwise (any other shape, including old-format or corrupted comments).
  - `SimuladorMT5.order_send` (action=1 path) now stores `comment` and `magic` on the position dict, and `positions_get` includes them (default `magic=0`, `comment=''` when not provided by the request — matches how a real order without those fields would come back). Task 3+ tests rely on this to build fixtures with specific comments/magics.

**Step-by-step:**

- [ ] **Step 1: Write the failing test for `parse_group_comment`**

```python
# services/trade_orchestrator/test_trade_utils_parse_comment.py
from services.trade_orchestrator.trade_utils import parse_group_comment


def test_parses_tp1_leg_comment():
    assert parse_group_comment("TM-GRP1-tp1") == (1, "tp1")


def test_parses_runner_leg_comment():
    assert parse_group_comment("TM-GRP42-runner") == (42, "runner")


def test_parses_multi_digit_group_id():
    assert parse_group_comment("TM-GRP1234-tp1") == (1234, "tp1")


def test_rejects_missing_prefix():
    assert parse_group_comment("GRP1-tp1") is None


def test_rejects_unknown_leg():
    assert parse_group_comment("TM-GRP1-scalp") is None


def test_rejects_non_numeric_group_id():
    assert parse_group_comment("TM-GRPabc-tp1") is None


def test_rejects_empty_string():
    assert parse_group_comment("") is None


def test_rejects_unrelated_comment():
    assert parse_group_comment("PartialClose") is None


def test_rejects_none():
    assert parse_group_comment(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_trade_utils_parse_comment.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_group_comment'`

- [ ] **Step 3: Add `parse_group_comment` to `trade_utils.py`**

Add this function right after `safe_comment` (`services/trade_orchestrator/trade_utils.py`, after the existing function ending in `return base[:31]`):

```python
_GROUP_COMMENT_RE = re.compile(r"^TM-GRP(\d+)-(tp1|runner)$")


def parse_group_comment(comment: Optional[str]) -> Optional[tuple[int, str]]:
    """
    Parsea el comment de una posicion MT5 abierta por este sistema
    (formato TM-GRP{group_id}-{leg}, ver safe_comment). Retorna
    (group_id, leg) si matchea exactamente, None si no matchea en absoluto
    (comment de otra version del sistema, corrupto, o de otro origen) —
    usado por TradeManager.reconcile_from_mt5() para distinguir posiciones
    propias reconstruibles de posiciones huerfanas a solo notificar.
    """
    if not comment:
        return None
    m = _GROUP_COMMENT_RE.match(comment)
    if not m:
        return None
    return int(m.group(1)), m.group(2)
```

Check the top of `services/trade_orchestrator/trade_utils.py` for existing imports — `re` and `Optional` must both be available. If `Optional` isn't already imported, add `from typing import Optional` near the top with the other imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/trade_orchestrator/test_trade_utils_parse_comment.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Extend `SimuladorMT5` to track `comment`/`magic`**

In `tests/test_simulador_mt5.py`, find the `order_send` method's `action == 1` branch (the dict built at `self.positions[ticket] = {...}`) and add two fields:

```python
    def order_send(self, req):
        action = req.get('action')
        if action == 1:  # OPEN
            self.last_ticket += 1
            ticket = self.last_ticket
            self.positions[ticket] = {
                'ticket': ticket,
                'symbol': req['symbol'],
                'volume': req.get('volume', 0.01),
                'price_open': req.get('price', self.price),
                'sl': req.get('sl', 0.0),
                'tp': req.get('tp', 0.0),
                'price_current': self.price,
                'type': req.get('type', 0),
                'comment': req.get('comment', ''),
                'magic': req.get('magic', 0),
            }
            return type('OrderSendResult', (), {'retcode': 10009, 'order': ticket, 'deal': ticket, 'comment': 'Request executed'})()
```

(Only the two new dict entries — `'comment'` and `'magic'` — are new; the rest of the method is unchanged.)

- [ ] **Step 6: Run the full existing test suite to confirm nothing broke**

Run: `pytest services/ -v`
Expected: PASS (all existing tests, plus the 9 new ones from this task) — `positions_get()` already returns whatever's in the dict via `type('TradePosition', (), v)()`, so adding two new dict keys is additive and doesn't change any existing attribute access.

- [ ] **Step 7: Commit**

```bash
git add services/trade_orchestrator/trade_utils.py services/trade_orchestrator/test_trade_utils_parse_comment.py tests/test_simulador_mt5.py
git commit -m "feat: add parse_group_comment; extend SimuladorMT5 with comment/magic

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire `TradeStateStore` writes into `TradeManager`

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py`
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `TradeStateStore` from Task 1 (`save_group`, `close_group`).
- Produces: `TradeManager.__init__(self, mt5_executor, *, notifier=None, config_provider=None, state_store=None)` — new optional `state_store` kwarg, `None` by default (so every existing test/caller that doesn't pass it keeps working exactly as before — no writes happen if `state_store` is `None`).
- Produces: `TradeManager._group_doc(group_id: int) -> Optional[dict]` — builds the persistable dict from the current `self.trades` for a given `group_id` (used internally by every write point below; exposed as a method so it's independently testable).

**Step-by-step:**

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py` (uses the existing `DummyExecutor`/`DummyNotifier`/`ACCOUNT` fixtures already in that file):

```python
class RecordingStore:
    """Test double for TradeStateStore — records every save/close call."""
    def __init__(self):
        self.saved: list[dict] = []
        self.closed: list[int] = []

    async def save_group(self, doc):
        self.saved.append(doc)

    async def close_group(self, group_id):
        self.closed.append(group_id)


@pytest.mark.asyncio
async def test_open_group_persists_the_new_group():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert len(store.saved) == 1
    doc = store.saved[-1]
    assert doc["group_id"] == group_id
    assert doc["symbol"] == "XAUUSD"
    assert doc["direction"] == "BUY"
    assert doc["tp1_price"] == 2510.0
    assert doc["tp2_price"] == 2530.0
    tp1_ticket = next(t.ticket for t in tm.trades.values() if t.leg == "tp1")
    runner_ticket = next(t.ticket for t in tm.trades.values() if t.leg == "runner")
    assert doc["legs"]["tp1"]["ticket"] == tp1_ticket
    assert doc["legs"]["runner"]["ticket"] == runner_ticket


@pytest.mark.asyncio
async def test_update_group_signal_persists_new_tp_values():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2470.0, tp1=None, tp2=None)
    store.saved.clear()

    await tm.update_group_signal(group_id, sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert len(store.saved) == 1
    assert store.saved[-1]["tp1_price"] == 2510.0
    assert store.saved[-1]["tp2_price"] == 2530.0


@pytest.mark.asyncio
async def test_tp1_leg_closing_persists_be_applied():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    store.saved.clear()
    del sim.positions[tp1_leg.ticket]

    await tm._tick_once_account(ACCOUNT)

    runner_docs = [d for d in store.saved if d["group_id"] == group_id]
    assert len(runner_docs) == 1
    assert runner_docs[-1]["legs"]["runner"]["be_applied"] is True


@pytest.mark.asyncio
async def test_trailing_update_persists_peak_multiple():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE
    store.saved.clear()
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    sim.price = 2522.0  # 60% of unit=20 past tp1=2510
    sim.positions[runner_leg.ticket]['price_current'] = sim.price

    await tm._tick_once_account(ACCOUNT)

    assert len(store.saved) == 1
    assert store.saved[-1]["legs"]["runner"]["peak_multiple"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_both_legs_closing_closes_the_group_in_the_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # tp1 closes, BE applied to runner
    del sim.positions[runner_leg.ticket]

    await tm._tick_once_account(ACCOUNT)  # runner closes too

    assert group_id in store.closed


@pytest.mark.asyncio
async def test_mgmt_close_now_closes_the_group_in_the_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    result = await tm.apply_mgmt_action(action="close_now", symbol="XAUUSD", raw_text="close it", correction=None)

    assert result["status"] == "closed"
    assert group_id in store.closed


@pytest.mark.asyncio
async def test_no_state_store_is_a_safe_default():
    """TradeManager() without state_store (existing callers, all prior tests) must keep working unchanged."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())  # no state_store kwarg

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert group_id is not None  # did not raise despite no store configured
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "persist or store" -v`
Expected: FAIL — `TradeManager.__init__() got an unexpected keyword argument 'state_store'`

- [ ] **Step 3: Add `state_store` support to `TradeManager`**

In `services/trade_orchestrator/trade_manager.py`, modify `__init__` (currently at line 36):

```python
    def __init__(self, mt5_executor, *, notifier=None, config_provider=None, state_store=None):
        self.mt5 = mt5_executor
        self.notifier = notifier
        self.config_provider = config_provider
        self.state_store = state_store
        self.trades: dict[int, ManagedTrade] = {}
        self._next_group_id = 1
```

Add a new helper method right after `_notify` (currently ending at line 59, before `DEFAULT_MT5_CALL_TIMEOUT_SECONDS = 20.0`):

```python
    def _group_doc(self, group_id: int) -> Optional[dict]:
        """
        Construye el documento persistible para group_id a partir del estado
        actual en self.trades. None si el grupo no tiene piernas activas.
        """
        legs = [t for t in self.trades.values() if t.group_id == group_id]
        if not legs:
            return None
        first = legs[0]
        doc = {
            "group_id": group_id,
            "account_name": first.account_name,
            "symbol": first.symbol,
            "direction": first.direction,
            "tp1_price": first.tp1_price,
            "tp2_price": first.tp2_price,
            "legs": {},
            "updated_ts": time.time(),
        }
        for t in legs:
            doc["legs"][t.leg] = {
                "ticket": t.ticket,
                "planned_sl": t.planned_sl,
                "entry_price": t.entry_price,
                "be_applied": t.be_applied,
                "peak_multiple": t.peak_multiple,
            }
        return doc

    async def _persist_group(self, group_id: int) -> None:
        """Guarda el estado actual de group_id en el store, si hay uno configurado."""
        if not self.state_store:
            return
        doc = self._group_doc(group_id)
        if doc is None:
            return
        await self.state_store.save_group(doc)

    async def _close_group_in_store(self, group_id: int) -> None:
        if not self.state_store:
            return
        await self.state_store.close_group(group_id)
```

Now wire the five write points and two close points. Each is a one-line addition right after the existing state mutation, using `self._persist_group(group_id)` / `self._close_group_in_store(group_id)`:

**In `open_group`** (currently ends with `return group_id` around line 292), right after the `await self._notify("group_opened", ...)` call and before `return group_id`:

```python
        await self._notify("group_opened", group_id=group_id, symbol=symbol, direction=direction,
                            tp1_ticket=tickets["tp1"], runner_ticket=tickets["runner"], sl=sl, tp1=tp1, tp2=tp2)
        await self._persist_group(group_id)
        return group_id
```

**In `update_group_signal`** (currently ends with `await self._notify("group_updated", ...)` around line 366), right after that call:

```python
        log.info("[TM] group %s actualizado: sl=%s tp1=%s tp2=%s", group_id, sl, tp1, tp2)
        await self._notify("group_updated", group_id=group_id, sl=sl, tp1=tp1, tp2=tp2)
        await self._persist_group(group_id)
```

**In `_on_tp1_leg_closed`** (currently around line 465-467), right after `runner.be_applied = True` and its notify call — inside the `if ok:` branch:

```python
        if ok:
            runner.be_applied = True
            await self._notify("tp1_hit", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, runner_ticket=runner.ticket)
            await self._persist_group(tp1_leg.group_id)
        else:
```

**In `_apply_trailing`** (currently around line 508-510), inside the `if ok:` branch after the notify call:

```python
        ok = await self._force_runner_sl(account, client, runner, new_sl, reason="trailing")
        if ok:
            await self._notify("trailing_updated", group_id=runner.group_id, ticket=runner.ticket, peak_multiple=multiple, new_sl=new_sl)
            await self._persist_group(runner.group_id)
```

**In `apply_mgmt_action`'s `move_sl_be_now` branch** (currently around line 553-556), inside the `if ok:` branch:

```python
            ok = await self._force_runner_sl(account, client, runner, be_price, reason="mgmt-fallback-BE")
            if ok:
                runner.be_applied = True
                await self._notify("mgmt_move_sl_be_applied", group_id=group_id, symbol=symbol, raw_text=raw_text)
                await self._persist_group(group_id)
                return {"status": "applied", "group_id": group_id}
```

**In `apply_mgmt_action`'s `signal_correction` branch** (currently around line 563-571) — `update_group_signal` already persists internally (added above), so this branch needs no additional call; leave it as-is.

**Close point 1 — `_tick_once_account`** (currently around lines 419-427), the loop that detects closed tickets. Add the close-detection after both legs of a group are gone. This needs slightly more logic than a one-liner: after popping a closed ticket, check whether the group has any legs left at all, and close it in the store if not:

```python
            # Detect closed tickets for this account (TP1 hit, SL hit, or manual close)
            for ticket in [t for t, mt in self.trades.items() if mt.account_name == account["name"]]:
                if ticket in pos_by_ticket:
                    continue
                closed_trade = self.trades.pop(ticket)
                if closed_trade.leg == "tp1":
                    await self._on_tp1_leg_closed(account, client, closed_trade)
                else:
                    await self._notify("runner_closed", group_id=closed_trade.group_id, ticket=ticket, symbol=closed_trade.symbol)
                remaining = [t for t in self.trades.values() if t.group_id == closed_trade.group_id]
                if not remaining:
                    await self._close_group_in_store(closed_trade.group_id)
```

(Only the added `remaining = [...]` / `if not remaining:` block at the end is new — the rest of this loop body is unchanged from what's already there.)

**Close point 2 — `apply_mgmt_action`'s `close_now` branch** (currently around lines 529-534):

```python
        if action == "close_now":
            for t in list(legs):
                await self._call(client.partial_close, account, t.ticket, 100)
                self.trades.pop(t.ticket, None)
            await self._notify("mgmt_close_now", group_id=group_id, symbol=symbol, raw_text=raw_text)
            await self._close_group_in_store(group_id)
            return {"status": "closed", "group_id": group_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS (all existing tests plus the 7 new ones from this task — existing tests all construct `TradeManager` without `state_store`, exercising the `None`-default safe path)

- [ ] **Step 5: Run the full suite**

Run: `pytest services/ -v`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat: wire TradeStateStore writes into TradeManager's write points

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: `reconcile_from_mt5()`

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py`
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `TradeStateStore.load_group`, `load_all_group_ids`, `compact` (Task 1); `parse_group_comment` (Task 2); `state_store` on `TradeManager` (Task 3).
- Produces: `async def TradeManager.reconcile_from_mt5(self, accounts: list[dict]) -> dict` — returns a summary dict `{"recovered_from_redis": int, "recovered_from_file": int, "degraded": int, "orphaned": list[dict]}` (each orphaned entry is `{"ticket": int, "symbol": str, "comment": str}`). Populates `self.trades` and sets `self._next_group_id`. Never sends `order_send` to open/close positions — the one exception (applying BE to a runner whose `tp1_leg` is confirmed gone) reuses the existing `_force_runner_sl`/`_on_tp1_leg_closed`-style call, which is itself just an `order_send` to *modify* SL on a still-open position, not to open or close anything.

**Step-by-step:**

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`. These build MT5 positions directly via `sim.order_send` with explicit `comment`/`magic` (bypassing `open_group`, since the point is to test recovery of state `open_group` never ran for in this process):

```python
from services.trade_orchestrator.trade_manager import MAGIC


def _open_raw_position(sim, *, ticket_price, sl, tp, comment, magic=MAGIC, direction_type=0):
    """Directly injects a position into SimuladorMT5, bypassing TradeManager —
    simulates a position that existed before this process started."""
    req = {
        "action": 1, "symbol": "XAUUSD", "volume": 0.04, "type": direction_type,
        "price": ticket_price, "sl": sl, "tp": tp, "comment": comment, "magic": magic,
    }
    res = sim.order_send(req)
    return res.order


@pytest.mark.asyncio
async def test_reconcile_recovers_full_state_from_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # its default load_group/load_all_group_ids read from store.docs directly

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": tp1_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": True, "peak_multiple": 0.35},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["recovered_from_redis"] == 1
    assert len(tm.trades) == 2
    runner = tm.trades[runner_ticket]
    assert runner.tp1_price == 2510.0
    assert runner.tp2_price == 2530.0
    assert runner.be_applied is True
    assert runner.peak_multiple == 0.35


@pytest.mark.asyncio
async def test_reconcile_falls_back_to_degraded_when_store_has_nothing():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # empty store.docs -- every group_id misses

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP7-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP7-runner")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["degraded"] == 1
    assert len(tm.trades) == 2
    runner = tm.trades[runner_ticket]
    assert runner.tp1_price is None
    assert runner.tp2_price is None
    assert runner.be_applied is False
    assert runner.peak_multiple == 0.0
    assert runner.planned_sl == 2490.0  # recovered directly from the MT5 position
    assert runner.entry_price == 2500.0


@pytest.mark.asyncio
async def test_reconcile_reports_orphan_for_unparseable_comment():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    store.compact = lambda active_group_ids: None

    ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="some-old-format")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert len(summary["orphaned"]) == 1
    assert summary["orphaned"][0]["ticket"] == ticket
    assert summary["orphaned"][0]["comment"] == "some-old-format"
    assert ticket not in tm.trades  # never managed automatically


@pytest.mark.asyncio
async def test_reconcile_applies_be_synchronously_when_tp1_closed_during_downtime():
    """The gap this whole design exists to close: tp1_leg closed while the
    process was down, runner is still open. The normal close-detection loop
    would never see this (tp1 was never re-inserted into self.trades) —
    reconcile_from_mt5 must apply BE inline, during reconciliation itself."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    # Only the runner still exists in MT5 -- tp1_leg closed during downtime.
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": 999999, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    runner_pos = sim.positions_get(ticket=runner_ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6  # moved to entry price (BE)
    assert tm.trades[runner_ticket].be_applied is True


@pytest.mark.asyncio
async def test_reconcile_sets_next_group_id_above_the_highest_seen():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # empty docs -- load_group naturally returns (None, "none")
    store.load_all_group_ids = lambda: {5}  # group 5 known only from a closed entry in the file

    _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP3-tp1")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    assert tm._next_group_id == 6  # max(3 from MT5, 5 from store) + 1


@pytest.mark.asyncio
async def test_reconcile_with_no_state_store_still_recovers_degraded():
    """No state_store configured at all -- must behave like every group is degraded, not crash."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())  # no state_store

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["degraded"] == 1
    assert len(tm.trades) == 2
```

Add `load_group`, `load_all_group_ids`, `compact` as no-op-by-default attributes on `RecordingStore` (defined in Task 3) so the plain constructor still works for tests that don't override them:

```python
class RecordingStore:
    """Test double for TradeStateStore — records every save/close call."""
    def __init__(self):
        self.saved: list[dict] = []
        self.closed: list[int] = []
        self.docs: dict[int, dict] = {}

    async def save_group(self, doc):
        self.saved.append(doc)

    async def close_group(self, group_id):
        self.closed.append(group_id)

    async def load_group(self, group_id):
        doc = self.docs.get(group_id)
        return (doc, "redis") if doc is not None else (None, "none")

    async def load_all_group_ids(self):
        return set(self.docs.keys())

    async def compact(self, active_group_ids):
        pass
```

(This replaces the `RecordingStore` class added in Task 3 — extend it in place rather than duplicating. `load_group` returns the same `(doc, source)` tuple shape `TradeStateStore.load_group` does — see Task 1 — so `reconcile_from_mt5` doesn't need to special-case the test double. The tests below that assign plain functions to `store.load_group` etc. instead override these defaults per-test; both styles work since Python instance attributes shadow class methods, but each override must still return that same tuple shape.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k reconcile -v`
Expected: FAIL — `AttributeError: 'TradeManager' object has no attribute 'reconcile_from_mt5'`

- [ ] **Step 3: Implement `reconcile_from_mt5`**

Add to `services/trade_orchestrator/trade_manager.py`, right after `apply_mgmt_action` (at the end of the class, after the `return {"status": "unknown_action"}` line):

```python
    async def reconcile_from_mt5(self, accounts: list[dict]) -> dict:
        """
        Reconstruye self.trades y self._next_group_id a partir de las
        posiciones reales en MT5, cruzadas contra el state_store (si hay
        uno configurado). Debe correr una sola vez, al arranque, antes de
        que run_forever() empiece a tickear — ver dual-TP spec de
        persistencia, seccion "Reconciliacion al arranque".
        Nunca envia order_send para abrir/cerrar posiciones — la unica
        excepcion es aplicar BE a un runner cuyo tp1_leg se confirma
        cerrado durante el downtime (mismo mecanismo que _on_tp1_leg_closed
        usa en produccion, invocado aqui de forma sincrona porque el tick
        loop normal nunca detectaria ese cierre por si solo).
        """
        summary = {"recovered_from_redis": 0, "recovered_from_file": 0, "degraded": 0, "orphaned": []}
        all_positions_by_group: dict[int, dict[str, object]] = {}
        highest_group_id_seen = 0

        for account in accounts:
            account = self._ensure_account_dict(account)
            if not account:
                continue
            client = self.mt5._client_for(account)
            try:
                positions = await self._call(client.positions_get) or []
            except Exception as e:
                log.error("[TM][RECONCILE] fallo obteniendo posiciones para cuenta %s: %s", account.get("name"), e)
                continue

            for pos in positions:
                if getattr(pos, "magic", None) != MAGIC:
                    continue
                comment = getattr(pos, "comment", "")
                parsed = parse_group_comment(comment)
                if parsed is None:
                    summary["orphaned"].append({"ticket": pos.ticket, "symbol": pos.symbol, "comment": comment})
                    continue
                group_id, leg = parsed
                highest_group_id_seen = max(highest_group_id_seen, group_id)
                all_positions_by_group.setdefault(group_id, {"account": account, "client": client})[leg] = pos

        if self.state_store:
            try:
                store_group_ids = await self.state_store.load_all_group_ids()
                if store_group_ids:
                    highest_group_id_seen = max(highest_group_id_seen, max(store_group_ids))
            except Exception as e:
                log.warning("[TM][RECONCILE] fallo listando group_ids del store: %s", e)

        for group_id, entry in all_positions_by_group.items():
            account = entry["account"]
            client = entry["client"]
            mt5_tp1 = entry.get("tp1")
            mt5_runner = entry.get("runner")

            doc = None
            source = "none"
            if self.state_store:
                try:
                    doc, source = await self.state_store.load_group(group_id)
                except Exception as e:
                    log.warning("[TM][RECONCILE] fallo leyendo group_id=%s del store: %s", group_id, e)

            if doc is not None:
                self._reconstruct_leg_from_doc(account, doc, "tp1", mt5_tp1)
                self._reconstruct_leg_from_doc(account, doc, "runner", mt5_runner)
                if source == "redis":
                    summary["recovered_from_redis"] += 1
                elif source == "file":
                    summary["recovered_from_file"] += 1
            else:
                if mt5_tp1 is not None:
                    self._reconstruct_leg_minimal(account, mt5_tp1, group_id, "tp1")
                if mt5_runner is not None:
                    self._reconstruct_leg_minimal(account, mt5_runner, group_id, "runner")
                summary["degraded"] += 1

            # The gap this whole design exists to close: the persisted doc knew
            # about a tp1_leg that's no longer in MT5, but runner still is. The
            # normal _tick_once_account close-detection loop compares against
            # self.trades — tp1 was never inserted into it this run, so it would
            # NEVER be seen as "closed". Apply BE synchronously, right here.
            if doc is not None and mt5_tp1 is None and mt5_runner is not None:
                runner_trade = self.trades.get(mt5_runner.ticket)
                if runner_trade is not None:
                    log.warning("[TM][RECONCILE] tp1_leg de group_id=%s cerro durante el downtime, aplicando BE ahora", group_id)
                    await self._on_tp1_leg_closed(account, client, ManagedTrade(
                        account_name=runner_trade.account_name, ticket=doc["legs"]["tp1"]["ticket"],
                        symbol=runner_trade.symbol, direction=runner_trade.direction,
                        group_id=group_id, leg="tp1", planned_sl=doc["legs"]["tp1"]["planned_sl"],
                    ))

            if doc is not None and mt5_tp1 is None and mt5_runner is None:
                # Both legs of a known group are gone -- closed during downtime, clean up.
                if self.state_store:
                    await self._close_group_in_store(group_id)

            # Re-persist to Redis anything that wasn't already there (recovered from the
            # file backup, or reconstructed in degraded mode) — so a subsequent restart
            # that keeps Redis intact recovers fully from layer 1 next time.
            if source != "redis" and self.state_store and self.trades:
                group_still_has_legs = any(t.group_id == group_id for t in self.trades.values())
                if group_still_has_legs:
                    await self._persist_group(group_id)

        self._next_group_id = highest_group_id_seen + 1
        ACTIVE_TRADES.set(len(self.trades))

        if self.state_store:
            try:
                active_ids = {t.group_id for t in self.trades.values()}
                await self.state_store.compact(active_ids)
            except Exception as e:
                log.warning("[TM][RECONCILE] fallo compactando el store: %s", e)

        log.info("[TM][RECONCILE] completado: recuperados_redis=%s degradados=%s huerfanos=%s",
                  summary["recovered_from_redis"], summary["degraded"], len(summary["orphaned"]))
        await self._notify("reconciliation_summary", **summary)
        return summary

    def _reconstruct_leg_from_doc(self, account, doc: dict, leg: str, mt5_pos) -> None:
        if mt5_pos is None:
            return
        leg_doc = doc["legs"].get(leg)
        if leg_doc is None:
            return
        self.trades[mt5_pos.ticket] = ManagedTrade(
            account_name=doc["account_name"], ticket=mt5_pos.ticket, symbol=doc["symbol"],
            direction=doc["direction"], group_id=doc["group_id"], leg=leg,
            planned_sl=leg_doc["planned_sl"], tp1_price=doc.get("tp1_price"), tp2_price=doc.get("tp2_price"),
            entry_price=leg_doc.get("entry_price"), be_applied=leg_doc.get("be_applied", False),
            peak_multiple=leg_doc.get("peak_multiple", 0.0),
        )

    def _reconstruct_leg_minimal(self, account, mt5_pos, group_id: int, leg: str) -> None:
        direction = "BUY" if getattr(mt5_pos, "type", 0) == 0 else "SELL"
        self.trades[mt5_pos.ticket] = ManagedTrade(
            account_name=account["name"], ticket=mt5_pos.ticket, symbol=mt5_pos.symbol,
            direction=direction, group_id=group_id, leg=leg,
            planned_sl=float(getattr(mt5_pos, "sl", 0.0)), entry_price=float(getattr(mt5_pos, "price_open", 0.0)),
        )
```

At the top of `trade_manager.py`, add the new import next to the existing `from .trade_utils import safe_comment`:

```python
from .trade_utils import safe_comment, parse_group_comment
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k reconcile -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest services/ -v`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat: add TradeManager.reconcile_from_mt5 for startup state recovery

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire into `app.py` + Docker infrastructure

**Files:**
- Modify: `services/trade_orchestrator/app.py`
- Modify: `docker-compose.yml`
- Modify: `.gitignore`
- Create: `data/trade_state.jsonl` (empty file, so the bind-mount source exists on the host before the first `docker compose up`)

**Interfaces:**
- Consumes: `TradeStateStore` (Task 1), `TradeManager(state_store=...)` (Task 3), `reconcile_from_mt5` (Task 4).
- Produces: nothing new for later tasks — this is the final wiring task.

**Step-by-step:**

- [ ] **Step 1: Create the `data/` directory and the empty backup file**

```bash
mkdir -p data
touch data/trade_state.jsonl
```

- [ ] **Step 2: Add `data/` to `.gitignore`**

In `.gitignore`, add this near the other operational-content entries (after the `*.session`/`*.session-journal` block):

```
# Trade state persistence (operational data, not versioned)
data/trade_state.jsonl
data/trade_state.jsonl.tmp
```

- [ ] **Step 3: Add the bind-mount to `docker-compose.yml`**

In `docker-compose.yml`, find the `trade_orchestrator` service's `volumes:` block (currently just the telegram session line) and add the new mount:

```yaml
  trade_orchestrator:
    build:
      context: .
      dockerfile: services/trade_orchestrator/Dockerfile
    container_name: atp-trade-orchestrator
    env_file:
      - .env
    depends_on:
      mt5_acct1:
        condition: service_started
      redis:
        condition: service_healthy

    restart: unless-stopped
    volumes:
      - ./services/telegram_ingestor/telegram_ingestor.session:/app/services/telegram_ingestor/telegram_ingestor.session:rw
      - ./data/trade_state.jsonl:/app/data/trade_state.jsonl:rw
    ports:
      - "8200:8200"
```

- [ ] **Step 4: Wire `TradeStateStore` and `reconcile_from_mt5` into `main()`**

In `services/trade_orchestrator/app.py`, add the import near the top (with the other `.` imports):

```python
from .trade_manager import TradeManager
from .mt5_executor import MT5Executor
from .trade_state_store import TradeStateStore
```

In `main()`, after `tradeManager = TradeManager(...)` (currently the line right before `from .mgmt_api import create_mgmt_app`), add the store construction and reconciliation call. The `TradeManager` constructor call itself needs the new `state_store` kwarg:

```python
    state_store = TradeStateStore(r, os.path.join(os.path.dirname(__file__), "..", "..", "data", "trade_state.jsonl"))
    tradeManager = TradeManager(tradeExecutor, notifier=notifier_adapter, config_provider=_config, state_store=state_store)

    reconciliation_summary = await tradeManager.reconcile_from_mt5(accounts)
    log.info("[RECONCILE] al arranque: %s", reconciliation_summary)

    from .mgmt_api import create_mgmt_app
```

(The path resolves to `<repo_root>/data/trade_state.jsonl` regardless of the working directory the container starts in — `os.path.dirname(__file__)` is `/app/services/trade_orchestrator`, so `../../data/trade_state.jsonl` is `/app/data/trade_state.jsonl`, matching the bind-mount destination from Step 3.)

- [ ] **Step 5: Run the full suite to confirm nothing broke**

Run: `pytest services/ -v`
Expected: PASS, no regressions (this task doesn't add new automated tests — it's wiring verified by the manual smoke test in Step 6)

- [ ] **Step 6: Manual smoke test — confirm the service still starts cleanly**

This can only be verified in an environment with a real (or simulated) MT5 connection and Redis — not achievable via `pytest` alone. Skip actual execution if no local MT5/Redis stack is available; the implementer should note this in their report and let the task reviewer decide whether local verification is required before merging. If a stack is available:

```bash
docker compose up -d --build trade_orchestrator
docker compose logs trade_orchestrator --tail=30
```

Expected: no traceback, and a log line matching `[RECONCILE] al arranque: {'recovered_from_redis': 0, 'recovered_from_file': 0, 'degraded': 0, 'orphaned': []}` on a clean environment with no pre-existing MT5 positions.

- [ ] **Step 7: Commit**

```bash
git add services/trade_orchestrator/app.py docker-compose.yml .gitignore data/trade_state.jsonl
git commit -m "feat: wire TradeStateStore and startup reconciliation into main()

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: End-to-end integration test

**Files:**
- Create: `services/trade_orchestrator/test_state_persistence_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4 (no new interfaces produced — this is a pure verification task).

**Step-by-step:**

- [ ] **Step 1: Write the integration test**

```python
# services/trade_orchestrator/test_state_persistence_integration.py
"""
Prueba de extremo a extremo: abre un grupo, deja que el trailing progrese,
simula la perdida completa del estado en memoria (un TradeManager nuevo,
mismo store real), reconcilia, y confirma que el trailing puede continuar
desde su peak_multiple anterior sin perder progreso — el escenario completo
que motivo este diseño.
"""
import os
import tempfile

import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.trade_state_store import TradeStateStore
from services.trade_orchestrator.test_trade_manager_dual_tp import DummyExecutor, DummyNotifier, ACCOUNT


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture
def tmp_file():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)
    yield path
    if os.path.exists(path):
        os.remove(path)
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.mark.asyncio
async def test_trailing_survives_a_full_process_restart(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)

    # Process instance #1: opens the group, TP1 hits, trailing progresses.
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm1 = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm1.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm1.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm1.trades.values() if t.group_id == group_id and t.leg == "runner")

    del sim.positions[tp1_leg.ticket]
    await tm1._tick_once_account(ACCOUNT)  # BE applied

    sim.price = 2522.0  # 60% of unit=20 past tp1=2510
    sim.positions[runner_leg.ticket]['price_current'] = sim.price
    await tm1._tick_once_account(ACCOUNT)  # trailing progresses to peak_multiple=0.6

    assert tm1.trades[runner_leg.ticket].peak_multiple == pytest.approx(0.6)

    # Simulate a full process restart: brand new TradeManager, same store, same MT5 state.
    tm2 = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    summary = await tm2.reconcile_from_mt5([ACCOUNT])

    assert summary["recovered_from_redis"] == 1
    recovered_runner = tm2.trades[runner_leg.ticket]
    assert recovered_runner.peak_multiple == pytest.approx(0.6)
    assert recovered_runner.be_applied is True
    assert tm2._next_group_id == group_id + 1

    # And trailing continues correctly from that recovered peak on the new instance.
    sim.price = 2530.0  # 100% of unit past tp1 -- must move the SL further, not reset
    sim.positions[runner_leg.ticket]['price_current'] = sim.price
    await tm2._tick_once_account(ACCOUNT)

    assert tm2.trades[runner_leg.ticket].peak_multiple == pytest.approx(1.0)
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    expected_sl = 2510.0 + (1.0 * 20.0) / 3.0
    assert abs(runner_pos.sl - expected_sl) < 1e-6
```

- [ ] **Step 2: Run the test**

Run: `pytest services/trade_orchestrator/test_state_persistence_integration.py -v`
Expected: PASS

- [ ] **Step 3: Run the entire project test suite one final time**

Run: `pytest services/ -v`
Expected: PASS, all tests green (Tasks 1-6 combined should add roughly 35 new tests to the suite)

- [ ] **Step 4: Commit**

```bash
git add services/trade_orchestrator/test_state_persistence_integration.py
git commit -m "test: add end-to-end trailing-survives-restart integration test

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```
