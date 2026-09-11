# MT5 Timeout Notification Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a hung `order_send`/`partial_close` call (timeout, not a clean MT5 rejection) from silently swallowing the notification of a trade event that already genuinely happened (confirmed via `deal.reason`), and stop one group's timeout from aborting the rest of that tick's processing for every other group on the same account.

**Architecture:** A new `MT5CallTimeoutError` exception lets `_force_runner_sl` distinguish "MT5 never responded" from "MT5 responded no" for its three callers. `_on_tp1_leg_closed` is reordered to notify `tp1_hit` before attempting the BE `order_send` at all (the TP1 is already a confirmed fact via `deal.reason` at that point — it never depended on the BE outcome). `_apply_tp2_partial_close` gets the same timeout-vs-failure split around its `partial_close` call. `_tick_once_account`'s two per-account loops move their per-iteration bodies into their own try/except, so one group's exception no longer aborts the account's remaining groups in that tick.

**Tech Stack:** Python 3, `pytest` + `pytest-asyncio` (existing test stack), `SimuladorMT5` (existing MT5 test double), the existing `monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", ...)` pattern already used in this file for simulating a real timeout without a real 10s wait.

**Spec:** `docs/superpowers/specs/2026-09-11-mt5-timeout-notification-safety-design.md`

## Global Constraints

