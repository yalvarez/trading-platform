# Ámbito de /mgmt/action por chat_id — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `/mgmt/action`'s `symbol`-based, single-most-recent-group
resolution with `chat_id`-based resolution over ALL active groups of that
chat, and close 5 silent `apply_mgmt_action` failure paths that never
reach n8n today.

**Architecture:** Add `chat_id` as a first-class field on `ManagedTrade`,
propagated from `router_parser` (already emits it) through
`handle_signal_fields` → `open_group` → persistence → `reconcile_from_mt5`.
Add a new resolution method `find_active_groups_for_chat` used only by
`/mgmt/action`. Rewrite `apply_mgmt_action` to iterate every active group
of a chat with per-group isolation (try/except) and per-group account
resolution, and to call `_notify` on every return path that is silent
today.

**Tech Stack:** Python 3, FastAPI, pytest + pytest-asyncio, the existing
`SimuladorMT5` test double (`tests/test_simulador_mt5.py`).

**Spec:** `docs/superpowers/specs/2026-09-08-mgmt-action-chat-id-scoping-design.md`

## Global Constraints

- `MgmtActionRequest.symbol: str` is removed; replaced with
  `chat_id: str`. No backward-compat shim for old request shape (n8n side
  is being updated in lockstep by the user, out of this repo).
- `ManagedTrade.chat_id: Optional[str] = None` — default `None` so every
  existing `open_group(...)` call site (tests included) keeps compiling
  unchanged unless a task explicitly updates it.
- `open_group(..., chat_id: Optional[str] = None)` — same default-`None`
  rule.
- **Plan deviation from spec, ruled here:** the spec's §2 says
  `find_active_group_for_symbol` is "replaced" by
  `find_active_groups_for_chat`. In the actual code,
  `find_active_group_for_symbol` has a second caller the spec's own §1
  never mentions touching: `handle_signal_fields` (`services/trade_orchestrator/app.py:39`)
  uses it to decide fast-signal-duplicate-vs-reopen (via
  `group_age_seconds`) and full-signal update-vs-open — logic that stays
  scoped to "this symbol" (the system is still single-symbol XAUUSD, per
  spec §2 "fuera de alcance") and has nothing to do with `chat_id` or
  `/mgmt/action`. Rewiring it to `chat_id` would silently change signal
  routing (two different Telegram channels sending signals for the same
  symbol would then interfere with each other's fast/full lifecycle,
  which is a behavior change wildly out of this spec's scope). Ruling:
  **`find_active_group_for_symbol` is kept as-is, unchanged, still used
  by `handle_signal_fields`.** `find_active_groups_for_chat` is added
  alongside it as a new method used only by `apply_mgmt_action`. This
  satisfies the spec's actual intent (symbol is removed from
  `/mgmt/action`'s contract and resolution) without an undocumented
  behavior change to signal ingestion. Cost if this ruling is wrong: a
  method rename spec literally asked for wasn't done — trivially
  correctable later, no data or behavior is at risk either way.
- Every one of the 16 existing `_notify(...)` calls in `trade_manager.py`
  already passes `message=`; no task in this plan touches those calls.
- Response JSON shapes for `close_now` / `move_sl_be_now` change from a
  single `{"status": ..., "group_id": ...}` to
  `{"status": "completed", "results": [...]}` — this is a breaking change
  to `/mgmt/action`'s response shape, in scope per spec §5.
- All new/changed code follows the file's existing conventions: Spanish
  log messages and docstrings, English identifiers, `log.info`/`log.error`
  as currently used, `_notify(event, **kwargs, message=...)` pattern.

---

## File Structure

Two files change; no new files are created:

- `services/trade_orchestrator/trade_manager.py` — `ManagedTrade`,
  `open_group`, `_group_doc`, `reconcile_from_mt5`'s two reconstruction
  helpers, new `find_active_groups_for_chat`, rewritten
  `apply_mgmt_action`.
- `services/trade_orchestrator/mgmt_api.py` — `MgmtActionRequest`, module
  docstring, the `/mgmt/action` handler's call to
  `apply_mgmt_action`.
- `services/trade_orchestrator/app.py` — `handle_signal_fields` reads
  `fields.get("chat_id")` and passes it to `open_group`.
- `services/trade_orchestrator/test_trade_manager_dual_tp.py` — updated/
  new tests for all of the above.
- `services/trade_orchestrator/test_mgmt_action_endpoint.py` — updated/
  new tests for the new request contract.

## Task Sequence

1. `ManagedTrade.chat_id` + `open_group(chat_id=...)` + `handle_signal_fields` propagation
2. Persistence: `_group_doc` + `reconcile_from_mt5` inherit/orphan `chat_id`
3. `find_active_groups_for_chat` (new method, additive)
4. Rewrite `apply_mgmt_action`: per-group iteration, isolation, per-group account resolution, all 5 new notifications
5. `mgmt_api.py` contract: `chat_id` replaces `symbol`
6. Full-suite verification

---

### Task 1: `ManagedTrade.chat_id` + `open_group` propagation

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py:19-33` (`ManagedTrade`), `:231-347` (`open_group`)
- Modify: `services/trade_orchestrator/app.py:20-112` (`handle_signal_fields`)
- Test: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Produces: `ManagedTrade.chat_id: Optional[str] = None` (new field, last
  in the dataclass, after `opened_ts`, keeping it keyword-friendly for
  every existing positional-then-kwargs call site in this file).
  `TradeManager.open_group(..., chat_id: Optional[str] = None) -> Optional[int]`
  (new keyword-only-by-convention param — existing callers pass no
  `chat_id` and keep working via the default).
- Consumes: nothing new from other tasks.

- [ ] **Step 1: Write the failing test for `ManagedTrade.chat_id` default and propagation**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`, right
after `test_open_group_opens_two_positions_with_shared_group_id` (after
line 52):

```python
@pytest.mark.asyncio
async def test_open_group_defaults_chat_id_to_none_when_not_passed():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert t.chat_id is None


@pytest.mark.asyncio
async def test_open_group_propagates_chat_id_to_both_legs():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
        chat_id="-1001234567890",
    )

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert t.chat_id == "-1001234567890"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k chat_id -v`
Expected: FAIL — `TypeError: open_group() got an unexpected keyword argument 'chat_id'`
(for the second test) and `AttributeError: 'ManagedTrade' object has no attribute 'chat_id'` (for the first, once the signature issue is worked around — both currently fail because `chat_id` doesn't exist anywhere yet).

- [ ] **Step 3: Add `chat_id` to `ManagedTrade`**

In `services/trade_orchestrator/trade_manager.py`, change the dataclass
(lines 19-33):

```python
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
    chat_id: Optional[str] = None
```

- [ ] **Step 4: Add `chat_id` parameter to `open_group` and assign it to both legs**

In `services/trade_orchestrator/trade_manager.py`, change the `open_group`
signature (line 231):

```python
    async def open_group(self, account: dict, *, symbol: str, direction: str, sl: float, tp1: Optional[float], tp2: Optional[float], entry_range: Optional[tuple] = None, chat_id: Optional[str] = None) -> Optional[int]:
```

Update its docstring (right after the signature, currently lines 232-245)
to add one line documenting the new parameter — insert after the existing
`entry_range` bullet:

```python
        - chat_id, si viene, se guarda en ambas piernas del grupo (dual-TP
          spec + chat_id-scoping spec seccion 3) — identifica el canal de
          Telegram que origino la senal, usado por /mgmt/action para
          resolver a que grupos aplicar una accion de gestion. None si la
          senal no trae chat_id (legacy) o si open_group se llama sin el
          (p. ej. en tests existentes) -- un grupo con chat_id=None queda
          huerfano de gestion automatica via /mgmt/action.
```

Then, in the leg-construction loop (lines 323-335), add `chat_id=chat_id`
to the `ManagedTrade(...)` call:

```python
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
                chat_id=chat_id,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k chat_id -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Propagate `chat_id` from `handle_signal_fields` to `open_group`**

In `services/trade_orchestrator/app.py`, `handle_signal_fields` (starting
line 20): read `chat_id` right after `symbol`/`direction` (line 26-27):

```python
    symbol = fields.get("symbol")
    direction = fields.get("direction")
    chat_id = fields.get("chat_id")
```

Then pass `chat_id=chat_id` to both `open_group` call sites — the fast
path (line 98, inside the `if is_fast:` block):

```python
        await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=default_tp1, tp2=default_tp2, chat_id=chat_id)
        return
```

and the full-signal path (line 112, the final line of the function):

```python
    await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=tp1, tp2=tp2, entry_range=entry_range, chat_id=chat_id)
