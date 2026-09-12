from .trade_utils import safe_comment, parse_group_comment
from .channel_names import resolve_channel_name
from .event_messages import (
    build_sl_hit_message,
    build_external_close_message,
    build_group_opened_message,
    build_tp1_hit_message,
    build_tp2_partial_closed_message,
    build_close_now_message,
    build_move_sl_be_applied_message,
    build_partial_failure_message,
    build_close_partial_now_message,
    build_tp1_hit_be_failed_message,
    build_tp1_hit_be_timeout_message,
    build_tp2_partial_timeout_message,
)
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


class MT5CallTimeoutError(Exception):
    """Raised when an MT5 order_send/partial_close call times out (asyncio.TimeoutError
    from TradeManager._call) rather than receiving a clean rejection from MT5. Distinct
    from a clean failure: a timeout means MT5 never responded in time, not that it said no
    -- the underlying action may still complete in the background (see TradeManager._call's
    docstring). Callers that notify a business event on failure must treat this differently
    from a clean rejection (see spec 2026-09-11-mt5-timeout-notification-safety-design.md)."""
    pass


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
    tp2_partial_applied: bool = False
    # Latch del guard de TP2: volumen con el que ya se determino que el cierre
    # parcial del 50% NO era honrable (ver _apply_tp2_partial_close). Existe
    # SOLO para no repetir esa consulta a MT5 y su WARNING en cada tick del
    # loop (10 veces por segundo, indefinidamente) — no es estado de negocio.
    # Guarda el volumen evaluado, no un bool, para que el veredicto deje de
    # aplicarse por si solo si el volumen vivo llegara a cambiar. Deliberadamente
    # NO se persiste en _group_doc: tras un restart, re-evaluarlo una vez es
    # correcto y barato.
    tp2_partial_skipped_volume: Optional[float] = None
    peak_multiple: float = 0.0
    opened_ts: float = field(default_factory=lambda: time.time())
    chat_id: Optional[str] = None

    @property
    def tp2_partial_skipped(self) -> bool:
        """True si el guard de TP2 ya descarto el cierre parcial para esta
        pierna (lectura conveniente del latch tp2_partial_skipped_volume)."""
        return self.tp2_partial_skipped_volume is not None


