
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import asyncio
import time
import re

from services.common.timewindow import parse_windows, in_windows
import logging

log = logging.getLogger("trade_orchestrator.mt5_executor")

from .trade_utils import safe_comment, pips_to_price, calcular_lotaje
from .notifications.n8n import N8nNotifierAdapter

@dataclass
class MT5OpenResult:
    tickets_by_account: dict[str, int]
    errors_by_account: dict[str, str]

class MT5Executor:
    def _notify_bg(self, account_name, message):
        # Notifica por Telegram si el adaptador está disponible
        try:
            if hasattr(self, 'notifier') and self.notifier:
                asyncio.create_task(self.notifier.notify(account_name, message))
        except Exception as e:
            log.error(f"[NOTIFY][ERROR] {account_name}: {e}")

    async def modify_sl(self, account: dict, ticket: int, new_sl: float, reason: str = "", provider_tag: str = None, reintentos: int = 5) -> bool:
        """
        Modifica el SL de la posición indicada por ticket a new_sl.
        Loguea el SL actual antes y después, el SL propuesto y el stop_level del símbolo.
        """
        client = self._client_for(account)
        pos_list = client.positions_get(ticket=int(ticket))
        if not pos_list:
            self._notify_bg(account["name"], f"❌ SL update falló | Ticket: {int(ticket)} | No se encontró la posición")
            return False
        pos = pos_list[0]
        symbol = pos.symbol
        info = client.symbol_info(symbol)
        if not info:
            self._notify_bg(account["name"], f"❌ SL update falló | Ticket: {int(ticket)} | No se encontró info de símbolo")
            return False
        point = float(getattr(info, "point", 0.0))
        is_buy = (int(getattr(pos, "type", 0)) == 0)
        sl_actual = float(getattr(pos, "sl", 0.0))
        stop_level = float(getattr(info, "stops_level", 0.0)) * point
        price_current = float(getattr(pos, "price_current", 0.0))
        from services.common.config import Settings
        from .trade_utils import calcular_sl_respetando_maximo
        sl_max_pips = Settings.sl_max_pips()
        # Centralizar el cálculo del SL respetando el máximo
        sl_pips = abs((price_current - new_sl) / (0.1 if symbol.upper().startswith("XAU") else point))
        new_sl = calcular_sl_respetando_maximo(symbol, price_current, "BUY" if is_buy else "SELL", sl_pips, point, sl_max_pips)
        # Validar que el nuevo SL cumple con el mínimo stop level
        if is_buy:
            min_sl = price_current - stop_level
            if new_sl > min_sl:
                log.warning(f"[SL-UPDATE] SL ({new_sl}) está demasiado cerca del precio actual ({price_current}), mínimo permitido: {min_sl}. Ajustando SL a {min_sl}")
                new_sl = round(min_sl, 2 if symbol.upper().startswith("XAU") else 5)
        else:
            max_sl = price_current + stop_level
            if new_sl < max_sl:
                log.warning(f"[SL-UPDATE] SL ({new_sl}) está demasiado cerca del precio actual ({price_current}), máximo permitido: {max_sl}. Ajustando SL a {max_sl}")
                new_sl = round(max_sl, 2 if symbol.upper().startswith("XAU") else 5)
        # Usar provider_tag actualizado en el comentario si se proporciona
        comment_tag = f"{provider_tag}-SLUPD-{reason}" if provider_tag else f"SLUPD-{reason}"
        req = {
            "action": 6,  # TRADE_ACTION_SLTP
            "position": int(ticket),
            "sl": float(new_sl),
            "tp": float(getattr(pos, "tp", 0.0)),
            "comment": self._safe_comment(comment_tag),
        }
        for intento in range(reintentos):
            res = await self._best_filling_order_send(client, symbol, req, account.get('name'))
            log.debug(f"[ORDER_SEND][SL-UPDATE][{intento+1}/{reintentos}] Respuesta completa de order_send: {repr(res)}")
            ok = bool(res and getattr(res, "retcode", None) in (10009, 10008))
            pos_list_after = client.positions_get(ticket=int(ticket))
            sl_after = float(getattr(pos_list_after[0], "sl", 0.0)) if pos_list_after else None
            log.debug(f"[SL-UPDATE] SL después del intento: {sl_after}")
            if ok:
                self._notify_bg(account["name"], f"✅ SL actualizado | Ticket: {int(ticket)} | SL: {new_sl:.5f}")
                return True
            # Si falla, intentar con un SL un poco más alejado pero nunca mayor a sl_max_pips
            if is_buy:
                new_sl -= pips_to_price(symbol, 1, point)  # Alejar 1 pip
                sl_pips = abs((price_current - new_sl) / (0.1 if symbol.upper().startswith("XAU") else point))
                new_sl = calcular_sl_respetando_maximo(symbol, price_current, "BUY", sl_pips, point, sl_max_pips)
            else:
                new_sl += pips_to_price(symbol, 1, point)
                sl_pips = abs((price_current - new_sl) / (0.1 if symbol.upper().startswith("XAU") else point))
                new_sl = calcular_sl_respetando_maximo(symbol, price_current, "SELL", sl_pips, point, sl_max_pips)
        self._notify_bg(account["name"], f"❌ SL update falló tras {reintentos} intentos | Ticket: {int(ticket)} | retcode={getattr(res,'retcode',None)} {getattr(res,'comment',None)}")
        return False
    def _safe_comment(self, tag: str) -> str:
        """
        Wrapper para safe_comment centralizado.
        """
        return safe_comment(tag, getattr(self, 'comment_prefix', 'TM'))

    def _client_for(self, account):
        """
        Devuelve el cliente MT5 para la cuenta dada.
        Usa el pool global — un solo cliente por (host, port) reutilizado en toda
        la vida del proceso. Elimina los 50-100ms de MT5.initialize() por llamada.
        """
        from .mt5_pool import MT5ClientPool
        return MT5ClientPool.get_for_account(account)

    async def _best_filling_order_send(self, client, symbol, req: dict, account_name: str = None):
        """
        Intenta enviar la orden usando el filling recomendado por el símbolo y, si falla, prueba los otros modos.
        """
        # Obtener el modo recomendado por el símbolo y loggear todos los campos relevantes
        info = client.symbol_info(symbol)
        tick = client.symbol_info_tick(symbol)
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_FOK = 3
        ORDER_FILLING_RETURN = 2
        candidates = []
        # Robust fill mode detection: try trade_fill_mode, then fill_mode, else None
        # fillmode = None
        # if info is not None:
        #     fillmode = getattr(info, "trade_fill_mode", None)
        #     if fillmode is None:
        #         fillmode = getattr(info, "fill_mode", None)
        log.debug(f"[SYMBOL-INFO][DEBUG] {info}")
        fillmode = None
        if info:
            try:
                fillmode = info.trade_fill_mode
            except AttributeError:
                try:
                    fillmode = info.fill_mode
                except AttributeError:
                    fillmode = None

        enabled = getattr(info, "visible", None) if info else None
        trademode = getattr(info, "trade_mode", None) if info else None
        bid = getattr(tick, "bid", None) if tick else None
        ask = getattr(tick, "ask", None) if tick else None
        ticktime = getattr(tick, "time", None) if tick else None
        log.info(f"[SYMBOL-STATE] symbol={symbol} enabled={enabled} trade_mode={trademode} fill_mode={fillmode} bid={bid} ask={ask} tick_time={ticktime}")
        # --- PATCH: Forzar FOK para StarTrader Demo y XAUUSD ---
        force_fok = False
        if account_name == 'StarTrader Demo' and symbol.upper() == 'XAUUSD':
            force_fok = True
        # --- FIN PATCH ---
        candidates = []
        if force_fok:
            candidates = [ORDER_FILLING_FOK]
            log.info(f"[FILLING-PATCH] Forzando FOK para cuenta StarTrader Demo y XAUUSD")
        elif fillmode is None:
            # If no fill mode is available, try all modes for compatibility
            candidates = [ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN]
            log.info(f"[FILLING-PATCH] fill_mode is None, probando todos los filling modes (IOC, FOK, RETURN)")
        else:
            if fillmode in (ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN):
                candidates.append(int(fillmode))
            for f in (ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN):
                if f not in candidates:
                    candidates.append(f)
        last_res = None
        import asyncio
        loop = asyncio.get_running_loop()
        for f in candidates:
            req_try = dict(req)
            req_try["type_filling"] = int(f)
            log.info(f"[FILLING] Probar type_filling={f} para {symbol} | req={req_try}")
            res = await loop.run_in_executor(None, client.order_send, req_try)
            last_res = res
            log.info(f"[ORDER_SEND][{account_name}] symbol={symbol} type_filling={f} req={req_try} response={repr(res)}")
            if res and getattr(res, "retcode", None) in (10009, 10008):
                return res
            # Logging detallado si la orden falla
            if res and getattr(res, "retcode", None) != 10030:
                log.warning(f"[ORDER-FAIL] retcode={getattr(res,'retcode',None)} comment={getattr(res,'comment',None)} req={req_try} res={res}")
                self._notify_bg(account_name, f"❌ Error al enviar orden: retcode={getattr(res,'retcode',None)} comment={getattr(res,'comment',None)}")
                return res
            if res and getattr(res, "retcode", None) == 10030:
                log.warning(f"[ORDER-INVALID-REQUEST] retcode=10030 comment={getattr(res,'comment',None)} req={req_try} res={res}")
                self._notify_bg(account_name, f"❌ Orden inválida: retcode=10030 comment={getattr(res,'comment',None)}")
        return last_res

    def __init__(
        self,
        accounts: list[dict],
        *,
        default_deviation: int = 50,
        magic: int = 987654,
        comment_prefix: str = "YsaCopyNew",
        notifier=None,
        trading_windows: str = "03:00-12:00,08:00-17:00",
        entry_wait_seconds: int = 60,
        entry_poll_ms: int = 500,
        entry_buffer_points: float = 0.0,
        config_provider=None,
    ):
        self.accounts = accounts
        self.default_deviation = default_deviation
        self.magic = magic
        self.comment_prefix = comment_prefix
        self.notifier = notifier
        self.windows = parse_windows(trading_windows)
        self.entry_buffer_points = entry_buffer_points
        self.entry_wait_seconds = entry_wait_seconds
        self.entry_poll_ms = entry_poll_ms
        self.config_provider = config_provider

    async def open_for_accounts(self, filtered_accounts: list[dict], *, provider_tag, symbol, direction, entry_range, sl, tps) -> "MT5OpenResult":
        """
        Ejecuta open_complete_trade usando un subconjunto de cuentas (filtered_accounts)
        en lugar de self.accounts. Evita crear una nueva instancia de MT5Executor por señal.
        """
        return await self.open_complete_trade(
            provider_tag, symbol, direction, entry_range, sl, tps,
            _accounts=filtered_accounts,
        )

    async def open_complete_trade(self, provider_tag, symbol, direction, entry_range, sl, tps, *, _accounts=None):
        tickets = {}
        errors = {}

        # _accounts permite que open_for_accounts pase un subset sin crear nueva instancia
        source_accounts = _accounts if _accounts is not None else self.accounts

        # Tomar snapshot de precio al inicio para referencia
        ref_client = self._client_for(source_accounts[0])
        ref_price = ref_client.tick_price(symbol, direction)
        ref_time = time.time()

        # Construir accounts con direction correcto para cada cuenta activa
        accounts = []
        for acct in source_accounts:
            if acct.get("active"):
                account = dict(acct)  # copia para no mutar el original
                account["symbol"] = symbol
                account["direction"] = direction
                account["sl"] = sl
                account["entry_range"] = entry_range
                account["provider_tag"] = provider_tag
                account["tps"] = tps
                accounts.append(account)

        async def send_order(account):
            entry_start = time.time()
            planned_sl_val = None  # Siempre local y explícito
            order_type = 0 if (account.get('direction', 'BUY')).upper() == 'BUY' else 1
            # Inicializar variables para evitar referencias antes de asignación
            fixed_lot = float(account.get("fixed_lot", 0))
            lot = 0.01
            risk_percent = float(account.get("risk_percent", 0))
            balance = 0.0
            # --- Variables requeridas (ajustar según integración real) ---
            # Estas variables deben ser pasadas o definidas en el contexto real de uso
            # Aquí se definen como ejemplo para evitar errores de referencia
            symbol = account.get('symbol') or 'XAUUSD'
            direction = account.get('direction', 'BUY')
            sl = account.get('sl') or 0.0
            entry_range = account.get('entry_range') or None
            provider_tag = account.get('provider_tag') or 'FAST'
            tps = account.get('tps') or []
            # tickets y errors deben estar definidos en el scope superior
            nonlocal tickets, errors

            # --- Función centralizada para obtener SL forzado si no viene ---
            from .trade_utils import calcular_sl_default
            async def get_forced_sl(client, symbol, direction, price):
                # Usar la función centralizada para calcular el SL por defecto
                point = 0.1 if symbol.upper().startswith('XAU') else 0.00001
                info = client.symbol_info(symbol)
                if info and getattr(info, 'point', None) is not None:
                    point = float(getattr(info, 'point', point))
                default_sl = getattr(self, 'default_sl_xauusd', 300) if symbol.upper().startswith('XAU') else getattr(self, 'default_sl', 100)
                return calcular_sl_default(symbol, direction, price, point, default_sl)
            name = account["name"]
            try:
                client = self._client_for(account)
                client.symbol_select(symbol, True)
                symbol_info = client.symbol_info(symbol)
                if not symbol_info:
                    log.warning(f"[SYMBOL] No symbol_info for {symbol} ({name}) after select. Symbol may not be available in MT5.")
                    self._notify_bg(name, f"⚠️ No symbol_info para {symbol} ({name}) después de select. El símbolo puede no estar disponible en MT5.")

                # --- Lógica de entrada optimizada para activos volátiles (XAUUSD) ---
                # Para oro: ventana máxima 5s, polling cada 100ms.
                # Para otros: usa entry_wait_seconds configurado (default 90s), polling entry_poll_ms.
                # Razonamiento: el oro puede moverse 50 pips en segundos — esperar 60s es contraproducente.
                is_gold = symbol.upper().startswith("XAU")
                entry_wait_max = 5.0 if is_gold else float(self.entry_wait_seconds)
                entry_poll = 0.1 if is_gold else (self.entry_poll_ms / 1000.0)

                entry_lo = None
                entry_hi = None
                if entry_range and isinstance(entry_range, (list, tuple)) and len(entry_range) == 2:
                    entry_lo = float(min(entry_range))
                    entry_hi = float(max(entry_range))
                elif entry_range and isinstance(entry_range, (float, int)):
                    entry_lo = entry_hi = float(entry_range)

                # symbol_info cacheado — no genera round-trip extra si ya se consultó
                symbol_info = client.symbol_info(symbol)
                point = 0.1 if is_gold else 0.00001
                if symbol_info and getattr(symbol_info, "point", None) is not None:
                    point = float(getattr(symbol_info, "point", point))

                if self.config_provider is not None:
                    tolerance_pips = float(self.config_provider.get("TOLERANCE_PIPS", "30"))
                else:
                    tolerance_pips = 30.0
                # Para oro: tolerancia en precio (0.1 point/pip × 30 pips = 3.0)
                pips_tolerance = tolerance_pips * point

                # Precio de referencia ya obtenido al inicio — reutilizar si está fresco (< 1s)
                age_ref = time.time() - ref_time
                if age_ref < 1.0 and ref_price and ref_price > 0:
                    price = ref_price
                    log.info("[ENTRY] Usando precio de referencia fresco: %s age=%.3fs (%s)", price, age_ref, name)
                else:
                    price = client.tick_price(symbol, direction)

                if price is None or price == 0.0:
                    log.error("[ENTRY][ERROR] No se pudo obtener precio para %s (%s). Abortando.", symbol, name)
                    return

                log.info("[ENTRY] %s %s price=%.5f range=[%s,%s] tol=%.5f wait_max=%.1fs poll=%.3fs",
                         name, symbol, price, entry_lo, entry_hi, pips_tolerance, entry_wait_max, entry_poll)

                if entry_lo is not None and entry_hi is not None:
                    is_buy = direction.upper() == "BUY"

                    def _price_in_range(p: float) -> bool:
                        if is_buy:
                            return entry_lo <= p <= entry_hi + pips_tolerance
                        return entry_lo - pips_tolerance <= p <= entry_hi

                    def _price_past_range(p: float) -> bool:
                        """Precio ya pasó el rango en dirección favorable — no tiene sentido esperar."""
                        if is_buy:
                            return p > entry_hi + pips_tolerance
                        return p < entry_lo - pips_tolerance

                    if _price_in_range(price):
                        log.info("[ENTRY] Precio %s en rango inmediato. Ejecutando %s.", price, name)
                    elif _price_past_range(price):
                        log.warning("[ENTRY] Precio %s ya pasó el rango para %s. Abortando.", price, name)
                        return
                    else:
                        # Esperar con ventana reducida para oro
                        deadline = time.time() + entry_wait_max
                        entered = False
                        while time.time() <= deadline:
                            await asyncio.sleep(entry_poll)
                            price = client.tick_price(symbol, direction)
                            if price is None or price == 0.0:
                                continue
                            if _price_in_range(price):
                                log.info("[ENTRY] %s precio %s entró en rango. Ejecutando.", name, price)
                                entered = True
                                break
                            if _price_past_range(price):
                                log.warning("[ENTRY] %s precio %s pasó rango favorable. Abortando.", name, price)
                                return
                        if not entered:
                            log.warning("[ENTRY] %s sin precio en rango tras %.1fs. Skipping.", name, entry_wait_max)
                            return
                else:
                    # Sin entry_range: ejecutar a mercado inmediatamente
                    price = client.tick_price(symbol, direction)
                    if price is None or price == 0.0:
                        log.error("[ENTRY][ERROR] No se pudo obtener precio de mercado para %s (%s).", symbol, name)
                        return

                entry_end = time.time()
                log.info("[ENTRY] Latencia entrada %s: %.3fs", name, entry_end - entry_start)
                order_type = 0 if direction == "BUY" else 1


                # --- Forzar SL si es necesario ---
                forced_sl = sl
                if not forced_sl or float(forced_sl) == 0.0:
                    forced_sl = await get_forced_sl(client, symbol, direction, price)
                    log.warning(f"[SL-FORCED] SL forzado para {name}: {forced_sl}")

                # --- REFORZAR: No abrir trade si SL es None o 0.0 ---
                if forced_sl is None or forced_sl == 0.0:
                    log.error(f"[ENTRY][ERROR] Operación ABORTADA: SL inválido (None o 0.0) para {symbol} en cuenta {name}. No se abrirá la operación.")
                    self._notify_bg(name, f"❌ Operación ABORTADA: SL inválido (None o 0.0) para {symbol}. No se abrirá la operación.")
                    errors[name] = "SL inválido (None o 0.0)"
                    return

                # planned_sl_val SIEMPRE local y explícito, debe reflejar el SL realmente usado
                try:
                    planned_sl_val = float(forced_sl) if forced_sl is not None else None
                except Exception:
                    planned_sl_val = None

                # --- Si el SL está demasiado cerca del precio actual, AJUSTAR al mínimo permitido ---
                # symbol_info ya está en caché del pool — sin round-trip extra
                min_stop_raw = None
                if symbol_info:
                    min_stop_raw = getattr(symbol_info, "stops_level", getattr(symbol_info, "stop_level", 0.0))
                else:
                    min_stop_raw = 0.0
                min_stop = float(min_stop_raw) * point if symbol_info else 0.0
                log.debug("[SL] stops_level=%s point=%s min_stop=%s", min_stop_raw, point, min_stop)
                if min_stop > 0 and abs(price - float(forced_sl)) < min_stop:
                    if direction.upper() == "BUY":
                        adjusted_sl = price - min_stop
                    else:
                        adjusted_sl = price + min_stop
                    log.warning(f"[SL-ADJUST] SL demasiado cerca del precio actual para {name}: SL={forced_sl} price={price} min_stop={min_stop}. Ajustando SL a {adjusted_sl}")
                    forced_sl = round(adjusted_sl, 2 if symbol.upper().startswith("XAU") else 5)
                    planned_sl_val = forced_sl  # Parche: reflejar ajuste también en planned_sl_val

                # FINAL PATCH: planned_sl_val debe reflejar SIEMPRE el SL realmente usado
                # Si forced_sl es válido, planned_sl_val debe ser igual a forced_sl
                if forced_sl is not None and forced_sl != 0.0:
                    planned_sl_val = float(forced_sl)
                else:
                    planned_sl_val = None

                # --- LOTE DINÁMICO O FIJO ---
                fixed_lot = float(account.get("fixed_lot", 0))
                risk_percent = float(account.get("risk_percent", 0))
                balance = 0.0
                lot = 0.01
                if fixed_lot > 0:
                    lot = fixed_lot
                elif risk_percent > 0 and forced_sl and float(forced_sl) > 0:
                    try:
                        acc_info = client.mt5.account_info()
                        if acc_info and getattr(acc_info, "balance", None) is not None:
                            balance = float(acc_info.balance)
                    except Exception as e:
                        log.warning("[LOTE] No se pudo obtener balance para %s: %s", name, e)
                    risk_money = balance * (risk_percent / 100.0)
                    sl_distance = abs(float(price) - float(forced_sl))
                    # symbol_info ya cacheado — reutilizar sin round-trip
                    tick_value = float(getattr(symbol_info, "tick_value", 0.0)) if symbol_info else 0.0
                    tick_size = float(getattr(symbol_info, "tick_size", 0.0)) if symbol_info else 0.0
                    lot_step = float(getattr(symbol_info, "volume_step", 0.01)) if symbol_info else 0.01
                    min_lot = float(getattr(symbol_info, "volume_min", 0.03)) if symbol_info else 0.03
                    lot = calcular_lotaje(balance, risk_money, sl_distance, tick_value, tick_size, lot_step, min_lot, fixed_lot)
                    log.info("[LOTE][%s] lotaje calculado=%s", name, lot)

                log.info(f"[ORDER_PREP] account={account} | lot={lot} | fixed_lot={account.get('fixed_lot')} | risk_percent={account.get('risk_percent')} | symbol={symbol} | direction={direction}")
                log.info(f"[ORDER_PREP][SL-DEBUG] forced_sl={forced_sl} planned_sl_val={planned_sl_val}")

                # --- Unificar lógica de envío con fallback robusto ---
                req = {
                    "action": 1,
                    "symbol": symbol,
                    "volume": float(lot),
                    "type": order_type,
                    "price": float(price),
                    "sl": float(forced_sl),
                    "tp": 0.0,
                    "deviation": int(self.default_deviation),
                    "magic": int(self.magic),
                    "comment": self._safe_comment(provider_tag),
                    "type_time": 0,
                }
                res = await self._best_filling_order_send(client, symbol, req, account.get('name'))
                log.info(f"[ORDER_SEND][DEBUG][OPEN] Respuesta completa de order_send: {repr(res)}")
                if res and getattr(res, "retcode", None) == 10009:
                    tickets[name] = int(getattr(res, "order", 0))
                    ticket = tickets[name]
                    log.info("open_complete_trade success acct=%s ticket=%s", name, ticket)
                    # Solo registrar/actualizar si forced_sl es válido
                    if hasattr(self, 'trade_manager') and self.trade_manager:
                        tm = self.trade_manager
                        # Si planned_sl_val es None, calcularlo usando get_forced_sl
                        if planned_sl_val is None:
                            log.warning(f"[MT5_EXECUTOR][DEBUG] planned_sl_val era None, se calculará usando get_forced_sl para registro. ticket={ticket} symbol={symbol} provider={provider_tag}")
                            # Usar el precio actual para calcular el SL por defecto
                            try:
                                price_actual = client.tick_price(symbol, direction)
                                planned_sl_val = await get_forced_sl(client, symbol, direction, price_actual)
                                log.info(f"[MT5_EXECUTOR][DEBUG] planned_sl_val calculado por defecto: {planned_sl_val}")
                            except Exception as e:
                                log.error(f"[MT5_EXECUTOR][ERROR] No se pudo calcular planned_sl por defecto: {e}")
                                planned_sl_val = 0.0
                        # Si es señal completa y provider_tag != 'FAST', buscar trade FAST previo para actualizarlo
                        fast_ticket = None
                        if provider_tag.upper() != 'FAST':
                            import os
                            now = time.time()
                            try:
                                window_seconds = int(self.config_provider.get('DEDUP_TTL_SECONDS', '120')) if self.config_provider else int(os.getenv('DEDUP_TTL_SECONDS', '120'))
                            except Exception:
                                window_seconds = 120
                            # Logging: mostrar todos los trades candidatos
                            for t in getattr(tm, 'trades', {}).values():
                                comment = getattr(t, 'provider_tag', '') or ''
                                opened_ts = getattr(t, 'opened_ts', None)
                                is_fast = 'FAST' in comment.upper()
                                is_recent = opened_ts and (now - opened_ts <= window_seconds)
                                log.info(f"[FAST-SEARCH] Revisando trade: ticket={getattr(t,'ticket',None)} acct={getattr(t,'account_name',None)} symbol={getattr(t,'symbol',None)} dir={getattr(t,'direction',None)} provider_tag={comment} opened_ts={opened_ts} is_fast={is_fast} is_recent={is_recent}")
                                # Relajar el match: solo por cuenta, símbolo y dirección, y que sea FAST y reciente
                                if (
                                    getattr(t,'account_name',None) == name and
                                    getattr(t,'symbol',None) == symbol and
                                    getattr(t,'direction',None) == direction and
                                    is_fast and
                                    is_recent
                                ):
                                    log.info(f"[MT5_EXECUTOR][DEBUG] FAST trade MATCHED for update: ticket={t.ticket} acct={t.account_name} symbol={t.symbol} dir={t.direction} provider_tag={t.provider_tag} opened_ts={opened_ts} now={now} window={window_seconds}")
                                    fast_ticket = t.ticket
                                    break
                        # Si hay trade FAST previo, actualizarlo
                        # Refuerzo: planned_sl_val nunca debe ser None antes de cualquier update o registro
                        if planned_sl_val is None:
                            log.warning(f"[MT5_EXECUTOR][PATCH] planned_sl_val era None antes de update/registro. Se usará forced_sl o 0.0. ticket={ticket} symbol={symbol} provider={provider_tag}")
                            if forced_sl is not None and forced_sl != 0.0:
                                planned_sl_val = float(forced_sl)
                            else:
                                planned_sl_val = 0.0

                        if fast_ticket:
                            log.info(f"[MT5_EXECUTOR][DEBUG] Actualizando trade FAST previo: ticket={fast_ticket} con datos de señal completa. planned_sl={planned_sl_val} tps={tps} provider_tag={provider_tag}")
                            tm.update_trade_signal(ticket=int(fast_ticket), tps=list(tps), planned_sl=planned_sl_val, provider_tag=provider_tag)
                            log.info(f"[TM] 🔄 updated FAST->COMPLETE ticket={fast_ticket} acct={name} provider={provider_tag} tps={tps} planned_sl={planned_sl_val}")
                        elif hasattr(tm, 'trades') and int(ticket) in tm.trades:
                            log.info(f"[MT5_EXECUTOR][DEBUG] Actualizando trade existente: ticket={ticket} planned_sl={planned_sl_val} tps={tps} provider_tag={provider_tag}")
                            tm.update_trade_signal(ticket=int(ticket), tps=list(tps), planned_sl=planned_sl_val, provider_tag=provider_tag)
                            log.info(f"[TM] 🔄 updated ticket={ticket} acct={name} provider={provider_tag} tps={tps} planned_sl={planned_sl_val}")
                        else:
                            log.info(f"[MT5_EXECUTOR][DEBUG] Registrando nuevo trade: ticket={ticket} planned_sl={planned_sl_val} tps={tps} provider_tag={provider_tag}")
                            tm.register_trade(
                                account_name=name,
                                ticket=ticket,
                                symbol=symbol,
                                direction=direction,
                                provider_tag=provider_tag,
                                tps=list(tps),
                                planned_sl=planned_sl_val,
                                group_id=ticket
                            )
                else:
                    log.warning(f"[MT5_EXECUTOR][DEBUG] No se registró trade porque la orden no fue exitosa o ticket no asignado. acct={name} retcode={getattr(res,'retcode',None)} ticket=None planned_sl_val={planned_sl_val}")
                    errors[name] = f"order_send failed or not registered retcode={getattr(res,'retcode',None)}"
            except Exception as e:
                errors[name] = f"Exception: {e}"
                log.error(f"[EXCEPTION] open_complete_trade failed acct={name}: {e}")



        per_account_timeout = 30  # seconds; adjust as needed

        async def send_order_with_timeout(account):
            name = account["name"]
            try:
                await asyncio.wait_for(send_order(account), timeout=per_account_timeout)
            except asyncio.TimeoutError:
                errors[name] = f"Timeout: trade execution exceeded {per_account_timeout}s"
                log.error(f"[TIMEOUT] open_complete_trade timed out acct={name}")
            except Exception as e:
                errors[name] = f"Exception: {e}"
                log.error(f"[EXCEPTION] open_complete_trade failed acct={name}: {e}")

        await asyncio.gather(*(send_order_with_timeout(account) for account in accounts), return_exceptions=True)
        return MT5OpenResult(tickets_by_account=tickets, errors_by_account=errors)