```

Do NOT change the `update_group_signal` call at line 106 — that path
updates an existing group in place and never touches `chat_id` (a
reopened/updated group keeps the `chat_id` it was opened with).

There is no existing unit test file for `handle_signal_fields` itself
(it's covered today only by the e2e suite against a live stream) — no
test changes are required for this step; `tests/e2e/scenarios/b*.py`
already sends real Telegram messages through the real pipeline and will
exercise this propagation once deployed (see plan Task 6 / post-merge
notes).

- [ ] **Step 7: Run the full trade_orchestrator test file**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS, all tests (no regressions from the dataclass/signature change)

- [ ] **Step 8: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/app.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat: add chat_id to ManagedTrade, open_group, and signal ingestion"
```

---

### Task 2: Persistence — `_group_doc` and `reconcile_from_mt5` carry `chat_id`

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py:64-91` (`_group_doc`), `:869-889` (`_reconstruct_leg_from_doc`, `_reconstruct_leg_minimal`)
- Test: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `ManagedTrade.chat_id` (Task 1).
- Produces: `_group_doc(group_id)` now includes `"chat_id"` in its
  returned dict. `_reconstruct_leg_from_doc` sets `chat_id` from
  `doc.get("chat_id")` (may be absent on old docs). `_reconstruct_leg_minimal`
  always leaves `chat_id=None` (degraded mode — no doc, no source of
  truth for it).

- [ ] **Step 1: Write the failing test for `_group_doc` including `chat_id`**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`. This
file already has a `RecordingStore` test double used by the persistence
tests around lines 780-833 — find it (it's defined earlier in the file,
used by `test_both_legs_closing_closes_the_group_in_the_store` etc.) and
add this test right after `test_no_state_store_is_a_safe_default` (after
line 831, before the `# --- Task 4: reconcile_from_mt5 ---` section
comment on line 834):

```python
@pytest.mark.asyncio
async def test_group_doc_includes_chat_id():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
        chat_id="-1001234567890",
    )

    doc = tm._group_doc(group_id)
    assert doc["chat_id"] == "-1001234567890"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k test_group_doc_includes_chat_id -v`
Expected: FAIL with `KeyError: 'chat_id'`

- [ ] **Step 3: Add `chat_id` to `_group_doc`**

In `services/trade_orchestrator/trade_manager.py`, change `_group_doc`
(lines 64-91) — add the key right after `"direction"`:

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
            "chat_id": first.chat_id,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k test_group_doc_includes_chat_id -v`
Expected: PASS

- [ ] **Step 5: Write the failing tests for `reconcile_from_mt5` chat_id inheritance and degraded-mode orphaning**

Add these two tests right after `test_reconcile_recovers_full_state_from_store`
(after line 873, in the `# --- Task 4: reconcile_from_mt5 startup recovery ---`
section — the file already has `_open_raw_position` defined at line 836,
reuse it):

