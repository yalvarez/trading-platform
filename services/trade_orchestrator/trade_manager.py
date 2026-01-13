# trade_manager.py
from __future__ import annotations

import asyncio
import time
import re
from dataclasses import dataclass, field
from typing import Optional
import os
import mt5_constants as mt5
from prometheus_client import Counter, Gauge
import logging
import datetime
import redis.asyncio as redis_async

log = logging.getLogger("trade_orchestrator.trade_manager")

# Metrics
TRADES_OPENED = Counter('trades_opened_total', 'Total trades opened')
TP_HITS = Counter('trade_tp_hits_total', 'TP hits', ['tp'])
PARTIAL_CLOSES = Counter('trade_partial_closes_total', 'Partial closes')
ACTIVE_TRADES = Gauge('active_trades', 'Active trades')

@dataclass
class ManagedTrade:
    account_name: str
    ticket: int
    symbol: str
    direction: str
    provider_tag: str
    group_id: int

    tps: list[float] = field(default_factory=list)
    planned_sl: Optional[float] = None
    tp_hit: set[int] = field(default_factory=set)  # indices 1..N

    mfe_peak_price: Optional[float] = None
    runner_enabled: bool = False
    initial_volume: Optional[float] = None
    entry_price: Optional[float] = None

    addon_done: bool = False
    opened_ts: float = field(default_factory=lambda: time.time())

    last_trailing_sl: Optional[float] = None
    last_trailing_ts: float = 0.0

    # ✅ dedup acciones por trade (gestión por mensajes)
    actions_done: set[str] = field(default_factory=set)


