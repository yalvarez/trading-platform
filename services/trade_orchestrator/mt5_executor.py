
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import asyncio
import time
import re

from common.timewindow import parse_windows, in_windows
import logging

log = logging.getLogger("trade_orchestrator.mt5_executor")
from mt5_client import MT5Client
from trade_manager import TradeManager

@dataclass
class MT5OpenResult:
    tickets_by_account: dict[str, int]
    errors_by_account: dict[str, str]

class MT5Executor:
    def _safe_comment(self, tag: str) -> str:
        base = f"{getattr(self, 'comment_prefix', 'TM')}-{tag}"
        base = re.sub(r"[^A-Za-z0-9\-_.]", "", base)
        return base[:31]

    def _client_for(self, account):
        # Implementación básica: asume que la cuenta tiene un campo 'client' o que se puede construir aquí
        # Ajusta según tu arquitectura real
        if 'client' in account:
            return account['client']
        # Si tienes un pool de clientes, puedes buscarlo aquí
        # Por defecto, crea uno nuevo (ajusta host/port según tu sistema)
        return MT5Client(account.get('host', 'localhost'), account.get('port', 18812))

    async def _apply_be(self, account: dict, ticket: int, be_offset_pips: Optional[float] = None, reason: str = "") -> bool:
        """
        Aplica break-even (BE) modificando el SL de la posición indicada.
        Loguea el SL actual antes y después, el SL propuesto y el stop_level del símbolo.
        """
        import logging
        client = self._client_for(account)
        pos_list = client.positions_get(ticket=int(ticket))
        if not pos_list:
            self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)} | No se encontró la posición")
            return False
        pos = pos_list[0]
        symbol = pos.symbol
        info = client.symbol_info(symbol)
        if not info:
            self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)} | No se encontró info de símbolo")
            return False
        point = float(getattr(info, "point", 0.0))
        entry = float(getattr(pos, "price_open", 0.0))
        is_buy = (int(getattr(pos, "type", 0)) == 0)
        sl_actual = float(getattr(pos, "sl", 0.0))
        stop_level = float(getattr(info, "stops_level", 0.0)) * point
        # Offset en pips
        off_pips = float(getattr(self, "be_offset_pips", 0.0) if be_offset_pips is None else be_offset_pips)
        # Usar función centralizada de conversión
        from trade_manager import TradeManager
        off_price = TradeManager._pips_to_price(symbol, off_pips, point)
        be_sl = (entry + off_price) if is_buy else (entry - off_price)
        be_sl = round(be_sl, 2 if symbol.upper().startswith("XAU") else 5)
        logging.info(f"[BE-DEBUG] account={account['name']} ticket={ticket} symbol={symbol} SL actual={sl_actual} SL BE propuesto={be_sl} stop_level={stop_level} entry={entry} is_buy={is_buy}")
        # Validar que el nuevo SL cumple con el mínimo stop level
        price_current = float(getattr(pos, "price_current", 0.0))
        if is_buy:
            min_sl = price_current - stop_level
            if be_sl > min_sl:
                logging.warning(f"[BE-DEBUG] SL BE ({be_sl}) está demasiado cerca del precio actual ({price_current}), mínimo permitido: {min_sl}. Ajustando SL a {min_sl}")
                be_sl = round(min_sl, 2 if symbol.upper().startswith("XAU") else 5)
        else:
            max_sl = price_current + stop_level
            if be_sl < max_sl:
                logging.warning(f"[BE-DEBUG] SL BE ({be_sl}) está demasiado cerca del precio actual ({price_current}), máximo permitido: {max_sl}. Ajustando SL a {max_sl}")
                be_sl = round(max_sl, 2 if symbol.upper().startswith("XAU") else 5)

            req = {
                "action": 6,  # TRADE_ACTION_SLTP
                "position": int(ticket),
                "sl": float(be_sl),
                "tp": 0.0,
                "comment": self._safe_comment(f"BE-{reason}"),
            }
            res = await self._best_filling_order_send(client, symbol, req)
            ok = bool(res and getattr(res, "retcode", None) in (10009, 10008))  # DONE, DONE_PARTIAL
            logging.info(f"[BE-DEBUG] Resultado order_send | res={res}")
            pos_list_after = client.positions_get(ticket=int(ticket))
            sl_after = float(getattr(pos_list_after[0], "sl", 0.0)) if pos_list_after else None
            logging.info(f"[BE-DEBUG] SL después del intento: {sl_after}")
            if ok:
                self._notify_bg(account["name"], f"🔒 BE aplicado | Ticket: {int(ticket)} | SL: {be_sl:.5f}")
                return True
            else:
                self._notify_bg(
                    account["name"],
                    f"❌ BE falló | Ticket: {int(ticket)} | retcode={getattr(res,'retcode',None)} {getattr(res,'comment',None)}"
                )
            return False

        def find_recent_fast_trade(trades, symbol, account_name, direction, max_age_seconds=60):
            """
            Busca el trade FAST más reciente para symbol, cuenta y dirección, dentro de la ventana de tiempo.
            Ignora SL, TP y provider_tag.
            """
            from datetime import datetime
            now = datetime.utcnow()
            candidates = []
            for t in trades:
                if (
                    t.get('symbol') == symbol
                    and t.get('account_name') == account_name
                    and t.get('direction') == direction
                ):
                    opened_at = t.get('opened_at')
                    if opened_at:
                        age = (now - opened_at).total_seconds()
                        if age <= max_age_seconds:
                            candidates.append((age, t))
            if not candidates:
                return None
            return sorted(candidates, key=lambda x: x[0])[0][1]

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
        tfm = getattr(info, "trade_fill_mode", None) if info else None
        enabled = getattr(info, "visible", None) if info else None
        trademode = getattr(info, "trade_mode", None) if info else None
        fillmode = getattr(info, "trade_fill_mode", None) if info else None
        bid = getattr(tick, "bid", None) if tick else None
        ask = getattr(tick, "ask", None) if tick else None
        ticktime = getattr(tick, "time", None) if tick else None
        log.info(f"[SYMBOL-STATE] symbol={symbol} enabled={enabled} trade_mode={trademode} trade_fill_mode={fillmode} bid={bid} ask={ask} tick_time={ticktime}")
        # --- PATCH: Forzar FOK para StarTrader Demo y XAUUSD ---
        # Si la cuenta es 'StarTrader Demo' y el símbolo es XAUUSD, forzar solo FOK
        # Para revertir, eliminar este bloque
        force_fok = False
        if account_name == 'StarTrader Demo' and symbol.upper() == 'XAUUSD':
            force_fok = True
        # --- FIN PATCH ---
        if force_fok:
            candidates = [ORDER_FILLING_FOK]
            log.info(f"[FILLING-PATCH] Forzando FOK para cuenta StarTrader Demo y XAUUSD")
        else:
            if tfm in (ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN):
                candidates.append(int(tfm))
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
            if res and getattr(res, "retcode", None) in (10009, 10008):
                return res
            # Logging detallado si la orden falla
            if res and getattr(res, "retcode", None) != 10030:
                log.warning(f"[ORDER-FAIL] retcode={getattr(res,'retcode',None)} comment={getattr(res,'comment',None)} req={req_try} res={res}")
                return res
            if res and getattr(res, "retcode", None) == 10030:
                log.warning(f"[ORDER-INVALID-REQUEST] retcode=10030 comment={getattr(res,'comment',None)} req={req_try} res={res}")
        return last_res


    def __init__(
        self,
        accounts: list[dict],
        *,
        default_deviation: int = 50,
        magic: int = 987654,
        comment_prefix: str = "YsaCopy",
        notifier=None,
        trading_windows: str = "03:00-12:00,08:00-17:00",
        entry_wait_seconds: int = 60,
        entry_poll_ms: int = 500,
        entry_buffer_points: float = 0.0,
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

    async def open_complete_trade(self, provider_tag, symbol, direction, entry_range, sl, tps):
        tickets = {}
        errors = {}

        # Construir accounts con direction correcto para cada cuenta activa
        accounts = []
        for acct in self.accounts:
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

            # --- Función local para obtener SL forzado si no viene ---
            async def get_forced_sl(client, symbol, direction, price):
                # Lógica simple: usar SL por defecto de XAUUSD si aplica
                if symbol.upper().startswith('XAU'):
                    # Buscar en .env/config, aquí hardcodeado como ejemplo
                    default_sl = getattr(self, 'default_sl_xauusd', 300)
                    if direction.upper() == 'BUY':
                        return price - default_sl * getattr(client.symbol_info(symbol), 'point', 0.1)
                    else:
                        return price + default_sl * getattr(client.symbol_info(symbol), 'point', 0.1)
                return price  # fallback
            name = account["name"]
            try:
                client = self._client_for(account)
                client.symbol_select(symbol, True)
                symbol_info = client.symbol_info(symbol)
                if not symbol_info:
                    log.warning(f"[SYMBOL] No symbol_info for {symbol} ({name}) after select. Symbol may not be available in MT5.")

                # --- Lógica de entrada: mitad del SL a hint+buffer ---
                entry_hint = None
                if entry_range and isinstance(entry_range, (list, tuple)) and len(entry_range) == 2:
                    # Si es venta, usar el precio más bajo; si es compra, el más alto
                    if direction.upper() == "SELL":
                        entry_hint = float(min(entry_range))
                    else:
                        entry_hint = float(max(entry_range))
                elif entry_range and isinstance(entry_range, (float, int)):
                    entry_hint = float(entry_range)
                else:
                    log.warning(f"[ENTRY] No entry_range provided for {symbol} ({name}), skipping price wait.")
                price = 0.0
                forced_sl = sl
                # Obtener SL real para calcular el rango
                if not forced_sl or float(forced_sl) == 0.0:
                    sl_val = None
                else:
                    sl_val = float(forced_sl)
                buffer = self.entry_buffer_points
                if entry_hint is not None and sl_val is not None:
                    # Calcular mitad del SL
                    mid_sl = (entry_hint + sl_val) / 2
                    # Definir rango válido
                    if direction.upper() == "BUY":
                        valid_lo = min(mid_sl, entry_hint)
                        valid_hi = entry_hint + buffer
                    else:
                        valid_lo = entry_hint - buffer
                        valid_hi = max(mid_sl, entry_hint)
                    # Obtener precio actual
                    price = client.tick_price(symbol, direction)
                    # Si el precio ya está en el rango, entrar inmediatamente
                    if valid_lo <= price <= valid_hi:
                        log.info(f"[ENTRY] Precio {price} dentro de rango [{valid_lo}, {valid_hi}] para {symbol} ({name}), entrando inmediatamente.")
                    else:
                        # Esperar a que el precio entre en el rango
                        log.info(f"[ENTRY] Esperando precio en rango [{valid_lo}, {valid_hi}] para {symbol} ({name})...")
                        deadline = time.time() + self.entry_wait_seconds
                        while time.time() <= deadline:
                            price = client.tick_price(symbol, direction)
                            if valid_lo <= price <= valid_hi:
                                log.info(f"[ENTRY] Precio {price} entró en rango [{valid_lo}, {valid_hi}] para {symbol} ({name}), ejecutando entrada.")
                                break
                            await asyncio.sleep(self.entry_poll_ms / 1000.0)
                        else:
                            log.warning(f"[ENTRY] No suitable price found in range [{valid_lo}, {valid_hi}] for {symbol} ({name}) during wait window. Skipping entry.")
                            return
                else:
                    price = client.tick_price(symbol, direction)
                    if price == 0.0:
                        log.warning(f"[PRICE] Price is 0.0 for {symbol} ({name}) - symbol may not be available, not selected, or market is closed.")
                        return
                order_type = 0 if direction == "BUY" else 1

                # --- Forzar SL si es necesario ---
                forced_sl = sl
                if not forced_sl or float(forced_sl) == 0.0:
                    forced_sl = await get_forced_sl(client, symbol, direction, price)
                    log.warning(f"[SL-FORCED] SL forzado para {name}: {forced_sl}")
                # planned_sl_val SIEMPRE local y explícito, debe reflejar el SL realmente usado
                try:
                    planned_sl_val = float(forced_sl) if forced_sl is not None else None
                except Exception:
                    planned_sl_val = None

                # Parche: asegurar que planned_sl_val SIEMPRE refleje el SL realmente usado
                # Si forced_sl se ajusta después, actualizar planned_sl_val también
                # (por ejemplo, tras ajuste por min_stop)

                # --- Si el SL está demasiado cerca del precio actual, AJUSTAR al mínimo permitido ---
                symbol_info = client.symbol_info(symbol)
                available_attrs = dir(symbol_info) if symbol_info else []
                log.info(f"[DEBUG] SymbolInfo attrs for {symbol}: {available_attrs}")
                # Acceso seguro a stops_level, stop_level y trade_fill_mode
                min_stop_raw = None
                if symbol_info:
                    if hasattr(symbol_info, "stops_level"):
                        min_stop_raw = getattr(symbol_info, "stops_level", None)
                    elif hasattr(symbol_info, "stop_level"):
                        min_stop_raw = getattr(symbol_info, "stop_level", 0.0)
                    else:
                        log.warning(f"[MT5_EXECUTOR][WARN] SymbolInfo for {symbol} no tiene stops_level ni stop_level. Usando 0.0")
                        min_stop_raw = 0.0
                    fill_mode = getattr(symbol_info, "trade_fill_mode", None) if hasattr(symbol_info, "trade_fill_mode") else None
                else:
                    min_stop_raw = 0.0
                    fill_mode = None
                min_stop = float(min_stop_raw) * float(getattr(symbol_info, "point", 0.0)) if symbol_info else 0.0
                log.info(f"[DEBUG] stops_level={getattr(symbol_info, 'stops_level', None) if symbol_info else None}, stop_level={getattr(symbol_info, 'stop_level', None) if symbol_info else None}, trade_fill_mode={fill_mode}")
                if min_stop > 0 and abs(price - float(forced_sl)) < min_stop:
                    if direction.upper() == "BUY":
                        adjusted_sl = price - min_stop
                    else:
                        adjusted_sl = price + min_stop
                    log.warning(f"[SL-ADJUST] SL demasiado cerca del precio actual para {name}: SL={forced_sl} price={price} min_stop={min_stop}. Ajustando SL a {adjusted_sl}")
                    forced_sl = round(adjusted_sl, 2 if symbol.upper().startswith("XAU") else 5)
                    planned_sl_val = forced_sl  # Parche: reflejar ajuste también en planned_sl_val

                # --- LOTE DINÁMICO O FIJO ---
                lot = 0.01
                fixed_lot = float(account.get("fixed_lot", 0))
                risk_percent = float(account.get("risk_percent", 0))
                balance = 0.0
                if fixed_lot > 0:
                    lot = fixed_lot
                elif risk_percent > 0 and forced_sl and float(forced_sl) > 0:
                    try:
                        acc_info = client.mt5.account_info()
                        if acc_info and hasattr(acc_info, "balance"):
                            balance = float(acc_info.balance)
                    except Exception as e:
                        log.warning(f"[LOTE] No se pudo obtener balance para {name}: {e}")
                    risk_money = balance * (risk_percent / 100.0)
                    sl_distance = abs(float(price) - float(forced_sl))
                    try:
                        symbol_info = client.symbol_info(symbol)
                        tick_value = float(getattr(symbol_info, "tick_value", 0.0))
                        tick_size = float(getattr(symbol_info, "tick_size", 0.0))
                        lot_step = float(getattr(symbol_info, "volume_step", 0.01))
                        min_lot = float(getattr(symbol_info, "volume_min", 0.03))
                    except Exception as e:
                        log.warning(f"[LOTE] No se pudo obtener info de símbolo para {name}: {e}")
                        tick_value = 0.0
                        tick_size = 0.0
                        lot_step = 0.01
                        min_lot = 0.03
                    log.info(f"[LOTE][{name}] balance={balance} risk_money={risk_money} sl_distance={sl_distance} tick_value={tick_value} tick_size={tick_size} lot_step={lot_step} min_lot={min_lot}")
                    if tick_value > 0 and tick_size > 0 and sl_distance > 0:
                        lot = risk_money / (sl_distance * (tick_value / tick_size))
                        lot = max(min_lot, round(lot / lot_step) * lot_step)
                        log.info(f"[LOTE][{name}] lotaje calculado={lot}")
                    else:
                        log.warning(f"[LOTE] No se pudo calcular lotaje dinámico para {name}, usando 0.03")
                        lot = 0.03
                # --- FIN LOTE ---

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
                ticket = tickets.get(name)
                if res and getattr(res, "retcode", None) == 10009 and ticket is not None:
                    log.info("open_complete_trade success acct=%s ticket=%s", name, ticket)
                    # Solo registrar/actualizar si forced_sl es válido
                    if hasattr(self, 'trade_manager') and self.trade_manager:
                        tm = self.trade_manager
                        # Usar siempre el SL forzado/calculado para planned_sl
                        if planned_sl_val is None or planned_sl_val == 0.0:
                            log.error(f"[MT5_EXECUTOR][DEBUG] Ignorado registro/actualización de trade por SL inválido. ticket={ticket} symbol={symbol} provider={provider_tag} forced_sl={forced_sl} planned_sl_val={planned_sl_val}")
                            return
                        # Si es señal completa y provider_tag != 'FAST', buscar trade FAST previo para actualizarlo
                        fast_ticket = None
                        if provider_tag.upper() != 'FAST':
                            import os
                            now = time.time()
                            try:
                                window_seconds = int(os.getenv('DEDUP_TTL_SECONDS', '120'))
                            except Exception:
                                window_seconds = 120
                            for t in getattr(tm, 'trades', {}).values():
                                comment = getattr(t, 'provider_tag', '') or ''
                                opened_ts = getattr(t, 'opened_ts', None)
                                is_fast = 'FAST' in comment.upper()
                                is_recent = opened_ts and (now - opened_ts <= window_seconds)
                                if (
                                    t.account_name == name and
                                    t.symbol == symbol and
                                    t.direction == direction and
                                    is_fast and
                                    is_recent
                                ):
                                    log.info(f"[MT5_EXECUTOR][DEBUG] FAST trade candidate for update: ticket={t.ticket} acct={t.account_name} symbol={t.symbol} dir={t.direction} provider_tag={t.provider_tag} opened_ts={opened_ts} now={now} window={window_seconds}")
                                    fast_ticket = t.ticket
                                    break
                        # Si hay trade FAST previo, actualizarlo
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
                    log.warning(f"[MT5_EXECUTOR][DEBUG] No se registró trade porque la orden no fue exitosa o ticket no asignado. acct={name} retcode={getattr(res,'retcode',None)} ticket={ticket} planned_sl_val={planned_sl_val}")
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