```python
@pytest.mark.asyncio
async def test_reconcile_inherits_chat_id_from_store_doc():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "chat_id": "-1001234567890",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": tp1_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": True, "peak_multiple": 0.35},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    assert tm.trades[tp1_ticket].chat_id == "-1001234567890"
    assert tm.trades[runner_ticket].chat_id == "-1001234567890"


@pytest.mark.asyncio
async def test_reconcile_leaves_chat_id_none_when_store_doc_predates_the_field():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        # Legacy doc persisted before chat_id existed -- no "chat_id" key at all.
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": tp1_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": True, "peak_multiple": 0.35},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    assert tm.trades[tp1_ticket].chat_id is None
    assert tm.trades[runner_ticket].chat_id is None


@pytest.mark.asyncio
async def test_reconcile_degraded_mode_leaves_chat_id_none():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # empty docs -- every group_id misses, forcing degraded mode

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP7-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP7-runner")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["degraded"] == 1
    assert tm.trades[tp1_ticket].chat_id is None
    assert tm.trades[runner_ticket].chat_id is None
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "inherits_chat_id or predates_the_field or degraded_mode_leaves_chat_id" -v`
Expected: FAIL — `AssertionError` (chat_id inheritance test expects
`"-1001234567890"` but gets `None`; the other two currently pass
trivially since `chat_id` doesn't exist as an attribute check target yet
— actually these will raise `AttributeError: 'ManagedTrade' object has no
attribute 'chat_id'` until Task 1 lands; assuming Task 1 already landed,
the inherits-chat_id test is the one that genuinely fails here).

- [ ] **Step 7: Update `_reconstruct_leg_from_doc` and `_reconstruct_leg_minimal`**

In `services/trade_orchestrator/trade_manager.py`, change
`_reconstruct_leg_from_doc` (lines 869-881):

```python
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
            peak_multiple=leg_doc.get("peak_multiple", 0.0), chat_id=doc.get("chat_id"),
        )
```

(`doc.get("chat_id")` resolves to `None` for any doc persisted before this
field existed — exactly the orphan behavior spec §4/§7 requires.)

`_reconstruct_leg_minimal` (lines 883-889) needs NO code change — it
never sets `chat_id` explicitly, so the dataclass default (`None`) already
applies. Confirm this by inspection; do not add a `chat_id=None` there
(redundant with the default, and every other field in that constructor
call is only ever a field the MT5 position itself can supply — adding
`chat_id=None` would misleadingly suggest it comes from `mt5_pos`).

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS, all tests (including the 3 new ones and no regressions in
the rest of the reconcile suite)

- [ ] **Step 9: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat: persist and reconcile chat_id across restarts"
```

---

### Task 3: `find_active_groups_for_chat` (new, additive)

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (add new method near `find_active_group_for_symbol`, line ~433-446)
- Test: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `ManagedTrade.chat_id`, `ManagedTrade.opened_ts`, `ManagedTrade.group_id` (Task 1).
- Produces: `TradeManager.find_active_groups_for_chat(chat_id: str) -> list[int]`
  — every `group_id` with at least one active leg whose `chat_id` equals
  the argument exactly, oldest-to-newest, deduplicated (a group has 2
  legs but must appear once). `find_active_group_for_symbol` is
  UNCHANGED (see Global Constraints ruling) — Task 4 will call the new
  method, `handle_signal_fields` keeps calling the old one.

- [ ] **Step 1: Write the failing tests**

Add right after `test_find_active_group_for_symbol_tie_breaks_on_group_id_when_opened_ts_equal`
(after line 474, before the `# --- Review fix 3 ---` comment):

```python
# --- chat_id-scoping: find_active_groups_for_chat ---

@pytest.mark.asyncio
async def test_find_active_groups_for_chat_returns_all_groups_oldest_first():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatB")

    found = tm.find_active_groups_for_chat("chatA")

    assert found == [g1, g2]


@pytest.mark.asyncio
async def test_find_active_groups_for_chat_returns_empty_list_for_unknown_chat():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")

    found = tm.find_active_groups_for_chat("chatZ")

    assert found == []


@pytest.mark.asyncio
async def test_find_active_groups_for_chat_never_returns_orphaned_none_chat_id_groups():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    # Opened without chat_id (legacy / test default) -- an orphan.
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    found_for_none = tm.find_active_groups_for_chat(None)
    found_for_real_chat = tm.find_active_groups_for_chat("chatA")

    assert found_for_none == []  # querying with None must not match orphans either
    assert found_for_real_chat == []


@pytest.mark.asyncio
async def test_find_active_groups_for_chat_deduplicates_group_ids_across_both_legs():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")

    found = tm.find_active_groups_for_chat("chatA")

    assert found == [g1]  # not [g1, g1] -- one entry per group, not per leg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k find_active_groups_for_chat -v`
Expected: FAIL with `AttributeError: 'TradeManager' object has no
attribute 'find_active_groups_for_chat'`

- [ ] **Step 3: Implement `find_active_groups_for_chat`**

In `services/trade_orchestrator/trade_manager.py`, add this method
immediately after `find_active_group_for_symbol` (after line 446, before
`group_age_seconds`):