- A confirmed business fact (TP1 hit, verified via `deal.reason == DEAL_REASON_TP`) must be notified regardless of what happens afterward when attempting the related MT5 side-effect (moving the runner to BE) — the notification must not be gated on that side-effect's success.
- A timeout (`asyncio.TimeoutError` from `TradeManager._call`) must produce a DIFFERENT event/message than a clean MT5 rejection (retcode failure after 3 retries) — timeout means "we don't know what happened", rejection means "MT5 told us no". Never conflate the two.
- New timeout events use `channel="both"` (spec's catalog: `tp1_hit_be_timeout`, `tp2_partial_timeout`) — same urgency tier as the existing `tp1_hit_be_failed`.
- One group's unhandled exception during `_tick_once_account`'s per-group processing must not prevent any other group on the same account from being processed in that same tick.
- `MT5_CALL_TIMEOUT_SECONDS`, `_call`'s retry/timeout logic itself, and `_classify_leg_closure` are NOT touched by this plan — only the callers' handling of what `_call` can raise.
- `/mgmt/action`'s existing per-group try/except pattern (already isolating exceptions per `group_id`) is preserved; it only gains recognition of the new `MT5CallTimeoutError` type to report `{"status": "timeout"}` instead of falling into the generic `{"status": "failed", "reason": "exception"}`.

---

## File Structure

Modified files only (no new files):
- `services/trade_orchestrator/trade_manager.py` — new `MT5CallTimeoutError` exception class; `_force_runner_sl` catches `asyncio.TimeoutError` from its `_call(client.order_send, ...)` and re-raises as `MT5CallTimeoutError`; `_on_tp1_leg_closed` reordered (notify `tp1_hit` first, then attempt BE, then notify BE outcome distinguishing timeout from failure); `_apply_tp2_partial_close` gets the same timeout-vs-failure split around its `_call(client.partial_close, ...)`; `apply_mgmt_action`'s `move_sl_be_now` branch recognizes `MT5CallTimeoutError` for a `{"status": "timeout"}` result; `_tick_once_account`'s two per-account loops get per-iteration try/except.
- `services/trade_orchestrator/event_messages.py` — two new message builders: `build_tp1_hit_be_timeout_message`, `build_tp2_partial_timeout_message`.
- `services/trade_orchestrator/test_event_messages.py` — tests for the two new builders.
- `services/trade_orchestrator/test_trade_manager_dual_tp.py` — all behavioral tests for this plan (reordering, timeout distinction, per-group isolation).

---

## Task 1: `MT5CallTimeoutError` + `_force_runner_sl` distinguishes timeout from rejection

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (`_force_runner_sl`, around line 789; add the new exception class near the top of the file alongside other module-level definitions like `MAGIC`/`ManagedTrade`)
- Test: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Produces: `class MT5CallTimeoutError(Exception): pass` (module-level in `trade_manager.py`, alongside `ManagedTrade`). `_force_runner_sl(...)` now raises `MT5CallTimeoutError` (instead of letting `asyncio.TimeoutError` propagate unchanged) when any attempt's `order_send` call times out; on a clean rejection (retcode not `10009` after all `attempts`) it still returns `False` exactly as today — no change to that path.

- [ ] **Step 1: Write the failing test**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py` (near the existing `test_call_times_out_instead_of_hanging_forever_on_a_stuck_mt5_socket` test, same file, so the `monkeypatch`/`time`/`asyncio` imports are already in scope):

```python
@pytest.mark.asyncio
async def test_force_runner_sl_raises_mt5_call_timeout_error_on_a_hung_order_send(monkeypatch):
    """Real production incident (group 122, 2026-09-11): a genuine TP1 hit
    (confirmed via deal.reason=DEAL_REASON_TP) was followed by an order_send
    that hung past MT5_CALL_TIMEOUT_SECONDS while moving the runner to BE.
    The resulting asyncio.TimeoutError propagated unchanged out of
    _force_runner_sl, was caught by _tick_once_account's single outer
    try/except, and aborted the rest of that tick -- silently losing the
    tp1_hit notification for an event that had already genuinely happened.
    _force_runner_sl must re-raise as MT5CallTimeoutError so callers can
    tell "MT5 never responded" apart from "MT5 said no"."""
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.05")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    client = sim

    def hung_order_send(req):
        time.sleep(0.3)  # longer than the patched 0.05s timeout
        return None

    monkeypatch.setattr(sim, "order_send", hung_order_send)

    with pytest.raises(MT5CallTimeoutError):
        await tm._force_runner_sl(ACCOUNT, client, runner, runner.entry_price, reason="TP1-BE")


@pytest.mark.asyncio
async def test_force_runner_sl_still_returns_false_on_a_clean_rejection_not_a_timeout():
    """Regression guard: a clean MT5 rejection (bad retcode, no hang) must
    keep returning False exactly as before -- only a real timeout should
    raise MT5CallTimeoutError."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    client = sim

    def rejecting_order_send(req):
        return type('OrderSendResult', (), {'retcode': 10016, 'order': 0, 'deal': 0, 'comment': 'Invalid stops'})()

    sim.order_send = rejecting_order_send

    ok = await tm._force_runner_sl(ACCOUNT, client, runner, runner.entry_price, reason="TP1-BE")

    assert ok is False
```

Add `MT5CallTimeoutError` to the existing import line at the top of the test file (find the line importing `TradeManager, ManagedTrade, MAGIC` from `services.trade_orchestrator.trade_manager` and add it there).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "force_runner_sl_raises or force_runner_sl_still_returns" -v`
Expected: FAIL — first test with `ImportError: cannot import name 'MT5CallTimeoutError'`, or once that's stubbed, with the timeout not being converted (plain `asyncio.TimeoutError` instead of `MT5CallTimeoutError`).

- [ ] **Step 3: Implement**

Add near the top of `trade_manager.py`, after the existing imports and before `ManagedTrade`:

```python
class MT5CallTimeoutError(Exception):
    """Raised when an MT5 order_send/partial_close call times out (asyncio.TimeoutError
    from TradeManager._call) rather than receiving a clean rejection from MT5. Distinct
    from a clean failure: a timeout means MT5 never responded in time, not that it said no
    -- the underlying action may still complete in the background (see TradeManager._call's
    docstring). Callers that notify a business event on failure must treat this differently
    from a clean rejection (see spec 2026-09-11-mt5-timeout-notification-safety-design.md)."""
    pass
```

Update `_force_runner_sl` (trade_manager.py:789-818), wrapping only the `order_send` call:

```python
    async def _force_runner_sl(self, account, client, runner: ManagedTrade, new_sl: float, *, reason: str, attempts: int = 3, retry_delay_seconds: float = 0.2) -> bool:
        """
        Mueve el SL del runner via order_send, reintentando unas pocas veces
        antes de darse por vencido. Real production bug: un solo intento no
        distinguia un rechazo transitorio de MT5 (el mas comun: el SL
        candidato cae dentro de trade_stops_level, el minimo de distancia al
        precio vivo que el broker exige — confirmado en logs reales sin
        retcode, "[TM] fallo moviendo SL ... reason=trailing/TP1-BE/mgmt-fallback-BE")
        de un fallo real. Cualquier caller de _force_runner_sl (BE automatico
        al cerrar TP1, trailing mecanico, o move_sl_be_now via /mgmt/action)
        se beneficia del mismo reintento sin duplicar su propio loop —
        centralizado aqui en vez de en cada caller, mismo patron que
        _get_price_with_retry ya usa para tick_price.

        Real production incident (group 122, 2026-09-11): a hung order_send
        (past MT5_CALL_TIMEOUT_SECONDS) raised a bare asyncio.TimeoutError
        that propagated unchanged, got caught by _tick_once_account's single
        outer try/except, and silently dropped the tp1_hit notification for
        an already-genuine TP1. A timeout means "MT5 never responded", not
        "MT5 said no" -- re-raised here as MT5CallTimeoutError so callers can
        tell the two apart and notify accordingly, instead of retrying (a
        call that already hung 10s is unlikely to succeed on an immediate
        retry) or losing the notification entirely.
        """
        # tp=0.0 explicito: el runner nunca lleva un TP real en MT5 (su unica salida
        # mecanica es el trailing SL) -- omitir "tp" en un request action=6 puede
        # limpiar o preservar el TP existente segun el broker, asi que lo fijamos
        # explicitamente en vez de depender de ese comportamiento implicito.
        req = {"action": 6, "position": runner.ticket, "sl": float(new_sl), "tp": 0.0}
        ok = False
        for attempt in range(1, attempts + 1):
            try:
                res = await self._call(client.order_send, req)
            except asyncio.TimeoutError:
                raise MT5CallTimeoutError(f"order_send colgado moviendo SL runner={runner.ticket} reason={reason}")
            ok = bool(res and getattr(res, "retcode", None) == 10009)
            if ok:
                break
            if attempt < attempts:
                await asyncio.sleep(retry_delay_seconds)
        if not ok:
            log.error("[TM] fallo moviendo SL runner=%s reason=%s tras %d intentos", runner.ticket, reason, attempts)
        return ok
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "force_runner_sl_raises or force_runner_sl_still_returns" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full test file to confirm no regression**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — all pre-existing tests still pass (this change only adds a new exception path on top of an existing one; the clean-rejection path is untouched).

- [ ] **Step 6: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat(trade_orchestrator): add MT5CallTimeoutError, distinguish order_send timeout from clean rejection

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Timeout message builders

**Files:**
- Modify: `services/trade_orchestrator/event_messages.py`
- Modify: `services/trade_orchestrator/test_event_messages.py`

**Interfaces:**
- Consumes: nothing new (pure functions, same pattern as every other builder in this file).
- Produces: `build_tp1_hit_be_timeout_message(*, channel_name, group_id, symbol, direction, runner_ticket) -> str`; `build_tp2_partial_timeout_message(*, channel_name, group_id, symbol, direction) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_event_messages.py`:

```python
def test_tp1_hit_be_timeout_message_conveys_uncertainty_and_urgency():
    msg = build_tp1_hit_be_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction="BUY", runner_ticket=12345,
    )
    assert "TP1" in msg.upper()
    assert "no se pudo confirmar" in msg.lower() or "no pudo confirmar" in msg.lower()
    assert "revisar" in msg.lower()


def test_tp1_hit_be_timeout_message_handles_direction_none():
    # Must not crash even if direction is somehow missing, same guard as the
    # other builders in this file (Task 6 of the audit-log plan already
    # established this pattern for every direction-taking builder).
    msg = build_tp1_hit_be_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction=None, runner_ticket=12345,
    )
    assert isinstance(msg, str) and len(msg) > 0


def test_tp2_partial_timeout_message_conveys_uncertainty():
    msg = build_tp2_partial_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction="SELL",
    )
    assert "TP2" in msg.upper()
    assert "revisar" in msg.lower()


def test_tp2_partial_timeout_message_handles_direction_none():
    msg = build_tp2_partial_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction=None,
    )
    assert isinstance(msg, str) and len(msg) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_event_messages.py -k "timeout" -v`
Expected: FAIL with `ImportError` (the two new functions don't exist yet).

- [ ] **Step 3: Implement**

Add to `services/trade_orchestrator/event_messages.py` (uses the file's existing `_fmt_direction` helper, confirmed present at `event_messages.py:22`, added by a prior plan — it returns `direction.upper()` or `"N/D"` if `direction` is `None`):

```python
def build_tp1_hit_be_timeout_message(*, channel_name, group_id, symbol, direction, runner_ticket) -> str:
    return (
        f"⚠️ TP1 ALCANZADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"No se pudo CONFIRMAR si el runner (ticket={runner_ticket}) quedo en breakeven "
        f"— MT5 no respondio a tiempo. El runner puede seguir con su SL original, o el "
        f"breakeven puede haberse aplicado igual en segundo plano sin que el sistema se "
        f"entere.\n"
        f"Revisar manualmente en MT5."
    )


def build_tp2_partial_timeout_message(*, channel_name, group_id, symbol, direction) -> str:
    return (
        f"⚠️ TP2 ALCANZADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"No se pudo confirmar si el cierre parcial del 50%% se ejecuto — MT5 no respondio "
        f"a tiempo.\n"
        f"Revisar manualmente el volumen real de la posicion en MT5."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_event_messages.py -v`
Expected: PASS — full file, including the 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/event_messages.py services/trade_orchestrator/test_event_messages.py
git commit -m "feat(trade_orchestrator): add tp1/tp2 timeout message builders

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Reorder `_on_tp1_leg_closed` — notify TP1 before attempting BE, distinguish timeout

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (`_on_tp1_leg_closed`, around line 682)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `MT5CallTimeoutError` (Task 1), `build_tp1_hit_be_timeout_message` (Task 2).
- Produces: `_on_tp1_leg_closed` now always notifies `tp1_hit` (channel="both") before attempting `_force_runner_sl`, regardless of the BE outcome. After the attempt: on success, behavior is unchanged (`be_applied=True`, `planned_sl` synced, no additional notify). On a clean rejection (`_force_runner_sl` returns `False`), notifies `tp1_hit_be_failed` exactly as today. On `MT5CallTimeoutError`, notifies the new `tp1_hit_be_timeout` event (channel="both") instead, and does NOT set `be_applied`/`planned_sl` (unknown outcome — leave the existing values as they were, matching the "unknown" spirit of this event; a later tick's reconciliation or manual check resolves the real state).

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:

```python
@pytest.mark.asyncio
async def test_tp1_hit_is_notified_even_when_the_be_order_send_times_out(monkeypatch):
    """Real production incident (group 122, 2026-09-11): confirmed via MT5's
    real history_deals_get that TP1 genuinely hit (reason=DEAL_REASON_TP,
    real profit), but the tp1_hit notification never reached the audit log,
    n8n, or Telegram -- because the order_send moving the runner to BE hung,
    raised a timeout, and the notify call (which used to sit AFTER the BE
    attempt) was never reached. tp1_hit must be notified as soon as TP1 is
    confirmed, independent of what happens when attempting BE afterward."""
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.05")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    def hung_order_send(req):
        time.sleep(0.3)
        return None

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
    sim.order_send = hung_order_send

    await tm._tick_once_account(ACCOUNT)

    tp1_hit_events = [kwargs for event, kwargs in tm.notifier.events if event == "tp1_hit"]
    assert len(tp1_hit_events) == 1
    assert tp1_hit_events[0]["pnl_money"] == 20.0


@pytest.mark.asyncio
async def test_be_timeout_notifies_tp1_hit_be_timeout_not_tp1_hit_be_failed(monkeypatch):
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.05")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    def hung_order_send(req):
        time.sleep(0.3)
        return None

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
    sim.order_send = hung_order_send

    await tm._tick_once_account(ACCOUNT)

    timeout_events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit_be_timeout"]
    failed_events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit_be_failed"]
    assert len(timeout_events) == 1
    assert len(failed_events) == 0
    # be_applied must NOT be set on an unknown-outcome timeout -- the runner's
    # real MT5 state is unverified, so the in-memory flag must not claim success.
    assert runner.be_applied is False


@pytest.mark.asyncio
async def test_be_clean_rejection_still_notifies_tp1_hit_be_failed_not_timeout(monkeypatch):
    """Regression guard: a clean rejection (not a timeout) must keep firing
    the existing tp1_hit_be_failed event, unchanged."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    def rejecting_order_send(req):
        return type('OrderSendResult', (), {'retcode': 10016, 'order': 0, 'deal': 0, 'comment': 'Invalid stops'})()

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
    sim.order_send = rejecting_order_send

    await tm._tick_once_account(ACCOUNT)

    failed_events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit_be_failed"]
    timeout_events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit_be_timeout"]
    assert len(failed_events) == 1
    assert len(timeout_events) == 0
```

Note: `_notify`'s signature is `async def _notify(self, event: str, *, channel: str = "audit", **kwargs)` (trade_manager.py:87) — `channel` is a dedicated keyword-only parameter, so it is consumed by the signature itself and never lands inside `**kwargs`. `DummyNotifier.notify_trade_event(self, event, **kwargs)` therefore never receives `channel` at all (confirmed by reading both definitions) — this is why the test above only asserts on `pnl_money`, not `channel`. The event-TYPE distinction (`tp1_hit_be_timeout` vs `tp1_hit_be_failed`) is what actually matters here and is covered by the next two tests, which assert on event names, not on a `channel` kwarg that this test double can't observe.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "tp1_hit_is_notified_even_when or be_timeout_notifies or be_clean_rejection_still_notifies" -v`
Expected: FAIL — `tp1_hit` is not notified before the hung BE attempt (current code order), and `tp1_hit_be_timeout` doesn't exist as an event yet.

- [ ] **Step 3: Implement**

Replace `_on_tp1_leg_closed` (trade_manager.py:682-747) with:

```python
    async def _on_tp1_leg_closed(self, account, client, tp1_leg: ManagedTrade) -> None:
        """TP1 hit -> notifica el hecho de inmediato, luego intenta mover el
        runner del mismo group_id a BE (dual-TP spec seccion 4).

        Real production incident (group 122, 2026-09-11): con el orden
        anterior (notificar tp1_hit solo DESPUES de un _force_runner_sl
        exitoso), un order_send colgado (timeout) en el intento de BE hacia
        que la excepcion se propagara antes de llegar al notify -- perdiendo
        en silencio la notificacion de un TP1 que ya habia ocurrido de
        verdad (confirmado con deal.reason=DEAL_REASON_TP en MT5). El TP1 ya
        es un hecho confirmado en el momento en que esta funcion se invoca
        (via _classify_leg_closure) -- no depende en absoluto de si el BE
        tiene exito, asi que se notifica primero, sin condicionarlo al
        resultado del intento de BE que sigue.
        """
        TP1_HITS.inc()
        runner = next((t for t in self.trades.values() if t.group_id == tp1_leg.group_id and t.leg == "runner"), None)
        if not runner:
            return

        deal_info = await self._get_close_deal_info(client, tp1_leg.ticket)
        channel_name = resolve_channel_name(tp1_leg.chat_id, self._channel_names())
        close_price = deal_info["price"] if deal_info else None
        pnl_money = deal_info["profit"] if deal_info else None
        close_volume = deal_info["volume"] if deal_info else None
        message = build_tp1_hit_message(
            channel_name=channel_name, group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, direction=tp1_leg.direction,
            close_price=close_price, close_volume=close_volume, pnl_money=pnl_money, account_currency="USD",
        )
        await self._notify(
            "tp1_hit", channel="both", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
            runner_ticket=runner.ticket, chat_id=tp1_leg.chat_id, channel_name=channel_name,
            close_price=close_price, close_volume=close_volume, pnl_money=pnl_money,
            message=message,
        )

        if runner.entry_price is None:
            log.error("[TM] no se puede aplicar BE: runner=%s no tiene entry_price registrado (group_id=%s)",
                      runner.ticket, tp1_leg.group_id)
            return

        # be_applied SOLO se marca True si el order_send realmente tuvo exito.
        # Bug real de produccion: marcarlo incondicionalmente dejaba el runner en
        # un estado inconsistente cuando el BE fallaba — el guard de _apply_trailing
        # (que exige be_applied) dejaba de bloquearlo, y el trailing intentaba
        # correr sobre un SL que en realidad nunca se movio a breakeven.
        # _force_runner_sl ya reintenta internamente (este es el unico momento
        # en que se dispara el BE — si se pierde aqui sin reintentar, el runner
        # queda huerfano de BE para siempre).
        try:
            ok = await self._force_runner_sl(account, client, runner, runner.entry_price, reason="TP1-BE")
        except MT5CallTimeoutError:
            log.error("[TM] timeout aplicando BE tras TP1, runner=%s group_id=%s — estado del BE desconocido",
                      runner.ticket, tp1_leg.group_id)
            timeout_message = build_tp1_hit_be_timeout_message(
                channel_name=channel_name, group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
                direction=tp1_leg.direction, runner_ticket=runner.ticket,
            )
            await self._notify(
                "tp1_hit_be_timeout", channel="both", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
                direction=tp1_leg.direction, runner_ticket=runner.ticket, chat_id=tp1_leg.chat_id,
                channel_name=channel_name, message=timeout_message,
            )
            return

        if ok:
            runner.be_applied = True
            # Real production bug found live (2026-09-10, e2e D1 scenario):
            # planned_sl was never updated to the new BE price here, only
            # be_applied was set. MT5's real SL was correct, but the
            # in-memory/persisted ManagedTrade.planned_sl stayed at the
            # OLD, pre-BE value -- _group_doc persists it as-is, and a
            # restart's reconcile_from_mt5 then rebuilds the runner with
            # that stale planned_sl. A later signal_correction/
            # update_group_signal call comparing against this field (its
            # own never-regress guard, see update_group_signal) would
            # compare against the wrong baseline.
            runner.planned_sl = runner.entry_price
            await self._persist_group(tp1_leg.group_id)
        else:
            log.error("[TM] BE no se pudo aplicar tras 3 intentos, runner=%s group_id=%s queda con SL original",
                      runner.ticket, tp1_leg.group_id)
            # channel="both": spec seccion 6 lista este evento como `both`. Es
            # el unico del catalogo donde el runner queda MAS expuesto de lo
            # normal (sigue vivo con su SL original, sin el BE que ya se
            # gano), asi que es discutiblemente la notificacion mas urgente
            # de todas — dejarla audit-only significaba que nadie se enteraba.
            failed_message = build_tp1_hit_be_failed_message(
                channel_name=channel_name, group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
                direction=tp1_leg.direction, runner_ticket=runner.ticket,
            )
            await self._notify(
                "tp1_hit_be_failed", channel="both", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
                direction=tp1_leg.direction, runner_ticket=runner.ticket, chat_id=tp1_leg.chat_id,
                channel_name=channel_name, message=failed_message,
            )
```

Add the import for the new message builder at the top of `trade_manager.py` (merge with the existing `event_messages` import block, don't duplicate the import line):

```python
from .event_messages import build_tp1_hit_be_timeout_message, build_tp2_partial_timeout_message
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — full file, including the 3 new tests, with no regressions in the existing TP1/BE tests (the success path and the clean-rejection path are behaviorally unchanged, only reordered/re-labeled on the timeout path).

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "fix(trade_orchestrator): notify tp1_hit before attempting BE, add tp1_hit_be_timeout event

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: `_apply_tp2_partial_close` distinguishes timeout from failure

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (`_apply_tp2_partial_close`, around line 865)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `MT5CallTimeoutError` (Task 1 — but note `partial_close` is called via `self._call(client.partial_close, ...)` directly, not through `_force_runner_sl`, so this task adds its OWN try/except around that specific `_call`, not a reuse of Task 1's exception-raising code path inside `_force_runner_sl`), `build_tp2_partial_timeout_message` (Task 2).
- Produces: on a timeout from the `partial_close` call, notifies `tp2_partial_timeout` (channel="both") instead of silently returning (today's behavior on `ok=False` just logs and returns — that stays unchanged for a clean `False`; only the timeout path is new). `tp2_partial_applied` and `tp2_partial_skipped_volume` are NOT modified on a timeout (unknown outcome — same principle as Task 3's BE timeout).

- [ ] **Step 1: Write the failing test**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:

```python
@pytest.mark.asyncio
async def test_tp2_partial_close_timeout_notifies_tp2_partial_timeout(monkeypatch):
    """Same failure class as the TP1/BE timeout (Task 3), applied to TP2's
    partial_close call: a hung MT5 call must not silently drop the
    notification, and must not falsely claim tp2_partial_applied succeeded
    or failed cleanly -- the real outcome in MT5 is unknown."""
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.05")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    runner.be_applied = True  # TP2 partial only runs once BE is already applied

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
    await tm._tick_once_account(ACCOUNT)  # processes the TP1 leg's closure, applies BE for real

    def hung_partial_close(account, ticket, percent):
        time.sleep(0.3)
        return True

    sim.price = 2530.0  # reaches tp2
    sim.partial_close = hung_partial_close

    await tm._tick_once_account(ACCOUNT)

    timeout_events = [event for event, kwargs in tm.notifier.events if event == "tp2_partial_timeout"]
    assert len(timeout_events) == 1
    assert runner.tp2_partial_applied is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "tp2_partial_close_timeout" -v`
Expected: FAIL — the hung call currently raises a bare `asyncio.TimeoutError` that isn't caught inside `_apply_tp2_partial_close`, propagating up and getting swallowed by `_tick_once_account`'s outer try/except with no `tp2_partial_timeout` event ever notified.

- [ ] **Step 3: Implement**

In `_apply_tp2_partial_close` (trade_manager.py:865-974), replace the `partial_close` call and its immediate failure handling (lines 944-948) with:

```python
        try:
            ok = await self._call(client.partial_close, account, runner.ticket, 50)
        except asyncio.TimeoutError:
            log.error("[TM] timeout aplicando partial close en tp2, runner=%s group_id=%s — estado desconocido",
                      runner.ticket, runner.group_id)
            channel_name = resolve_channel_name(runner.chat_id, self._channel_names())
            timeout_message = build_tp2_partial_timeout_message(
                channel_name=channel_name, group_id=runner.group_id, symbol=runner.symbol, direction=runner.direction,
            )
            await self._notify(
                "tp2_partial_timeout", channel="both", group_id=runner.group_id, ticket=runner.ticket,
                symbol=runner.symbol, chat_id=runner.chat_id, channel_name=channel_name, message=timeout_message,
            )
            return
        if not ok:
            log.error("[TM] fallo aplicando partial close en tp2 runner=%s group_id=%s",
                      runner.ticket, runner.group_id)
            return
```

Everything else in the function (lines 949-974, the success path) stays exactly as-is — this only wraps the one `_call` and adds the timeout branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — full file, including the new test, no regressions on the existing TP2 tests (success path and clean-`ok=False` path are unchanged).

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "fix(trade_orchestrator): add tp2_partial_timeout event for hung partial_close calls

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: `move_sl_be_now` (`/mgmt/action`) recognizes `MT5CallTimeoutError`

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (`apply_mgmt_action`'s `move_sl_be_now` branch, around line 1280-1345)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `MT5CallTimeoutError` (Task 1).
- Produces: on a timeout from `_force_runner_sl` inside this branch, the per-group result becomes `{"group_id": group_id, "status": "timeout"}` instead of falling into the existing generic `except Exception` handler's `{"status": "failed", "reason": "exception"}`. No new event type is introduced here — this action is already user-initiated (someone asked for BE via Telegram), so the existing per-group `results` list returned to the HTTP caller is the primary feedback channel; the spec's global constraint only requires this branch to recognize the exception type, not add a new Telegram notification (unlike Tasks 3/4, which cover BACKGROUND/automatic detection paths where the user has no other way to find out).

- [ ] **Step 1: Write the failing test**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py` (near the existing `move_sl_be_now` tests in this file — search for `"move_sl_be_now"` to find that test group and match its `CHAT_ID`/`open_group(..., chat_id=...)` conventions exactly):

```python
@pytest.mark.asyncio
async def test_move_sl_be_now_reports_timeout_status_distinct_from_failed(monkeypatch):
    """Regression guard extending Task 1's MT5CallTimeoutError: this action's
    existing per-group try/except must recognize the new exception type
    explicitly, rather than letting it fall into the generic 'reason':
    'exception' bucket -- the HTTP caller (n8n) should be able to tell
    'MT5 never responded' apart from 'something else broke'."""
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.05")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    def hung_order_send(req):
        time.sleep(0.3)
        return None

    sim.order_send = hung_order_send

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="pon en be", correction=None)

    assert result["results"][0]["status"] == "timeout"
```

Note: `CHAT_ID = "-1001234567890"` is the module-level constant already defined at `test_trade_manager_dual_tp.py:179` and reused by every existing `apply_mgmt_action` test in this file — the test above already uses it correctly.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "move_sl_be_now_reports_timeout" -v`
Expected: FAIL — the hung call today falls into the branch's outer `except Exception as e:` (trade_manager.py:1342-1344), producing `{"status": "failed", "reason": "exception"}`, not `{"status": "timeout"}`.

- [ ] **Step 3: Implement**

In `apply_mgmt_action`'s `move_sl_be_now` branch (trade_manager.py:1280-1345), wrap only the `_force_runner_sl` call (currently line 1322):

```python
                    try:
                        ok = await self._force_runner_sl(account, client, runner, be_price, reason="mgmt-fallback-BE")
                    except MT5CallTimeoutError:
                        log.error("[TM][MGMT] timeout aplicando BE via mgmt_action group_id=%s chat_id=%s", group_id, chat_id)
                        results.append({"group_id": group_id, "status": "timeout"})
                        continue
                    if ok:
```

(The `if ok:` block that follows, and everything after it through the end of the `for group_id in group_ids:` loop body, stays exactly as it is today — only the two lines calling `_force_runner_sl` and starting the `if ok:` need to change shape to accommodate the new `try/except`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — full file, including the new test, no regressions on the existing `move_sl_be_now` tests (success and clean-failure paths unchanged).

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "fix(trade_orchestrator): move_sl_be_now reports timeout status distinct from generic failure

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Per-group isolation in `_tick_once_account`'s two loops

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (`_tick_once_account`, lines 555-640)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_tick_once_account`'s first loop (closed-ticket detection, lines 565-624) and second loop (TP2/trailing, lines 628-637) each wrap their per-iteration body in its own try/except, logging the `group_id`/`ticket` on any unhandled exception and continuing to the next item — instead of relying solely on the function-level try/except (line 639) that today aborts the entire account's remaining processing on the first unhandled exception from any group.

- [ ] **Step 1: Write the failing test**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:

```python
@pytest.mark.asyncio
async def test_one_groups_unhandled_exception_does_not_block_another_groups_processing(monkeypatch):
    """Real production risk generalized from the group-122 incident: today
    _tick_once_account has a single try/except around its ENTIRE body, so
    any unhandled exception while processing one group (even one Task 1-5
    don't already catch) aborts processing for every other group on the
    same account in that same tick. Two independent groups on one account,
    one hitting an unhandled error and one closing cleanly via real TP,
    must both be processed in the same tick -- the second group's event
    must not be silently dropped because the first group blew up."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_a = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    group_b = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg_a = next(t for t in tm.trades.values() if t.group_id == group_a and t.leg == "tp1")
    tp1_leg_b = next(t for t in tm.trades.values() if t.group_id == group_b and t.leg == "tp1")

    sim.close_position_by_tp(tp1_leg_a.ticket, close_price=2510.0, profit=20.0)
    sim.close_position_by_tp(tp1_leg_b.ticket, close_price=2510.0, profit=20.0)

    # Force an unhandled (non-MT5CallTimeoutError) exception specifically
    # while classifying group_a's closed leg, without affecting group_b's.
    original_classify = tm._classify_leg_closure

    async def classify_raises_for_group_a(client, closed_trade):
        if closed_trade.group_id == group_a:
            raise RuntimeError("simulated unrelated bug processing group_a")
        return await original_classify(client, closed_trade)

    monkeypatch.setattr(tm, "_classify_leg_closure", classify_raises_for_group_a)

    await tm._tick_once_account(ACCOUNT)

    tp1_hit_group_ids = [kwargs["group_id"] for event, kwargs in tm.notifier.events if event == "tp1_hit"]
    assert group_b in tp1_hit_group_ids
    assert group_a not in tp1_hit_group_ids  # group_a's own exception did prevent ITS notification
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "one_groups_unhandled_exception" -v`
Expected: FAIL — today's single outer try/except means `group_a`'s `RuntimeError` aborts the whole loop before `group_b` is ever reached, so `group_b` is also missing from `tp1_hit_group_ids`.

- [ ] **Step 3: Implement**

Replace `_tick_once_account` (trade_manager.py:555-640) with:

```python
    async def _tick_once_account(self, account) -> None:
        account = self._ensure_account_dict(account)
        if not account:
            return
        try:
            client = self.mt5._client_for(account)
            positions = await self._call(client.positions_get) or []
            pos_by_ticket = {p.ticket: p for p in positions}

            # Detect closed tickets for this account (TP1 hit, SL hit, or manual close).
            # Each ticket's processing is isolated in its own try/except (real
            # production risk: an unhandled exception while processing one
            # group -- e.g. a timeout not already caught by Tasks 1/3/4/5, or
            # any other unexpected error -- must not abort processing for
            # every OTHER group on this same account in the same tick).
            for ticket in [t for t, mt in self.trades.items() if mt.account_name == account["name"]]:
                if ticket in pos_by_ticket:
                    continue
                closed_trade = self.trades.pop(ticket)
                try:
                    # Real production bug: a tp1_leg disappearing from positions_get
                    # was ALWAYS treated as "TP1 reached", regardless of the real
                    # close price -- a SL hit, a manual close, or a test's own
                    # emergency cleanup on this same ticket all triggered the
                    # "TP1 alcanzado" flow (moving the runner to BE, incrementing
                    # TP1_HITS, and logging a false tp1_hit event to n8n). Confirmed
                    # live: a tp1_leg closed via partial_close (DEAL_REASON_CLIENT)
                    # at a loss, well below its own tp1_price, still got logged as
                    # a TP1 hit. Verify the real close price actually reached
                    # tp1_price (within half the entry->tp1 distance, to tolerate
                    # normal slippage) before treating this as a genuine TP1 event.
                    classification = await self._classify_leg_closure(client, closed_trade)
                    cause = classification["cause"]
                    if cause == "tp1":
                        await self._on_tp1_leg_closed(account, client, closed_trade)
                    elif cause == "sl":
                        channel_name = resolve_channel_name(closed_trade.chat_id, self._channel_names())
                        message = build_sl_hit_message(
                            channel_name=channel_name, group_id=closed_trade.group_id, symbol=closed_trade.symbol,
                            direction=closed_trade.direction, close_price=classification["price"],
                            close_volume=classification["volume"], pnl_money=classification["profit"],
                        )
                        await self._notify(
                            "sl_hit_detected", channel="both", group_id=closed_trade.group_id, chat_id=closed_trade.chat_id,
                            channel_name=channel_name, symbol=closed_trade.symbol, direction=closed_trade.direction,
                            leg=closed_trade.leg, close_price=classification["price"], close_volume=classification["volume"],
                            pnl_money=classification["profit"], message=message,
                        )
                    else:
                        channel_name = resolve_channel_name(closed_trade.chat_id, self._channel_names())
                        if cause == "external":
                            message = build_external_close_message(
                                channel_name=channel_name, group_id=closed_trade.group_id, symbol=closed_trade.symbol,
                                direction=closed_trade.direction, leg=closed_trade.leg,
                                close_price=classification["price"], close_volume=classification["volume"],
                                pnl_money=classification["profit"],
                            )
                            await self._notify(
                                "external_close_detected", channel="both", group_id=closed_trade.group_id,
                                chat_id=closed_trade.chat_id, channel_name=channel_name, symbol=closed_trade.symbol,
                                direction=closed_trade.direction, leg=closed_trade.leg,
                                close_price=classification["price"], close_volume=classification["volume"],
                                pnl_money=classification["profit"], message=message,
                            )
                        else:
                            leg_label = "Runner" if closed_trade.leg == "runner" else "tp1_leg"
                            await self._notify(
                                "runner_closed" if closed_trade.leg == "runner" else "tp1_leg_closed_not_at_tp1",
                                channel="audit", group_id=closed_trade.group_id, ticket=ticket, symbol=closed_trade.symbol,
                                message=f"{leg_label} del grupo {closed_trade.group_id} ({closed_trade.symbol}, ticket={ticket}) "
                                        f"se cerro sin poder determinar la causa. Precio de apertura "
                                        f"{self._fmt_price(closed_trade.entry_price)}.",
                            )
                    remaining = [t for t in self.trades.values() if t.group_id == closed_trade.group_id]
                    if not remaining:
                        await self._close_group_in_store(closed_trade.group_id)
                except Exception as e:
                    log.error("[TM] error procesando cierre de ticket=%s group_id=%s: %s",
                              ticket, closed_trade.group_id, e)

            ACTIVE_TRADES.set(len(self.trades))

            for ticket, t in [(tk, mt) for tk, mt in self.trades.items() if mt.account_name == account["name"]]:
                try:
                    pos = pos_by_ticket.get(ticket)
                    if not pos or t.leg != "runner" or not t.be_applied:
                        continue
                    await self._apply_tp2_partial_close(account, client, t, pos)
                    # Re-fetch: partial_close above may have changed this position's
                    # live volume, and _apply_trailing's SL move must act on that
                    # up-to-date position, not a stale pre-partial-close snapshot.
                    pos = (await self._call(client.positions_get, ticket=ticket) or [pos])[0]
                    await self._apply_trailing(account, client, t, pos)
                except Exception as e:
                    log.error("[TM] error aplicando TP2/trailing a ticket=%s group_id=%s: %s", ticket, t.group_id, e)

        except Exception as e:
            log.error("[TM] error gestionando cuenta %s: %s", account.get("name"), e)
```

Note: `MT5CallTimeoutError` deliberately is NOT given a special case in either of these two new per-iteration `except` blocks — Tasks 3, 4, and 5 already catch it at the exact point it's raised (inside `_on_tp1_leg_closed` and `_apply_tp2_partial_close` themselves), so by the time control reaches these outer per-iteration try/excepts, that exception type has already been converted into a notified event and doesn't propagate this far. These per-iteration catches are the second-layer safety net for anything else (see spec §3.3's "three levels don't overlap" note).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — full file, including the new test, no regressions (this is a pure refactor of exception scope; every existing single-group test still only has one group to isolate, so its behavior is unchanged).

- [ ] **Step 5: Run the full repo suite**

Run: `pytest -v` (from repo root)
Expected: PASS — full suite, confirming this change (touching the core management loop) doesn't break any e2e scenario or any other test file.

- [ ] **Step 6: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "fix(trade_orchestrator): isolate per-group exceptions in _tick_once_account so one group's failure doesn't block others

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Final verification and full regression

**Files:** none modified — verification only.

**Interfaces:** none new.

- [ ] **Step 1: Run the full repo suite one more time**

Run: `pytest -v` (from repo root)
Expected: PASS across the whole repo (baseline before this plan: 356 passed; expect 356 + the new tests from Tasks 1-6 — count them from the task list above and confirm the final number matches, reporting the exact count either way rather than assuming).

- [ ] **Step 2: Manually trace the group-122 scenario one more time by reading the final code**

Read the final state of `_on_tp1_leg_closed` and confirm by inspection (not just tests) that: (a) `tp1_hit` is notified before any `order_send` is attempted, (b) a hung `order_send` during the BE attempt cannot prevent that already-sent notification from having gone out, (c) the timeout case notifies a distinctly-named event (`tp1_hit_be_timeout`) rather than either staying silent or reusing `tp1_hit_be_failed`. Report this confirmation in the final summary rather than assuming the tests alone prove it — a human reading the code should also be able to see the fix at a glance.

- [ ] **Step 3: Report final commit list and test count**

No commit for this task (verification only) — summarize the full commit range from Task 1 through Task 6 and the final `pytest -v` count in the completion report.
