from .trade_utils import safe_comment
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

    @staticmethod
    async def _call(fn, *args, **kwargs):
        """
        Ejecuta una llamada MT5/RPyC sincrona (order_send, positions_get, tick_price,
        symbol_select, symbol_info, partial_close) en un hilo aparte via
        asyncio.to_thread, para no bloquear el event loop compartido por el loop de
        senales, el loop de gestion mecanica (run_forever) y el endpoint /mgmt/action
        cuando MT5 tarda, se cuelga, o esta reconectando (PooledMT5Client puede
        bloquear hasta 1.5s en un intento de reconexion con un lock tomado).
        """
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _get_price_with_retry(self, client, symbol: str, direction: str, attempts: int = 3, delay_seconds: float = 0.15) -> float:
        """
        Pide tick_price con reintentos cortos. mt5linux abre una conexion RPyC
        nueva por cada llamada (sin sesion persistente), asi que un
        symbol_select seguido de inmediato por tick_price puede correr contra
        el terminal MT5 (bajo Wine) antes de que este propague el estado del
        simbolo recien seleccionado — tick_price entonces devuelve 0.0 sin
        lanzar excepcion (no hay nada que _call pueda reintentar). Vimos esto
        en produccion: una senal real se aborto por "sin precio" pese a que
        el simbolo y el broker estaban perfectamente disponibles un segundo
        despues. Reintentar aqui, en vez de abortar a la primera, absorbe ese
        glitch transitorio sin enmascarar una falla real (broker cerrado,
        simbolo inexistente) — tras `attempts` intentos vacios, se rinde igual.
        """
        for attempt in range(1, attempts + 1):
            price = await self._call(client.tick_price, symbol, direction)
            if price:
                return price
            if attempt < attempts:
                log.warning(
                    "[TM][OPEN] tick_price vacio para %s (intento %d/%d), reintentando",
                    symbol, attempt, attempts,
                )
                await asyncio.sleep(delay_seconds)
        return 0.0

    async def _wait_for_entry_range(self, client, symbol: str, direction: str, initial_price: float, entry_range: tuple) -> Optional[float]:
        """
        Espera a que el precio entre en [min(entry_range), max(entry_range)] antes de
        ejecutar, con tolerancia TOLERANCE_PIPS y ventana entry_wait_seconds/entry_poll_ms
        (reducida a 5s de espera / 100ms de poll para oro, dado su movimiento rapido).
        Retorna el precio con el que ejecutar si entro en rango, o None si nunca entro
        o si ya lo paso en la direccion favorable (no tiene sentido esperar mas).
        """
        entry_lo = float(min(entry_range))
        entry_hi = float(max(entry_range))
        is_buy = direction.upper() == "BUY"
        is_gold = symbol.upper().startswith("XAU")

        cp = self.config_provider
        entry_wait_seconds = float(cp.get("ENTRY_WAIT_SECONDS", 60)) if cp else 60.0
        entry_poll_ms = float(cp.get("ENTRY_POLL_MS", 500)) if cp else 500.0
        tolerance_pips = float(cp.get("TOLERANCE_PIPS", 30)) if cp else 30.0

        entry_wait_max = 5.0 if is_gold else entry_wait_seconds
        entry_poll = 0.1 if is_gold else (entry_poll_ms / 1000.0)

        symbol_info = await self._call(client.symbol_info, symbol)
        point = 0.1 if is_gold else 0.00001
        if symbol_info and getattr(symbol_info, "point", None) is not None:
            point = float(getattr(symbol_info, "point", point))
        pips_tolerance = tolerance_pips * point

        def _price_in_range(p: float) -> bool:
            if is_buy:
                return entry_lo <= p <= entry_hi + pips_tolerance
            return entry_lo - pips_tolerance <= p <= entry_hi

        def _price_past_range(p: float) -> bool:
            if is_buy:
                return p > entry_hi + pips_tolerance
            return p < entry_lo - pips_tolerance

        price = initial_price
        if _price_in_range(price):
            return price
        if _price_past_range(price):
            return None

        deadline = time.time() + entry_wait_max
        while time.time() <= deadline:
            await asyncio.sleep(entry_poll)
            price = await self._call(client.tick_price, symbol, direction.upper())
            if not price:
                continue
            if _price_in_range(price):
                return price
            if _price_past_range(price):
                return None
        return None

    async def open_group(self, account: dict, *, symbol: str, direction: str, sl: float, tp1: Optional[float], tp2: Optional[float], entry_range: Optional[tuple] = None) -> Optional[int]:
        """
        Abre dos posiciones (tp1_leg, runner_leg) con el mismo symbol/direction/SL,
        vinculadas por un group_id nuevo. Ver dual-TP spec seccion 3.
        - tp1/tp2 pueden venir None (senal fast): se abre el par con SL guard,
          sin TP fijo todavia — update_group_signal los completa despues.
        - Si tp1 y tp2 vienen ambos, valida unit=tp2-tp1 en la direccion correcta
          antes de abrir; aborta si unit<=0.
        - entry_range, si viene (min, max), espera a que el precio entre en rango
          antes de ejecutar (hasta entry_wait_seconds, con tolerancia TOLERANCE_PIPS;
          ventana reducida a 5s para oro dado su movimiento rapido) — mismo mecanismo
          que existia en MT5Executor.open_complete_trade antes de la reescritura dual-TP.
        Retorna el group_id nuevo, o None si se aborto (unit invalido, SL invalido,
        sin precio disponible, o el precio nunca entro/ya paso el rango).
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
        await self._call(client.symbol_select, symbol, True)
        price = await self._get_price_with_retry(client, symbol, direction.upper())
        if not price:
            log.error("[TM][OPEN] Abortado: sin precio para %s", symbol)
            await self._notify("open_aborted", symbol=symbol, reason="no_price")
            return None

        if entry_range and len(entry_range) == 2:
            price = await self._wait_for_entry_range(client, symbol, direction, price, entry_range)
            if price is None:
                log.warning("[TM][OPEN] Abortado: precio nunca entro/ya paso el rango de entrada symbol=%s range=%s", symbol, entry_range)
                await self._notify("open_aborted", symbol=symbol, reason="entry_range_missed", entry_range=list(entry_range))
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
            res = await self._call(client.order_send, req)
            if not res or getattr(res, "retcode", None) != 10009:
                log.error("[TM][OPEN] Fallo abriendo leg=%s symbol=%s retcode=%s", leg, symbol, getattr(res, "retcode", None))
                for t in tickets.values():
                    await self._call(client.partial_close, account, t, 100)
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
            # Rescale peak_multiple to the new unit BEFORE overwriting tp1/tp2_price,
            # so a runner already trailing/BE'd doesn't get its progress stranded when
            # tp1/tp2 change (unit = tp2_price - tp1_price changes underneath it).
            if t.leg == "runner" and (tp1 is not None or tp2 is not None) and t.peak_multiple > 0 \
                    and t.tp1_price is not None and t.tp2_price is not None:
                is_buy = t.direction == "BUY"
                old_unit = (t.tp2_price - t.tp1_price) if is_buy else (t.tp1_price - t.tp2_price)
                new_tp1 = float(tp1) if tp1 is not None else t.tp1_price
                new_tp2 = float(tp2) if tp2 is not None else t.tp2_price
                new_unit = (new_tp2 - new_tp1) if is_buy else (new_tp1 - new_tp2)
                if old_unit > 0 and new_unit > 0:
                    # Absolute price distance from the OLD tp1 at the old peak, re-based
                    # onto the new tp1, then re-expressed as a multiple of the new unit.
                    old_peak_distance = t.peak_multiple * old_unit
                    tp1_shift = new_tp1 - t.tp1_price
                    new_peak_distance = old_peak_distance - (tp1_shift if is_buy else -tp1_shift)
                    t.peak_multiple = max(0.0, new_peak_distance / new_unit)

            if sl is not None:
                t.planned_sl = float(sl)
            if tp1 is not None:
                t.tp1_price = float(tp1)
            if tp2 is not None:
                t.tp2_price = float(tp2)

            new_sl = t.planned_sl
            new_tp = t.tp1_price if (t.leg == "tp1" and t.tp1_price is not None) else 0.0

            # Never regress a live SL that's already better than the new planned_sl
            # (e.g. BE-applied or trailed forward) — only write an improvement, or
            # keep the current live SL when the leg has data and it's not an
            # improvement (still send tp updates for the tp1 leg unaffected).
            is_buy = t.direction == "BUY"
            pos_list = await self._call(client.positions_get, ticket=t.ticket)
            current_sl = float(pos_list[0].sl) if pos_list else None
            if current_sl is not None:
                new_is_better = (new_sl > current_sl) if is_buy else (new_sl < current_sl)
                if not new_is_better:
                    log.info("[TM][UPDATE] SL no mejora, se conserva el SL actual | ticket=%s leg=%s current_sl=%s new_sl=%s",
                              t.ticket, t.leg, current_sl, new_sl)
                    new_sl = current_sl

            req = {
                "action": 6,
                "position": t.ticket,
                "sl": float(new_sl),
                "tp": float(new_tp),
            }
            res = await self._call(client.order_send, req)
            ok = bool(res and getattr(res, "retcode", None) == 10009)
            if not ok:
                log.error("[TM][UPDATE] fallo actualizando ticket=%s leg=%s", t.ticket, t.leg)

        log.info("[TM] group %s actualizado: sl=%s tp1=%s tp2=%s", group_id, sl, tp1, tp2)
        await self._notify("group_updated", group_id=group_id, sl=sl, tp1=tp1, tp2=tp2)

    def find_active_group_for_symbol(self, symbol: str) -> Optional[int]:
        """
        Devuelve el group_id mas reciente con al menos una pierna abierta para
        `symbol`, o None (dual-TP spec seccion 5.2 — respuesta 'no_active_trade').
        """
        candidates = [t for t in self.trades.values() if t.symbol == symbol]
        if not candidates:
            return None
        # Tie-break on group_id (an incrementing counter) since time.time() has
        # coarse resolution on some platforms (e.g. ~15.6ms on Windows) and two
        # groups opened back-to-back can share an opened_ts — max() would
        # otherwise return the first (older) tied element.
        newest = max(candidates, key=lambda t: (t.opened_ts, t.group_id))
        return newest.group_id

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
            positions = await self._call(client.positions_get) or []
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
        if runner.entry_price is None:
            log.error("[TM] no se puede aplicar BE: runner=%s no tiene entry_price registrado (group_id=%s)",
                      runner.ticket, tp1_leg.group_id)
            return
        await self._force_runner_sl(account, client, runner, runner.entry_price, reason="TP1-BE")
        runner.be_applied = True
        await self._notify("tp1_hit", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, runner_ticket=runner.ticket)

    async def _force_runner_sl(self, account, client, runner: ManagedTrade, new_sl: float, *, reason: str) -> bool:
        # tp=0.0 explicito: el runner nunca lleva un TP real en MT5 (su unica salida
        # mecanica es el trailing SL) -- omitir "tp" en un request action=6 puede
        # limpiar o preservar el TP existente segun el broker, asi que lo fijamos
        # explicitamente en vez de depender de ese comportamiento implicito.
        req = {"action": 6, "position": runner.ticket, "sl": float(new_sl), "tp": 0.0}
        res = await self._call(client.order_send, req)
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
        if not account:
            log.error("[TM][MGMT] no se pudo resolver la cuenta para group_id=%s symbol=%s", group_id, symbol)
            return {"status": "failed", "group_id": group_id, "reason": "account_unresolved"}
        client = self.mt5._client_for(account)

        if action == "close_now":
            for t in list(legs):
                await self._call(client.partial_close, account, t.ticket, 100)
                self.trades.pop(t.ticket, None)
            await self._notify("mgmt_close_now", group_id=group_id, symbol=symbol, raw_text=raw_text)
            return {"status": "closed", "group_id": group_id}

        if action == "move_sl_be_now":
            runner = next((t for t in legs if t.leg == "runner"), None)
            if not runner:
                return {"status": "no_active_trade"}
            if runner.entry_price is None:
                log.error("[TM][MGMT] move_sl_be_now: runner=%s no tiene entry_price registrado (group_id=%s)",
                          runner.ticket, group_id)
                return {"status": "failed", "group_id": group_id, "reason": "no_entry_price"}
            pos_list = await self._call(client.positions_get, ticket=runner.ticket)
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