```python
    def find_active_groups_for_chat(self, chat_id: str) -> list[int]:
        """
        Todos los group_id con al menos una pierna activa cuyo chat_id
        coincide exactamente con `chat_id` (chat_id-scoping spec seccion 5).
        Un grupo con chat_id=None (huerfano -- legacy o reconciliado en
        modo degradado) NUNCA aparece aqui, sin importar que chat_id se
        consulte (incluido chat_id=None): no hay gestion automatica para
        un grupo cuyo canal de origen no se conoce con certeza. Usado
        exclusivamente por apply_mgmt_action -- handle_signal_fields sigue
        usando find_active_group_for_symbol para su propia logica de
        fast/full por simbolo, que no tiene relacion con /mgmt/action.
        Ordenado de mas antiguo a mas reciente (por opened_ts, luego
        group_id como desempate -- mismo criterio que
        find_active_group_for_symbol ya usa).
        """
        if chat_id is None:
            return []
        candidates = [t for t in self.trades.values() if t.chat_id == chat_id]
        group_ids = sorted(
            {t.group_id for t in candidates},
            key=lambda gid: min(
                (t.opened_ts, t.group_id) for t in candidates if t.group_id == gid
            ),
        )
        return group_ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k find_active_groups_for_chat -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full trade_orchestrator test file**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat: add find_active_groups_for_chat for chat_id-scoped mgmt actions"
```

---

### Task 4: Rewrite `apply_mgmt_action` — per-group iteration, isolation, and the 5 new notifications

This is the largest task. It replaces the entire body of the
`apply_mgmt_action` method and every existing test that calls it with
`symbol=`. Line numbers below (originally 623-710) are from the file as
read at plan-writing time and may have shifted slightly after Tasks 1-3's
insertions earlier in the file — locate the method by its
`async def apply_mgmt_action(` signature if the given lines don't match.

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (the `apply_mgmt_action` method, originally at lines 623-710)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py` (replace the 13 existing `apply_mgmt_action`-related tests at lines 288-393, 456-491 `test_move_sl_be_now_with_missing_entry_price_returns_failed_not_raise`, 809-819 `test_mgmt_close_now_closes_the_group_in_the_store`, and 1053-1069 `test_mgmt_close_now_message_includes_entry_and_close_price_per_leg`, with new `chat_id=`-based equivalents)

**Interfaces:**
- Consumes: `find_active_groups_for_chat` (Task 3), `ManagedTrade.chat_id`
  (Task 1).
- Produces: `apply_mgmt_action(*, action: str, chat_id: str, raw_text: str, correction: Optional[dict]) -> dict`
  — signature changes `symbol: str` → `chat_id: str`. Response shapes per
  spec §5 and §5.1:
  - No active group for `chat_id`: `{"status": "no_active_trade"}`, plus
    `_notify(event="mgmt_no_active_trade", ...)`.
  - `close_now`: `{"status": "completed", "results": [{"group_id": N, "status": "closed" | "failed", **maybe "reason": "exception"}]}`
  - `move_sl_be_now`: `{"status": "completed", "results": [{"group_id": N, "status": "applied" | "already_satisfied" | "failed" | "no_active_trade", **maybe "reason": "exception"}]}`
  - `note_sl_hit`: `{"status": "noted", "group_ids": [...]}`
  - `signal_correction`: `{"status": "applied", "group_id": N}` (most
    recent group only) or `{"status": "invalid_correction"}`.
  - `ignore`: `{"status": "ignored"}`
  - Unknown action: `{"status": "unknown_action"}`

- [ ] **Step 1: Write the new failing tests (replacing all `symbol=`-based `apply_mgmt_action` tests)**

First, delete these 11 existing test functions entirely from
`services/trade_orchestrator/test_trade_manager_dual_tp.py` — find each
by its `def` name (line numbers have shifted from Tasks 1-3's insertions,
so search by name, not position) and remove the whole
`@pytest.mark.asyncio` + `async def ...` block for each:
`test_apply_mgmt_action_close_now_closes_both_legs_before_tp1`,
`test_apply_mgmt_action_no_active_trade_returns_no_active_trade`,
`test_apply_mgmt_action_move_sl_be_now_forces_be_when_worse`,
`test_apply_mgmt_action_move_sl_be_now_noop_when_already_better`,
`test_apply_mgmt_action_note_sl_hit_does_not_touch_mt5`,
`test_apply_mgmt_action_signal_correction_updates_tp1_on_mt5_and_tp2_reference_only`,
`test_apply_mgmt_action_ignore_is_a_noop`,
`test_signal_correction_after_trailing_does_not_regress_sl_and_trailing_still_progresses`
(this one is replaced by an equivalent test of the same name later in
this same step's new block — deleting the old one first avoids a
duplicate function name),
`test_move_sl_be_now_with_missing_entry_price_returns_failed_not_raise`,
`test_mgmt_close_now_closes_the_group_in_the_store`,
`test_mgmt_close_now_message_includes_entry_and_close_price_per_leg`.

Every one of these calls `tm.apply_mgmt_action(..., symbol=...)` — that's
the reliable way to find each one's full extent (from its `@pytest.mark.asyncio`
line down to the blank line before the next `@pytest.mark.asyncio` or
section comment).

Insert the replacement block below in the same place the first deleted
group used to live: right after
`test_update_group_signal_applies_real_sl_even_when_narrower_than_fast_default`,
before `test_find_active_group_for_symbol_returns_most_recent`.

```python
# --- apply_mgmt_action: chat_id-scoped resolution (chat_id-scoping spec section 5) ---

CHAT_ID = "-1001234567890"


