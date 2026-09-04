from .trade_utils import safe_comment, parse_group_comment
import asyncio
import inspect
import os
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
    def __init__(self, mt5_executor, *, notifier=None, config_provider=None, state_store=None):
        self.mt5 = mt5_executor
        self.notifier = notifier
        self.config_provider = config_provider
        self.state_store = state_store
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

    DEFAULT_MT5_CALL_TIMEOUT_SECONDS = 20.0

    @staticmethod
    async def _call(fn, *args, **kwargs):
        """
        Ejecuta una llamada MT5/RPyC sincrona (order_send, positions_get, tick_price,
        symbol_select, symbol_info, partial_close) en un hilo aparte via
        asyncio.to_thread, para no bloquear el event loop compartido por el loop de
        senales, el loop de gestion mecanica (run_forever) y el endpoint /mgmt/action
        cuando MT5 tarda, se cuelga, o esta reconectando (PooledMT5Client puede
        bloquear hasta 1.5s en un intento de reconexion con un lock tomado).

        Envuelto en asyncio.wait_for: RPyC ya trae su propio sync_request_timeout
        (30s por defecto), pero ese timeout vive dentro de AsyncResult.wait(), que
        sigue sirviendo el canal en un loop y puede no cortar de forma confiable si
        el socket sigue "vivo" a nivel TCP sin que el lado Wine/MT5 responda nunca
        (visto en produccion: un fallo de order_send fue seguido por un cuelgue que
        paralizo run_forever por completo, sin ningun log de error ni timeout
        durante minutos). asyncio.wait_for es la garantia dura: si el hilo de
        fondo sigue colgado tras el timeout no hay forma de matarlo (Python no
        puede cancelar un hilo a la fuerza), y seguira reteniendo el
        threading.Lock de PooledMT5Client para ese host:port especifico — pero
        el resto del sistema (otras cuentas, el loop de senales, /mgmt/action)
        deja de esperar indefinidamente por esta unica llamada.

        Timeout configurable via MT5_CALL_TIMEOUT_SECONDS (leido en cada llamada,
        no cacheado, para que un valor invalido en .env no tumbe el arranque).
        """
        timeout = TradeManager.DEFAULT_MT5_CALL_TIMEOUT_SECONDS
        raw_timeout = os.getenv("MT5_CALL_TIMEOUT_SECONDS", "")
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError:
                log.warning("[TM] MT5_CALL_TIMEOUT_SECONDS='%s' invalido, usando default %.0fs",
                            raw_timeout, TradeManager.DEFAULT_MT5_CALL_TIMEOUT_SECONDS)
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("[TM] MT5 call %s colgada tras %.0fs (timeout) — abortando esta operacion, el hilo puede seguir vivo en 2do plano",
                       getattr(fn, "__name__", fn), timeout)
            raise

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
                await self._notify(
                    "open_aborted", symbol=symbol, reason="invalid_unit", tp1=tp1, tp2=tp2,
                    message=f"Señal {direction.upper()} {symbol} no ejecutada: TP1/TP2 inconsistentes con la direccion (tp1={tp1}, tp2={tp2}).",
                )
                return None

        if sl is None or float(sl) == 0.0:
            log.error("[TM][OPEN] Abortado: SL invalido symbol=%s", symbol)
            await self._notify(
                "open_aborted", symbol=symbol, reason="invalid_sl",
                message=f"Señal {direction.upper()} {symbol} no ejecutada: SL invalido o ausente.",
            )
            return None

        client = self.mt5._client_for(account)
        await self._call(client.symbol_select, symbol, True)
        price = await self._get_price_with_retry(client, symbol, direction.upper())
        if not price:
            log.error("[TM][OPEN] Abortado: sin precio para %s", symbol)
            await self._notify(
                "open_aborted", symbol=symbol, reason="no_price",
                message=f"Señal {direction.upper()} {symbol} no ejecutada: no se pudo obtener el precio actual de MT5.",
            )
            return None

        if entry_range and len(entry_range) == 2:
            price = await self._wait_for_entry_range(client, symbol, direction, price, entry_range)
            if price is None:
                log.warning("[TM][OPEN] Abortado: precio nunca entro/ya paso el rango de entrada symbol=%s range=%s", symbol, entry_range)
                lo, hi = float(min(entry_range)), float(max(entry_range))
                await self._notify(
                    "open_aborted", symbol=symbol, reason="entry_range_missed", entry_range=list(entry_range),
                    message=f"Señal {direction.upper()} {symbol} no ejecutada: el precio no entro en el rango de entrada {lo}-{hi} dentro del tiempo de espera.",
                )
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
        await self._persist_group(group_id)
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
        await self._persist_group(group_id)

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

    def group_age_seconds(self, group_id: int) -> Optional[float]:
        """
        Segundos desde que se abrio `group_id` (min opened_ts entre sus piernas),
        o None si el grupo no tiene piernas activas. Usado por handle_signal_fields
        (app.py) para decidir si una señal fast nueva del mismo simbolo es un
        duplicado reciente a ignorar, o una reapertura legitima (BUY o SELL) a
        abrir aparte — ver REOPEN_COOLDOWN_SECONDS.
        """
        legs = [t for t in self.trades.values() if t.group_id == group_id]
        if not legs:
            return None
        return time.time() - min(t.opened_ts for t in legs)

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
                remaining = [t for t in self.trades.values() if t.group_id == closed_trade.group_id]
                if not remaining:
                    await self._close_group_in_store(closed_trade.group_id)

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
        # be_applied SOLO se marca True si el order_send realmente tuvo exito.
        # Bug real de produccion: marcarlo incondicionalmente dejaba el runner en
        # un estado inconsistente cuando el BE fallaba — el guard de _apply_trailing
        # (que exige be_applied) dejaba de bloquearlo, y el trailing intentaba
        # correr sobre un SL que en realidad nunca se movio a breakeven. Reintenta
        # unas pocas veces (mismo patron que _get_price_with_retry) antes de darse
        # por vencido: este es el unico momento en que se dispara el BE — si se
        # pierde aqui sin reintentar, el runner queda huerfano de BE para siempre.
        ok = False
        for attempt in range(1, 4):
            ok = await self._force_runner_sl(account, client, runner, runner.entry_price, reason="TP1-BE")
            if ok:
                break
            if attempt < 3:
                await asyncio.sleep(0.2)
        if ok:
            runner.be_applied = True
            await self._notify("tp1_hit", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, runner_ticket=runner.ticket)
            await self._persist_group(tp1_leg.group_id)
        else:
            log.error("[TM] BE no se pudo aplicar tras 3 intentos, runner=%s group_id=%s queda con SL original",
                      runner.ticket, tp1_leg.group_id)
            await self._notify(
                "tp1_hit_be_failed", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, runner_ticket=runner.ticket,
                message=f"TP1 de {tp1_leg.symbol} (group {tp1_leg.group_id}) se cerro, pero el runner (ticket {runner.ticket}) NO pudo moverse a breakeven tras 3 intentos. Requiere revision manual — sigue con su SL original.",
            )

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
            await self._persist_group(runner.group_id)

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
            await self._close_group_in_store(group_id)
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
                await self._persist_group(group_id)
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

    @staticmethod
    async def _maybe_await(result):
        """
        Devuelve el valor de `result`, awaiteandolo solo si es awaitable.
        Los metodos de TradeStateStore son async, pero reconcile_from_mt5
        tambien se usa contra dobles de test que exponen esos mismos metodos
        de forma sincrona; sin esto, un doble sincrono lanzaria TypeError
        dentro del try/except de cada llamada al store y el fallo quedaria
        enmascarado como un simple warning (perdiendo, por ejemplo, los
        group_ids del store al calcular _next_group_id).
        """
        if inspect.isawaitable(result):
            return await result
        return result

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
                store_group_ids = await self._maybe_await(self.state_store.load_all_group_ids())
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
                    doc, source = await self._maybe_await(self.state_store.load_group(group_id))
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
                await self._maybe_await(self.state_store.compact(active_ids))
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