class TradeManager:
    def __init__(self, mt5_executor, *, notifier=None, event_bus=None, config_provider=None, state_store=None, channel_names=None):
        self.mt5 = mt5_executor
        self.notifier = notifier
        self.event_bus = event_bus
        self.config_provider = config_provider
        self.state_store = state_store
        self.channel_names = channel_names or {}
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

    async def _notify(self, event: str, *, channel: str = "audit", **kwargs) -> None:
        log.info("[TM][EVENT] %s %s", event, kwargs)
        message = kwargs.pop("message", "")
        if self.event_bus is not None:
            try:
                await self.event_bus.emit(event, channel, message, kwargs)
            except Exception as e:
                log.warning("[TM] event_bus.emit failed for event=%s: %s", event, e)
            return
        if not self.notifier:
            return
        try:
            await self.notifier.notify_trade_event(event, message=message, **kwargs)
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
                "tp2_partial_applied": t.tp2_partial_applied,
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

    async def open_group(self, account: dict, *, symbol: str, direction: str, sl: float, tp1: Optional[float], tp2: Optional[float], entry_range: Optional[tuple] = None, chat_id: Optional[str] = None) -> Optional[int]:
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
        - chat_id, si viene, se guarda en ambas piernas del grupo (dual-TP
          spec + chat_id-scoping spec seccion 3) — identifica el canal de
          Telegram que origino la senal, usado por /mgmt/action para
          resolver a que grupos aplicar una accion de gestion. None si la
          senal no trae chat_id (legacy) o si open_group se llama sin el
          (p. ej. en tests existentes) -- un grupo con chat_id=None queda
          huerfano de gestion automatica via /mgmt/action.
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
                await self._notify(
                    "open_failed", symbol=symbol, leg=leg, group_id=group_id,
                    message=f"Grupo {group_id} ({symbol}): fallo abriendo la pierna '{leg}' en MT5 "
                            f"(retcode={getattr(res, 'retcode', None)}). Se revirtieron las piernas ya abiertas del grupo.",
                )
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
                chat_id=chat_id,
            )
        TRADES_OPENED.inc(2)
        ACTIVE_TRADES.set(len(self.trades))
        log.info("[TM] group %s opened: tp1=%s runner=%s symbol=%s dir=%s sl=%s tp1_price=%s tp2_price=%s",
                  group_id, tickets["tp1"], tickets["runner"], symbol, direction, sl, tp1, tp2)
        channel_name = resolve_channel_name(chat_id, self._channel_names())
        message = build_group_opened_message(
            channel_name=channel_name, group_id=group_id, symbol=symbol, direction=direction,
            entry_price=price, sl=sl, tp1=tp1, tp2=tp2, volume=account.get("fixed_lot", 0.01),
        )
        await self._notify(
            "group_opened", channel="both", group_id=group_id, symbol=symbol, direction=direction,
            tp1_ticket=tickets["tp1"], runner_ticket=tickets["runner"], sl=sl, tp1=tp1, tp2=tp2,
            chat_id=chat_id, channel_name=channel_name, entry_price=price, volume=account.get("fixed_lot", 0.01),
            message=message,
        )
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
            # ONLY once real management (BE/trailing) has actually moved it — that's
            # what be_applied tracks. Before that, the live SL is just the fast
            # signal's wide default protective SL (or the still-unmoved real SL),
            # not an earned improvement, so the incoming signal's SL must always
            # win even if it looks numerically "worse" (narrower/closer to price)
            # than that placeholder. Real production bug: comparing unconditionally
            # left both legs stuck on the fast default forever, since a fast
            # signal's default SL is deliberately wide and a real signal's SL is
            # usually narrower.
            is_buy = t.direction == "BUY"
            pos_list = await self._call(client.positions_get, ticket=t.ticket)
            current_sl = float(pos_list[0].sl) if pos_list else None
            if current_sl is not None and t.be_applied:
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
        await self._notify(
            "group_updated", group_id=group_id, sl=sl, tp1=tp1, tp2=tp2,
            message=f"Grupo {group_id} actualizado: sl={sl}, tp1={tp1}, tp2={tp2}.",
        )
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

    DEAL_REASON_TP = 5
    DEAL_REASON_SL = 4

    async def _classify_leg_closure(self, client, closed_trade: "ManagedTrade") -> dict:
        """
        Clasifica por que se cerro una pierna que desaparecio de
        positions_get, usando deal.reason directamente (no heuristica de
        tolerancia de precio -- ver spec 2026-09-10, seccion 5). Los
        cierres que el propio TradeManager origina de forma sincrona
        (close_now, close_partial_now, TP2 partial) ya remueven el ticket
        de self.trades ANTES de que este metodo se llame -- por eso
        cualquier otra causa detectada aqui (fuera de TP genuino y SL) se
        trata como 'external': nadie del sistema lo pidio.
        """
        info = await self._get_close_deal_info(client, closed_trade.ticket)
        if info is None:
            # Deal aun no propago o fallo de red -- comportamiento previo:
            # asumir TP1 para no bloquear el BE automatico en el caso comun.
            return {"cause": "tp1" if closed_trade.leg == "tp1" else "unknown", "price": None,
                    "reason": None, "profit": None, "volume": None, "commission": None, "swap": None}
        reason = info["reason"]
        if reason == self.DEAL_REASON_TP and closed_trade.leg == "tp1":
            cause = "tp1"
        elif reason == self.DEAL_REASON_SL:
            cause = "sl"
        elif reason is not None:
            # A real deal was found with a real reason that's neither TP nor
            # SL (typically DEAL_REASON_CLIENT) -- since close_now/
            # close_partial_now/TP2-partial already remove the ticket from
            # self.trades synchronously before this code ever runs (see the
            # module docstring note above), this really is an outside close.
            cause = "external"
        else:
            # reason is None: the deal was found but MT5 didn't report a
            # reason (or the simulator/test double left it unset). Too
            # uncertain to raise a security alert over -- fall back to the
            # old undifferentiated audit-only event instead of guessing.
            cause = "unknown"
        return {"cause": cause, **info}

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

    async def _get_close_deal_info(self, client, ticket: int) -> Optional[dict]:
        """
        Busca el deal real de salida (DEAL_ENTRY_OUT=1) de `ticket` en el
        historial de MT5, con toda la informacion necesaria para
        auditoria/notificacion: precio, causa (reason), P&L real, volumen
        cerrado, comision y swap. Nunca debe tumbar el flujo de notificacion
        -- cualquier fallo (de red, o el deal aun no propago) devuelve None.
        """
        try:
            deals = await self._call(client.history_deals_get, position=ticket)
        except Exception as e:
            log.warning("[TM] fallo obteniendo historial de deals para ticket=%s: %s", ticket, e)
            return None
        if not deals:
            return None
        out_deals = [d for d in deals if getattr(d, "entry", None) == 1]
        if not out_deals:
            return None
        closing = max(out_deals, key=lambda d: getattr(d, "time", 0))
        return {
            "price": float(closing.price),
            "reason": getattr(closing, "reason", None),
            "profit": float(getattr(closing, "profit", 0.0) or 0.0),
            "volume": float(getattr(closing, "volume", 0.0) or 0.0),
            "commission": float(getattr(closing, "commission", 0.0) or 0.0),
            "swap": float(getattr(closing, "swap", 0.0) or 0.0),
        }

    async def _get_close_price(self, client, ticket: int) -> Optional[float]:
        """Compat: varios call sites solo necesitan el precio de cierre."""
        info = await self._get_close_deal_info(client, ticket)
        return info["price"] if info else None

    def _channel_names(self) -> dict:
        return getattr(self, "channel_names", {}) or {}

    @staticmethod
    def _fmt_price(price: Optional[float]) -> str:
        return f"{price:.5f}" if price is not None else "N/D"

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

    async def _check_partial_close_is_honourable(self, client, ticket: int, symbol: str, percent: float) -> Optional[dict]:
        """
        Predice si MT5 cerraria EXACTAMENTE la fraccion pedida de `ticket`, o
        si su clamp de volumen minimo terminaria cerrando algo distinto.
        Devuelve None si el pedido es honrable tal cual; si no, un dict con
        {volume, close_vol, volume_min, reason} describiendo por que no lo es.

        Bug real de produccion (dinero): services/common/mt5_client.py's
        partial_close calcula close_vol = step * int(raw_close / step) y, si
        eso queda por debajo de volume_min, lo SUBE a min_vol — o, cuando la
        posicion entera no supera min_vol, al VOLUMEN COMPLETO. Con el default
        documentado de produccion (fixed_lot=0.01 == volume_min=0.01),
        `volume > min_vol` es siempre False, asi que CUALQUIER pedido de
        cierre parcial cerraba el 100% de la posicion sin que nadie lo pidiera.

        La matematica de aqui replica linea por linea la de mt5_client.py a
        proposito: si divergiera, la validacion dejaria de predecir lo que MT5
        realmente haria, que es justamente lo que la hace util. La decision de
        producto (confirmada con el usuario) es rechazar antes de tocar MT5,
        nunca cerrar silenciosamente mas ni menos de lo pedido.
        """
        pos_list = await self._call(client.positions_get, ticket=ticket)
        if not pos_list:
            return {"volume": None, "close_vol": None, "volume_min": None, "reason": "position_not_found"}
        volume = float(getattr(pos_list[0], "volume", 0.0) or 0.0)
        info = await self._call(client.symbol_info, symbol)
        step = float(getattr(info, "volume_step", 0.01)) if info else 0.01
        min_vol = float(getattr(info, "volume_min", 0.01)) if info else 0.01
        if volume <= 0:
            return {"volume": volume, "close_vol": None, "volume_min": min_vol, "reason": "invalid_volume"}
        raw_close = volume * (float(percent) / 100.0)
        close_vol = step * int(raw_close / step)
        # Tolerancia de 1e-9: raw_close/step en floats puede dar 4.999999999
        # para lo que conceptualmente es 5 pasos exactos, y int() truncaria
        # un paso de mas — el mismo riesgo existe en MT5, pero aqui preferimos
        # no rechazar un pedido valido por ruido de punto flotante.
        if abs(round(raw_close / step) - (raw_close / step)) < 1e-9:
            close_vol = step * round(raw_close / step)
        remaining = volume - close_vol
        if close_vol < min_vol - 1e-9:
            return {"volume": volume, "close_vol": close_vol, "volume_min": min_vol, "reason": "close_below_min"}
        if remaining < min_vol - 1e-9:
            return {"volume": volume, "close_vol": close_vol, "volume_min": min_vol, "reason": "remainder_below_min"}
        return None

    async def _apply_tp2_partial_close(self, account, client, runner: ManagedTrade, pos) -> None:
        """
        TP2 partial close (product decision 2026-09-08, dual-TP spec seccion 4
        ampliada): la primera vez que el precio en vivo del runner alcanza
        tp2_price, se cierra el 50% de su volumen VIVO en ese momento (mismo
        patron/helper partial_close ya usado en el resto del codigo para
        cierres totales), una sola vez por grupo (flag tp2_partial_applied,
        mismo patron que be_applied). El 50% restante sigue el trailing
        exactamente igual que hoy -- tp2 no se vuelve un nuevo ancla, y
        peak_multiple/SL no se tocan aqui en absoluto.

        Motivacion: antes de esto, tp2_price era puramente decorativo para el
        runner (solo define `unit`, la escala del trailing) -- nunca se
        tomaba ganancia ahi. Simulado contra escenarios reales: asegurar la
        mitad en tp2 gana sistematicamente cuando el precio revierte despues
        de tocar tp2 (protege una porcion de la ganancia que el trailing,
        deliberadamente lento en alcanzar el precio, dejaria expuesta) y solo
        cuesta rendimiento cuando el precio sigue corriendo sin revertir --
        pero ese costo esta acotado a la mitad del volumen, mientras que la
        ganancia protegida en una reversion suele superarlo (ver discusion y
        simulaciones de la sesion 2026-09-08).

        Disparo simple (price>=tp2 para BUY, price<=tp2 para SELL), sin
        umbral de confirmacion -- decision explicita del usuario para
        mantenerlo fiel a la mecanica tal como fue especificada.
        """
        if runner.tp2_partial_applied or runner.tp2_price is None:
            return
        is_buy = runner.direction == "BUY"
        current = float(pos.price_current)
        reached_tp2 = (current >= runner.tp2_price) if is_buy else (current <= runner.tp2_price)
        if not reached_tp2:
            return
        # Latch: si ya se determino que el parcial no es honrable PARA ESTE
        # MISMO volumen, no se vuelve a consultar a MT5 ni se re-loguea.
        #
        # Bug real introducido por el propio fix de TP2 (encontrado en la
        # re-revision): tp2_partial_applied se queda en False a proposito
        # (nada paso en MT5), asi que sin este latch el guard se re-evaluaba
        # en CADA tick — LOOP_INTERVAL=0.1s, o sea 10 veces por segundo — por
        # el resto de la vida del runner. En la cuenta de produccion
        # (fixed_lot=0.01, donde el skip SIEMPRE se dispara) eso son ~36k
        # llamadas extra a positions_get y ~36k lineas WARNING por hora, por
        # runner. positions_get toma el lock de PooledMT5Client, que tiene un
        # bug conocido en vivo (el lock no se libera si la llamada hace
        # timeout), asi que esto aumentaba la exposicion a ese bug
        # justamente en el camino de produccion por defecto.
        #
        # Se compara contra el volumen del snapshot `pos` que ya tenemos en
        # mano (gratis, sin RPC): si el volumen no cambio, el veredicto
        # anterior sigue siendo valido y no hay nada que reconsiderar.
        current_volume = getattr(pos, "volume", None)
        if runner.tp2_partial_skipped_volume is not None and current_volume is not None \
                and abs(float(current_volume) - runner.tp2_partial_skipped_volume) < 1e-9:
            return
        # Mismo bug de dinero que Fix 1, en el segundo camino de codigo que
        # llama partial_close con un porcentaje: con el default de produccion
        # (fixed_lot=0.01 == volume_min=0.01), el clamp de mt5_client.py
        # convierte este 50% en un cierre del 100% y el runner desaparece
        # entero al tocar TP2, en vez de quedarse con la mitad haciendo
        # trailing. Si el parcial no se puede honrar exactamente, se omite y
        # el runner sigue con su volumen completo bajo trailing — nunca se
        # cierra mas de lo que la mecanica pide.
        problem = await self._check_partial_close_is_honourable(client, runner.ticket, runner.symbol, 50)
        if problem is not None:
            # Latchear ANTES de loguear, sobre el volumen que realmente se
            # evaluo (el que vio el helper), cayendo al del snapshot si el
            # helper no pudo leerlo (posicion ya inexistente).
            evaluated_volume = problem["volume"]
            if evaluated_volume is None:
                evaluated_volume = float(current_volume) if current_volume is not None else -1.0
            runner.tp2_partial_skipped_volume = float(evaluated_volume)
            log.warning("[TM] TP2 partial close omitido: cerrar 50%% de %s dejaria un volumen menor al minimo "
                        "operable %s (runner=%s group_id=%s motivo=%s) — el runner sigue completo con trailing. "
                        "No se repetira este chequeo mientras el volumen no cambie.",
                        problem["volume"], problem["volume_min"], runner.ticket, runner.group_id, problem["reason"])
            return
        # El parcial es honrable: cualquier latch previo quedo obsoleto.
        runner.tp2_partial_skipped_volume = None
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
        runner.tp2_partial_applied = True
        deal_info = await self._get_close_deal_info(client, runner.ticket)
        channel_name = resolve_channel_name(runner.chat_id, self._channel_names())
        close_price = deal_info["price"] if deal_info else None
        pnl_money = deal_info["profit"] if deal_info else None
        close_volume = deal_info["volume"] if deal_info else None
        # Fix 6: `pos` es un snapshot tomado ANTES de partial_close, asi que su
        # .volume es el volumen PRE-cierre. Usarlo producia un mensaje
        # auto-contradictorio ("Cerrado 50%: 0.05 lots ... 0.1 lots restantes"
        # cuando en realidad quedaban 0.05). Se re-lee la posicion viva
        # despues del cierre — mismo patron que usa _tick_once_account tras
        # llamar a este metodo. Se prefiere la re-lectura sobre calcular
        # volume*0.5 para no atarse a que el 50% de arriba nunca cambie.
        post_close = await self._call(client.positions_get, ticket=runner.ticket)
        remaining_volume = float(post_close[0].volume) if post_close else getattr(pos, "volume", None)
        message = build_tp2_partial_closed_message(
            channel_name=channel_name, group_id=runner.group_id, symbol=runner.symbol, direction=runner.direction,
            close_price=close_price, close_volume=close_volume, pnl_money=pnl_money, remaining_volume=remaining_volume,
        )
        await self._notify(
            "tp2_partial_closed", channel="both", group_id=runner.group_id, ticket=runner.ticket, symbol=runner.symbol,
            chat_id=runner.chat_id, channel_name=channel_name, close_price=close_price,
            close_volume=close_volume, pnl_money=pnl_money, remaining_volume=remaining_volume,
            message=message,
        )
        await self._persist_group(runner.group_id)

    async def _apply_trailing(self, account, client, runner: ManagedTrade, pos) -> None:
        """
        Trailing proporcional sin techo (dual-TP spec seccion 4, revisado
        2026-09-09): unit = tp2_price - tp1_price (constante, mide el avance
        del precio en "unidades de tp2" mas alla de tp1, multiple = avance/unit,
        peak = maximo multiple historico, nunca decrece); SL = entry_price +
        multiple * (tp1_price - entry_price).

        Ancla en entry_price (BE), NO en tp1_price (fix 2026-09-08 — ver nota
        vieja mas abajo). Y el offset del SL escala con (tp1_price -
        entry_price), NO con unit (fix 2026-09-09): usar unit como escala del
        offset acoplaba dos distancias sin relacion necesaria entre si —
        cuanto tiene que "recorrer" el SL para llegar a tp1 (entry->tp1,
        determinado por el riesgo de la señal) vs. cuanto se separan tp1 y
        tp2 (unit, una decision de escala independiente de la señal). Caso
        real (grupo 61, 2026-09-08 en produccion): entry->tp1=34.87pts,
        unit=40pts — con el offset viejo (multiple*unit/3), al tocar tp2
        exacto (multiple=1.0) el SL solo habia recorrido unit/3=13.3pts de
        esos 34.87, quedando a 21.5pts de tp1 en vez de cerca como se
        esperaria intuitivamente. Con unit mucho mas chico que entry->tp1
        (tp1/tp2 muy juntos) el desacople es aun peor: el SL podia tardar
        multiples de 20+ en alcanzar tp1. Escalando el offset por
        (tp1_price - entry_price) en vez de unit, el SL SIEMPRE alcanza
        tp1_price exactamente en multiple=1.0 (precio en tp2), sin importar
        la relacion entre unit y la distancia entry->tp1 — resuelve el
        acople de raiz. unit sigue siendo la escala de multiple (que tan
        lejos mas alla de tp1, en "unidades tp2", esta el precio) — solo el
        offset del SL dejo de usarla.

        Nota vieja (2026-09-08): la formula original del spec anclaba el SL
        en tp1_price, lo que dejaba el SL a 0-3 puntos del precio vivo justo
        al cruzar TP1 (peak cerca de 0) -- mas cerca que el propio BE (~10
        puntos de colchon en valores tipicos), y ese colchon minimo es
        tambien lo que hace mas probable el rechazo por trade_stops_level
        que ya vimos en produccion (grupo 60). Un retroceso de precio del
        todo normal recien despues de TP1 bastaba para tocar ese SL y cerrar
        el runner casi junto con tp1_leg. Anclar en entry_price hace que en
        peak=0 el SL sea exactamente el BE (mismo colchon que ya se gano al
        cerrar tp1_leg).
        """
        if runner.tp1_price is None or runner.tp2_price is None or runner.entry_price is None:
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
        # Offset scaled by the entry->tp1 distance (the ground the SL actually
        # has to cover to "reach" tp1), not by unit (tp1->tp2, an unrelated
        # scale) — see docstring above. entry_to_tp1 can be 0 in a degenerate
        # case (fill price landed exactly on tp1); guarded to avoid a SL that
        # never advances silently masking as "trailing is working".
        entry_to_tp1 = (runner.tp1_price - runner.entry_price) if is_buy else (runner.entry_price - runner.tp1_price)
        if entry_to_tp1 <= 0:
            return
        sl_offset = multiple * entry_to_tp1
        new_sl = runner.entry_price + sl_offset if is_buy else runner.entry_price - sl_offset
        # new_sl is always strictly better than entry_price (peak_multiple
        # never negative, "never decreases" guard above already requires
        # multiple > peak_multiple >= 0 here, sl_offset > 0) — but that does
        # NOT mean new_sl is never worse than the SL already live in MT5.
        # Real production bug (2026-09-09, found while re-testing the
        # signal_correction path after the entry_to_tp1 rescale of the
        # offset): update_group_signal's peak_multiple rescale (a few dozen
        # lines up) re-projects peak_multiple correctly for the "never
        # decreases" GATE (multiple > peak_multiple) when tp1/tp2 change —
        # that part is still right. But once unit changes a lot (e.g. a
        # signal_correction moving tp2 far away), a multiple that newly
        # clears that gate can still map, through entry_to_tp1 (which the
        # rescale does NOT touch), to a new_sl in absolute points BELOW the
        # SL already sitting in MT5 (observed: SL=2515 live, next tick's
        # "valid" multiple computed new_sl=2500.41 — a real regression the
        # old tp1_price-anchored/unit-scaled formula never produced, because
        # back then preserving peak_multiple*unit under rescale WAS
        # preserving the SL). Guard directly against the live SL instead of
        # trying to keep the rescale perfectly in sync with the offset
        # formula — same pattern update_group_signal's own SL write and
        # move_sl_be_now already use.
        current_sl = float(getattr(pos, "sl", 0.0) or 0.0)
        if current_sl:
            candidate_is_worse = (new_sl < current_sl) if is_buy else (new_sl > current_sl)
            if candidate_is_worse:
                return
        # peak_multiple must only advance once the SL move actually lands in MT5.
        # Real production bug (group 60, live): peak_multiple was bumped here
        # unconditionally, before knowing whether order_send succeeded. When MT5
        # rejected the candidate SL (e.g. too close to trade_stops_level right
        # after TP1), the real SL stayed frozen at the last level that *did*
        # land, while this in-memory peak kept climbing — so the "never
        # decreases" guard above then silently discarded subsequent price
        # levels MT5 would have accepted, and a trailing_updated notification
        # fired for an SL that was never actually applied. Same failure mode
        # _apply_be already guards against for the breakeven move.
        ok = await self._force_runner_sl(account, client, runner, new_sl, reason="trailing")
        if ok:
            runner.peak_multiple = multiple
            # Same fix as _on_tp1_leg_closed/move_sl_be_now: planned_sl was
            # never kept in sync with the SL actually applied here either --
            # only peak_multiple advanced. MT5's real SL was correct, but
            # _group_doc persists the stale planned_sl, and reconcile_from_mt5
            # rebuilds the runner with it after any restart.
            runner.planned_sl = new_sl
            log.info(f"Trailing SL actualizado para el runner del grupo {runner.group_id} (ticket={runner.ticket}): nuevo sl={self._fmt_price(new_sl)}, peak_multiple={multiple:.2f}.")
            await self._persist_group(runner.group_id)

    async def apply_mgmt_action(self, *, action: str, chat_id: str, raw_text: str, correction: Optional[dict], percent: Optional[float] = None) -> dict:
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
                    channel_name = resolve_channel_name(chat_id, self._channel_names())
                    leg_summaries = []
                    leg_results = []
                    any_leg_failed = False
                    for t in list(legs):
                        ok = await self._call(client.partial_close, account, t.ticket, 100)
                        if not ok:
                            any_leg_failed = True
                            log.error("[TM][MGMT] partial_close rechazado por el broker | ticket=%s leg=%s group_id=%s",
                                      t.ticket, t.leg, group_id)
                            continue
                        deal_info = await self._get_close_deal_info(client, t.ticket)
                        close_price = deal_info["price"] if deal_info else None
                        pnl_money = deal_info["profit"] if deal_info else None
                        close_volume = deal_info["volume"] if deal_info else None
                        leg_summaries.append(
                            f"{t.leg} (ticket={t.ticket}, apertura {self._fmt_price(t.entry_price)}, "
                            f"cierre {self._fmt_price(close_price)})"
                        )
                        leg_results.append({
                            "leg": t.leg, "close_price": close_price,
                            "close_volume": close_volume, "pnl_money": pnl_money,
                        })
                        self.trades.pop(t.ticket, None)
                    if any_leg_failed:
                        message = build_partial_failure_message(
                            channel_name=channel_name, group_id=group_id, leg_summaries=leg_summaries,
                        )
                        await self._notify(
                            "mgmt_close_now_partial_failure", channel="both", group_id=group_id, chat_id=chat_id,
                            channel_name=channel_name, raw_text=raw_text, message=message,
                        )
                        results.append({"group_id": group_id, "status": "failed", "reason": "partial_close_rejected"})
                        continue
                    total_pnl_money = sum(lr["pnl_money"] for lr in leg_results if lr["pnl_money"] is not None)
                    message = build_close_now_message(
                        channel_name=channel_name, group_id=group_id, raw_text=raw_text,
                        leg_results=leg_results, total_pnl_money=total_pnl_money,
                    )
                    await self._notify(
                        "mgmt_close_now", channel="both", group_id=group_id, chat_id=chat_id,
                        channel_name=channel_name, raw_text=raw_text,
                        # Fix 5: sin estos kwargs, leg_results/total_pnl_money solo
                        # sobrevivian como prosa dentro de `message` y nunca llegaban
                        # al payload de auditoria ni a la Data Table de n8n.
                        # mgmt_close_partial_now ya lo hacia bien; este no.
                        leg_results=leg_results, total_pnl_money=total_pnl_money,
                        message=message,
                    )
                    await self._close_group_in_store(group_id)
                    results.append({"group_id": group_id, "status": "closed"})
                except Exception as e:
                    log.error("[TM][MGMT] excepcion cerrando group_id=%s chat_id=%s: %s", group_id, chat_id, e)
                    results.append({"group_id": group_id, "status": "failed", "reason": "exception"})
            return {"status": "completed", "results": results}

        if action == "close_partial_now":
            # Fix 2: `percent` viene de una extraccion LLM (Ollama) sobre texto
            # libre, asi que un valor basura es una entrada realista. mgmt_api
            # ya lo valida con Pydantic, pero apply_mgmt_action es parte de la
            # API interna publica (tests y cualquier caller futuro la llaman
            # directo): nunca confiar en una validacion que solo vive en el
            # borde de red para una funcion que tambien se invoca por dentro.
            if percent is not None and not (0 < percent < 100):
                log.error("[TM][MGMT] close_partial_now con percent invalido=%s chat_id=%s text=%r",
                          percent, chat_id, raw_text[:80])
                await self._notify(
                    "mgmt_invalid_percent", chat_id=chat_id, action=action, percent=percent, raw_text=raw_text,
                    message=f"Cierre parcial solicitado con un porcentaje invalido ({percent}). "
                            f"Debe ser mayor a 0 y menor a 100. No se toco ninguna posicion. "
                            f"Texto: {raw_text!r}",
                )
                return {"status": "invalid_percent", "percent": percent}
            effective_percent = percent if percent is not None else 50.0
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
                    channel_name = resolve_channel_name(chat_id, self._channel_names())
                    leg_results = []
                    any_leg_failed = False
                    leg_summaries = []
                    for t in list(legs):
                        # Fix 1: validar POR PIERNA (cada una tiene su propio
                        # volumen vivo) antes de tocar MT5 — ver
                        # _check_partial_close_is_honourable para el bug de
                        # dinero que esto evita.
                        problem = await self._check_partial_close_is_honourable(
                            client, t.ticket, t.symbol, effective_percent,
                        )
                        if problem is not None:
                            any_leg_failed = True
                            # El texto debe nombrar la causa REAL. Antes decia
                            # siempre "volumen menor al minimo operable" con
                            # volumen/minimo en None cuando en realidad la
                            # posicion ya no existia (p. ej. el SL salto justo
                            # antes de que llegara el comando) — engañoso para
                            # el operador, aunque el comportamiento de fondo
                            # (abstenerse de actuar) siempre fue el correcto.
                            if problem["reason"] == "position_not_found":
                                detail = "la posicion ya no existe en MT5 (pudo cerrarse por SL/TP o externamente)"
                            elif problem["reason"] == "invalid_volume":
                                detail = "MT5 reporta un volumen invalido para la posicion"
                            else:
                                detail = (f"{effective_percent:.0f}% de {problem['volume']} resultaria en un "
                                          f"volumen menor al minimo operable {problem['volume_min']}")
                            leg_summaries.append(f"{t.leg} (ticket={t.ticket}, rechazado: {detail})")
                            log.error("[TM][MGMT] close_partial_now rechazado | ticket=%s leg=%s "
                                      "group_id=%s percent=%s volume=%s close_vol=%s volume_min=%s motivo=%s",
                                      t.ticket, t.leg, group_id, effective_percent, problem["volume"],
                                      problem["close_vol"], problem["volume_min"], problem["reason"])
                            continue
                        ok = await self._call(client.partial_close, account, t.ticket, effective_percent)
                        if not ok:
                            any_leg_failed = True
                            leg_summaries.append(f"{t.leg} (ticket={t.ticket}, rechazado)")
                            log.error("[TM][MGMT] partial_close (parcial %.0f%%) rechazado | ticket=%s leg=%s group_id=%s",
                                      effective_percent, t.ticket, t.leg, group_id)
                            continue
                        deal_info = await self._get_close_deal_info(client, t.ticket)
                        leg_results.append({
                            "leg": t.leg,
                            "close_price": deal_info["price"] if deal_info else None,
                            "close_volume": deal_info["volume"] if deal_info else None,
                            "pnl_money": deal_info["profit"] if deal_info else None,
                        })
                    if any_leg_failed:
                        message = build_partial_failure_message(channel_name=channel_name, group_id=group_id, leg_summaries=leg_summaries)
                        await self._notify(
                            "mgmt_close_partial_now_failure", channel="both", group_id=group_id, chat_id=chat_id,
                            channel_name=channel_name, raw_text=raw_text, percent_requested=effective_percent,
                            leg_summaries=leg_summaries, message=message,
                        )
                        results.append({"group_id": group_id, "status": "failed", "reason": "partial_close_rejected"})
                        continue
                    message = build_close_partial_now_message(
                        channel_name=channel_name, group_id=group_id, raw_text=raw_text,
                        percent_requested=effective_percent, leg_results=leg_results,
                    )
                    await self._notify(
                        "mgmt_close_partial_now", channel="both", group_id=group_id, chat_id=chat_id,
                        channel_name=channel_name, raw_text=raw_text, percent_requested=effective_percent,
                        leg_results=leg_results, message=message,
                    )
                    results.append({"group_id": group_id, "status": "applied"})
                except Exception as e:
                    log.error("[TM][MGMT] excepcion en close_partial_now group_id=%s chat_id=%s: %s", group_id, chat_id, e)
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
                    try:
                        ok = await self._force_runner_sl(account, client, runner, be_price, reason="mgmt-fallback-BE")
                    except MT5CallTimeoutError:
                        log.error("[TM][MGMT] timeout aplicando BE via mgmt_action group_id=%s chat_id=%s", group_id, chat_id)
                        results.append({"group_id": group_id, "status": "timeout"})
                        continue
                    if ok:
                        runner.be_applied = True
                        # Same fix as _on_tp1_leg_closed: keep planned_sl in
                        # sync with the real, just-applied BE price -- see
                        # that call site's comment for why this matters
                        # across a restart's reconcile_from_mt5.
                        runner.planned_sl = be_price
                        channel_name = resolve_channel_name(chat_id, self._channel_names())
                        message = build_move_sl_be_applied_message(
                            channel_name=channel_name, group_id=group_id, new_sl=be_price, raw_text=raw_text,
                        )
                        await self._notify(
                            "mgmt_move_sl_be_applied", channel="both", group_id=group_id, chat_id=chat_id,
                            channel_name=channel_name, raw_text=raw_text, message=message,
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
        # store_errors cuenta cada fallo silenciado de una llamada al store. Los
        # try/except de este metodo existen para que un store roto nunca tumbe el
        # arranque, pero sin este contador esa degradacion solo aparece en los logs
        # — incluirlo en el summary lo sube a la notificacion de n8n, donde una
        # persona puede verlo.
        summary = {"recovered_from_redis": 0, "recovered_from_file": 0, "degraded": 0,
                   "orphaned": [], "store_errors": 0}
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
                summary["store_errors"] += 1
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
                    summary["store_errors"] += 1
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
            # Real production bug: doc["legs"] only still has "tp1" if THIS
            # doc predates tp1's close (i.e. it closed during the current
            # downtime). Once _on_tp1_leg_closed runs once (live, in a
            # previous process lifetime) and re-persists the group, its own
            # _group_doc only ever includes legs still in self.trades — "tp1"
            # is gone from the doc for good. Without this guard, a restart
            # any time after that permanently crash-loops reconcile_from_mt5
            # with a KeyError, since doc["legs"]["tp1"] no longer exists even
            # though mt5_tp1 is (correctly) still None.
            if doc is not None and mt5_tp1 is None and mt5_runner is not None and "tp1" in doc["legs"]:
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
                    try:
                        await self._close_group_in_store(group_id)
                    except Exception as e:
                        summary["store_errors"] += 1
                        log.warning("[TM][RECONCILE] fallo cerrando group_id=%s en el store: %s", group_id, e)

            # Re-persist to Redis anything that wasn't already there (recovered from the
            # file backup, or reconstructed in degraded mode) — so a subsequent restart
            # that keeps Redis intact recovers fully from layer 1 next time.
            if source != "redis" and self.state_store:
                group_still_has_legs = any(t.group_id == group_id for t in self.trades.values())
                if group_still_has_legs:
                    try:
                        await self._persist_group(group_id)
                    except Exception as e:
                        summary["store_errors"] += 1
                        log.warning("[TM][RECONCILE] fallo re-persistiendo group_id=%s en el store: %s", group_id, e)

        self._next_group_id = highest_group_id_seen + 1
        ACTIVE_TRADES.set(len(self.trades))

        if self.state_store:
            try:
                active_ids = {t.group_id for t in self.trades.values()}
                await self._maybe_await(self.state_store.compact(active_ids))
            except Exception as e:
                summary["store_errors"] += 1
                log.warning("[TM][RECONCILE] fallo compactando el store: %s", e)

        log.info("[TM][RECONCILE] completado: recuperados_redis=%s recuperados_archivo=%s degradados=%s huerfanos=%s errores_store=%s",
                  summary["recovered_from_redis"], summary["recovered_from_file"], summary["degraded"],
                  len(summary["orphaned"]), summary["store_errors"])
        await self._notify(
            "reconciliation_summary", **summary,
            message=f"Reconciliacion al arranque completada: recuperados_redis={summary['recovered_from_redis']}, "
                    f"recuperados_archivo={summary['recovered_from_file']}, degradados={summary['degraded']}, "
                    f"huerfanos={len(summary['orphaned'])}, errores_store={summary['store_errors']}.",
        )
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
            tp2_partial_applied=leg_doc.get("tp2_partial_applied", False),
            peak_multiple=leg_doc.get("peak_multiple", 0.0), chat_id=doc.get("chat_id"),
        )

    def _reconstruct_leg_minimal(self, account, mt5_pos, group_id: int, leg: str) -> None:
        direction = "BUY" if getattr(mt5_pos, "type", 0) == 0 else "SELL"
        self.trades[mt5_pos.ticket] = ManagedTrade(
            account_name=account["name"], ticket=mt5_pos.ticket, symbol=mt5_pos.symbol,
            direction=direction, group_id=group_id, leg=leg,
            planned_sl=float(getattr(mt5_pos, "sl", 0.0)), entry_price=float(getattr(mt5_pos, "price_open", 0.0)),
        )