@pytest.mark.asyncio
async def test_apply_mgmt_action_no_active_trade_for_chat_returns_no_active_trade_and_notifies():
    sim = SimuladorMT5()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="Close now", correction=None)

    assert result == {"status": "no_active_trade"}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_no_active_trade"]
    assert len(events) == 1
    assert "Close now" in events[0]["message"]
    assert events[0]["chat_id"] == CHAT_ID
    assert events[0]["action"] == "close_now"


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_closes_single_group_before_tp1():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="Close now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "closed"}]}
    remaining = [t for t in tm.trades.values() if t.group_id == group_id]
    assert remaining == []


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_closes_all_groups_of_the_same_chat():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    other_chat_group = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="other-chat")

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close both", correction=None)

    assert result == {"status": "completed", "results": [
        {"group_id": g1, "status": "closed"},
        {"group_id": g2, "status": "closed"},
    ]}
    # The other chat's group must be untouched.
    remaining_other = [t for t in tm.trades.values() if t.group_id == other_chat_group]
    assert len(remaining_other) == 2


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_isolates_a_real_exception_in_one_group():
    """
    A real exception (not just a failed retcode) while processing one
    group must not abort the whole request -- the other group of the same
    chat_id must still be processed and reported (chat_id-scoping spec
    section 5, per-group isolation).
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    g1_ticket = next(t.ticket for t in tm.trades.values() if t.group_id == g1)
    real_partial_close = sim.partial_close

    def _boom(account, ticket, pct):
        if ticket == g1_ticket:
            raise RuntimeError("simulated RPyC network failure")
        return real_partial_close(account, ticket, pct)

    sim.partial_close = _boom

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close both", correction=None)

    results_by_group = {r["group_id"]: r for r in result["results"]}
    assert results_by_group[g1] == {"group_id": g1, "status": "failed", "reason": "exception"}
    assert results_by_group[g2] == {"group_id": g2, "status": "closed"}
    # g2 must have actually been closed in spite of g1's exception.
    remaining_g2 = [t for t in tm.trades.values() if t.group_id == g2]
    assert remaining_g2 == []


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_applies_to_all_groups_with_mixed_outcomes():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    # Force g2's runner to already be at/above BE so it reports already_satisfied.
    g2_tp1 = next(t for t in tm.trades.values() if t.group_id == g2 and t.leg == "tp1")
    g2_runner = next(t for t in tm.trades.values() if t.group_id == g2 and t.leg == "runner")
    del sim.positions[g2_tp1.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied to g2's runner at 2500

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    results_by_group = {r["group_id"]: r for r in result["results"]}
    assert results_by_group[g1]["status"] == "applied"
    assert results_by_group[g2]["status"] == "already_satisfied"
    g1_runner = next(t for t in tm.trades.values() if t.group_id == g1 and t.leg == "runner")
    assert tm.trades[g1_runner.ticket].be_applied is True


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_reports_no_active_trade_for_a_group_without_runner_and_notifies():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del tm.trades[runner_leg.ticket]  # simulate the runner leg missing entirely

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "no_active_trade"}]}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_no_runner_leg"]
    assert len(events) == 1
    assert events[0]["group_id"] == group_id
    assert events[0]["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_with_missing_entry_price_reports_failed_not_raise():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    runner_leg.entry_price = None

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "failed", "reason": "no_entry_price"}]}


@pytest.mark.asyncio
async def test_apply_mgmt_action_note_sl_hit_notifies_once_per_group_without_touching_mt5():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    legs_before = {t.ticket: sim.positions_get(ticket=t.ticket)[0].sl for t in tm.trades.values()}

    result = await tm.apply_mgmt_action(action="note_sl_hit", chat_id=CHAT_ID, raw_text="HIT SL", correction=None)

    assert result == {"status": "noted", "group_ids": [g1, g2]}
    for ticket, sl_before in legs_before.items():
        assert sim.positions_get(ticket=ticket)[0].sl == sl_before


@pytest.mark.asyncio
async def test_apply_mgmt_action_signal_correction_applies_only_to_the_most_recent_group():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="TP2 IS 4687",
        correction={"field": "tp2", "value": 4687.0},
    )

    assert result == {"status": "applied", "group_id": g2}
    g1_runner = next(t for t in tm.trades.values() if t.group_id == g1 and t.leg == "runner")
    g2_runner = next(t for t in tm.trades.values() if t.group_id == g2 and t.leg == "runner")
    assert g1_runner.tp2_price == 2530.0  # untouched
    assert g2_runner.tp2_price == 4687.0


@pytest.mark.asyncio
async def test_apply_mgmt_action_signal_correction_with_invalid_field_notifies_and_returns_invalid_correction():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="volume is 2 lots",
        correction={"field": "volume", "value": 2.0},
    )

    assert result == {"status": "invalid_correction"}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_invalid_correction"]
    assert len(events) == 1
    assert "volume" in events[0]["message"]
    assert "volume is 2 lots" in events[0]["message"]


@pytest.mark.asyncio
async def test_apply_mgmt_action_ignore_is_a_noop_and_does_not_notify():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="ignore", chat_id=CHAT_ID, raw_text="spam your feedbacks", correction=None)

    assert result == {"status": "ignored"}


@pytest.mark.asyncio
async def test_apply_mgmt_action_unknown_action_notifies_and_returns_unknown_action():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="frobnicate", chat_id=CHAT_ID, raw_text="do the thing", correction=None)

    assert result == {"status": "unknown_action"}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_unknown_action"]
    assert len(events) == 1
    assert "frobnicate" in events[0]["message"]
    assert "do the thing" in events[0]["message"]


@pytest.mark.asyncio
async def test_apply_mgmt_action_account_unresolved_notifies_per_group_and_reports_failed():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    for t in tm.trades.values():
        if t.group_id == group_id:
            t.account_name = "nonexistent-account"

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "failed", "reason": "account_unresolved"}]}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_account_unresolved"]
    assert len(events) == 1
    assert events[0]["group_id"] == group_id
    assert events[0]["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_signal_correction_after_trailing_does_not_regress_sl_and_trailing_still_progresses():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500

    # Trail forward: price at 150% of unit past tp1 = 2510 + 30 = 2540 -> SL = 2510 + 10 = 2520
    sim.positions[runner_leg.ticket]["price_current"] = 2540.0
    sim.price = 2540.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_trailing = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert abs(sl_after_trailing - 2520.0) < 1e-6

    # A signal_correction that only touches tp2 must NOT regress the live SL
    # back down to the original planned_sl (2490).
    result = await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="TP2 correction",
        correction={"field": "tp2", "value": 4687.0},
    )
    assert result == {"status": "applied", "group_id": group_id}
    sl_after_correction = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after_correction == sl_after_trailing  # unchanged, never regressed
    assert sl_after_correction >= 2500.0  # still at/above BE, not stranded below entry

    # Trailing must still be able to progress afterward with a further price move.
    sim.positions[runner_leg.ticket]["price_current"] = 2600.0
    sim.price = 2600.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_further_move = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after_further_move >= sl_after_correction  # trailing not dead/frozen


@pytest.mark.asyncio
async def test_mgmt_close_now_closes_the_group_in_the_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close it", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "closed"}]}
    assert group_id in store.closed


@pytest.mark.asyncio
async def test_mgmt_close_now_message_includes_entry_and_close_price_per_leg():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="Close now", correction=None)

    assert result["status"] == "completed"
    close_events = [kwargs for event, kwargs in notifier.events if event == "mgmt_close_now"]
    assert len(close_events) == 1
    message = close_events[0]["message"]
    assert "tp1" in message
    assert "runner" in message
    assert "2500.0" in message or "2500.00000" in message  # entry price for both legs
```

Note: `tm.notifier.events` above relies on `DummyNotifier` (already
defined at the top of this file) storing `(event, kwargs)` tuples in
`self.events` — confirm this matches the existing `DummyNotifier` class
(it does, see lines 22-27 of the current file); no change needed there.

`test_apply_mgmt_action_close_now_isolates_a_real_exception_in_one_group`
monkeypatches `sim.partial_close` directly on the `SimuladorMT5` instance
— this works because `DummyExecutor._client_for` always returns the same
`sim` object, and `TradeManager._call` invokes `client.partial_close` by
attribute lookup at call time, so replacing the bound method on the
instance takes effect immediately without touching the class.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "apply_mgmt_action or mgmt_close_now or signal_correction_after_trailing" -v`
Expected: FAIL — `TypeError: apply_mgmt_action() got an unexpected
keyword argument 'chat_id'` (current signature still takes `symbol`).

- [ ] **Step 3: Rewrite `apply_mgmt_action`**

In `services/trade_orchestrator/trade_manager.py`, replace the entire
method body (lines 623-710) with:

```python
    async def apply_mgmt_action(self, *, action: str, chat_id: str, raw_text: str, correction: Optional[dict]) -> dict:
        """
        Ejecuta una decision de /mgmt/action (chat_id-scoping spec seccion 5).
        Resuelve TODOS los grupos activos del `chat_id` que mando el mensaje
        de gestion -- no un simbolo, y no solo el grupo mas reciente -- y
        aplica la accion segun su propia semantica (ver cada rama abajo).
        """
        group_ids = self.find_active_groups_for_chat(chat_id)
        if not group_ids:
            log.info("[TM][MGMT] no_active_trade chat_id=%s action=%s text=%r", chat_id, action, raw_text[:80])
            await self._notify(
                "mgmt_no_active_trade",
                message=f"Acción '{action}' recibida pero no hay trades activos para este chat. Texto: {raw_text!r}",
                chat_id=chat_id,
                action=action,
            )
            return {"status": "no_active_trade"}

        if action == "close_now":
            results = []
            for group_id in group_ids:
                try:
                    legs = [t for t in self.trades.values() if t.group_id == group_id]
                    account = self._ensure_account_dict(legs[0].account_name)
                    if not account:
                        log.error("[TM][MGMT] no se pudo resolver la cuenta para group_id=%s chat_id=%s", group_id, chat_id)
                        await self._notify(
                            "mgmt_account_unresolved",
                            message=f"No se pudo resolver la cuenta del grupo {group_id} al aplicar '{action}'.",
                            chat_id=chat_id, group_id=group_id, action=action,
                        )
                        results.append({"group_id": group_id, "status": "failed", "reason": "account_unresolved"})
                        continue
                    client = self.mt5._client_for(account)
                    leg_summaries = []
                    for t in list(legs):
                        await self._call(client.partial_close, account, t.ticket, 100)
                        close_price = await self._get_close_price(client, t.ticket)
                        leg_summaries.append(
                            f"{t.leg} (ticket={t.ticket}, apertura {self._fmt_price(t.entry_price)}, "
                            f"cierre {self._fmt_price(close_price)})"
                        )
                        self.trades.pop(t.ticket, None)
                    await self._notify(
                        "mgmt_close_now", group_id=group_id, chat_id=chat_id, raw_text=raw_text,
                        message=f"Grupo {group_id} cerrado manualmente via /mgmt/action: "
                                f"{', '.join(leg_summaries)}. Texto original: {raw_text!r}",
                    )
                    await self._close_group_in_store(group_id)
                    results.append({"group_id": group_id, "status": "closed"})
                except Exception as e:
                    log.error("[TM][MGMT] excepcion cerrando group_id=%s chat_id=%s: %s", group_id, chat_id, e)
                    results.append({"group_id": group_id, "status": "failed", "reason": "exception"})
            return {"status": "completed", "results": results}

        if action == "move_sl_be_now":
            results = []
            for group_id in group_ids:
                try:
                    legs = [t for t in self.trades.values() if t.group_id == group_id]
                    account = self._ensure_account_dict(legs[0].account_name)
                    if not account:
                        log.error("[TM][MGMT] no se pudo resolver la cuenta para group_id=%s chat_id=%s", group_id, chat_id)
                        await self._notify(
                            "mgmt_account_unresolved",
                            message=f"No se pudo resolver la cuenta del grupo {group_id} al aplicar '{action}'.",
                            chat_id=chat_id, group_id=group_id, action=action,
                        )
                        results.append({"group_id": group_id, "status": "failed", "reason": "account_unresolved"})
                        continue
                    client = self.mt5._client_for(account)
                    runner = next((t for t in legs if t.leg == "runner"), None)
                    if not runner:
                        await self._notify(
                            "mgmt_no_runner_leg",
                            message=f"Grupo {group_id} no tiene runner leg activo; no se pudo mover SL a BE.",
                            chat_id=chat_id, group_id=group_id,
                        )
                        results.append({"group_id": group_id, "status": "no_active_trade"})
                        continue
                    if runner.entry_price is None:
                        log.error("[TM][MGMT] move_sl_be_now: runner=%s no tiene entry_price registrado (group_id=%s)",
                                  runner.ticket, group_id)
                        results.append({"group_id": group_id, "status": "failed", "reason": "no_entry_price"})
                        continue
                    pos_list = await self._call(client.positions_get, ticket=runner.ticket)
                    current_sl = float(pos_list[0].sl) if pos_list else None
                    be_price = runner.entry_price
                    is_buy = runner.direction == "BUY"
                    worse_than_be = current_sl is None or (current_sl < be_price if is_buy else current_sl > be_price)
                    if not worse_than_be:
                        await self._notify(
                            "mgmt_move_sl_be_already_satisfied", group_id=group_id, chat_id=chat_id,
                            message=f"Grupo {group_id}: SL ya estaba en breakeven o mejor, no se aplico ningun cambio.",
                        )
                        results.append({"group_id": group_id, "status": "already_satisfied"})
                        continue
                    ok = await self._force_runner_sl(account, client, runner, be_price, reason="mgmt-fallback-BE")
                    if ok:
                        runner.be_applied = True
                        await self._notify(
                            "mgmt_move_sl_be_applied", group_id=group_id, chat_id=chat_id, raw_text=raw_text,
                            message=f"Grupo {group_id}: SL movido a breakeven manualmente via /mgmt/action. "
                                    f"Texto original: {raw_text!r}",
                        )
                        await self._persist_group(group_id)
                        results.append({"group_id": group_id, "status": "applied"})
                    else:
                        results.append({"group_id": group_id, "status": "failed"})
                except Exception as e:
                    log.error("[TM][MGMT] excepcion aplicando BE a group_id=%s chat_id=%s: %s", group_id, chat_id, e)
                    results.append({"group_id": group_id, "status": "failed", "reason": "exception"})
            return {"status": "completed", "results": results}

        if action == "note_sl_hit":
            await self._notify(
                "mgmt_note_sl_hit", group_ids=group_ids, chat_id=chat_id, raw_text=raw_text,
                message=f"SL hit reportado via /mgmt/action para los grupos {group_ids} (solo nota, sin accion en MT5). "
                        f"Texto original: {raw_text!r}",
            )
            return {"status": "noted", "group_ids": group_ids}

        if action == "signal_correction":
            if not correction or correction.get("field") not in ("sl", "tp1", "tp2"):
                await self._notify(
                    "mgmt_invalid_correction",
                    message=f"Corrección con campo inválido ('{correction.get('field') if correction else None}') para el chat. Texto: {raw_text!r}",
                    chat_id=chat_id, correction=correction,
                )
                return {"status": "invalid_correction"}
            group_id = group_ids[-1]  # solo el grupo mas reciente
            field = correction["field"]
            value = float(correction["value"])
            kwargs = {"sl": None, "tp1": None, "tp2": None}
            kwargs[field] = value
            await self.update_group_signal(group_id, **kwargs)
            return {"status": "applied", "group_id": group_id}

        if action == "ignore":
            return {"status": "ignored"}

        await self._notify(
            "mgmt_unknown_action",
            message=f"Acción desconocida '{action}' recibida. Texto: {raw_text!r}",
            chat_id=chat_id, action=action,
        )
        return {"status": "unknown_action"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS, all tests (the whole file — this rewrite must not break
any other test in it, including the reconcile/persistence/notification
tests from Tasks 1-3).

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat: rewrite apply_mgmt_action for chat_id-scoped multi-group resolution

Replaces the single-most-recent-group-by-symbol resolution with
find_active_groups_for_chat, applying close_now/move_sl_be_now to every
active group of the chat with per-group isolation (a real exception in
one group no longer aborts the others) and per-group account
resolution. signal_correction still targets only the most recent group.

Also closes 5 previously-silent apply_mgmt_action return paths
(no_active_trade, account_unresolved, no_runner_leg, invalid_correction,
unknown_action) that never reached n8n before -- each now calls
_notify with a descriptive message, using raw_text where available."
```

---

### Task 5: `mgmt_api.py` contract — `chat_id` replaces `symbol`

**Files:**
- Modify: `services/trade_orchestrator/mgmt_api.py` (module docstring lines 1-9, `MgmtActionRequest` lines 29-33, handler lines 52-62)
- Modify: `services/trade_orchestrator/test_mgmt_action_endpoint.py`

**Interfaces:**
- Consumes: `apply_mgmt_action(*, action, chat_id, raw_text, correction)` (Task 4).
- Produces: `MgmtActionRequest(action: str, chat_id: str, raw_text: str, correction: Optional[Correction] = None)`.

- [ ] **Step 1: Write the failing tests**

Replace the entire content of
`services/trade_orchestrator/test_mgmt_action_endpoint.py` with:

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
CHAT_ID = "-1001234567890"


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
    resp = client.post("/mgmt/action", json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})
    assert resp.status_code == 401


def test_mgmt_action_no_active_trade_returns_200(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_active_trade"


def test_mgmt_action_rejects_request_missing_chat_id(tm_and_client):
    """The old `symbol` field is no longer accepted in place of chat_id -- a
    request without chat_id must fail Pydantic validation (422), not be
    silently treated as chat_id=None."""
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_mgmt_action_close_now_closes_group(tm_and_client):
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["results"][0]["status"] == "closed"
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_mgmt_action_close_now_only_affects_the_matching_chat_id(tm_and_client):
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="other-chat")

    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    remaining_chats = {t.chat_id for t in tm.trades.values()}
    assert remaining_chats == {"other-chat"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -v`
Expected: FAIL — the existing `MgmtActionRequest` still requires `symbol`
and doesn't accept `chat_id`, so most of these get 422s where 200 is
expected (and the "missing chat_id" test currently gets 422 for the wrong
reason — missing `symbol` — which happens to look like a pass; re-verify
it for the right reason after Step 3).

- [ ] **Step 3: Update `mgmt_api.py`**

In `services/trade_orchestrator/mgmt_api.py`, update the module docstring
(lines 1-9):

```python
"""
mgmt_api.py
Endpoint HTTP /mgmt/action que recibe decisiones de gestion desde un
flujo n8n/Ollama externo, para mensajes del canal que el parser de
senales no reconoce (chat_id-scoping spec, seccion 6). Se monta junto al
consumer de Redis Streams de trade_orchestrator, en el mismo proceso,
porque necesita el TradeManager en memoria para resolver los grupos
activos por chat_id (todos los trades abiertos por el bot para el mismo
canal de Telegram que mando el mensaje de gestion -- nunca por simbolo).
"""
```

Update `MgmtActionRequest` (lines 29-33):

```python
class MgmtActionRequest(BaseModel):
    action: str
    chat_id: str
    raw_text: str
    correction: Optional[Correction] = None
```

Update the handler (lines 52-62):

```python
    @app.post("/mgmt/action", dependencies=[Depends(_check_key)])
    async def mgmt_action(req: MgmtActionRequest) -> dict:
        correction = req.correction.model_dump() if req.correction else None
        try:
            result = await trade_manager.apply_mgmt_action(
                action=req.action, chat_id=req.chat_id, raw_text=req.raw_text, correction=correction,
            )
        except Exception as e:
            log.exception("[MGMT_API] apply_mgmt_action fallo inesperadamente: action=%s chat_id=%s", req.action, req.chat_id)
            return {"status": "failed", "reason": "internal_error", "detail": str(e)}
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -v`
Expected: PASS, all tests

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/mgmt_api.py services/trade_orchestrator/test_mgmt_action_endpoint.py
git commit -m "feat: replace symbol with chat_id in /mgmt/action's request contract"
```

---

### Task 6: Full-suite verification

**Files:** none modified — verification only.

- [ ] **Step 1: Run the full repository test suite**

Run: `pytest -v --ignore=tests/e2e`

(The e2e suite is excluded here deliberately — it requires a live VPS/MT5
connection per `docs/superpowers/specs/2026-09-04-e2e-test-suite-design.md`
and is not part of this plan's scope; it gets exercised for real only
after this change is deployed, per the plan's post-merge notes below.)

Expected: PASS, 0 failures. If anything outside
`services/trade_orchestrator/` fails, stop and investigate before
proceeding — this plan should not have touched any other service, so a
failure elsewhere means something unexpected (e.g. a shared fixture) was
affected.

- [ ] **Step 2: Grep the whole repo for any remaining `symbol=` call to `apply_mgmt_action` or `find_active_group_for_symbol` misuse**

Run: `grep -rn "apply_mgmt_action(" --include=*.py .` and manually confirm
every call site uses `chat_id=`, not `symbol=`. Also run:
`grep -rn "find_active_groups_for_chat\|find_active_group_for_symbol" --include=*.py services/`
and confirm `find_active_group_for_symbol` is still called only from
`services/trade_orchestrator/app.py` (the fast/full signal logic, per the
Global Constraints ruling) and `find_active_groups_for_chat` only from
`services/trade_orchestrator/trade_manager.py`'s own `apply_mgmt_action`.

Expected: no stray `symbol=` call to `apply_mgmt_action` anywhere; the
two resolution methods are not cross-used.

- [ ] **Step 3: No commit for this task** (verification only, nothing to add to git).

---

## Post-merge notes (not part of this plan's tasks — for the controller/user after implementation)

- This changes `trade_orchestrator`'s live contract. Deploying it means
  rebuilding and restarting the `trade_orchestrator` container on the
  production VPS (`root@illuminatis-vps`) — a service that manages real,
  possibly-open MT5 positions. Confirm no risky window (open trades near
  a mechanical BE/trailing decision) before restarting, per the session's
  established practice of asking before touching production.
- Once deployed, n8n's flow must be updated (by the user, outside this
  repo) to send `chat_id` instead of `symbol` in its `/mgmt/action`
  payload — until both sides change together, every real request will
  422 (missing `chat_id`) or use a stale contract.
- Family B e2e scenarios (`tests/e2e/scenarios/b*.py`) already use
  `TG_TEST_CHAT_ID` as their test channel per the e2e spec's §7
  backward-compatibility note — no test code changes are needed there,
  only re-running them against the new contract once n8n is updated.