class TradeManager:
    def register_trade(self, account_name: str, ticket: int, symbol: str, direction: str, provider_tag: str, tps: list[float], planned_sl: float = None, group_id: int = None):
        # Forzar SL por defecto si falta
        default_sl_pips = getattr(self, 'default_sl', None)
        if planned_sl is None or planned_sl == 0.0:
            # Obtener precio de entrada y punto
            entry_price = None
            point = None
            # Intentar obtener precio de entrada y punto desde MT5 si posible
            account = next((a for a in self.mt5.accounts if a.get("active")), None)
            client = self.mt5._client_for(account) if account else None
            pos_list = client.positions_get(ticket=int(ticket)) if client else []
            if pos_list:
                pos = pos_list[0]
                entry_price = float(getattr(pos, 'price_open', 0.0))
                point = float(getattr(client.symbol_info(symbol), 'point', 0.01))
            # Si no se puede obtener, usar None
            log.warning(f"[TM] ⚠️ Trade registrado SIN SL! ticket={ticket} symbol={symbol} provider={provider_tag} (asignando SL por defecto: {default_sl_pips})")
            if default_sl_pips is not None and entry_price is not None and point is not None:
                # Calcular SL por defecto en pips
                sl_pips = float(default_sl_pips)
                if direction.upper() == "BUY":
                    planned_sl = entry_price - (sl_pips * point)
                else:
                    planned_sl = entry_price + (sl_pips * point)
            elif default_sl_pips is not None:
                planned_sl = float(default_sl_pips)
        """
        Registers a new trade in the manager. Used when a trade is opened externally (e.g., by signal handler).
        """
        gid = int(group_id) if group_id is not None else int(ticket)
        self.trades[int(ticket)] = ManagedTrade(
            account_name=account_name,
            ticket=int(ticket),
            symbol=symbol,
            direction=direction,
            provider_tag=provider_tag,
            group_id=gid,
            tps=list(tps or []),
            planned_sl=float(planned_sl) if planned_sl is not None else None,
        )
        self.group_addon_count.setdefault((account_name, gid), 0)
        log.info("[TM] ✅ registered ticket=%s acct=%s group=%s provider=%s tps=%s planned_sl=%s", ticket, account_name, gid, provider_tag, tps, planned_sl)
        try:
            TRADES_OPENED.inc()
            ACTIVE_TRADES.set(len(self.trades))
        except Exception:
            pass
    def _effective_close_percent(self, ticket: int, desired_percent: int) -> int:
        if desired_percent >= 100:
            return 100

        # Use the first active account for context (should be refactored to always have account)
        account = next((a for a in self.mt5.accounts if a.get("active")), None)
        client = self.mt5._client_for(account) if account else None
        pos_list = client.positions_get(ticket=int(ticket)) if client else []
        if not pos_list:
            return desired_percent
        pos = pos_list[0]

        info = client.symbol_info(pos.symbol) if client else None
        if not info:
            return desired_percent

        v = float(pos.volume)
        step = float(info.volume_step) if float(info.volume_step) > 0 else 0.0
        vmin = float(info.volume_min) if float(info.volume_min) > 0 else 0.0

        if v <= 0 or step <= 0 or vmin <= 0:
            return desired_percent

        raw_close = v * (float(desired_percent) / 100.0)
        # Ajustar al múltiplo inferior de step
        close_vol = step * int(raw_close / step)

        if close_vol < vmin or close_vol <= 0:
            return 100

        remaining = v - close_vol
        # Si el restante es menor al mínimo, cerrar todo
        if remaining > 0 and remaining < vmin:
            return 100

        # Calcular el porcentaje real que se puede cerrar
        pct_real = int((close_vol / v) * 100) if v > 0 else desired_percent
        return pct_real
    # --- Scaling out para trades sin TP (ej. TOROFX) ---
    async def _maybe_scaling_out_no_tp(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        # Configuración de trailing tras el tercer tramo
        trailing_pips_last_tramo = getattr(self, 'torofx_trailing_last_tramo_pips', 20.0)
        if not hasattr(t, 'trailing_active_last_tramo'):
            t.trailing_active_last_tramo = False
        if not hasattr(t, 'trailing_peak_last_tramo'):
            t.trailing_peak_last_tramo = None
        # Guardar el precio del cierre del primer tramo para BE futuro
        if not hasattr(t, 'first_tramo_close_price'):
            t.first_tramo_close_price = None
        # Solo aplica a trades sin TP y con provider_tag TOROFX (o configurable)
        if t.tps:
            return
        if self.torofx_provider_tag_match not in (t.provider_tag or '').upper():
            return
        tramo_pips = getattr(self, 'scaling_tramo_pips', 30.0)
        percent_per_tramo = getattr(self, 'scaling_percent_per_tramo', 25)
        entry = t.entry_price if t.entry_price is not None else float(pos.price_open)
        symbol = t.symbol.upper() if hasattr(t, 'symbol') else ''
        client = self.mt5._client_for(account)
        # Estado: guardar tramos ya ejecutados en t.actions_done
        if not hasattr(t, 'actions_done') or t.actions_done is None:
            t.actions_done = set()
        # Calcular cuántos tramos de 30 pips se han recorrido desde la entrada
        pips_ganados = (current - entry) / point if is_buy else (entry - current) / point
        tramos = int(pips_ganados // tramo_pips)
        # Ejecutar cierre parcial por cada tramo no ejecutado
        for tramo in range(1, tramos + 1):
            if tramo in t.actions_done:
                continue
            # Cerrar el 25% del volumen actual
            v = float(getattr(pos, 'volume', 0.0))
            vmin = float(getattr(client.symbol_info(symbol), 'volume_min', 0.0))
            step = float(getattr(client.symbol_info(symbol), 'volume_step', 0.01))
            close_vol = max(step, round(v * percent_per_tramo / 100.0 / step) * step)
            if close_vol < vmin or close_vol >= v:
                continue
            req = {
                "action": 1,  # TRADE_ACTION_DEAL
                "position": int(pos.ticket),
                "symbol": symbol,
                "volume": close_vol,
                "type": 1 if not is_buy else 0,  # 0=BUY, 1=SELL
                "price": float(current),
                "deviation": self.deviation,
                "magic": self.magic,
                "type_filling": getattr(client.symbol_info(symbol), 'filling_mode', 1),
                "type_time": 0,
                "comment": "ScalingOut"
            }
            log.info(f"[TOROFX-SCALING] Enviando cierre parcial tramo {tramo} | req={req}")
            res = client.order_send(req)
            log.info(f"[TOROFX-SCALING] Resultado cierre parcial tramo {tramo} | res={res}")
            if res and res.retcode in (0, 10009, 10008):  # TRADE_RETCODE_DONE, etc
                t.actions_done.add(tramo)
                # Guardar el precio del cierre del primer tramo
                if tramo == 1:
                    t.first_tramo_close_price = float(current)
                self._notify_bg(account["name"], f"🎯 ScalingOut TOROFX: Parcial tramo {tramo} ({percent_per_tramo}%) ejecutado | Ticket: {int(pos.ticket)}")
                await self.notify_trade_event(
                    'partial',
                    account_name=account["name"],
                    ticket=int(pos.ticket),
                    symbol=symbol,
                    percent=percent_per_tramo,
                    tramo=tramo
                )
                # Al cerrar el primer tramo, poner BE (SL=entry)
                if tramo == 1:
                    # Usar siempre el precio de apertura real para BE
                    entry_price_real = float(getattr(pos, 'price_open', entry))
                    be_req = {
                        "action": 3,  # TRADE_ACTION_SLTP
                        "position": int(pos.ticket),
                        "symbol": symbol,
                        "sl": entry_price_real,
                        "tp": 0.0,
                        "magic": self.magic
                    }
                    log.info(f"[TOROFX-SCALING] Aplicando BE tras primer tramo | req={be_req}")
                    be_res = client.order_send(be_req)
                    log.info(f"[TOROFX-SCALING] Resultado BE tras primer tramo | res={be_res}")
                    self._notify_bg(account["name"], f"🔒 BE aplicado tras primer tramo | Ticket: {int(pos.ticket)} | SL: {entry_price_real}")
                # Al cerrar el tercer tramo, poner BE al precio del cierre del primer tramo
                if tramo == 3 and t.first_tramo_close_price:
                    be_req = {
                        "action": 3,  # TRADE_ACTION_SLTP
                        "position": int(pos.ticket),
                        "symbol": symbol,
                        "sl": float(t.first_tramo_close_price),
                        "tp": 0.0,
                        "magic": self.magic
                    }
                    log.info(f"[TOROFX-SCALING] Aplicando BE tras tercer tramo | req={be_req}")
                    be_res = client.order_send(be_req)
                    log.info(f"[TOROFX-SCALING] Resultado BE tras tercer tramo | res={be_res}")
                    self._notify_bg(account["name"], f"🔒 BE movido tras tercer tramo | Ticket: {int(pos.ticket)} | SL: {t.first_tramo_close_price}")
        # Activar trailing solo después del cierre del tercer tramo
        if 3 in t.actions_done and not t.trailing_active_last_tramo:
            t.trailing_active_last_tramo = True
            t.trailing_peak_last_tramo = float(current)
            self._notify_bg(account["name"], f"🚦 Trailing activado tras tercer tramo | Ticket: {int(pos.ticket)} | Peak: {current}")

        # Si el trailing tras el tercer tramo está activo, monitorear retroceso
        if t.trailing_active_last_tramo:
            peak = t.trailing_peak_last_tramo or float(current)
            # Actualizar el máximo alcanzado
            if (is_buy and current > peak) or (not is_buy and current < peak):
                t.trailing_peak_last_tramo = float(current)
                peak = float(current)
            retroceso = (peak - current) / point if is_buy else (current - peak) / point
            if retroceso >= trailing_pips_last_tramo:
                # Cerrar el trade por completo
                v = float(getattr(pos, 'volume', 0.0))
                vmin = float(getattr(client.symbol_info(symbol), 'volume_min', 0.0))
                step = float(getattr(client.symbol_info(symbol), 'volume_step', 0.01))
                close_vol = v
                if close_vol >= vmin:
                    req = {
                        "action": 1,  # TRADE_ACTION_DEAL
                        "position": int(pos.ticket),
                        "symbol": symbol,
                        "volume": close_vol,
                        "type": 1 if not is_buy else 0,  # 0=BUY, 1=SELL
                        "price": float(current),
                        "deviation": self.deviation,
                        "magic": self.magic,
                        "type_filling": getattr(client.symbol_info(symbol), 'filling_mode', 1),
                        "type_time": 0,
                        "comment": "TrailingClose"
                    }
                    log.info(f"[TOROFX-SCALING] Trailing: cerrando trade por retroceso de {trailing_pips_last_tramo} pips | req={req}")
                    res = client.order_send(req)
                    log.info(f"[TOROFX-SCALING] Resultado cierre trailing último tramo | res={res}")
                    self._notify_bg(account["name"], f"🚦 Trailing: Trade cerrado por retroceso de {trailing_pips_last_tramo} pips | Ticket: {int(pos.ticket)}")
                    await self.notify_trade_event(
                        'close',
                        account_name=account["name"],
                        ticket=int(pos.ticket),
                        symbol=symbol,
                        reason=f"Trailing retroceso {trailing_pips_last_tramo} pips"
                    )
                    t.trailing_active_last_tramo = False

    async def notify_trailing(self, account, pos, new_sl):
        await self.notify_trade_event(
            'trailing',
            account_name=account["name"],
            message=f"🔄 Trailing actualizado | Ticket: {int(pos.ticket)} | SL: {new_sl:.5f}"
        )

    async def notify_addon(self, account, addon_ticket, t, addon_level, add_vol):
        await self.notify_trade_event(
            'addon',
            account_name=account["name"],
            ticket=addon_ticket,
            symbol=t.symbol,
            addon_price=addon_level,
            addon_lot=add_vol,
        )

    async def notify_manual_close(self, account, pos, t):
        await self.notify_trade_event(
            'close',
            account_name=account["name"],
            message=f"❌ Cierre manual | Ticket: {int(pos.ticket)} | {t.symbol} | {t.direction}"
        )
    def __init__(
        self,
        mt5_exec,
        *,
        magic: int = 987654,
        loop_sleep_sec: float = 1.0,

        scalp_tp1_percent: int = 60,
        scalp_tp2_percent: int = 100,

        long_tp1_percent: int = 50,
        long_tp2_percent: int = 50,
        runner_retrace_pips: float = 10,
        buffer_pips: float = 2.0,

        enable_be_after_tp1: bool = True,
        be_offset_pips: float = 3.0,

        enable_trailing: bool = True,
        trailing_activation_after_tp2: bool = True,
        trailing_activation_pips: float = 30.0,
        trailing_stop_pips: float = 15.0,

        trailing_min_change_pips: float = 1.0,
        trailing_cooldown_sec: float = 2.0,

        deviation: int = 20,

        # ✅ addon midpoint entry–SL
        enable_addon: bool = True,
        addon_max: int = 1,
        addon_lot_factor: float = 0.5,
        addon_min_seconds_from_open: int = 5,
        addon_entry_sl_ratio: float = 0.5,  # 0.5 = mitad entre entry y SL

        # ✅ TOROFX management defaults
        torofx_partial_default_percent: int = 30,  # “tomar parcial…” sin %
        torofx_partial_min_pips: float = 50.0,     # “+50/60” -> usa 50 por defecto
        torofx_close_entry_tolerance_pips: float = 10.0,  # para “cierro mi entrada 4330”
        torofx_provider_tag_match: str = "TOROFX",  # substring en provider_tag

        # --- Scaling out config ---
        scaling_tramo_pips: float = 30.0,
        scaling_percent_per_tramo: int = 25,

        default_sl: float = 60.0,  # SL por defecto en pips

        notifier=None,
        notify_connect: bool | None = None,  # compat
        redis_url: str = None, redis_conn=None):
        self.mt5 = mt5_exec
        self.magic = magic
        self.loop_sleep_sec = loop_sleep_sec

        self.scalp_tp1_percent = scalp_tp1_percent
        self.scalp_tp2_percent = scalp_tp2_percent

        self.long_tp1_percent = long_tp1_percent
        self.long_tp2_percent = long_tp2_percent
        self.default_sl = default_sl
        self.runner_retrace_pips = runner_retrace_pips
        self.buffer_pips = buffer_pips

        self.enable_be_after_tp1 = enable_be_after_tp1
        self.be_offset_pips = be_offset_pips

        self.enable_trailing = enable_trailing
        self.trailing_activation_after_tp2 = trailing_activation_after_tp2
        self.trailing_activation_pips = trailing_activation_pips
        self.trailing_stop_pips = trailing_stop_pips

        self.trailing_min_change_pips = trailing_min_change_pips
        self.trailing_cooldown_sec = trailing_cooldown_sec

        self.deviation = deviation

        self.enable_addon = enable_addon
        self.addon_max = int(addon_max)
        self.addon_lot_factor = float(addon_lot_factor)
        self.addon_min_seconds_from_open = int(addon_min_seconds_from_open)
        self.addon_entry_sl_ratio = float(addon_entry_sl_ratio)

        self.torofx_partial_default_percent = int(torofx_partial_default_percent)
        self.torofx_partial_min_pips = float(torofx_partial_min_pips)
        self.torofx_close_entry_tolerance_pips = float(torofx_close_entry_tolerance_pips)
        self.torofx_provider_tag_match = (torofx_provider_tag_match or "TOROFX").upper()

        self.scaling_tramo_pips = float(scaling_tramo_pips)
        self.scaling_percent_per_tramo = int(scaling_percent_per_tramo)

        self.notifier = notifier
        self.trades = {}
        self.group_addon_count = {}

        # --- Redis connection for PnL tracking ---
        self.redis_url = redis_url or os.getenv('REDIS_URL', 'redis://localhost:6379/0')
        self.redis = redis_conn

    # ----------------------------
    # Notifier
    # ----------------------------
    def _notify_bg(self, account_name: str, message: str):
        if not self.notifier:
            return
        try:
            asyncio.create_task(self.notifier.notify(account_name, message))
        except RuntimeError:
            log.warning("[NOTIFY][NO_LOOP] %s: %s", account_name, message)

    async def notify_trade_event(self, event: str, **kwargs):
        """
        Notifica al chat_id de la cuenta el evento relevante del trade.
        event: 'opened', 'tp', 'sl', 'be', 'trailing', 'partial', 'addon', 'close', etc.
        kwargs: datos relevantes del evento
        """
        if not self.notifier:
            return
        account_name = kwargs.get('account_name')
        msg = None
        if event == 'opened':
            msg = f"🎯 TRADE OPENED | Cuenta: {account_name} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} {kwargs.get('direction')} | Entry: {kwargs.get('entry_price')} | SL: {kwargs.get('sl_price')} | TP: {kwargs.get('tp_prices')} | Lote: {kwargs.get('lot')} | Provider: {kwargs.get('provider')}"
        elif event == 'tp':
            msg = f"🎯 TP HIT | Cuenta: {account_name} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | TP{kwargs.get('tp_index')}: {kwargs.get('tp_price')} | Precio actual: {kwargs.get('current_price')}"
        elif event == 'partial':
            msg = f"🎯 Partial Close | Cuenta: {account_name} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | {kwargs.get('close_percent')}% | Motivo: {kwargs.get('reason')}"
        elif event == 'sl':
            msg = f"❌ SL HIT | Cuenta: {account_name} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | SL: {kwargs.get('sl_price')} | Close: {kwargs.get('close_price')}"
        elif event == 'be':
            msg = kwargs.get('message')
        elif event == 'trailing':
            msg = kwargs.get('message')
        elif event == 'addon':
            msg = f"➕ Addon | Cuenta: {account_name} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | Precio: {kwargs.get('addon_price')} | Lote: {kwargs.get('addon_lot')}"
        elif event == 'close':
            msg = kwargs.get('message')
        else:
            msg = kwargs.get('message')
        if msg:
            await self.notifier.notify(account_name, msg)


    def update_trade_signal(self, *, ticket: int, tps: list[float], planned_sl: Optional[float], provider_tag: Optional[str] = None):
        t = self.trades.get(int(ticket))
        if not t:
            return
        t.tps = list(tps or [])
        t.planned_sl = float(planned_sl) if planned_sl is not None else None
        if provider_tag:
            t.provider_tag = provider_tag

    def _looks_like_recovery(self, provider_tag: str) -> bool:
        up = (provider_tag or "").upper()
        return ("RECOVERY" in up) or (up.startswith("REC")) or (" REC " in up)

    def _infer_group_for_recovery(self, account_name: str, symbol: str, direction: str) -> Optional[int]:
        candidates = [
            t for t in self.trades.values()
            if t.account_name == account_name and t.symbol == symbol and t.direction == direction
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.opened_ts, reverse=True)
        return int(candidates[0].group_id)

    # ----------------------------
    # Loop
    # ----------------------------
    async def run_forever(self):
        """
        Bucle principal: gestiona todas las cuentas en paralelo usando asyncio.gather.
        Cada cuenta se gestiona de forma independiente para mejorar la latencia.
        """
        log.info("[RUN_FOREVER] TradeManager loop iniciado y activo.")
        tick_count = 0
        while True:
            accounts = [a for a in self.mt5.accounts if a.get("active")]
            await asyncio.gather(*(self._tick_once_account(account) for account in accounts))
            tick_count += 1
            if tick_count % 60 == 0:
                if tick_count % 600 == 0:
                    log.info(f"[RUN_FOREVER] TradeManager sigue activo. Ticks: {tick_count}")
                else:
                    log.debug(f"[RUN_FOREVER] TradeManager sigue activo. Ticks: {tick_count}")
            await asyncio.sleep(self.loop_sleep_sec)

    async def _tick_once_account(self, account):
        """
        Gestiona los trades de una sola cuenta (idéntico a la lógica previa de _tick_once, pero por cuenta).
        """
        try:
            client = self.mt5._client_for(account)
            if hasattr(client, 'connect_to_account') and not client.connect_to_account(account):
                return

            positions = client.positions_get()
            if not positions:
                # Si no hay posiciones, limpia los trades registrados para esta cuenta
                for ticket in list(self.trades.keys()):
                    if self.trades[ticket].account_name == account["name"]:
                        del self.trades[ticket]
                return

            pos_by_ticket = {p.ticket: p for p in positions}

            # Elimina trades cerrados
            for ticket in list(self.trades.keys()):
                t = self.trades[ticket]
                if t.account_name != account["name"]:
                    continue
                if ticket not in pos_by_ticket:
                    try:
                        del self.trades[ticket]
                    except KeyError:
                        pass
            try:
                ACTIVE_TRADES.set(len(self.trades))
            except Exception:
                pass

            # Gestión de cada trade activo
            for ticket, t in list(self.trades.items()):
                if t.account_name != account["name"]:
                    continue

                pos = pos_by_ticket.get(ticket)
                if not pos:
                    continue
                if pos.magic != self.mt5.magic:
                    continue

                info = client.symbol_info(t.symbol)
                tick = client.symbol_info_tick(t.symbol)
                if not info or not tick:
                    continue

                point = float(info.point)
                is_buy = (t.direction == "BUY")
                current = float(pos.price_current)

                # Guarda entry y volumen inicial si no están
                if t.entry_price is None:
                    t.entry_price = float(pos.price_open)
                if t.initial_volume is None:
                    t.initial_volume = float(pos.volume)

                # 1) Gestión de Take Profits
                await self._maybe_take_profits(account, pos, point, is_buy, current, t)

                # 1b) Scaling out para trades sin TP (solo si no tiene TP)
                await self._maybe_scaling_out_no_tp(account, pos, point, is_buy, current, t)

                # 2) Addon midpoint (añadir posición si corresponde)
                if self.enable_addon:
                    await self._maybe_addon_midpoint(account, pos, point, is_buy, current, t)

                # 3) Trailing Stop
                if self.enable_trailing:
                    await self._maybe_trailing(account, pos, point, is_buy, current, t)
        except Exception as e:
            # Supresión de errores de conexión repetidos
            if hasattr(self, '_last_conn_error') and self._last_conn_error == str(e):
                self._conn_error_count = getattr(self, '_conn_error_count', 0) + 1
                if self._conn_error_count <= 3:
                    log.error(f"[TM] Error en gestión de cuenta {account.get('name')}: {e}")
                elif self._conn_error_count == 4:
                    log.error(f"[TM] Error en gestión de cuenta {account.get('name')}: {e} (suprimido, repite)")
            else:
                self._last_conn_error = str(e)
                self._conn_error_count = 1
                log.error(f"[TM] Error en gestión de cuenta {account.get('name')}: {e}")
            # Intentar reconectar en el siguiente ciclo
            return

    async def _tick_once(self):
        """
        Recorre todas las cuentas activas y gestiona los trades:
        - Elimina trades cerrados
        - Actualiza métricas
        - Aplica gestión: TP, BE, trailing, addon
        - Maneja reconexión automática y errores de red para robustez
        """
        for account in [a for a in self.mt5.accounts if a.get("active")]:
            try:
                client = self.mt5._client_for(account)
                if hasattr(client, 'connect_to_account') and not client.connect_to_account(account):
                    continue

                positions = client.positions_get()
                if not positions:
                    # Si no hay posiciones, limpia los trades registrados para esta cuenta
                    for ticket in list(self.trades.keys()):
                        if self.trades[ticket].account_name == account["name"]:
                            del self.trades[ticket]
                    continue

                pos_by_ticket = {p.ticket: p for p in positions}

                # Elimina trades cerrados
                for ticket in list(self.trades.keys()):
                    t = self.trades[ticket]
                    if t.account_name != account["name"]:
                        continue
                    if ticket not in pos_by_ticket:
                        # Notificación de cierre manual
                        try:
                            t = self.trades[ticket]
                            # Si el trade sigue registrado pero ya no está en posiciones, se asume cierre manual
                            pos = None
                            await self.notify_manual_close(account, t, t)
                        except Exception:
                            pass
                        try:
                            del self.trades[ticket]
                        except KeyError:
                            pass
                try:
                    ACTIVE_TRADES.set(len(self.trades))
                except Exception:
                    pass

                # Gestión de cada trade activo
                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos:
                        # Si la posición desapareció, puede ser SL o cierre manual
                        # Aquí podrías distinguir SL si tienes info previa, por ahora notificamos ambos
                        try:
                            await self.notify_sl(account, t, t)
                        except Exception:
                            pass
                        continue
                    if pos.magic != self.mt5.magic:
                        continue

                    info = client.symbol_info(t.symbol)
                    tick = client.symbol_info_tick(t.symbol)
                    if not info or not tick:
                        continue

                    point = float(info.point)
                    is_buy = (t.direction == "BUY")
                    current = float(pos.price_current)

                    # Guarda entry y volumen inicial si no están
                    if t.entry_price is None:
                        t.entry_price = float(pos.price_open)
                    if t.initial_volume is None:
                        t.initial_volume = float(pos.volume)

                    # 1) Gestión de Take Profits
                    await self._maybe_take_profits(account, pos, point, is_buy, current, t)

                    # 2) Addon midpoint (añadir posición si corresponde)
                    if self.enable_addon:
                        await self._maybe_addon_midpoint(account, pos, point, is_buy, current, t)

                    # 3) Trailing Stop
                    if self.enable_trailing:
                        await self._maybe_trailing(account, pos, point, is_buy, current, t)
            except Exception as e:
                # Supresión de errores de conexión repetidos
                if hasattr(self, '_last_conn_error') and self._last_conn_error == str(e):
                    self._conn_error_count = getattr(self, '_conn_error_count', 0) + 1
                    if self._conn_error_count <= 3:
                        log.error(f"[TM] Error en gestión de cuenta {account.get('name')}: {e}")
                    elif self._conn_error_count == 4:
                        log.error(f"[TM] Error en gestión de cuenta {account.get('name')}: {e} (suprimido, repite)")
                else:
                    self._last_conn_error = str(e)
                    self._conn_error_count = 1
                    log.error(f"[TM] Error en gestión de cuenta {account.get('name')}: {e}")
                continue

    # ----------------------------
    # Helpers
    # ----------------------------
    def _is_long_mode(self, t: ManagedTrade) -> bool:
        return len(t.tps) >= 3

    def _tp_hit(self, is_buy: bool, current: float, tp: float, buffer_price: float) -> bool:
        if is_buy:
            return current >= (tp - buffer_price)
        return current <= (tp + buffer_price)

    async def _do_be(self, account: dict, ticket: int, point: float, is_buy: bool):
        """
        Aplica break-even (SL a precio de entrada + offset) con soporte para override por símbolo/cuenta.
        """
        import asyncio
        log.info(f"[BE-DEBUG] INICIO _do_be | account={account.get('name')} ticket={ticket} is_buy={is_buy}")
        client = self.mt5._client_for(account)
        max_retries = 8
        delay_seconds = 0.2
        last_volume = None
        last_time_update = None
        # Esperar a que la posición refleje el partial close (volumen y/o time_update cambian)
        for attempt in range(1, max_retries + 1):
            pos_list = client.positions_get(ticket=int(ticket))
            log.info(f"[BE-DEBUG] positions_get result | ticket={ticket} pos_list={pos_list}")
            if not pos_list:
                log.warning(f"[BE-DEBUG] No position found for ticket={ticket} en _do_be (attempt {attempt})")
                if attempt < max_retries:
                    await asyncio.sleep(delay_seconds)
                    continue
                log.error(f"[BE-DEBUG] FIN _do_be FAIL | account={account.get('name')} ticket={ticket} - No position found")
                self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)}\nNo se encontró la posición para aplicar BE.")
                await self.notify_trade_event(
                    'be',
                    account_name=account["name"],
                    message=f"❌ BE falló | Ticket: {int(ticket)}\nNo se encontró la posición para aplicar BE."
                )
                return
            pos = pos_list[0]
            # Si es el primer intento, guarda volumen y time_update
            if last_volume is None:
                last_volume = float(getattr(pos, 'volume', 0.0))
                last_time_update = int(getattr(pos, 'time_update', 0))
            else:
                # Si volumen o time_update cambiaron, la posición está actualizada
                curr_volume = float(getattr(pos, 'volume', 0.0))
                curr_time_update = int(getattr(pos, 'time_update', 0))
                if curr_volume != last_volume or curr_time_update != last_time_update:
                    log.info(f"[BE-DEBUG] Detected position update after partial close | volume: {last_volume} -> {curr_volume}, time_update: {last_time_update} -> {curr_time_update}")
                    break
            await asyncio.sleep(delay_seconds)
        # --- Definir symbol y calcular precio BE ---
        symbol = getattr(pos, 'symbol', None)
        if not symbol:
            log.error(f"[BE-DEBUG] No se pudo determinar el símbolo para el ticket={ticket} en _do_be")
            return
        # Calcular precio BE: SL = precio de entrada (entry_price)
        entry_price = float(getattr(pos, 'price_open', 0.0))
        offset = getattr(self, 'be_offset_pips', 0.0) * point if hasattr(self, 'be_offset_pips') else 0.0
        if is_buy:
            be = entry_price + offset
        else:
            be = entry_price - offset
        info = client.symbol_info(symbol) if client else None
        if not info:
            log.error(f"[BE-DEBUG] No se pudo obtener info de símbolo para {symbol} en _do_be")
            return 100
        # --- Probar todos los filling modes para modificar SL (BE) ---
        supported_filling_modes = [1, 3, 2]  # IOC, FOK, RETURN
        for type_filling in supported_filling_modes:
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "symbol": symbol,
                "sl": float(be),
                "tp": 0.0,
                "magic": 987654,
                "type_filling": type_filling,
            }
            log.info(f"[BE-DEBUG] Enviando order_send | req={req}")
            res = client.order_send(req)
            log.info(f"[BE-DEBUG] Resultado order_send | res={res}")
            if res and getattr(res, "retcode", None) == 10009:
                # Validar que el SL realmente cambió
                await asyncio.sleep(1)
                pos_check = client.positions_get(ticket=int(ticket))
                sl_actual = None
                if pos_check and len(pos_check) > 0:
                    sl_actual = float(getattr(pos_check[0], 'sl', 0.0))
                if sl_actual is not None and abs(sl_actual - float(be)) < 1e-4:
                    self._notify_bg(account["name"], f"✅ BE aplicado | Ticket: {int(ticket)} | SL: {be:.5f}")
                    log.info("[TM] BE applied ticket=%s sl=%.5f", int(ticket), be)
                    await self.notify_trade_event(
                        'be',
                        account_name=account["name"],
                        message=f"✅ BE aplicado | Ticket: {int(ticket)} | SL: {be:.5f}"
                    )
                    log.info(f"[BE-DEBUG] FIN _do_be OK | account={account.get('name')} ticket={ticket}")
                    return
                else:
                    log.error(f"[BE-DEBUG] SL no cambió tras BE | esperado={be} actual={sl_actual}")
                    self._notify_bg(
                        account["name"],
                        f"❌ BE falló | Ticket: {int(ticket)}\nSL no cambió tras BE (esperado={be}, actual={sl_actual})"
                    )
                    await self.notify_trade_event(
                        'be',
                        account_name=account["name"],
                        message=f"❌ BE falló | Ticket: {int(ticket)}\nSL no cambió tras BE (esperado={be}, actual={sl_actual})"
                    )
                    return
            elif res and getattr(res, "retcode", None) not in [10030, 10013]:
                # Si el error no es de filling mode, no seguir probando
                retcode = getattr(res, 'retcode', None)
                comment = getattr(res, 'comment', None)
                log.error(f"[BE-DEBUG] FIN _do_be FAIL | account={account.get('name')} ticket={ticket} - retcode={retcode} comment={comment}")
                self._notify_bg(
                    account["name"],
                    f"❌ BE falló | Ticket: {int(ticket)}\nretcode={retcode} {comment}"
                )
                await self.notify_trade_event(
                    'be',
                    account_name=account["name"],
                    message=f"❌ BE falló | Ticket: {int(ticket)}\nretcode={retcode} {comment}"
                )
                return
        # Si ninguno funcionó
        log.error(f"[BE-DEBUG] FIN _do_be FAIL | account={account.get('name')} ticket={ticket} - No filling mode funcionó")
        self._notify_bg(
            account["name"],
            f"❌ BE falló | Ticket: {int(ticket)}\nNo filling mode funcionó para modificar SL."
        )
        await self.notify_trade_event(
            'be',
            account_name=account["name"],
            message=f"❌ BE falló | Ticket: {int(ticket)}\nNo filling mode funcionó para modificar SL."
        )

        v = float(pos.volume)
        step = float(info.volume_step) if float(info.volume_step) > 0 else 0.0
        vmin = float(info.volume_min) if float(info.volume_min) > 0 else 0.0

        if v <= 0 or step <= 0 or vmin <= 0:
            return 100

        # The following logic is not relevant for BE, just return 100
        return 100

    async def _do_partial_close(self, account: dict, ticket: int, percent: int, reason: str):
        log.info(f"[DEBUG] Entering _do_partial_close | account={account['name']} ticket={int(ticket)} percent={int(percent)} reason={reason}")
        client = self.mt5._client_for(account)
        # Obtener volumen antes del cierre parcial
        pos_list_before = client.positions_get(ticket=int(ticket))
        vol_before = float(getattr(pos_list_before[0], 'volume', 0.0)) if pos_list_before else 0.0
        ok = client.partial_close(account=account, ticket=int(ticket), percent=int(percent))
        log.info(f"[DEBUG] Result of client.partial_close: ok={ok} | account={account['name']} ticket={int(ticket)} percent={int(percent)} reason={reason}")
        # Esperar un momento para que el bridge procese el cierre
        await asyncio.sleep(1)
        pos_list_after = client.positions_get(ticket=int(ticket))
        vol_after = float(getattr(pos_list_after[0], 'volume', 0.0)) if pos_list_after else 0.0
        log.info(f"[DEBUG] Volumen antes del cierre parcial: {vol_before} | después: {vol_after} | delta: {vol_before - vol_after}")
        pos = pos_list_after[0] if pos_list_after else None
        symbol = getattr(pos, 'symbol', '') if pos else ''
        volume = vol_after
        info = client.symbol_info(symbol) if client and symbol else None
        if info:
            step = float(getattr(info, 'volume_step', 0.01))
            vmin = float(getattr(info, 'volume_min', 0.01))
        else:
            step = 0.01
            vmin = 0.01
        # Ajustar al múltiplo inferior de step
        raw_close = volume * (float(percent) / 100.0) if volume > 0 else 0.0
        close_vol = step * int(raw_close / step)
        if close_vol < vmin or close_vol <= 0:
            close_vol = volume
        if ok:
            log.info("[TM] 🎯 partial_close ticket=%s percent=%s reason=%s", int(ticket), int(percent), reason)
            try:
                PARTIAL_CLOSES.inc()
            except Exception:
                pass
            await self.notify_trade_event(
                'partial',
                account_name=account["name"],
                ticket=int(ticket),
                symbol=symbol,
                close_percent=percent,
                close_price=getattr(pos, 'price_current', 0.0) if pos else 0.0,
                closed_volume=close_vol,
            )
            # Si el cierre es total, auditar solo si el trade existe
            if percent >= 100:
                t_audit = self.trades.get(int(ticket))
                if t_audit is not None:
                    await self.audit_trade_close(account["name"], int(ticket), t_audit, reason, pos)
        else:
            log.warning("[TM] ❌ partial_close FAILED ticket=%s percent=%s reason=%s", int(ticket), int(percent), reason)
            await self.notify_trade_event(
                'partial',
                account_name=account["name"],
                ticket=int(ticket),
                symbol=symbol,
                close_percent=percent,
                close_price=getattr(pos, 'price_current', 0.0) if pos else 0.0,
                closed_volume=close_vol,
            )

    # ----------------------------
    # TP / Runner / BE
    # ----------------------------
    async def _maybe_take_profits(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        # Solo loguear eventos relevantes, no cada tick
        buffer_price = self.buffer_pips * point
        if not t.tps:
            return
        long_mode = self._is_long_mode(t)

        if long_mode:
            if t.mfe_peak_price is None:
                t.mfe_peak_price = current
            else:
                if is_buy and current > t.mfe_peak_price:
                    t.mfe_peak_price = current
                if (not is_buy) and current < t.mfe_peak_price:
                    t.mfe_peak_price = current

        # TP1, TP2, TP3+ (dinámico)
        tp_percents = [
            self.long_tp1_percent if long_mode else self.scalp_tp1_percent,
            self.long_tp2_percent if long_mode else self.scalp_tp2_percent,
        ]
        # Si hay más de 2 TPs, los siguientes usan el 100% restante
        for idx, tp in enumerate(t.tps):
            tp_idx = idx + 1
            if tp_idx not in t.tp_hit and self._tp_hit(is_buy, current, float(tp), buffer_price):
                # LOG DETALLADO: Precios y trigger
                log.info(f"[TP-DEBUG] Evaluando TP{tp_idx} | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol} dir={t.direction} precio_objetivo={float(tp):.5f} precio_actual={current:.5f} buffer={buffer_price:.5f}")
                if idx < len(tp_percents):
                    pct = tp_percents[idx]
                else:
                    pct = 100  # TP3+ cierra todo lo que queda
                pct_eff = self._effective_close_percent(ticket=int(pos.ticket), desired_percent=int(pct))
                log.info(f"[AUDIT] TP{tp_idx} hit | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol} dir={t.direction} tp={float(tp):.5f} close_pct={pct_eff}")
                await self.notify_trade_event(
                    'tp',
                    account_name=account["name"],
                    ticket=int(pos.ticket),
                    symbol=t.symbol,
                    tp_index=idx,
                    tp_price=float(tp),
                    current_price=current,
                )
                log.info(f"[DEBUG] Calling _do_partial_close for TP{tp_idx} | account={account['name']} ticket={int(pos.ticket)} pct={pct_eff}")
                # LOG DETALLADO: Precio de ejecución real tras cierre parcial
                await self._do_partial_close(account, pos.ticket, pct_eff, reason=f"TP{tp_idx} (objetivo={float(tp):.5f} actual={current:.5f})")
                log.info(f"[DEBUG] Finished _do_partial_close for TP{tp_idx} | account={account['name']} ticket={int(pos.ticket)} pct={pct_eff}")
                t.tp_hit.add(tp_idx)
                # BE solo tras TP1
                if tp_idx == 1 and self.enable_be_after_tp1:
                    log.info(f"[BE-DEBUG] Intentando aplicar BE | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol} dir={t.direction} entry={t.entry_price} tp1={float(tp)}")
                    await self._do_be(account, pos.ticket, point, is_buy)
                    log.info(f"[BE-DEBUG] BE ejecutado | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol}")
                # Runner solo tras TP2
                if tp_idx == 2 and long_mode:
                    t.runner_enabled = True
                # Trailing post TP3+
                if tp_idx >= 3:
                    t.runner_enabled = True  # Forzar trailing
                try:
                    TP_HITS.labels(tp=f"tp{tp_idx}").inc()
                except Exception:
                    pass
                # Solo procesar un TP por tick
                return

        # Runner retrace
        if long_mode and t.runner_enabled and t.mfe_peak_price is not None:
            retrace_price = self.runner_retrace_pips * point
            if is_buy and (t.mfe_peak_price - current) >= retrace_price:
                log.info(f"[AUDIT] RUNNER retrace close | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol} dir={t.direction} mfe_peak={t.mfe_peak_price:.5f} current={current:.5f}")
                await self.notify_trade_event(
                    'close',
                    account_name=account["name"],
                    message=f"🔚 RUNNER retrace close | Ticket: {int(pos.ticket)} | {t.symbol} | {t.direction}\nMFE: {t.mfe_peak_price:.5f} | Current: {current:.5f}"
                )
                await self._do_partial_close(account, pos.ticket, 100, reason="RUNNER retrace")
            if (not is_buy) and (current - t.mfe_peak_price) >= retrace_price:
                log.info(f"[AUDIT] RUNNER retrace close | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol} dir={t.direction} mfe_peak={t.mfe_peak_price:.5f} current={current:.5f}")
                await self.notify_trade_event(
                    'close',
                    account_name=account["name"],
                    message=f"🔚 RUNNER retrace close | Ticket: {int(pos.ticket)} | {t.symbol} | {t.direction}\nMFE: {t.mfe_peak_price:.5f} | Current: {current:.5f}"
                )
                await self._do_partial_close(account, pos.ticket, 100, reason="RUNNER retrace")

    # --- AUDITORÍA DE CIERRE DE TRADE ---
    async def audit_trade_close(self, account_name, ticket, t: ManagedTrade, reason: str, pos=None):
        """Loguea un resumen de la gestión del trade al cerrarse y suma el PnL diario en Redis."""
        log.info(
            f"[AUDIT] TRADE CLOSED | account={account_name} ticket={ticket} symbol={t.symbol} dir={t.direction} "
            f"provider={t.provider_tag} group={t.group_id} entry={t.entry_price} initial_vol={t.initial_volume} "
            f"TPs={t.tps} TP_hit={sorted(t.tp_hit)} runner={t.runner_enabled} SL={t.planned_sl} reason={reason} "
            f"pos={getattr(pos, 'price_current', None) if pos else None}"
        )
        # --- PnL calculation ---
        pnl = None
        try:
            if pos and hasattr(pos, 'profit'):
                pnl = float(getattr(pos, 'profit', 0.0))
            elif hasattr(t, 'entry_price') and hasattr(pos, 'price_current') and hasattr(pos, 'volume'):
                # Fallback: estimate PnL
                direction = 1 if t.direction == 'BUY' else -1
                pnl = direction * (float(pos.price_current) - float(t.entry_price)) * float(pos.volume)
        except Exception as e:
            log.warning(f"[PnL] Error calculating PnL for account={account_name} ticket={ticket}: {e}")
        if pnl is not None:
            # --- Redis connection (lazy init if needed) ---
            if self.redis is None:
                self.redis = await redis_async.from_url(self.redis_url, decode_responses=True)
            today = datetime.datetime.utcnow().strftime('%Y%m%d')
            key = f"pnl:{account_name}:{today}"
            try:
                await self.redis.incrbyfloat(key, pnl)
                log.info(f"[PnL] Added {pnl} to {key}")
            except Exception as e:
                log.warning(f"[PnL] Error updating Redis for {key}: {e}")

    # ----------------------------
    # ✅ Addon MIDPOINT Entry–SL (NO pirámide en ganancia)
    # ----------------------------
    async def _maybe_addon_midpoint(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        if not self.enable_addon or self.addon_max <= 0:
            return

        if "-ADDON" in (t.provider_tag or "").upper():
            return

        if (time.time() - t.opened_ts) < self.addon_min_seconds_from_open:
            return

        gid = int(t.group_id)
        gkey = (account["name"], gid)
        used = int(self.group_addon_count.get(gkey, 0))
        if used >= int(self.addon_max):
            return

        entry = float(pos.price_open)
        sl_pos = float(pos.sl) if float(pos.sl) != 0.0 else 0.0
        sl = sl_pos if sl_pos != 0.0 else (float(t.planned_sl) if t.planned_sl is not None else 0.0)
        if sl == 0.0:
            return

        if is_buy and not (entry > sl):
            return
        if (not is_buy) and not (entry < sl):
            return

        r = self.addon_entry_sl_ratio
        r = max(0.0, min(1.0, r))

        addon_level = (1.0 - r) * entry + r * sl
        buffer_price = self.buffer_pips * point

        trigger = (current <= addon_level + buffer_price) if is_buy else (current >= addon_level - buffer_price)
        if not trigger:
            return

        if is_buy and current <= sl + (2.0 * buffer_price):
            return
        if (not is_buy) and current >= sl - (2.0 * buffer_price):
            return

        client = self.mt5._client_for(account)
        info = client.symbol_info(t.symbol)
        tick = client.symbol_info_tick(t.symbol)
        if not info or not tick:
            return

        base_vol = float(pos.volume)
        add_vol = base_vol * float(self.addon_lot_factor)

        step = float(info.volume_step) if float(info.volume_step) > 0 else 0.0
        vmin = float(info.volume_min) if float(info.volume_min) > 0 else 0.0
        vmax = float(info.volume_max) if float(info.volume_max) > 0 else 0.0

        if step > 0:
            add_vol = step * round(add_vol / step)

        if vmin > 0 and add_vol < vmin:
            return

        if vmax > 0:
            add_vol = min(vmax, add_vol)
        if vmin > 0:
            add_vol = max(vmin, add_vol)

        if add_vol <= 0:
            return

        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if is_buy else tick.bid)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": t.symbol,
            "volume": float(add_vol),
            "type": order_type,
            "price": price,
            "sl": float(sl) if sl else 0.0,
            "tp": 0.0,
            "deviation": int(self.deviation),
            "magic": int(self.mt5.magic),
            "comment": (f"{self.mt5.comment_prefix}-ADDON"[:31] if hasattr(self.mt5, "comment_prefix") else "YsaCopy-ADDON"),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": getattr(self.mt5, "_best_filling", lambda s: mt5.ORDER_FILLING_IOC)(t.symbol),
        }

        client = self.mt5._client_for(account)
        send = getattr(client, "_order_send_with_filling_fallback", None)
        res = send(req) if callable(send) else client.order_send(req)

        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            addon_ticket = int(res.order)

            self.group_addon_count[gkey] = used + 1
            t.addon_done = True

            self.register_trade(
                account_name=account["name"],
                ticket=addon_ticket,
                symbol=t.symbol,
                direction=t.direction,
                provider_tag=f"{t.provider_tag}-ADDON",
                tps=list(t.tps),
                planned_sl=t.planned_sl,
                group_id=t.group_id,
            )

            self._notify_bg(
                account["name"],
                f"➕ ADDON (MID) abierto | Group: {gid} ({self.group_addon_count[gkey]}/{self.addon_max})\n"
                f"Level≈{addon_level:.5f} | Current≈{current:.5f}\n"
                f"Ticket: {addon_ticket} | BaseTicket: {int(pos.ticket)} | Vol: {add_vol:.2f}"
            )
            await self.notify_trade_event(
                'addon',
                account_name=account["name"],
                ticket=addon_ticket,
                symbol=t.symbol,
                addon_price=addon_level,
                addon_lot=add_vol,
            )

    # ----------------------------
    # Trailing
    # ----------------------------
    async def _maybe_trailing(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        """
        Aplica trailing stop con soporte para override por símbolo/cuenta.
        """
        now = time.time()
        symbol = getattr(pos, 'symbol', None)
        acc_name = account.get('name')
        # Permitir override granular
        trailing_activation_pips = self.trailing_activation_pips
        trailing_stop_pips = self.trailing_stop_pips
        trailing_min_change_pips = self.trailing_min_change_pips
        trailing_cooldown_sec = self.trailing_cooldown_sec
        trailing_activation_after_tp2 = self.trailing_activation_after_tp2
        if hasattr(self, 'config'):
            try:
                trailing_cfg = self.config.get('trailing', {})
                if acc_name in trailing_cfg and symbol in trailing_cfg[acc_name]:
                    cfg = trailing_cfg[acc_name][symbol]
                    trailing_activation_pips = cfg.get('activation_pips', trailing_activation_pips)
                    trailing_stop_pips = cfg.get('stop_pips', trailing_stop_pips)
                    trailing_min_change_pips = cfg.get('min_change_pips', trailing_min_change_pips)
                    trailing_cooldown_sec = cfg.get('cooldown_sec', trailing_cooldown_sec)
                    trailing_activation_after_tp2 = cfg.get('activation_after_tp2', trailing_activation_after_tp2)
            except Exception:
                pass
        open_price = getattr(pos, 'price_open', 0.0) if pos else 0.0
        profit_pips = ((current - open_price) / point) if is_buy else ((open_price - current) / point)
        activate = profit_pips >= trailing_activation_pips
        if trailing_activation_after_tp2 and (t.runner_enabled or (2 in t.tp_hit)):
            activate = True
        if not activate:
            return

        trail_dist = trailing_stop_pips * point
        new_sl = (current - trail_dist) if is_buy else (current + trail_dist)

        cur_sl = float(pos.sl)
        min_change = trailing_min_change_pips * point

        if cur_sl != 0.0 and abs(new_sl - cur_sl) < min_change:
            return
        if t.last_trailing_sl is not None and abs(new_sl - t.last_trailing_sl) < min_change:
            return

        improved = (cur_sl == 0.0) or (is_buy and new_sl > cur_sl + min_change) or ((not is_buy) and new_sl < cur_sl - min_change)
        if not improved:
            return

        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(pos.ticket), "sl": float(new_sl), "tp": 0.0}
        client = self.mt5._client_for(account)
        res = client.order_send(req)
        ok = bool(res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL))

        if ok:
            t.last_trailing_sl = float(new_sl)
            t.last_trailing_ts = now
            log.info("[TM] 🔄 trailing update ticket=%s sl=%.5f", int(pos.ticket), new_sl)
            self._notify_bg(account["name"], f"🔄 Trailing actualizado | Ticket: {int(pos.ticket)} | SL: {new_sl:.5f}")
            await self.notify_trade_event(
                'trailing',
                account_name=account["name"],
                message=f"🔄 Trailing actualizado | Ticket: {int(pos.ticket)} | SL: {new_sl:.5f}"
            )

    # ======================================================================
    # ✅ TOROFX MANAGEMENT (mensajes de seguimiento) — NO abre trades
    # ======================================================================
    def handle_torofx_management_message(self, source_chat_id: int, raw_text: str) -> bool:
        """
        Procesa mensajes tipo:
        - "Asegurando profits... quitando riesgo..." -> BE (una vez por trade)
        - "Cerrando el 50% ... +30" -> partial 50% when >=30 pips
        - "parcial ... +50/60" -> partial default % when >=torofx_partial_min_pips
        - "cerrando mi entrada de 4330 y dejando 4325" -> close ticket por entry
        Retorna True si consumió el mensaje (aunque no ejecutara nada aún).
        """
        text = (raw_text or "").strip()
        if not text:
            return False

        up = text.upper()

        # Detectores básicos
        has_close_word = any(w in up for w in ["CERRANDO", "CERRAR", "CIERRO", "CERRAD", "CERRAD0", "CERRARÉ"])
        has_partial_word = any(w in up for w in ["PARCIAL", "PARTIAL", "RECOGER", "COGER"])
        has_be_word = any(w in up for w in ["BREAKEVEN", "BREAK EVEN", "BREAK-EVEN", "QUITANDO EL RIESGO", "SIN RIESGO", "RISK OFF", "ASEGURANDO"])

        # Extrae porcentaje explícito: "50%" / "80 %"
        m_pct = re.search(r"(\d{1,3})\s*%", text)
        pct = int(m_pct.group(1)) if m_pct else None
        if pct is not None:
            pct = max(1, min(100, pct))

        # Extrae pips: "+30" o "+50/60"
        m_pips = re.search(r"\+(\d{1,4})(?:\s*/\s*(\d{1,4}))?", text)
        pips_threshold = None
        if m_pips:
            a = float(m_pips.group(1))
            b = float(m_pips.group(2)) if m_pips.group(2) else None
            # para "+50/60" tomamos el menor como umbral
            pips_threshold = min(a, b) if b is not None else a

        # Extrae precios tipo 4330 / 4325
        prices = [float(x) for x in re.findall(r"\b(4\d{3}(?:\.\d+)?)\b", text)]

        # Define acciones deseadas
        wants_close_entry = ("ENTRADA" in up) and has_close_word and len(prices) >= 1
        wants_be = has_be_word and not has_partial_word and not wants_close_entry
        wants_partial = (has_partial_word or (has_close_word and pct is not None)) and not wants_close_entry

        # Si no parece gestión, no consumir
        if not (wants_close_entry or wants_be or wants_partial):
            return False

        # Ejecutar por cada cuenta (solo trades ya registrados TOROFX)
        any_matched_trade = False
        for account in [a for a in self.mt5.accounts if a.get("active")]:
            client = self.mt5._client_for(account)
            positions = client.positions_get()
            if not positions:
                continue
            pos_by_ticket = {p.ticket: p for p in positions}

            # ---- 1) Cerrar entrada específica (por price_open) ----
            if wants_close_entry:
                close_price = prices[0]
                keep_price = prices[1] if len(prices) >= 2 else None

                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if self.torofx_provider_tag_match not in (t.provider_tag or "").upper():
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue

                    info = self.mt5.symbol_info(t.symbol)
                    if not info:
                        continue
                    point = float(info.point)
                    tol = self.torofx_close_entry_tolerance_pips * point

                    entry = float(pos.price_open)

                    # No cerrar el "keep" si viene
                    if keep_price is not None and abs(entry - keep_price) <= tol:
                        continue

                    if abs(entry - close_price) <= tol:
                        action_key = f"TOROFX_CLOSE_ENTRY_{int(close_price)}"
                        if action_key in t.actions_done:
                            continue

                        any_matched_trade = True
                        t.actions_done.add(action_key)
                        self._do_partial_close(account, ticket, 100, reason=f"TOROFX close entry {close_price}")
                        self._notify_bg(
                            account["name"],
                            f"🧹 TOROFX: cerrada entrada ≈{close_price}\nTicket: {ticket} | Entry: {entry:.2f}"
                        )
                continue  # si fue “close entry”, no hacemos otras acciones en el mismo mensaje

            # ---- 2) BE / risk off ----
            if wants_be:
                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if self.torofx_provider_tag_match not in (t.provider_tag or "").upper():
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue

                    action_key = "TOROFX_BE"
                    if action_key in t.actions_done:
                        continue

            t.actions_done.add(action_key)

            # aplica BE directo (sin offset) usando executor
            self.mt5.set_be(account=account, ticket=int(ticket))
            continue

            # ---- 3) Parcial por pips ----
            if wants_partial:
                pct_use = pct if pct is not None else int(self.torofx_partial_default_percent)
                pct_use = max(1, min(100, pct_use))

                # Si no trae pips en el mensaje, usamos el default torofx_partial_min_pips
                pips_need = float(pips_threshold) if pips_threshold is not None else float(self.torofx_partial_min_pips)

                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if self.torofx_provider_tag_match not in (t.provider_tag or "").upper():
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue

                    info = self.mt5.symbol_info(t.symbol)
                    if not info:
                        continue
                    point = float(info.point)

                    is_buy = (t.direction == "BUY")
                    entry = float(pos.price_open)
                    current = float(pos.price_current)

                    profit_pips = ((current - entry) / point) if is_buy else ((entry - current) / point)

                    # gate por pips
                    if profit_pips < pips_need:
                        continue

                    action_key = f"TOROFX_PARTIAL_{pct_use}_AT_{int(pips_need)}"
                    if action_key in t.actions_done:
                        continue

                    any_matched_trade = True
                    t.actions_done.add(action_key)

                    # cierre parcial con fallback min-lot (lo resuelve executor)
                    self._do_partial_close(account, ticket, pct_use, reason=f"TOROFX partial {pct_use}% @ +{pips_need}")
                    self._notify_bg(
                        account["name"],
                        f"✂️ TOROFX parcial ejecutado\nTicket: {ticket} | {t.symbol} | {t.direction}\n"
                        f"Profit≈{profit_pips:.1f} pips | Cierre: {pct_use}%"
                    )

        # Consumimos el mensaje si era de gestión TOROFX (aunque no haya match en ese instante)
        return True

