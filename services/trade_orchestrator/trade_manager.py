from .trade_utils import safe_comment, parse_group_comment, pips_to_price, calcular_sl_default, calcular_tp_default
from .channel_names import resolve_channel_name
from .mt5_pool import MT5ConnectionStuckError
from .event_messages import (
    build_sl_hit_message,
    build_external_close_message,
    build_external_partial_close_message,
    build_group_opened_message,
    build_tp1_hit_message,
    build_tp2_partial_closed_message,
    build_tp2_hit_message,
    build_close_now_message,
    build_opposite_signal_close_message,
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

# MT5: ORDER_FILLING_* (valor del request) y bits de SYMBOL_FILLING_MODE
# (lo que el simbolo acepta). Verificado en vivo 2026-09-24: Vantage XAUUSD
# filling_mode=2 (solo IOC), ORDER_FILLING_FOK/IOC/RETURN = 0/1/2.
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
SYMBOL_FILLING_FOK_BIT, SYMBOL_FILLING_IOC_BIT = 1, 2
TRADE_RETCODE_INVALID_FILL = 10030


def filling_modes_for(symbol_info) -> list[int]:
    """Orden de type_filling a probar: primero los que el simbolo declara en su
    bitmask filling_mode, luego el resto (IOC, FOK, RETURN) como respaldo."""
    mask = getattr(symbol_info, "filling_mode", None)
    declared = []
    if isinstance(mask, int):
        if mask & SYMBOL_FILLING_IOC_BIT:
            declared.append(ORDER_FILLING_IOC)
        if mask & SYMBOL_FILLING_FOK_BIT:
            declared.append(ORDER_FILLING_FOK)
    return declared + [m for m in (ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN) if m not in declared]


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
    # Volumen vivo visto la ultima vez que se proceso este ticket en el tick
    # loop, tras aplicar cualquier cierre parcial que el propio sistema haya
    # iniciado (TP2 partial, close_partial_now). Una caida de volumen entre
    # dos ticks que NO coincide con ninguna de esas acciones del sistema es
    # un cierre parcial externo (el usuario cerrando manualmente desde MT5) --
    # ver _tick_once_account. None hasta el primer tick que vea la posicion.
    last_known_volume: Optional[float] = None
    # Marca de tiempo (deal.time, epoch) del deal de salida mas reciente ya
    # sumado a un evento de auditoria para este ticket (TP1/SL/cierre externo
    # total o parcial). _get_close_deal_info usa esto para sumar TODOS los
    # deals de salida nuevos desde la ultima auditoria, no solo el ultimo --
    # sin esto, una posicion cerrada en varios partials externos antes de
    # desaparecer del todo pierde el P&L de los partials intermedios (solo
    # se ve el del ultimo deal). Ver caso real grupo 169, 2026-09-28.
    last_audited_deal_time: int = 0

    @property
    def tp2_partial_skipped(self) -> bool:
        """True si el guard de TP2 ya descarto el cierre parcial para esta
        pierna (lectura conveniente del latch tp2_partial_skipped_volume)."""
        return self.tp2_partial_skipped_volume is not None


@dataclass
class PendingGroup:
    """
    Grupo cuya apertura quedo sin confirmar: order_send no respondio a tiempo
    (o lanzo un error de conexion) y MT5 tampoco mostraba la posicion. Eso NO
    prueba que la orden no se ejecuto -- caso real grupo 204 (2026-10-05): la
    terminal de Vantage estuvo colgada ~100s y lleno la orden 99s despues de
    enviada, cuando el grupo ya se habia dado por fallido; la posicion quedo
    sin gestion (sin la actualizacion de la señal completa, fuera del alcance
    del close_now). Mientras esta pendiente el grupo sigue siendo visible para
    la señal completa, el filtro de duplicados fast y los cierres del canal;
    _reconcile_untracked_positions lo adopta en cuanto la posicion aparece.
    """
    group_id: int
    account_name: str
    symbol: str
    direction: str
    chat_id: Optional[str]
    sl: float
    tp1: Optional[float]
    tp2: Optional[float]
    price: float  # precio con el que se envio la orden
    unconfirmed_legs: set  # enviadas, sin confirmacion de MT5
    unsent_legs: list  # nunca enviadas (las que seguian a la pierna sin confirmar)
    failure_kind: str  # "timeout" (la orden llego a la terminal) | "error" (pudo no llegar)
    created_ts: float
    deadline_ts: float
    expired: bool = False
    cancel_reason: Optional[str] = None  # texto del cierre del canal recibido mientras estaba pendiente
    resend_attempted: bool = False
    be_requested: bool = False  # el canal pidio BE/parcial antes de que hubiera piernas confirmadas
    notes: list = field(default_factory=list)


class TradeManager:
    RUNNER_MODES = ("fixed_tp2", "trailing")

    def __init__(self, mt5_executor, *, notifier=None, event_bus=None, config_provider=None, state_store=None,
                 channel_names=None, runner_mode: str = "trailing"):
        # fixed_tp2 (strategy C, 3-month backtest 2026-10-02): runner keeps a real
        # broker TP at tp2 and its SL stays at BE after tp1 -- no TP2 partial, no
        # trailing. trailing: the previous mechanic (TP2 50% partial + trailing).
        if runner_mode not in self.RUNNER_MODES:
            raise ValueError(f"runner_mode invalido: {runner_mode!r} (validos: {self.RUNNER_MODES})")
        self.runner_mode = runner_mode
        self.mt5 = mt5_executor
        self.notifier = notifier
        self.event_bus = event_bus
        self.config_provider = config_provider
        self.state_store = state_store
        self.channel_names = channel_names or {}
        self.trades: dict[int, ManagedTrade] = {}
        self._next_group_id = 1
        # Tickets que close_now esta cerrando: _tick_once_account no debe
        # clasificarlos como cierre externo en la ventana entre el cierre en MT5
        # y el pop de self.trades (ver apply_mgmt_action, rama close_now).
        self._mgmt_closing: set[int] = set()
        # Aperturas sin confirmar (ver PendingGroup) y grupos con open_group en
        # curso (sus posiciones existen en MT5 antes de entrar a self.trades: la
        # reconciliacion del tick no debe tomarlas por huerfanas).
        self._pending: dict[int, PendingGroup] = {}
        self._opening_groups: set[int] = set()
        # (account_name, ticket) -> primera vez vista en MT5 sin estar en
        # self.trades. Solo se adopta tras adopt_grace_seconds: una foto de
        # positions_get tomada justo antes de que close_now cierre una pierna
        # todavia la muestra, y no debe re-adoptarse.
        self._untracked_seen: dict[tuple, float] = {}
        self.adopt_grace_seconds = 3.0
        # Grupos cuyo tp1 ya cerro en TP: un runner adoptado despues (tardio u
        # huerfano) se mueve a BE al adoptarlo, como habria pasado a tiempo.
        self._tp1_hit_groups: set[int] = set()

    def _cfg_float(self, key: str, default: float) -> float:
        raw = self.config_provider.get(key, None) if self.config_provider else os.getenv(key)
        try:
            return float(raw) if raw not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _broker_tp_for_leg(self, leg: str, tp1: Optional[float], tp2: Optional[float]) -> float:
        if leg == "tp1":
            return float(tp1) if tp1 is not None else 0.0
        if self.runner_mode == "fixed_tp2" and tp2 is not None:
            return float(tp2)
        return 0.0

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
        except MT5ConnectionStuckError as e:
            # Subclase de asyncio.TimeoutError: se re-lanza igual para que todo el
            # manejo de timeouts existente aplique, solo cambia el log.
            log.error("[TM] MT5 call %s no ejecutada: %s", getattr(fn, "__name__", fn), e)
            raise
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
        # TOLERANCE_PIPS esta en pips, como el resto de *_PIPS: para oro 1 pip =
        # 0.1 (pips_to_price), no el point del broker -- Vantage reporta
        # point=0.01, lo que dejaba TOLERANCE_PIPS=10 en $0.10 en vez de $1.00
        # (aborto real entry_range_missed 2026-09-17 05:38).
        pips_tolerance = pips_to_price(symbol, tolerance_pips, point)

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

    async def _find_position_by_group_comment(self, client, group_id: int, leg: str):
        """
        Busca en MT5 una posicion viva cuyo comment coincida exactamente con
        safe_comment(f"GRP{group_id}-{leg}") -- el identificador unico que
        open_group ya pone en toda orden que abre (ver parse_group_comment).
        Usado tras un timeout/retcode malo en order_send: un timeout significa
        "la respuesta no llego a tiempo", NO "la orden no se ejecuto" -- MT5
        puede haber llenado la orden del lado del broker igual. Sin esto, el
        unico rastro de una pierna recien abierta es el `tickets` dict que
        open_group construye en memoria, que por definicion NUNCA se puebla
        para la pierna que justamente tuvo el timeout -- dejando esa posicion
        real huerfana, sin gestion, mientras open_group cree (y notifica) que
        "se revirtio" algo que nunca llego a intentar revertir (casos reales
        2026-09-29, grupos 170/171).
        Retorna el objeto posicion si la encuentra, None si no existe o si la
        propia consulta falla (vuelve a ser un caso ambiguo, no uno resuelto).
        """
        target_comment = safe_comment(f"GRP{group_id}-{leg}")
        try:
            positions = await self._call(client.positions_get) or []
        except Exception as e:
            log.warning("[TM][OPEN] fallo consultando positions_get para reconciliar group_id=%s leg=%s: %s",
                        group_id, leg, e)
            return None
        for pos in positions:
            if getattr(pos, "comment", None) == target_comment:
                return pos
        return None

    async def _revert_opened_legs(self, account: dict, client, tickets: dict) -> bool:
        """
        Cierra (partial_close 100%) cualquier pierna ya abierta en `tickets`
        cuando open_group aborta a mitad de camino (la segunda pierna
        rechazada o con timeout). Cada cierre se aisla de los demas y de un
        timeout propio -- un revert que se cuelga no debe silenciar el
        hecho de que puede haber quedado una posicion huerfana sin gestion
        (ver open_group: el mensaje al usuario cambia segun el resultado de
        esto). Devuelve True solo si TODAS las piernas se revirtieron
        confirmadamente.
        """
        all_ok = True
        for t in tickets.values():
            try:
                ok = bool(await self._call(client.partial_close, account, t, 100))
            except asyncio.TimeoutError:
                log.error("[TM][OPEN] timeout revirtiendo pierna ticket=%s tras fallo de apertura", t)
                ok = False
            if not ok:
                all_ok = False
        return all_ok

    async def open_group(self, account: dict, *, symbol: str, direction: str, sl: Optional[float],
                          tp1: Optional[float], tp2: Optional[float], entry_range: Optional[tuple] = None,
                          chat_id: Optional[str] = None, fast_pips: Optional[dict] = None) -> Optional[int]:
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
        - fast_pips, si viene ({"sl": pips, "tp1": pips, "tp2_extra": pips}),
          IGNORA los sl/tp1/tp2 absolutos recibidos y los recalcula aqui mismo
          contra el precio que esta funcion ya obtiene mas abajo -- evita el
          bug real de produccion (grupo 176, 2026-10-01) donde una senal fast
          calculaba SL/TP absolutos en app.py contra un fetch de precio
          PREVIO, y luego open_group volvia a pedir el precio para la orden
          real: si el precio se movio entre ambos fetches, el SL/TP quedaba a
          una distancia incorrecta del precio real de ejecucion -- suficiente
          para violar el stops_level de una cuenta mas estricta (STARTRADER,
          35) mientras la misma senal abria sin problema en otra mas laxa
          (Vantage, 20). Con fast_pips, SL/TP se calculan contra el MISMO
          precio usado para el order_send, sin ventana de drift.
        Retorna el group_id nuevo, o None si se aborto (unit invalido, SL invalido,
        sin precio disponible, o el precio nunca entro/ya paso el rango).
        """
        account = self._ensure_account_dict(account)
        if not account:
            return None

        if fast_pips is None and tp1 is not None and tp2 is not None:
            unit = (tp2 - tp1) if direction.upper() == "BUY" else (tp1 - tp2)
            if unit <= 0:
                log.error("[TM][OPEN] Abortado: unit invalido (tp1=%s tp2=%s dir=%s) symbol=%s", tp1, tp2, direction, symbol)
                await self._notify(
                    "open_aborted", symbol=symbol, reason="invalid_unit", tp1=tp1, tp2=tp2,
                    message=f"Señal {direction.upper()} {symbol} no ejecutada: TP1/TP2 inconsistentes con la direccion (tp1={tp1}, tp2={tp2}).",
                )
                return None

        if fast_pips is None and (sl is None or float(sl) == 0.0):
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

        if fast_pips is not None:
            # Calculado AQUI, contra el `price` que esta misma funcion acaba
            # de fijar (no un fetch anterior en app.py) -- ver docstring.
            point = 0.1 if symbol.upper().startswith("XAU") else 0.00001
            sl = calcular_sl_default(symbol, direction, price, point, float(fast_pips.get("sl", 0) or 0))
            tp1_pips = float(fast_pips.get("tp1", 0) or 0)
            tp1 = calcular_tp_default(symbol, direction, price, point, tp1_pips) if tp1_pips > 0 else None
            tp2 = None
            if tp1 is not None:
                tp2_extra_pips = float(fast_pips.get("tp2_extra", 0) or 0)
                tp2 = calcular_tp_default(symbol, direction, tp1, point, tp2_extra_pips) if tp2_extra_pips > 0 else None
            if sl is None or float(sl) == 0.0:
                log.error("[TM][OPEN] Abortado: SL invalido (fast_pips) symbol=%s", symbol)
                await self._notify(
                    "open_aborted", symbol=symbol, reason="invalid_sl",
                    message=f"Señal {direction.upper()} {symbol} no ejecutada: SL invalido o ausente.",
                )
                return None

        order_type = 0 if direction.upper() == "BUY" else 1
        try:
            filling_modes = filling_modes_for(await self._call(client.symbol_info, symbol))
        except Exception as e:
            log.warning("[TM][OPEN] symbol_info no disponible para elegir filling mode (%s), usando orden por defecto", e)
            filling_modes = filling_modes_for(None)
        group_id = self._next_group_id
        self._next_group_id += 1

        try:
            return await self._open_group_legs(
                account, client, group_id, symbol=symbol, direction=direction, order_type=order_type,
                sl=sl, tp1=tp1, tp2=tp2, price=price, chat_id=chat_id, filling_modes=filling_modes,
            )
        except Exception as e:
            # Real production bug (group 133, 2026-09-14): open_group's leg
            # loop only ever caught asyncio.TimeoutError and a bad retcode --
            # ANY other exception (an RPyC EOFError, a bug in _notify/
            # build_group_opened_message, anything) escaped this function
            # entirely, caught only by app.py's generic
            # `except Exception: log.exception(...)` around signal
            # processing, which emits NO business event. tp1 can have
            # already opened for real in MT5 at that point with ZERO
            # internal tracking (never inserted into self.trades) -- totally
            # unmanaged and unreported, the group_id simply vanishes from
            # the audit log with no trace. Every path through leg-opening
            # must end in at least one notification, even an unexpected one.
            log.error("[TM][OPEN] excepcion inesperada abriendo group_id=%s symbol=%s: %s",
                      group_id, symbol, e, exc_info=True)
            real_tp1 = await self._find_position_by_group_comment(client, group_id, "tp1")
            real_runner = await self._find_position_by_group_comment(client, group_id, "runner")
            orphan_note = ""
            if real_tp1 is not None or real_runner is not None:
                tickets_found = [t for t in (real_tp1, real_runner) if t is not None]
                untracked = {leg for leg, t in (("tp1", real_tp1), ("runner", real_runner))
                             if t is not None and int(t.ticket) not in self.trades}
                if untracked and group_id in self._pending:
                    self._pending[group_id].unconfirmed_legs.update(untracked)
                elif untracked:
                    # La reconciliacion del tick las adopta por este registro:
                    # con su canal y niveles, no como huerfanas sin canal.
                    now = time.time()
                    self._pending[group_id] = PendingGroup(
                        group_id=group_id, account_name=account["name"], symbol=symbol,
                        direction=direction.upper(), chat_id=chat_id, sl=float(sl), tp1=tp1, tp2=tp2,
                        price=float(price), unconfirmed_legs=untracked, unsent_legs=[], failure_kind="timeout",
                        created_ts=now, deadline_ts=now + self._pending_timeout_seconds("timeout"),
                    )
                managed = all(int(t.ticket) in self.trades for t in tickets_found)
                orphan_note = (f" ADVERTENCIA: {len(tickets_found)} pierna(s) SI se abrieron en MT5 "
                                f"(tickets={[int(t.ticket) for t in tickets_found]}) y " +
                                ("quedaron bajo gestion." if managed else
                                 "se pondran bajo gestion automaticamente en unos segundos -- revisar el aviso."))
            await self._notify(
                "open_failed", symbol=symbol, group_id=group_id, reason="unexpected_error",
                message=f"Grupo {group_id} ({symbol}): error inesperado abriendo el grupo ({e}).{orphan_note}",
            )
            return None

    def _leg_request(self, account: dict, group_id: int, leg: str, *, symbol: str, order_type: int, price: float,
                     sl: float, tp1: Optional[float], tp2: Optional[float]) -> dict:
        return {
            "action": 1,
            "symbol": symbol,
            "volume": float(account.get("fixed_lot", 0.01) or 0.01),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": self._broker_tp_for_leg(leg, tp1, tp2),
            "deviation": 50,
            "magic": MAGIC,
            "comment": safe_comment(f"GRP{group_id}-{leg}", "TM"),
            "type_time": 0,
        }

    async def _send_open_order(self, client, req: dict, filling_modes: list, *, leg: str, group_id: int):
        """
        Envia la orden de apertura de una pierna. Devuelve (res, outcome):
        outcome "sent" (hay respuesta de MT5, buena o mala), "not_sent" (el pool
        fallo antes de enviarla: MT5ConnectionStuckError), "timeout" (enviada,
        sin respuesta a tiempo -- la terminal la tiene y puede ejecutarla
        tarde) o "error" (excepcion de conexion: pudo llegar o no).
        type_filling sale de lo que el simbolo declara (filling_modes_for), y
        solo se reintenta con el siguiente modo ante INVALID_FILL: otro rechazo
        cualquiera nunca se reenvia (no duplicar ordenes).
        """
        res = None
        for attempt, filling in enumerate(filling_modes):
            req["type_filling"] = filling
            try:
                res = await self._call(client.order_send, req)
            except MT5ConnectionStuckError:
                return None, "not_sent"
            except (asyncio.TimeoutError, TimeoutError) as e:
                # TimeoutError builtin: el "result expired" de rpyc (en Python 3.10
                # no es asyncio.TimeoutError). La orden llego a la terminal.
                log.error("[TM][OPEN] timeout abriendo leg=%s symbol=%s group_id=%s: %s",
                          leg, req["symbol"], group_id, e or "sin respuesta")
                return None, "timeout"
            except Exception as e:
                log.error("[TM][OPEN] error de conexion abriendo leg=%s symbol=%s group_id=%s: %s",
                          leg, req["symbol"], group_id, e)
                return None, "error"
            if getattr(res, "retcode", None) == TRADE_RETCODE_INVALID_FILL and attempt < len(filling_modes) - 1:
                log.warning("[TM][OPEN] filling mode %s rechazado (10030) leg=%s symbol=%s -- probando %s",
                            filling, leg, req["symbol"], filling_modes[attempt + 1])
                continue
            break
        return res, "sent"

    def _insert_leg(self, account: dict, group_id: int, leg: str, ticket: int, *, symbol: str, direction: str,
                    sl: float, tp1: Optional[float], tp2: Optional[float], entry_price: float,
                    chat_id: Optional[str]) -> ManagedTrade:
        trade = ManagedTrade(
            account_name=account["name"],
            ticket=ticket,
            symbol=symbol,
            direction=direction.upper(),
            group_id=group_id,
            leg=leg,
            planned_sl=float(sl),
            tp1_price=float(tp1) if tp1 is not None else None,
            tp2_price=float(tp2) if tp2 is not None else None,
            entry_price=float(entry_price),
            chat_id=chat_id,
        )
        self.trades[ticket] = trade
        TRADES_OPENED.inc()
        ACTIVE_TRADES.set(len(self.trades))
        return trade

    async def _notify_group_opened(self, account: dict, group_id: int, *, symbol: str, direction: str, sl: float,
                                   tp1: Optional[float], tp2: Optional[float], entry_price: float,
                                   chat_id: Optional[str], note: str = "") -> None:
        legs = {t.leg: t.ticket for t in self.trades.values() if t.group_id == group_id}
        channel_name = resolve_channel_name(chat_id, self._channel_names())
        message = build_group_opened_message(
            channel_name=channel_name, group_id=group_id, symbol=symbol, direction=direction,
            entry_price=entry_price, sl=sl, tp1=tp1, tp2=tp2, volume=account.get("fixed_lot", 0.01),
        )
        if note:
            message = f"{message}\n{note}"
        await self._notify(
            "group_opened", channel="both", group_id=group_id, symbol=symbol, direction=direction,
            tp1_ticket=legs.get("tp1"), runner_ticket=legs.get("runner"), sl=sl, tp1=tp1, tp2=tp2,
            chat_id=chat_id, channel_name=channel_name, entry_price=entry_price, volume=account.get("fixed_lot", 0.01),
            message=message,
        )

    async def _open_group_legs(self, account: dict, client, group_id: int, *, symbol: str, direction: str,
                                order_type: int, sl: float, tp1: Optional[float], tp2: Optional[float],
                                price: float, chat_id: Optional[str], filling_modes: list) -> Optional[int]:
        self._opening_groups.add(group_id)
        try:
            return await self._open_group_legs_inner(
                account, client, group_id, symbol=symbol, direction=direction, order_type=order_type,
                sl=sl, tp1=tp1, tp2=tp2, price=price, chat_id=chat_id, filling_modes=filling_modes,
            )
        finally:
            self._opening_groups.discard(group_id)

    async def _open_group_legs_inner(self, account: dict, client, group_id: int, *, symbol: str, direction: str,
                                      order_type: int, sl: float, tp1: Optional[float], tp2: Optional[float],
                                      price: float, chat_id: Optional[str], filling_modes: list) -> Optional[int]:
        tickets = {}
        legs_order = ("tp1", "runner")
        for leg in legs_order:
            req = self._leg_request(account, group_id, leg, symbol=symbol, order_type=order_type, price=price,
                                    sl=sl, tp1=tp1, tp2=tp2)
            # DEBUG unicamente (no toca el flujo): deja el request exacto en el
            # log para poder confirmar numericamente la causa de un futuro
            # retcode 10016/INVALID_STOPS sin reconstruirlo a mano -- caso real
            # grupo 176, 2026-10-01 (cuenta STARTRADER rechazo una senal fast
            # que SI abrio en Vantage; no se pudo confirmar con certeza si fue
            # drift de precio entre el fetch de app.py y el de open_group, o
            # alguna otra diferencia de cuenta, por falta de este log).
            log.debug("[TM][OPEN] req account=%s group_id=%s leg=%s req=%s", account.get("name"), group_id, leg, req)
            # Real production incident (2026-09-14): a hung order_send raised
            # a bare asyncio.TimeoutError that escaped open_group entirely --
            # the signal was silently dropped with no open_aborted/
            # open_failed notification reaching n8n or Telegram. Every
            # outcome of _send_open_order below ends in a notification.
            res, outcome = await self._send_open_order(client, req, filling_modes, leg=leg, group_id=group_id)

            if outcome == "sent" and res and getattr(res, "retcode", None) == 10009:
                tickets[leg] = int(res.order)
                continue

            # Ni un timeout ni un retcode malo prueban que la orden no se
            # ejecuto -- la respuesta pudo perderse mientras MT5 si la
            # procesaba. Reconciliar contra MT5 real por el comment unico
            # de esta pierna ANTES de asumir fallo (ver
            # _find_position_by_group_comment: casos reales 170/171).
            real_pos = await self._find_position_by_group_comment(client, group_id, leg)
            if real_pos is not None:
                log.warning("[TM][OPEN] leg=%s symbol=%s group_id=%s parecia fallida pero SI existe en MT5 "
                            "(ticket=%s) -- continuando como si order_send hubiera respondido a tiempo.",
                            leg, symbol, group_id, real_pos.ticket)
                tickets[leg] = int(real_pos.ticket)
                continue

            if outcome in ("timeout", "error"):
                # La orden pudo llegar a la terminal y ejecutarse tarde (grupo
                # 204: 99s despues). No se da por fallida ni se revierte lo ya
                # abierto: queda pendiente y la reconciliacion del tick la
                # adopta si aparece (ver PendingGroup).
                remaining = list(legs_order[legs_order.index(leg) + 1:])
                return await self._register_pending_group(
                    account, group_id, leg, remaining, tickets, failure_kind=outcome, symbol=symbol,
                    direction=direction, sl=sl, tp1=tp1, tp2=tp2, price=price, chat_id=chat_id,
                )

            # Rechazo limpio de MT5 (retcode) o el pool ni siquiera la envio
            # (conexion atascada): la orden de esta pierna no existe.
            not_sent = outcome == "not_sent"
            log.error("[TM][OPEN] Fallo abriendo leg=%s symbol=%s retcode=%s",
                      leg, symbol, None if not_sent else getattr(res, "retcode", None))
            reverted_all = await self._revert_opened_legs(account, client, tickets)
            detail = ("Se revirtieron las piernas ya abiertas del grupo." if (reverted_all and tickets) else
                      "No habia piernas abiertas que revertir." if not tickets else
                      "ADVERTENCIA: MT5 no confirmo a tiempo si las piernas ya abiertas del grupo "
                      "se revirtieron -- revisar manualmente si quedo una posicion huerfana sin gestion.")
            reason_text = "MT5 no respondio a tiempo" if not_sent else \
                f"fallo en MT5 (retcode={getattr(res, 'retcode', None)})"
            await self._notify(
                "open_failed", symbol=symbol, leg=leg, group_id=group_id, reason="timeout" if not_sent else None,
                message=f"Grupo {group_id} ({symbol}): {reason_text} abriendo la pierna '{leg}'. {detail}",
            )
            return None

        for leg, ticket in tickets.items():
            self._insert_leg(account, group_id, leg, ticket, symbol=symbol, direction=direction, sl=sl,
                             tp1=tp1, tp2=tp2, entry_price=price, chat_id=chat_id)
        log.info("[TM] group %s opened: tp1=%s runner=%s symbol=%s dir=%s sl=%s tp1_price=%s tp2_price=%s",
                  group_id, tickets["tp1"], tickets["runner"], symbol, direction, sl, tp1, tp2)
        await self._notify_group_opened(account, group_id, symbol=symbol, direction=direction, sl=sl, tp1=tp1,
                                        tp2=tp2, entry_price=price, chat_id=chat_id)
        await self._persist_group(group_id)
        return group_id

    def _pending_timeout_seconds(self, failure_kind: str) -> float:
        timeout = self._cfg_float("OPEN_PENDING_TIMEOUT_SECONDS", 180.0)
        # Un error de conexion inmediato casi siempre significa que la orden no
        # salio: no hace falta esperar tanto como tras un cuelgue de la terminal.
        return min(timeout, 30.0) if failure_kind == "error" else timeout

    async def _register_pending_group(self, account: dict, group_id: int, leg: str, unsent_legs: list,
                                      tickets: dict, *, failure_kind: str, symbol: str, direction: str, sl: float,
                                      tp1: Optional[float], tp2: Optional[float], price: float,
                                      chat_id: Optional[str]) -> int:
        now = time.time()
        timeout = self._pending_timeout_seconds(failure_kind)
        self._pending[group_id] = PendingGroup(
            group_id=group_id, account_name=account["name"], symbol=symbol, direction=direction.upper(),
            chat_id=chat_id, sl=float(sl), tp1=tp1, tp2=tp2, price=float(price), unconfirmed_legs={leg},
            unsent_legs=list(unsent_legs), failure_kind=failure_kind, created_ts=now, deadline_ts=now + timeout,
        )
        for opened_leg, ticket in tickets.items():
            self._insert_leg(account, group_id, opened_leg, ticket, symbol=symbol, direction=direction, sl=sl,
                             tp1=tp1, tp2=tp2, entry_price=price, chat_id=chat_id)
        if tickets:
            await self._persist_group(group_id)
        log.warning("[TM][OPEN] group_id=%s leg=%s sin confirmar (%s) -- pendiente hasta %.0fs, ya abiertas=%s",
                    group_id, leg, failure_kind, timeout, list(tickets))
        channel_name = resolve_channel_name(chat_id, self._channel_names())
        opened_note = (f" La pierna {', '.join(tickets)} ya esta abierta y bajo gestion." if tickets else "")
        await self._notify(
            "open_pending", channel="both", group_id=group_id, symbol=symbol, direction=direction.upper(),
            leg=leg, reason=failure_kind, chat_id=chat_id, channel_name=channel_name, account=account["name"],
            message=(f"⏳ APERTURA SIN CONFIRMAR — Canal: {channel_name} (grupo {group_id}, cuenta {account['name']})\n"
                     f"{symbol} {direction.upper()}: MT5 no confirmo a tiempo la pierna '{leg}'. La orden pudo "
                     f"ejecutarse igual: se vigila MT5 durante {timeout:.0f}s y, si aparece, queda bajo gestion "
                     f"normal (señal completa y cierres del canal incluidos).{opened_note}"),
        )
        return group_id

    async def update_group_signal(self, group_id: int, *, sl: Optional[float], tp1: Optional[float], tp2: Optional[float]) -> None:
        """
        Aplica valores nuevos de SL/TP1/TP2 a ambas piernas de un grupo existente.
        Usado tanto para el update fast->full (dual-TP spec seccion 3) como para
        signal_correction via /mgmt/action (dual-TP spec seccion 5.2). En
        runner_mode=fixed_tp2 un cambio de tp2 mueve el TP real del runner en
        MT5; en trailing solo actualiza la referencia usada por el trailing.
        """
        pending = self._pending.get(group_id)
        if pending is not None:
            # Los niveles nuevos se aplican a la pierna en cuanto MT5 la confirme
            # (_adopt_pending_leg), igual que si hubiera abierto a tiempo.
            if sl is not None:
                pending.sl = float(sl)
            if tp1 is not None:
                pending.tp1 = float(tp1)
            if tp2 is not None:
                pending.tp2 = float(tp2)
        legs = [t for t in self.trades.values() if t.group_id == group_id]
        if not legs:
            if pending is not None:
                log.info("[TM] group %s (pendiente de confirmacion) actualizado: sl=%s tp1=%s tp2=%s",
                         group_id, sl, tp1, tp2)
                await self._notify(
                    "group_updated", group_id=group_id, sl=sl, tp1=tp1, tp2=tp2,
                    message=f"Grupo {group_id} (pendiente de confirmacion en MT5) actualizado: sl={sl}, tp1={tp1}, tp2={tp2}.",
                )
                return
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
            new_tp = self._broker_tp_for_leg(t.leg, t.tp1_price, t.tp2_price)

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

    def find_active_group_for_symbol(self, symbol: str, *, chat_id: Optional[str], direction: Optional[str] = None,
                                      account_name: Optional[str] = None) -> Optional[int]:
        """
        Devuelve el group_id mas reciente con al menos una pierna abierta para
        `symbol` originado en `chat_id` (y, si viene, en `direction`), o None
        (dual-TP spec seccion 5.2 — respuesta 'no_active_trade').
        chat_id se compara exacto, incluido None == None (señales legacy sin
        chat_id siguen encontrando sus propios grupos). Antes solo filtraba
        por simbolo: con dos canales XAUUSD permitidos, una señal completa de
        un canal sobrescribia SL/TP del grupo abierto del otro canal, y una
        señal SELL podia escribir sus niveles en un grupo BUY.
        account_name, si viene, restringe la busqueda a grupos de esa cuenta
        -- necesario quando la misma senal se replica en varias cuentas: sin
        esto, dos cuentas con grupos activos para el mismo chat_id/symbol/
        direction resolverian siempre al mismo group_id (el mas reciente
        entre TODAS las cuentas), dejando el otro grupo sin actualizar.
        """
        candidates = [
            (t.opened_ts, t.group_id) for t in self.trades.values()
            if t.symbol == symbol and t.chat_id == chat_id
            and (direction is None or t.direction == direction.upper())
            and (account_name is None or t.account_name == account_name)
        ]
        # Un grupo pendiente de confirmacion tambien cuenta: sin esto la señal
        # completa abriria un grupo NUEVO encima de la orden que MT5 puede
        # estar por ejecutar (casi pasa con el grupo 204).
        candidates += [
            (p.created_ts, p.group_id) for p in self._active_pending()
            if p.symbol == symbol and p.chat_id == chat_id
            and (direction is None or p.direction == direction.upper())
            and (account_name is None or p.account_name == account_name)
        ]
        if not candidates:
            return None
        # Tie-break on group_id (an incrementing counter) since time.time() has
        # coarse resolution on some platforms (e.g. ~15.6ms on Windows) and two
        # groups opened back-to-back can share an opened_ts — max() would
        # otherwise return the first (older) tied element.
        return max(candidates)[1]

    def _active_pending(self) -> list[PendingGroup]:
        """Pendientes aun dentro de su ventana de espera (los vencidos solo se
        conservan para adoptar una ejecucion muy tardia, ver _expire_pending)."""
        return [p for p in self._pending.values() if not p.expired and p.cancel_reason is None]

    def _cancel_expired_pendings(self, chat_id: Optional[str], raw_text: str, *, direction: Optional[str] = None,
                                 symbol: Optional[str] = None) -> None:
        """Un pendiente vencido sigue vigilado por si MT5 lo ejecuta muy tarde:
        un cierre del canal debe alcanzarlo tambien, o esa ejecucion tardia se
        adoptaria y quedaria abierta contra la instruccion del canal."""
        if chat_id is None:
            return
        for p in self._pending.values():
            if p.expired and p.cancel_reason is None and p.chat_id == chat_id \
                    and (direction is None or p.direction == direction.upper()) \
                    and (symbol is None or p.symbol == symbol):
                p.cancel_reason = raw_text

    def _group_symbol_direction(self, group_id: int) -> Optional[tuple[str, str]]:
        leg = next((t for t in self.trades.values() if t.group_id == group_id), None)
        if leg is not None:
            return leg.symbol, leg.direction
        pending = self._pending.get(group_id)
        if pending is not None:
            return pending.symbol, pending.direction
        return None

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
        first_seen: dict[int, float] = {}
        for t in self.trades.values():
            if t.chat_id == chat_id:
                first_seen[t.group_id] = min(first_seen.get(t.group_id, t.opened_ts), t.opened_ts)
        for p in self._active_pending():
            if p.chat_id == chat_id:
                first_seen[p.group_id] = min(first_seen.get(p.group_id, p.created_ts), p.created_ts)
        return sorted(first_seen, key=lambda gid: (first_seen[gid], gid))

    def _filter_groups_by_direction(self, group_ids: list[int], direction_hint: str) -> tuple[list[int], list[int]]:
        """
        Separa group_ids en (kept, excluded) segun si la direccion de sus
        legs coincide con direction_hint. Las dos piernas de un grupo
        comparten direccion, asi que basta inspeccionar cualquiera. Usado
        exclusivamente por la rama close_now de apply_mgmt_action -- ver
        docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md
        seccion 6 para por que no se aplica a otras acciones.
        """
        kept, excluded = [], []
        for group_id in group_ids:
            meta = self._group_symbol_direction(group_id)
            if meta is not None and meta[1] == direction_hint:
                kept.append(group_id)
            else:
                excluded.append(group_id)
        return kept, excluded

    def group_age_seconds(self, group_id: int) -> Optional[float]:
        """
        Segundos desde que se abrio `group_id` (min opened_ts entre sus piernas),
        o None si el grupo no tiene piernas activas. Usado por handle_signal_fields
        (app.py) para decidir si una señal fast nueva del mismo simbolo es un
        duplicado reciente a ignorar, o una reapertura legitima (BUY o SELL) a
        abrir aparte — ver REOPEN_COOLDOWN_SECONDS.
        """
        opened = [t.opened_ts for t in self.trades.values() if t.group_id == group_id]
        pending = self._pending.get(group_id)
        if pending is not None:
            opened.append(pending.created_ts)
        if not opened:
            return None
        return time.time() - min(opened)

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
            snapshot_ts = time.time()
            pos_by_ticket = {p.ticket: p for p in positions}

            # Detect closed tickets for this account (TP1 hit, SL hit, or manual close).
            # Each ticket's processing is isolated in its own try/except (real
            # production risk: an unhandled exception while processing one
            # group -- e.g. a timeout not already caught by Tasks 1/3/4/5, or
            # any other unexpected error -- must not abort processing for
            # every OTHER group on this same account in the same tick).
            for ticket in [t for t, mt in self.trades.items() if mt.account_name == account["name"]]:
                if ticket in pos_by_ticket or ticket in self._mgmt_closing:
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
                    elif cause == "tp2":
                        channel_name = resolve_channel_name(closed_trade.chat_id, self._channel_names())
                        message = build_tp2_hit_message(
                            channel_name=channel_name, group_id=closed_trade.group_id, symbol=closed_trade.symbol,
                            direction=closed_trade.direction, close_price=classification["price"],
                            close_volume=classification["volume"], pnl_money=classification["profit"],
                        )
                        await self._notify(
                            "tp2_hit", channel="both", group_id=closed_trade.group_id, chat_id=closed_trade.chat_id,
                            channel_name=channel_name, symbol=closed_trade.symbol, direction=closed_trade.direction,
                            leg=closed_trade.leg, close_price=classification["price"], close_volume=classification["volume"],
                            pnl_money=classification["profit"], message=message,
                        )
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
                              ticket, closed_trade.group_id, e, exc_info=True)

            ACTIVE_TRADES.set(len(self.trades))

            # Posiciones propias (TM-GRP*) que MT5 tiene y self.trades no: una
            # apertura confirmada tarde o una huerfana. Reutiliza la foto de
            # positions_get de este tick -- cero llamadas extra a MT5.
            try:
                await self._reconcile_untracked_positions(account, client, positions, snapshot_ts=snapshot_ts)
            except Exception as e:
                log.error("[TM] error reconciliando posiciones sin gestion en cuenta %s: %s",
                          account.get("name"), e, exc_info=True)

            # Cierre parcial externo: el ticket SIGUE en positions_get (no es
            # el caso de arriba) pero su volumen vivo bajo respecto al ultimo
            # tick, sin que el propio sistema lo haya pedido (TP2 partial y
            # close_partial_now actualizan last_known_volume ellos mismos
            # justo despues de actuar -- ver sus docstrings/comentarios). El
            # usuario cerrando manualmente una porcion desde MT5 es el caso
            # real que motiva esto (grupos 168/172, 2026-09-2x: el audit log
            # nunca se entero porque el ticket seguia vivo).
            for ticket, t in [(tk, mt) for tk, mt in self.trades.items() if mt.account_name == account["name"]]:
                try:
                    pos = pos_by_ticket.get(ticket)
                    if not pos:
                        continue
                    live_volume = float(pos.volume)
                    if t.last_known_volume is None:
                        t.last_known_volume = live_volume
                        continue
                    if live_volume < t.last_known_volume - 1e-9:
                        closed_volume = t.last_known_volume - live_volume
                        deal_info = await self._get_close_deal_info(client, ticket, t)
                        channel_name = resolve_channel_name(t.chat_id, self._channel_names())
                        pnl_money = deal_info["profit"] if deal_info else None
                        message = build_external_partial_close_message(
                            channel_name=channel_name, group_id=t.group_id, symbol=t.symbol, direction=t.direction,
                            leg=t.leg, closed_volume=round(closed_volume, 2), remaining_volume=round(live_volume, 2),
                            pnl_money=pnl_money,
                        )
                        await self._notify(
                            "external_partial_close_detected", channel="both", group_id=t.group_id,
                            chat_id=t.chat_id, channel_name=channel_name, symbol=t.symbol, direction=t.direction,
                            leg=t.leg, closed_volume=round(closed_volume, 2), remaining_volume=round(live_volume, 2),
                            pnl_money=pnl_money, message=message,
                        )
                    t.last_known_volume = live_volume
                except Exception as e:
                    log.error("[TM] error detectando cierre parcial externo ticket=%s group_id=%s: %s",
                              ticket, t.group_id, e, exc_info=True)

            for ticket, t in [(tk, mt) for tk, mt in self.trades.items() if mt.account_name == account["name"]]:
                try:
                    pos = pos_by_ticket.get(ticket)
                    if self.runner_mode != "trailing" or not pos or t.leg != "runner" or not t.be_applied:
                        continue
                    await self._apply_tp2_partial_close(account, client, t, pos)
                    # Re-fetch: partial_close above may have changed this position's
                    # live volume, and _apply_trailing's SL move must act on that
                    # up-to-date position, not a stale pre-partial-close snapshot.
                    pos = (await self._call(client.positions_get, ticket=ticket) or [pos])[0]
                    await self._apply_trailing(account, client, t, pos)
                except Exception as e:
                    log.error("[TM] error aplicando TP2/trailing a ticket=%s group_id=%s: %s", ticket, t.group_id, e, exc_info=True)

        except Exception as e:
            log.error("[TM] error gestionando cuenta %s: %s", account.get("name"), e)

    PENDING_RETENTION_SECONDS = 3600.0

    async def _reconcile_untracked_positions(self, account: dict, client, positions,
                                             snapshot_ts: Optional[float] = None) -> None:
        """
        Adopta posiciones propias (magic + comment TM-GRP{id}-{leg}) que MT5
        tiene abiertas y self.trades no conoce, y vence los grupos pendientes
        cuya ventana de espera paso. Corre en cada tick con la foto de
        positions_get que el tick ya tomo (snapshot_ts = cuando se tomo): si MT5
        no responde, no corre, asi que un "no esta" aqui siempre se apoya en una
        consulta real -- y solo cuenta si la foto es posterior al vencimiento.

        Antes reconcile_from_mt5 era lo unico que adoptaba posiciones, y solo
        al arrancar: las del grupo 204 (2026-10-05) quedaron sin gestion hasta
        cerrarse solas por su TP original.

        Nunca cierra una posicion por su cuenta -- las unicas excepciones son
        una pierna tardia de un grupo que el canal ya pidio cerrar, o una
        pierna tardia de una señal que ya se copio en otro grupo.
        """
        now = time.time()
        snapshot_ts = now if snapshot_ts is None else snapshot_ts
        name = account["name"]
        seen_now = set()
        visible_legs = set()  # (group_id, leg) propias sin gestion en esta foto, adoptadas o no
        to_adopt = []
        for pos in positions:
            if getattr(pos, "magic", None) != MAGIC:
                continue
            ticket = pos.ticket
            if ticket in self.trades or ticket in self._mgmt_closing:
                continue
            parsed = parse_group_comment(getattr(pos, "comment", ""))
            if parsed is None:
                continue  # comment de otro formato: reconcile_from_mt5 ya los reporta al arrancar
            group_id, leg = parsed
            visible_legs.add((group_id, leg))
            if group_id in self._opening_groups:
                continue  # open_group la esta registrando ahora mismo
            key = (name, ticket)
            seen_now.add(key)
            first_seen = self._untracked_seen.setdefault(key, now)
            if now - first_seen >= self.adopt_grace_seconds:
                to_adopt.append((key, pos, group_id, leg))
        for key in [k for k in self._untracked_seen if k[0] == name and k not in seen_now]:
            self._untracked_seen.pop(key, None)

        for key, pos, group_id, leg in to_adopt:
            if pos.ticket in self.trades:
                continue  # una adopcion anterior de este mismo tick ya la registro
            self._untracked_seen.pop(key, None)
            pending = self._pending.get(group_id)
            if pending is not None and pending.account_name == name and leg in pending.unconfirmed_legs:
                await self._adopt_pending_leg(account, client, pending, pos, leg)
            else:
                await self._adopt_orphan_position(account, client, pos, group_id, leg)

        for pending in [p for p in self._pending.values() if p.account_name == name]:
            if (not pending.expired and pending.unconfirmed_legs and snapshot_ts >= pending.deadline_ts
                    and not any((pending.group_id, l) in visible_legs for l in pending.unconfirmed_legs)):
                await self._expire_pending(account, client, pending)
            if not pending.unconfirmed_legs:
                # Nada mas que confirmar (las piernas sin enviar solo se abren
                # dentro de una adopcion; si quedaron aqui es que esa adopcion
                # fallo a mitad de camino y no hay quien las abra).
                self._pending.pop(pending.group_id, None)
            elif pending.expired and now - pending.created_ts > self.PENDING_RETENTION_SECONDS:
                self._pending.pop(pending.group_id, None)

    async def _close_late_leg(self, account: dict, client, pending: PendingGroup, trade: ManagedTrade,
                              why: str) -> None:
        """Cierra una pierna confirmada tarde que ya no debe quedar abierta y
        avisa el resultado. Si el cierre falla, queda bajo gestion normal."""
        channel_name = resolve_channel_name(pending.chat_id, self._channel_names())
        log.warning("[TM][PENDING] cerrando pierna tardia group_id=%s leg=%s ticket=%s: %s",
                    pending.group_id, trade.leg, trade.ticket, why)
        self._mgmt_closing.add(trade.ticket)
        deal_info = None
        try:
            try:
                ok = await self._force_full_close(account, client, trade.ticket)
            except MT5CallTimeoutError:
                ok = False
            if ok:
                deal_info = await self._get_close_deal_info(client, trade.ticket, trade)
                self.trades.pop(trade.ticket, None)
        finally:
            self._mgmt_closing.discard(trade.ticket)
        pnl = deal_info["profit"] if deal_info else None
        result = (f"Cerrada (resultado {pnl:+.2f})." if ok and pnl is not None else
                  "Cerrada." if ok else
                  "NO se pudo cerrar: queda bajo gestion normal, revisar en MT5.")
        await self._notify(
            "pending_leg_closed", channel="both", group_id=pending.group_id, chat_id=pending.chat_id,
            channel_name=channel_name, leg=trade.leg, ticket=trade.ticket, entry_price=trade.entry_price,
            closed=ok, pnl_money=pnl,
            message=(f"⚠️ CIERRE DE APERTURA TARDIA — Canal: {channel_name} (grupo {pending.group_id})\n"
                     f"MT5 confirmo tarde la pierna '{trade.leg}' (ticket={trade.ticket}, entrada "
                     f"{self._fmt_price(trade.entry_price)}), pero {why}. {result}"),
        )
        if any(t.group_id == pending.group_id for t in self.trades.values()):
            await self._persist_group(pending.group_id)
        else:
            await self._close_group_in_store(pending.group_id)

    def _late_leg_close_reason(self, pending: PendingGroup) -> Optional[str]:
        """Por que una pierna tardia de `pending` no debe quedar abierta, o None."""
        if pending.cancel_reason is not None:
            return f"el canal ya habia pedido cerrar (\"{pending.cancel_reason}\")"
        if pending.expired:
            others = sorted({t.group_id for t in self.trades.values()
                             if t.group_id != pending.group_id and t.account_name == pending.account_name
                             and t.chat_id == pending.chat_id and t.symbol == pending.symbol
                             and t.direction == pending.direction and t.opened_ts >= pending.created_ts})
            if others:
                return f"la señal ya se habia copiado en el grupo {others[-1]} (evita duplicar la exposicion)"
        return None

    async def _apply_be_if_owed(self, account: dict, client, pending_or_none, trade: ManagedTrade) -> None:
        """BE que la pierna adoptada se perdio mientras no estaba gestionada: su
        tp1 ya toco TP (runner), o el canal pidio BE/parcial sobre el grupo."""
        owed = (trade.leg == "runner" and trade.group_id in self._tp1_hit_groups) or \
            (pending_or_none is not None and pending_or_none.be_requested)
        if not owed or trade.be_applied:
            return
        try:
            result = await self._move_group_legs_to_be(account, client, [trade], reason="BE-adopcion-tardia")
        except Exception as e:
            log.error("[TM][PENDING] fallo aplicando BE a ticket=%s: %s", trade.ticket, e)
            result = {"leg_ok": {trade.leg: False}}
        if result and not result["leg_ok"].get(trade.leg):
            note = f"No se pudo mover '{trade.leg}' a break-even: revisar en MT5."
            if pending_or_none is not None:
                pending_or_none.notes.append(note)
            else:
                log.error("[TM][ORPHAN] %s ticket=%s", note, trade.ticket)

    async def _sync_broker_levels(self, client, trade: ManagedTrade, pos) -> bool:
        """Lleva SL/TP de MT5 a los niveles que el grupo tiene en memoria (la
        orden pudo salir con los del fast). Si falla, la memoria se alinea con
        MT5 en vez de quedar desincronizada (ver planned-sl desync, 2026-09-10)."""
        desired_tp = self._broker_tp_for_leg(trade.leg, trade.tp1_price, trade.tp2_price)
        live_sl = float(getattr(pos, "sl", 0.0) or 0.0)
        live_tp = float(getattr(pos, "tp", 0.0) or 0.0)
        if abs(live_sl - trade.planned_sl) <= 1e-9 and abs(live_tp - desired_tp) <= 1e-9:
            return True
        try:
            res = await self._call(client.order_send, {"action": 6, "position": trade.ticket,
                                                       "sl": float(trade.planned_sl), "tp": float(desired_tp)})
            if res and getattr(res, "retcode", None) in (10009, 10025):  # 10025: ya los tenia (redondeo)
                return True
            log.error("[TM][PENDING] SL/TP no aplicados a ticket=%s (retcode=%s)", trade.ticket,
                      getattr(res, "retcode", None))
        except Exception as e:
            log.error("[TM][PENDING] fallo aplicando SL/TP a ticket=%s: %s", trade.ticket, e)
        trade.planned_sl = live_sl
        return False

    async def _adopt_pending_leg(self, account: dict, client, pending: PendingGroup, pos, leg: str) -> None:
        entry = float(getattr(pos, "price_open", 0.0) or pending.price)
        trade = self._insert_leg(account, pending.group_id, leg, pos.ticket, symbol=pending.symbol,
                                 direction=pending.direction, sl=pending.sl, tp1=pending.tp1, tp2=pending.tp2,
                                 entry_price=entry, chat_id=pending.chat_id)
        pending.unconfirmed_legs.discard(leg)
        why_close = self._late_leg_close_reason(pending)
        if why_close is not None:
            pending.unsent_legs.clear()  # no abrir mas piernas de un grupo que no debe seguir
            await self._close_late_leg(account, client, pending, trade, why_close)
            return

        log.warning("[TM][PENDING] group_id=%s leg=%s confirmada tarde por MT5 (ticket=%s entrada=%s) -- adoptando",
                    pending.group_id, leg, pos.ticket, entry)
        if not await self._sync_broker_levels(client, trade, pos):
            pending.notes.append(f"No se pudieron aplicar los SL/TP actuales a '{leg}' en MT5: revisar.")
        await self._apply_be_if_owed(account, client, pending, trade)
        for unsent in list(pending.unsent_legs):
            await self._open_late_leg(account, client, pending, unsent, reference_entry=entry)
        await self._persist_group(pending.group_id)
        await self._maybe_finish_pending(account, pending)

    async def _open_late_leg(self, account: dict, client, pending: PendingGroup, leg: str, *,
                             reference_entry: float) -> None:
        """
        Abre una pierna que nunca se llego a enviar porque la anterior quedo sin
        confirmar (tipicamente el runner tras un tp1 confirmado tarde). Solo
        dentro de la ventana del pendiente, sin cierre pedido por el canal, con
        el precio todavia dentro de TOLERANCE_PIPS de la entrada ya ejecutada y
        sin haber pasado tp1 -- la misma tolerancia que open_group acepta.
        """
        if leg in pending.unsent_legs:
            pending.unsent_legs.remove(leg)
        if pending.cancel_reason is not None or pending.expired:
            return
        if pending.be_requested:
            # El canal ya esta asegurando ganancia (BE/parcial): abrir ahora una
            # pierna nueva a precio de mercado con el SL original iria en contra.
            pending.notes.append(f"La pierna '{leg}' no se abrio: el canal ya habia pedido BE/cierre parcial.")
            return
        is_buy = pending.direction == "BUY"
        try:
            price = await self._get_price_with_retry(client, pending.symbol, pending.direction)
        except Exception as e:
            log.error("[TM][PENDING] sin precio para abrir '%s' tarde en group_id=%s: %s", leg, pending.group_id, e)
            price = 0.0
        point = 0.1 if pending.symbol.upper().startswith("XAU") else 0.00001
        tolerance = pips_to_price(pending.symbol, self._cfg_float("TOLERANCE_PIPS", 30.0), point)
        past_tp1 = pending.tp1 is not None and ((price >= pending.tp1) if is_buy else (price <= pending.tp1))
        if not price or abs(price - reference_entry) > tolerance or past_tp1:
            log.warning("[TM][PENDING] '%s' de group_id=%s no se abre tarde: precio=%s entrada=%s tolerancia=%s tp1=%s",
                        leg, pending.group_id, price, reference_entry, tolerance, pending.tp1)
            pending.notes.append(f"La pierna '{leg}' no se abrio: el precio ya se habia alejado de la entrada.")
            return
        try:
            filling_modes = filling_modes_for(await self._call(client.symbol_info, pending.symbol))
        except Exception:
            filling_modes = filling_modes_for(None)
        if pending.cancel_reason is not None or pending.expired:
            return  # el canal pidio cerrar mientras se preparaba la orden
        req = self._leg_request(account, pending.group_id, leg, symbol=pending.symbol, order_type=0 if is_buy else 1,
                                price=price, sl=pending.sl, tp1=pending.tp1, tp2=pending.tp2)
        log.debug("[TM][OPEN] req tardio account=%s group_id=%s leg=%s req=%s", account.get("name"), pending.group_id, leg, req)
        res, outcome = await self._send_open_order(client, req, filling_modes, leg=leg, group_id=pending.group_id)
        if outcome == "sent" and res and getattr(res, "retcode", None) == 10009:
            trade = self._insert_leg(account, pending.group_id, leg, int(res.order), symbol=pending.symbol,
                                     direction=pending.direction, sl=pending.sl, tp1=pending.tp1, tp2=pending.tp2,
                                     entry_price=price, chat_id=pending.chat_id)
            why_close = self._late_leg_close_reason(pending)
            if why_close is not None:  # el cierre llego mientras se enviaba la orden
                await self._close_late_leg(account, client, pending, trade, why_close)
        elif outcome in ("timeout", "error"):
            # Otra vez sin confirmar: se vigila igual que la primera.
            pending.unconfirmed_legs.add(leg)
            pending.failure_kind = outcome
            pending.deadline_ts = time.time() + self._pending_timeout_seconds(outcome)
        else:
            pending.notes.append(f"La pierna '{leg}' fue rechazada por MT5 "
                                 f"(retcode={getattr(res, 'retcode', None)}).")

    async def _maybe_finish_pending(self, account: dict, pending: PendingGroup) -> None:
        """Cuando ya no queda ninguna pierna por confirmar, anuncia la apertura
        con lo que realmente quedo abierto (una sola vez)."""
        if pending.unconfirmed_legs or pending.unsent_legs:
            return
        legs = [t for t in self.trades.values() if t.group_id == pending.group_id]
        if not legs:
            return
        entry = next((t.entry_price for t in legs if t.leg == "tp1"), legs[0].entry_price) or pending.price
        waited = time.time() - pending.created_ts
        note = f"(MT5 confirmo la apertura con {waited:.0f}s de retraso.)"
        if pending.notes:
            note += " " + " ".join(pending.notes)
        await self._notify_group_opened(account, pending.group_id, symbol=pending.symbol, direction=pending.direction,
                                        sl=pending.sl, tp1=pending.tp1, tp2=pending.tp2, entry_price=entry,
                                        chat_id=pending.chat_id, note=note)
        self._pending.pop(pending.group_id, None)

    async def _positions_snapshot(self, client) -> Optional[list]:
        try:
            return list(await self._call(client.positions_get) or [])
        except Exception as e:
            log.warning("[TM][PENDING] positions_get fallo verificando antes de reenviar: %s", e)
            return None

    async def _expire_pending(self, account: dict, client, pending: PendingGroup) -> None:
        """
        Vence la ventana de un grupo pendiente con una foto real de MT5 que no
        muestra la pierna. Tras un error de conexion (la orden pudo no salir)
        se reenvia UNA vez si una consulta fresca confirma que sigue sin estar
        y el precio sigue dentro de tolerancia; tras un timeout nunca (la
        terminal la tiene: en los 3 casos reales se ejecuto). El registro se
        conserva PENDING_RETENTION_SECONDS: si MT5 la ejecuta aun mas tarde, se
        adopta con canal y niveles, pero sin abrir piernas nuevas.
        """
        missing = sorted(pending.unconfirmed_legs)
        if pending.cancel_reason is not None:
            # El canal ya pidio cerrar: no hay nada que anunciar como fallido.
            log.info("[TM][PENDING] group_id=%s vencio con cierre ya pedido; se sigue vigilando por si aparece",
                     pending.group_id)
            pending.expired = True
            pending.unsent_legs.clear()
            return
        if pending.failure_kind == "error" and not pending.resend_attempted:
            fresh = await self._positions_snapshot(client)
            if fresh is None:
                pending.deadline_ts = time.time() + 10.0  # sin verificacion no se reenvia nada
                return
            fresh_legs = {parse_group_comment(getattr(p, "comment", "")) for p in fresh
                          if getattr(p, "magic", None) == MAGIC}
            if any((pending.group_id, l) in fresh_legs for l in missing):
                return  # aparecio: la adopcion del proximo tick se encarga
            pending.resend_attempted = True
            reference = next((t.entry_price for t in self.trades.values()
                              if t.group_id == pending.group_id and t.entry_price), pending.price)
            for leg in reversed(missing):
                pending.unconfirmed_legs.discard(leg)
                pending.unsent_legs.insert(0, leg)
            log.warning("[TM][PENDING] group_id=%s: %s no aparecio tras error de conexion -- reenviando una vez",
                        pending.group_id, missing)
            for leg in list(pending.unsent_legs):
                await self._open_late_leg(account, client, pending, leg, reference_entry=reference)
            if pending.unconfirmed_legs:
                return  # el reenvio quedo sin confirmar: nueva ventana
            pending.unsent_legs.clear()
            not_opened = [l for l in missing if not any(t.group_id == pending.group_id and t.leg == l
                                                         for t in self.trades.values())]
            if not not_opened:
                await self._persist_group(pending.group_id)
                await self._maybe_finish_pending(account, pending)
                return
            # El reenvio no salio (precio fuera de tolerancia o rechazo): la
            # orden original pudo llegar igual, asi que se sigue vigilando en el
            # registro vencido -- si aparece, se adopta con su canal.
            pending.unconfirmed_legs.update(not_opened)
            missing = not_opened

        pending.expired = True
        pending.unsent_legs.clear()
        waited = time.time() - pending.created_ts
        legs = [t for t in self.trades.values() if t.group_id == pending.group_id]
        channel_name = resolve_channel_name(pending.chat_id, self._channel_names())
        if legs:
            entry = legs[0].entry_price
            note = (f"(La pierna {', '.join(missing)} no se ejecuto en MT5 -- verificado durante {waited:.0f}s; "
                    f"el grupo sigue gestionado solo con {', '.join(t.leg for t in legs)}.)")
            if pending.notes:
                note += " " + " ".join(pending.notes)
            await self._notify_group_opened(account, pending.group_id, symbol=pending.symbol,
                                            direction=pending.direction, sl=pending.sl, tp1=pending.tp1,
                                            tp2=pending.tp2, entry_price=entry or pending.price,
                                            chat_id=pending.chat_id, note=note)
            return
        await self._notify(
            "open_failed", channel="both", symbol=pending.symbol, leg=missing[0] if missing else None,
            group_id=pending.group_id, reason="not_executed", chat_id=pending.chat_id, channel_name=channel_name,
            message=(f"Grupo {pending.group_id} ({pending.symbol} {pending.direction}, cuenta {pending.account_name}): "
                     f"MT5 no ejecuto la orden -- verificado en MT5 durante {waited:.0f}s. Señal no copiada en esta "
                     f"cuenta. Si MT5 la ejecutara aun mas tarde, se adoptara y se avisara."
                     + (" " + " ".join(pending.notes) if pending.notes else "")),
        )

    async def _adopt_orphan_position(self, account: dict, client, pos, group_id: int, leg: str) -> None:
        """
        Posicion propia sin pierna pendiente que la explique (p. ej. una orden
        duplicada, o una ejecucion posterior a un fallo inesperado de
        open_group). Se pone bajo gestion con los niveles y el canal de su
        grupo si se conocen (otra pierna viva o el state_store); si no, con su
        SL/TP de MT5 y sin canal -- avisando que los cierres del canal no la
        alcanzaran.
        """
        name = account["name"]
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        direction = "BUY" if getattr(pos, "type", 0) == 0 else "SELL"
        sibling = next((t for t in self.trades.values() if t.group_id == group_id and t.account_name == name), None)
        doc = None
        if sibling is None and self.state_store:
            try:
                doc, _ = await self._maybe_await(self.state_store.load_group(group_id))
                if doc is not None and doc.get("account_name") != name:
                    doc = None
            except Exception as e:
                log.warning("[TM][ORPHAN] fallo leyendo group_id=%s del store: %s", group_id, e)
                doc = None
        levels_synced = True
        if sibling is not None:
            source = "group"
            trade = self._insert_leg(account, group_id, leg, pos.ticket, symbol=pos.symbol, direction=direction,
                                     sl=sibling.planned_sl, tp1=sibling.tp1_price, tp2=sibling.tp2_price,
                                     entry_price=entry, chat_id=sibling.chat_id)
            levels_synced = await self._sync_broker_levels(client, trade, pos)
        elif doc is not None and doc.get("legs", {}).get(leg) is not None:
            source = "store"  # estado completo (BE, trailing) tal como se persistio
            self._reconstruct_leg_from_doc(account, doc, leg, pos)
            trade = self.trades[pos.ticket]
            if trade.entry_price is None:
                trade.entry_price = entry
        else:
            source = "store" if doc is not None else None
            tp = float(getattr(pos, "tp", 0.0) or 0.0) or None
            tp1, tp2 = (tp, None) if leg == "tp1" else (None, tp)
            if doc is not None:
                tp1, tp2 = doc.get("tp1_price", tp1), doc.get("tp2_price", tp2)
            trade = self._insert_leg(account, group_id, leg, pos.ticket, symbol=pos.symbol, direction=direction,
                                     sl=float(getattr(pos, "sl", 0.0) or 0.0), tp1=tp1, tp2=tp2,
                                     entry_price=entry, chat_id=doc.get("chat_id") if doc is not None else None)
        self._next_group_id = max(self._next_group_id, group_id + 1)
        await self._apply_be_if_owed(account, client, None, trade)
        await self._persist_group(group_id)
        log.warning("[TM][ORPHAN] posicion sin gestion adoptada: cuenta=%s ticket=%s group_id=%s leg=%s origen=%s",
                    name, pos.ticket, group_id, leg, source)
        channel_name = resolve_channel_name(trade.chat_id, self._channel_names())
        warning = ("" if trade.chat_id is not None else
                   " No se conoce su canal de origen: los mensajes de cierre del canal NO la alcanzaran -- "
                   "revisar manualmente.")
        if not levels_synced:
            warning += " No se pudieron aplicar en MT5 los SL/TP de su grupo: conserva los propios, revisar."
        await self._notify(
            "orphan_position_adopted", channel="both", group_id=group_id, leg=leg, ticket=pos.ticket,
            account=name, symbol=pos.symbol, direction=direction, entry_price=entry, sl=trade.planned_sl,
            chat_id=trade.chat_id, channel_name=channel_name, source=source,
            message=(f"⚠️ POSICION SIN GESTION DETECTADA — Cuenta {name} (grupo {group_id}, pierna '{leg}')\n"
                     f"{pos.symbol} {direction} ticket={pos.ticket}, entrada {self._fmt_price(entry)}, "
                     f"SL {self._fmt_price(trade.planned_sl)}. Ahora queda bajo gestion.{warning}"),
        )

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
        info = await self._get_close_deal_info(client, closed_trade.ticket, closed_trade)
        if info is None:
            # Deal aun no propago o fallo de red -- comportamiento previo:
            # asumir TP1 para no bloquear el BE automatico en el caso comun.
            return {"cause": "tp1" if closed_trade.leg == "tp1" else "unknown", "price": None,
                    "reason": None, "profit": None, "volume": None, "commission": None, "swap": None}
        reason = info["reason"]
        if reason == self.DEAL_REASON_TP and closed_trade.leg == "tp1":
            cause = "tp1"
        elif reason == self.DEAL_REASON_TP and closed_trade.leg == "runner":
            cause = "tp2"
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
        self._tp1_hit_groups.add(tp1_leg.group_id)
        runner = next((t for t in self.trades.values() if t.group_id == tp1_leg.group_id and t.leg == "runner"), None)
        if not runner:
            # Grupo sin runner abierto (no se abrio, o sigue sin confirmar --
            # si aparece, _apply_be_if_owed lo pone en BE al adoptarlo). El TP1
            # se notifica igual: antes se perdia en silencio.
            deal_info = await self._get_close_deal_info(client, tp1_leg.ticket, tp1_leg)
            channel_name = resolve_channel_name(tp1_leg.chat_id, self._channel_names())
            close_price = deal_info["price"] if deal_info else None
            close_volume = deal_info["volume"] if deal_info else None
            pnl_money = deal_info["profit"] if deal_info else None
            message = build_tp1_hit_message(
                channel_name=channel_name, group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
                direction=tp1_leg.direction, close_price=close_price, close_volume=close_volume,
                pnl_money=pnl_money, account_currency="USD",
            ).replace("SL movido a break-even", "Grupo sin runner abierto")
            await self._notify(
                "tp1_hit", channel="both", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, runner_ticket=None,
                chat_id=tp1_leg.chat_id, channel_name=channel_name, close_price=close_price,
                close_volume=close_volume, pnl_money=pnl_money, message=message,
            )
            return

        deal_info = await self._get_close_deal_info(client, tp1_leg.ticket, tp1_leg)
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

    async def _get_close_deal_info(self, client, ticket: int, managed_trade: Optional["ManagedTrade"] = None) -> Optional[dict]:
        """
        Busca los deals de salida (DEAL_ENTRY_OUT=1) de `ticket` en el
        historial de MT5, con toda la informacion necesaria para
        auditoria/notificacion: precio (del deal mas reciente, usado para
        clasificar TP/SL/externo), causa (reason, del mas reciente), y P&L/
        volumen/comision/swap SUMADOS de todos los deals de salida nuevos
        desde la ultima vez que se audito este ticket. Nunca debe tumbar el
        flujo de notificacion -- cualquier fallo (de red, o el deal aun no
        propago) devuelve None.

        managed_trade, si viene, filtra por deal.time > last_audited_deal_time
        y lo actualiza al deal mas reciente encontrado -- sin esto (o en
        llamadas sin managed_trade, p.ej. tests viejos) se usa el ultimo deal
        unicamente, el comportamiento previo. Necesario porque una posicion
        puede acumular varios cierres parciales (manuales o del TP2 partial)
        antes de cerrarse del todo: tomar solo el ultimo deal perdia el P&L
        de los anteriores (caso real grupo 169, 2026-09-28, un cierre externo
        de dos partials donde solo se audito el segundo).
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
        since = managed_trade.last_audited_deal_time if managed_trade else 0
        new_deals = [d for d in out_deals if getattr(d, "time", 0) > since] or out_deals
        closing = max(new_deals, key=lambda d: getattr(d, "time", 0))
        if managed_trade is not None:
            managed_trade.last_audited_deal_time = int(getattr(closing, "time", since))
        return {
            "price": float(closing.price),
            "reason": getattr(closing, "reason", None),
            "profit": sum(float(getattr(d, "profit", 0.0) or 0.0) for d in new_deals),
            "volume": sum(float(getattr(d, "volume", 0.0) or 0.0) for d in new_deals),
            "commission": sum(float(getattr(d, "commission", 0.0) or 0.0) for d in new_deals),
            "swap": sum(float(getattr(d, "swap", 0.0) or 0.0) for d in new_deals),
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

    async def _move_group_legs_to_be(self, account, client, legs: list[ManagedTrade], *, reason: str) -> Optional[dict]:
        """
        Mueve a breakeven (precio de entrada compartido por ambas piernas)
        cualquier pierna de `legs` (tp1, runner, o ambas) cuyo SL en vivo
        siga peor que BE. Piernas ya en BE o mejor se omiten. Devuelve
        {"be_price": ..., "leg_ok": {leg_name: bool}} para las piernas que
        SI necesitaban moverse, o None si ninguna lo necesitaba.

        Compartido por move_sl_be_now (accion dedicada) y close_partial_now
        (BE automatico tras un cierre parcial, decision de producto
        2026-09-14: proteger capital siempre que se toma parcial, con o sin
        mencion explicita de BE en el mensaje del canal) -- misma logica de
        "que piernas siguen vivas y peor que BE" en ambos casos.
        """
        live_legs = [t for t in legs if t.entry_price is not None]
        if not live_legs:
            return None
        be_price: float = live_legs[0].entry_price  # type: ignore[assignment]
        is_buy = live_legs[0].direction == "BUY"

        legs_needing_be = []
        for leg in live_legs:
            pos_list = await self._call(client.positions_get, ticket=leg.ticket)
            current_sl = float(pos_list[0].sl) if pos_list else None
            worse_than_be = current_sl is None or (current_sl < be_price if is_buy else current_sl > be_price)
            if worse_than_be:
                legs_needing_be.append(leg)
        if not legs_needing_be:
            return None

        leg_ok = {}
        for leg in legs_needing_be:
            leg_ok[leg.leg] = await self._force_runner_sl(account, client, leg, be_price, reason=reason)
        for leg in legs_needing_be:
            if leg_ok.get(leg.leg):
                leg.be_applied = True
                # Same fix as _on_tp1_leg_closed: keep planned_sl in sync
                # with the real, just-applied BE price -- see that call
                # site's comment for why this matters across a restart's
                # reconcile_from_mt5.
                leg.planned_sl = be_price
        return {"be_price": be_price, "leg_ok": leg_ok}

    async def _force_full_close(self, account, client, ticket: int, *, attempts: int = 3, retry_delay_seconds: float = 0.2) -> bool:
        """
        Cierra el 100% de `ticket` via partial_close, reintentando unas pocas
        veces ante un rechazo transitorio del broker antes de darse por
        vencido -- mismo patron que _force_runner_sl ya usa para order_send.

        close_now es una instruccion absoluta: el usuario pide estar
        completamente fuera del mercado YA, casi siempre porque el trade va
        en contra (o simplemente quiere salir sin importar el P&L). A
        diferencia de close_partial_now, no existe un fallback sensato tipo
        "aplica BE en su lugar" -- eso no cumple lo que se pidio y, si el
        trade esta en negativo, tampoco protege nada real. Lo correcto es
        agotar los reintentos antes de reportar fallo, no rendirse al primer
        rechazo.

        Real production incident (group 127, 2026-09-13): un partial_close
        colgado (timeout) lanzo un asyncio.TimeoutError crudo que escapo del
        loop por-pierna y aborto TODO el grupo via el try/except externo --
        la pierna hermana (runner) nunca se intento siquiera. Re-lanzado aqui
        como MT5CallTimeoutError para que el caller pueda aislar el timeout
        de UNA pierna sin dejar de intentar la otra.
        """
        ok = False
        for attempt in range(1, attempts + 1):
            try:
                ok = bool(await self._call(client.partial_close, account, ticket, 100))
            except asyncio.TimeoutError:
                raise MT5CallTimeoutError(f"partial_close colgado cerrando ticket={ticket}")
            if ok:
                break
            if attempt < attempts:
                await asyncio.sleep(retry_delay_seconds)
        return ok

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
        # tp explicito, nunca omitido: omitir "tp" en un request action=6
        # puede limpiar o preservar el TP existente segun el broker, asi que
        # siempre lo fijamos explicitamente en vez de depender de ese
        # comportamiento implicito. El runner lleva TP real en tp2 solo en
        # runner_mode=fixed_tp2 (en trailing es 0.0) -- y la pierna tp1 SI tiene un TP fijo real en
        # el broker (real production bug, group 132, 2026-09-14: mover BE a
        # la pierna tp1 via este mismo helper, ya sea desde move_sl_be_now o
        # el BE automatico de close_partial_now, mandaba tp=0.0
        # incondicionalmente y borraba ese TP -- el precio cruzo tp1_price
        # sin ninguna orden ahi que lo ejecutara, y la pierna quedo viva
        # hasta que el precio retrocedio y toco el SL en BE en su lugar, un
        # viaje de ida y vuelta completo que el TP fijo habria evitado).
        tp_to_keep = self._broker_tp_for_leg(runner.leg, runner.tp1_price, runner.tp2_price)
        req = {"action": 6, "position": runner.ticket, "sl": float(new_sl), "tp": float(tp_to_keep)}
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
        deal_info = await self._get_close_deal_info(client, runner.ticket, runner)
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
        # Igual que en close_partial_now: evita que el tick loop confunda esta
        # caida de volumen (iniciada por el propio sistema) con un cierre
        # parcial externo en el proximo tick.
        if remaining_volume is not None:
            runner.last_known_volume = remaining_volume
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

    async def _close_group_now(self, group_id: int, *, chat_id: str, raw_text: str, action: str,
                               event_name: str = "mgmt_close_now",
                               failure_event: str = "mgmt_close_now_partial_failure",
                               message_builder=None) -> dict:
        """
        Cierra al 100% ambas piernas de un grupo y notifica el resultado. Compartido por
        la rama close_now de apply_mgmt_action y por close_opposite_groups_before_tp1
        (mismo cierre, reintentos y aislamiento por pierna; solo cambian el evento y el
        mensaje). Nunca lanza: devuelve {"group_id", "status", ["reason"]}.
        """
        message_builder = message_builder or build_close_now_message
        try:
            legs = [t for t in self.trades.values() if t.group_id == group_id]
            pending = self._pending.get(group_id)
            if pending is not None:
                # La pierna sin confirmar se cierra en cuanto MT5 la muestre
                # (_adopt_pending_leg), no se adopta.
                pending.cancel_reason = raw_text
                if not legs:
                    channel_name = resolve_channel_name(chat_id, self._channel_names())
                    await self._notify(
                        "pending_close_requested", channel="both", group_id=group_id, chat_id=chat_id,
                        channel_name=channel_name, raw_text=raw_text, action=action,
                        message=(f"⚠️ CIERRE SOLICITADO — Canal: {channel_name} (grupo {group_id})\n"
                                 f"Motivo: \"{raw_text}\"\nEl grupo seguia sin confirmar en MT5: si la orden "
                                 f"se ejecuto, se cerrara en cuanto aparezca."),
                    )
                    return {"group_id": group_id, "status": "pending_cancelled"}
            account = self._ensure_account_dict(legs[0].account_name)
            if not account:
                log.error("[TM][MGMT] no se pudo resolver la cuenta para group_id=%s chat_id=%s", group_id, chat_id)
                await self._notify(
                    "mgmt_account_unresolved",
                    message=f"No se pudo resolver la cuenta del grupo {group_id} al aplicar '{action}'.",
                    chat_id=chat_id, group_id=group_id, action=action,
                )
                return {"group_id": group_id, "status": "failed", "reason": "account_unresolved"}
            client = self.mt5._client_for(account)
            channel_name = resolve_channel_name(chat_id, self._channel_names())
            leg_summaries = []
            leg_results = []
            any_leg_failed = False
            for t in list(legs):
                # Cada pierna se aisla en su propio try/except: un
                # timeout en tp1 no debe impedir que se intente
                # cerrar el runner tambien (ver docstring de
                # _force_full_close para el incidente real que esto
                # arregla).
                # Mientras se cierra, el tick de run_forever no debe verla
                # desaparecer de MT5 y reportarla tambien como
                # external_close_detected (P&L duplicado en el audit log,
                # grupos reales 146/149/150/160). Si el cierre falla, el
                # finally la devuelve al tick normal.
                self._mgmt_closing.add(t.ticket)
                try:
                    try:
                        ok = await self._force_full_close(account, client, t.ticket)
                    except MT5CallTimeoutError:
                        any_leg_failed = True
                        log.error("[TM][MGMT] close_now: timeout cerrando ticket=%s leg=%s group_id=%s",
                                  t.ticket, t.leg, group_id)
                        leg_summaries.append(f"{t.leg} (ticket={t.ticket}, timeout: MT5 no respondio)")
                        continue
                    if not ok:
                        any_leg_failed = True
                        log.error("[TM][MGMT] partial_close rechazado por el broker | ticket=%s leg=%s group_id=%s",
                                  t.ticket, t.leg, group_id)
                        leg_summaries.append(f"{t.leg} (ticket={t.ticket}, rechazado)")
                        continue
                    deal_info = await self._get_close_deal_info(client, t.ticket, t)
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
                finally:
                    self._mgmt_closing.discard(t.ticket)
            if any_leg_failed:
                message = build_partial_failure_message(
                    channel_name=channel_name, group_id=group_id, leg_summaries=leg_summaries,
                )
                await self._notify(
                    failure_event, channel="both", group_id=group_id, chat_id=chat_id,
                    channel_name=channel_name, raw_text=raw_text, message=message,
                )
                return {"group_id": group_id, "status": "failed", "reason": "partial_close_rejected"}
            total_pnl_money = sum(lr["pnl_money"] for lr in leg_results if lr["pnl_money"] is not None)
            message = message_builder(
                channel_name=channel_name, group_id=group_id, raw_text=raw_text,
                leg_results=leg_results, total_pnl_money=total_pnl_money,
            )
            await self._notify(
                event_name, channel="both", group_id=group_id, chat_id=chat_id,
                channel_name=channel_name, raw_text=raw_text,
                # Fix 5: sin estos kwargs, leg_results/total_pnl_money solo
                # sobrevivian como prosa dentro de `message` y nunca llegaban
                # al payload de auditoria ni a la Data Table de n8n.
                # mgmt_close_partial_now ya lo hacia bien; este no.
                leg_results=leg_results, total_pnl_money=total_pnl_money,
                message=message,
            )
            await self._close_group_in_store(group_id)
            return {"group_id": group_id, "status": "closed"}
        except Exception as e:
            log.error("[TM][MGMT] excepcion cerrando group_id=%s chat_id=%s: %s", group_id, chat_id, e)
            return {"group_id": group_id, "status": "failed", "reason": "exception"}

    async def close_opposite_groups_before_tp1(self, *, chat_id: Optional[str], symbol: str, direction: str) -> list[dict]:
        """
        Llega una señal `direction` de `chat_id`: cierra los grupos de ESE canal y
        simbolo en la direccion contraria que aun no llegaron a TP1 (ninguna pierna
        con be_applied). Los que ya estan protegidos en BE se dejan correr.

        Caso real 2026-09-25: TradePulse mando SELL (grupo 167) y, sin ninguna orden
        de cierre, BUY 2h40m despues; la SELL siguio abierta hasta su SL (-$88.20)
        mientras la BUY tocaba TP1. Backtest de 3 meses: 16 casos asi; cerrar el
        grupo contrario si aun no tocaba TP1 dio +$248 frente a dejarlo abierto
        (cerrar tambien los que ya estaban en BE daba menos, +$220).
        """
        if chat_id is None:
            return []
        opposite = "SELL" if direction.upper() == "BUY" else "BUY"
        self._cancel_expired_pendings(chat_id, f"Señal {direction.upper()} {symbol} contraria", direction=opposite,
                                      symbol=symbol)
        targets = []
        for group_id in self.find_active_groups_for_chat(chat_id):
            legs = [t for t in self.trades.values() if t.group_id == group_id]
            meta = self._group_symbol_direction(group_id)
            if meta is None or meta[0] != symbol or meta[1] != opposite:
                continue
            if any(t.be_applied for t in legs):
                log.info("[TM][OPPOSITE] grupo %s %s ya protegido en BE, se deja correr pese a señal %s",
                         group_id, opposite, direction.upper())
                continue
            targets.append(group_id)
        for group_id in targets:
            log.info("[TM][OPPOSITE] cerrando grupo %s %s por señal %s contraria (chat_id=%s)",
                     group_id, opposite, direction.upper(), chat_id)
        results = list(await asyncio.gather(*(
            self._close_group_now(
                group_id, chat_id=chat_id, action="opposite_signal_close",
                raw_text=f"Señal {direction.upper()} {symbol} recibida con este grupo {opposite} abierto y sin llegar a TP1",
                event_name="opposite_signal_close", failure_event="opposite_signal_close_failure",
                message_builder=build_opposite_signal_close_message,
            )
            for group_id in targets
        )))
        return results

    async def apply_mgmt_action(self, *, action: str, chat_id: str, raw_text: str, correction: Optional[dict], percent: Optional[float] = None, direction_hint: Optional[str] = None) -> dict:
        """
        Ejecuta una decision de /mgmt/action (chat_id-scoping spec seccion 5).
        Resuelve TODOS los grupos activos del `chat_id` que mando el mensaje
        de gestion -- no un simbolo, y no solo el grupo mas reciente -- y
        aplica la accion segun su propia semantica (ver cada rama abajo).
        """
        if action == "close_now":
            self._cancel_expired_pendings(chat_id, raw_text, direction=direction_hint)
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
            if direction_hint:
                group_ids, excluded = self._filter_groups_by_direction(group_ids, direction_hint)
                if excluded:
                    await self._notify(
                        "mgmt_direction_filtered",
                        chat_id=chat_id, direction_hint=direction_hint, excluded_group_ids=excluded, action=action,
                        message=(f"Acción '{action}' limitada a grupos {direction_hint}. "
                                 f"Grupos excluidos por dirección opuesta: {excluded}."),
                    )
                if not group_ids:
                    log.info("[TM][MGMT] direction_hint=%s dejo cero grupos para chat_id=%s", direction_hint, chat_id)
                    await self._notify(
                        "mgmt_no_active_trade",
                        message=f"Acción '{action}' recibida pero no hay trades activos de dirección {direction_hint} para este chat. Texto: {raw_text!r}",
                        chat_id=chat_id, action=action,
                    )
                    return {"status": "no_active_trade"}
            # Paralelo entre grupos (y por lo tanto entre cuentas): cada
            # group_id es independiente (tickets/cuenta propios), y un activo
            # volatil castiga la latencia acumulada de cerrar uno por uno --
            # ver caso real 2026-10-01, ~0.77s entre la apertura secuencial
            # en dos cuentas. _close_group_now ya aisla sus propias
            # excepciones, asi que gather no necesita return_exceptions.
            results = list(await asyncio.gather(
                *(self._close_group_now(group_id, chat_id=chat_id, raw_text=raw_text, action=action)
                  for group_id in group_ids)
            ))
            return {"status": "completed", "results": results}

        # Cierre parcial / BE sobre un grupo sin ninguna pierna confirmada en MT5
        # no tiene sobre que actuar; los grupos con piernas reales siguen normal.
        if action in ("close_partial_now", "move_sl_be_now"):
            # Piernas aun sin confirmar: se recuerda el pedido y se aplica al
            # adoptarlas (BE; y no se abre un runner nuevo tardio -- ver
            # _open_late_leg). Las piernas ya confirmadas siguen el camino normal.
            for g in group_ids:
                if g in self._pending:
                    self._pending[g].be_requested = True
            pending_only = [g for g in group_ids if not any(t.group_id == g for t in self.trades.values())]
            if pending_only:
                log.info("[TM][MGMT] %s: grupos %s aun sin confirmar en MT5 -- BE al confirmarse", action, pending_only)
                group_ids = [g for g in group_ids if g not in pending_only]

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
            # Paralelo entre grupos/cuentas -- ver nota en close_now. Cada
            # _apply_close_partial_now_for_group es autonoma (su propio
            # try/except, nunca propaga), asi que gather no necesita
            # return_exceptions.
            results = list(await asyncio.gather(*(
                self._apply_close_partial_now_for_group(
                    group_id, chat_id=chat_id, action=action, raw_text=raw_text, effective_percent=effective_percent,
                )
                for group_id in group_ids
            )))
            return {"status": "completed", "results": results}

        if action == "move_sl_be_now":
            results = list(await asyncio.gather(*(
                self._apply_move_sl_be_now_for_group(group_id, chat_id=chat_id, action=action, raw_text=raw_text)
                for group_id in group_ids
            )))
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

    async def _apply_close_partial_now_for_group(self, group_id: int, *, chat_id: str, action: str, raw_text: str,
                                                  effective_percent: float) -> dict:
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
                return {"group_id": group_id, "status": "failed", "reason": "account_unresolved"}
            client = self.mt5._client_for(account)
            channel_name = resolve_channel_name(chat_id, self._channel_names())
            leg_results = []
            any_leg_failed = False
            leg_summaries = []
            # Product decision 2026-09-14: only the runner leg takes
            # the discretionary partial. tp1 has a fixed job (exit in
            # full at its own TP1) -- partial-closing it too would
            # double the "lock in profit" mechanism and shrink the
            # volume it exits with for no risk-management reason,
            # since BE (applied below, unconditionally) already
            # protects the position. Same pattern as TP2's own
            # partial-close mechanic, which also only ever touches
            # the runner.
            partial_legs = [t for t in legs if t.leg == "runner"]
            for t in partial_legs:
                # Real production incident (group 129, 2026-09-14): a
                # hung partial_close raised a bare asyncio.TimeoutError
                # that escaped this loop entirely and skipped the
                # automatic-BE step below (added for the group-128
                # fix) -- the position sat unprotected until the
                # call's background thread finally landed and closed
                # it externally. A timeout on one leg must not skip
                # the rest of this action, same as close_now's
                # per-leg isolation.
                try:
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
                    deal_info = await self._get_close_deal_info(client, t.ticket, t)
                    # t sigue abierto con menos volumen (no es un cierre
                    # total) -- sin esto, el tick loop veria caer el
                    # volumen en vivo en el proximo tick y lo marcaria
                    # como cierre parcial externo, duplicando este
                    # mismo P&L en un segundo evento de auditoria.
                    remaining_pos = await self._call(client.positions_get, ticket=t.ticket)
                    if remaining_pos:
                        t.last_known_volume = float(remaining_pos[0].volume)
                    leg_results.append({
                        "leg": t.leg,
                        "close_price": deal_info["price"] if deal_info else None,
                        "close_volume": deal_info["volume"] if deal_info else None,
                        "pnl_money": deal_info["profit"] if deal_info else None,
                    })
                except asyncio.TimeoutError:
                    any_leg_failed = True
                    leg_summaries.append(f"{t.leg} (ticket={t.ticket}, timeout: MT5 no respondio)")
                    log.error("[TM][MGMT] close_partial_now: timeout en ticket=%s leg=%s group_id=%s",
                              t.ticket, t.leg, group_id)

            # Product decision 2026-09-14: BE protection is applied
            # unconditionally after a close_partial_now, independent
            # of whether the partial itself succeeded -- protecting
            # capital is always correct once the message asked to
            # lock in profit, and must not depend on the message
            # explicitly mentioning BE (real bug: n8n's classifier
            # had to pick ONE action for messages combining "secure
            # partials" + "set BE", silently dropping whichever one
            # lost -- see group 128 incident). Applies to every leg
            # still open (tp1 if not yet hit, and the runner with
            # whatever volume remains after the partial attempt).
            try:
                await self._move_group_legs_to_be(account, client, legs, reason="mgmt-close-partial-auto-BE")
            except MT5CallTimeoutError:
                log.error("[TM][MGMT] timeout aplicando BE automatico tras close_partial_now group_id=%s chat_id=%s", group_id, chat_id)

            if any_leg_failed:
                message = build_partial_failure_message(channel_name=channel_name, group_id=group_id, leg_summaries=leg_summaries)
                await self._notify(
                    "mgmt_close_partial_now_failure", channel="both", group_id=group_id, chat_id=chat_id,
                    channel_name=channel_name, raw_text=raw_text, percent_requested=effective_percent,
                    leg_summaries=leg_summaries, message=message,
                )
                return {"group_id": group_id, "status": "failed", "reason": "partial_close_rejected"}
            message = build_close_partial_now_message(
                channel_name=channel_name, group_id=group_id, raw_text=raw_text,
                percent_requested=effective_percent, leg_results=leg_results,
            )
            # total_pnl_money a nivel superior, como mgmt_close_now: antes el
            # P&L realizado del parcial solo vivia dentro de leg_results y
            # cualquier suma del audit log lo omitia.
            total_pnl_money = sum(lr["pnl_money"] for lr in leg_results if lr.get("pnl_money") is not None)
            await self._notify(
                "mgmt_close_partial_now", channel="both", group_id=group_id, chat_id=chat_id,
                channel_name=channel_name, raw_text=raw_text, percent_requested=effective_percent,
                leg_results=leg_results, total_pnl_money=total_pnl_money, message=message,
            )
            await self._persist_group(group_id)
            return {"group_id": group_id, "status": "applied"}
        except Exception as e:
            log.error("[TM][MGMT] excepcion en close_partial_now group_id=%s chat_id=%s: %s", group_id, chat_id, e)
            return {"group_id": group_id, "status": "failed", "reason": "exception"}

    async def _apply_move_sl_be_now_for_group(self, group_id: int, *, chat_id: str, action: str, raw_text: str) -> dict:
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
                return {"group_id": group_id, "status": "failed", "reason": "account_unresolved"}
            client = self.mt5._client_for(account)
            runner = next((t for t in legs if t.leg == "runner"), None)
            if not runner and group_id in self._pending:
                # Runner aun sin confirmar en MT5 (recibira BE al adoptarse, ver
                # be_requested): proteger ya la pierna tp1 que si esta abierta.
                runner = next((t for t in legs if t.leg == "tp1"), None)
            if not runner:
                await self._notify(
                    "mgmt_no_runner_leg",
                    message=f"Grupo {group_id} no tiene runner leg activo; no se pudo mover SL a BE.",
                    chat_id=chat_id, group_id=group_id,
                )
                return {"group_id": group_id, "status": "no_active_trade"}
            if runner.entry_price is None:
                log.error("[TM][MGMT] move_sl_be_now: runner=%s no tiene entry_price registrado (group_id=%s)",
                          runner.ticket, group_id)
                return {"group_id": group_id, "status": "failed", "reason": "no_entry_price"}
            # Fix (group 128, 2026-09-13/14): un mensaje de canal puede
            # pedir BE antes de que el sistema haya registrado el TP1
            # de este grupo (el canal afirmaba "ROAD TO TP1" pero
            # tp1_hit nunca se disparo para ese grupo). Si la pierna
            # tp1 sigue viva en ese momento, tambien debe moverse a
            # BE -- de lo contrario queda con su SL original mientras
            # el runner si se protege, y esa pierna se come una
            # perdida completa que BE habria evitado.
            tp1_leg = next((t for t in legs if t.leg == "tp1"), None)
            legs_to_move = [runner] + ([tp1_leg] if tp1_leg and tp1_leg is not runner else [])
            try:
                be_result = await self._move_group_legs_to_be(account, client, legs_to_move, reason="mgmt-fallback-BE")
            except MT5CallTimeoutError:
                log.error("[TM][MGMT] timeout aplicando BE via mgmt_action group_id=%s chat_id=%s", group_id, chat_id)
                return {"group_id": group_id, "status": "timeout"}
            if be_result is None:
                await self._notify(
                    "mgmt_move_sl_be_already_satisfied", group_id=group_id, chat_id=chat_id,
                    message=f"Grupo {group_id}: SL ya estaba en breakeven o mejor, no se aplico ningun cambio.",
                )
                return {"group_id": group_id, "status": "already_satisfied"}
            be_price = be_result["be_price"]
            leg_ok = be_result["leg_ok"]
            if all(leg_ok.values()):
                channel_name = resolve_channel_name(chat_id, self._channel_names())
                message = build_move_sl_be_applied_message(
                    channel_name=channel_name, group_id=group_id, new_sl=be_price, raw_text=raw_text,
                )
                await self._notify(
                    "mgmt_move_sl_be_applied", channel="both", group_id=group_id, chat_id=chat_id,
                    channel_name=channel_name, raw_text=raw_text, message=message,
                )
                await self._persist_group(group_id)
                return {"group_id": group_id, "status": "applied"}
            failed_legs = [leg_name for leg_name, ok in leg_ok.items() if not ok]
            log.error("[TM][MGMT] move_sl_be_now: fallo moviendo BE para legs=%s group_id=%s", failed_legs, group_id)
            return {"group_id": group_id, "status": "failed", "reason": "partial_be_rejected", "failed_legs": failed_legs}
        except Exception as e:
            log.error("[TM][MGMT] excepcion aplicando BE a group_id=%s chat_id=%s: %s", group_id, chat_id, e)
            return {"group_id": group_id, "status": "failed", "reason": "exception"}

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
