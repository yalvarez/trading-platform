from .trade_utils import pips_to_price, safe_comment, valor_pip, calcular_sl_por_pnl, calcular_volumen_parcial, calcular_trailing_retroceso, calcular_sl_default
from .mt5_executor import MT5Executor
from .notifications.telegram import TelegramNotifierAdapter
import asyncio
import time
import re
from dataclasses import dataclass, field
from typing import Optional
import os
from enum import Enum
from . import mt5_constants as mt5
from .mt5_client import MT5Client
from prometheus_client import Counter, Gauge
import logging
import datetime
import redis.asyncio as redis_async

class TradingMode(Enum):
    GENERAL = "general"
    BE_PIPS = "be_pips"
    BE_PNL = "be_pnl"
    REENTRY = "reentry"

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
    # Timestamp para ventana de gracia reentry
    reentry_tp1_time: Optional[float] = None


class TradeManager:

    def _ensure_account_dict(self, account):
        """
        Garantiza que account sea un dict de cuenta válido.
        Si recibe un string, busca el dict correspondiente en self.mt5.accounts y config_provider.get_accounts().
        Si no lo encuentra, loguea y retorna None.
        """
        if isinstance(account, dict):
            return account
        name = str(account)
        # Buscar en self.mt5.accounts
        accounts = []
        if hasattr(self.mt5, 'accounts') and self.mt5.accounts:
            accounts.extend(self.mt5.accounts)
        # Buscar en config_provider si existe
        if hasattr(self, 'config_provider') and self.config_provider:
            try:
                accounts.extend(self.config_provider.get_accounts())
            except Exception:
                pass
        for acc in accounts:
            if acc.get('name') == name:
                return acc
        log.error(f"[TM][ERROR] No se encontró el dict de cuenta para el nombre: {name}. Abortando operación.")
        return None

    @staticmethod
    # pips_to_price y safe_comment ahora están en trade_utils.py
    def _pips_to_price(self, symbol: str, pips: float, point: float) -> float:
        return pips_to_price(symbol, pips, point)
    def _safe_comment(self, tag: str) -> str:
        return safe_comment(tag, getattr(self, 'comment_prefix', 'TM'))
    def register_trade(self, account_name: str, ticket: int, symbol: str, direction: str, provider_tag: str, tps: list[float], planned_sl: float = None, group_id: int = None):
        """
        Registra un trade en el manager. Si no hay SL válido, calcula uno por fallback y lo asigna.
        """

        # Si planned_sl es None o 0.0, lanzar un warning y no registrar el trade
        if planned_sl is None or planned_sl == 0.0:
            log.error(f"[TM][ERROR] planned_sl debe ser el SL real usado en MT5. Registro ignorado. ticket={ticket} symbol={symbol} provider={provider_tag} planned_sl={planned_sl}")
            return

        groupId = int(group_id) if group_id is not None else int(ticket)
        self.trades[int(ticket)] = ManagedTrade(
            account_name=account_name,
            ticket=int(ticket),
            symbol=symbol,
            direction=direction,
            provider_tag=provider_tag,
            group_id=groupId,
            tps=list(tps or []),
            planned_sl=float(planned_sl),
        )
        self.group_addon_count.setdefault((account_name, groupId), 0)
        log.info("[TM] ✅ registered ticket=%s acct=%s group=%s provider=%s tps=%s planned_sl=%s", ticket, account_name, groupId, provider_tag, tps, planned_sl)
        try:
            TRADES_OPENED.inc()
            ACTIVE_TRADES.set(len(self.trades))
        except Exception:
            pass
    def _effective_close_percent(self, ticket: int, desired_percent: int) -> int:
        if desired_percent >= 100:
            return 100

        # Use the first active account for context (should be refactored to always have account)
        accounts = self.config_provider.get_accounts() if self.config_provider else self.mt5.accounts
        account = next((a for a in accounts if a.get("active")), None)
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

        close_vol = calcular_volumen_parcial(v, desired_percent, step, vmin)
        if close_vol < vmin or close_vol <= 0:
            return 100
        remaining = v - close_vol
        if remaining > 0 and remaining < vmin:
            return 100
        pct_real = int((close_vol / v) * 100) if v > 0 else desired_percent
        return pct_real
    # --- Scaling out para trades sin TP (ej. TOROFX) ---
    async def _maybe_scaling_out_no_tp(self, account: dict, pos, point: float, is_buy: bool, current: float, trade: ManagedTrade):
        account = self._ensure_account_dict(account)
        trailing_pips_last_tramo = getattr(self, 'torofx_trailing_last_tramo_pips', 40.0)
        if not hasattr(trade, 'trailing_active_last_tramo'):
            trade.trailing_active_last_tramo = False
        if not hasattr(trade, 'trailing_peak_last_tramo'):
            trade.trailing_peak_last_tramo = None
        if not hasattr(trade, 'first_tramo_close_price'):
            trade.first_tramo_close_price = None
        if trade.tps:
            return
        tramo_pips = float(self.config_provider.get('SCALING_TRAMO_PIPS', getattr(self, 'scaling_tramo_pips', 40.0)))
        percent_per_tramo = float(self.config_provider.get('SCALING_PERCENT_PER_TRAMO', getattr(self, 'scaling_percent_per_tramo', 25)))
        entry = trade.entry_price if trade.entry_price is not None else float(pos.price_open)
        symbol = trade.symbol.upper() if hasattr(trade, 'symbol') else ''
        client = self.mt5._client_for(account)
        if not hasattr(trade, 'actions_done') or trade.actions_done is None:
            trade.actions_done = set()
        pips_ganados = ((current - entry) if is_buy else (entry - current)) / 0.1
        tramo = int(pips_ganados // tramo_pips)

        # Usar partial close robusto
        for t in [1, 2, 3]:
            action = "HIT_TP_SCALING_TRAMO_{t}"
            if tramo >= t and action not in trade.actions_done:                
                result = await self._do_partial_close(account, int(pos.ticket), percent=int(percent_per_tramo), reason=f"ScalingOut-{t}")
                if t == 1:
                    trade.first_tramo_close_price = float(current)
                if t == 2:
                    await self._do_be(account, int(pos.ticket), point, is_buy)
                if t == 3:
                    await self._do_be(account, int(pos.ticket), point, is_buy, override_price=trade.first_tramo_close_price)
                if result is not None:
                    trade.actions_done.add(action)
                    break

        # Activar trailing solo después del cierre del tercer tramo
        if "HIT_TP_SCALING_TRAMO_3" in trade.actions_done and not trade.trailing_active_last_tramo:
            trade.trailing_active_last_tramo = True
            trade.trailing_peak_last_tramo = float(current)
            self._notify_bg(account, f"🚦 Trailing activado tras tercer tramo | Ticket: {int(pos.ticket)} | Peak: {current}")

        # Si el trailing tras el tercer tramo está activo, monitorear retroceso
        if trade.trailing_active_last_tramo:
            peak = trade.trailing_peak_last_tramo or float(current)
            if (is_buy and current > peak) or (not is_buy and current < peak):
                trade.trailing_peak_last_tramo = float(current)
                peak = float(current)
            retroceso = calcular_trailing_retroceso(peak, current, point, is_buy)
            if retroceso >= trailing_pips_last_tramo:
                # Cierre total usando partial close robusto
                await self._do_partial_close(account, int(pos.ticket), percent=100, reason="TrailingClose")
                self._notify_bg(account, f"🚦 Trailing: Trade cerrado por retroceso de {trailing_pips_last_tramo} pips | Ticket: {int(pos.ticket)}")
                await self.notify_trade_event(
                    'close',
                    account_name=account["name"],
                    ticket=int(pos.ticket),
                    symbol=symbol,
                    reason=f"Trailing retroceso {trailing_pips_last_tramo} pips"
                )
                trade.trailing_active_last_tramo = False

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
        mt5_exec=None,
        mt5=None,
        *,
        magic: int = 987654,
        loop_sleep_sec: float = 1.0,

        scalp_tp1_percent: int = 50,
        scalp_tp2_percent: int = 80,

        long_tp1_percent: int = 50,
        long_tp2_percent: int = 80,
        runner_retrace_pips: float = 20,
        buffer_pips: float = 2.0,

        enable_be_after_tp1: bool = True,
        be_offset_pips: float = 3.0,

        enable_trailing: bool = True,
        trailing_activation_after_tp2: bool = True,
        trailing_activation_pips: float = 30.0,
        trailing_stop_pips: float = 20.0,

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
        torofx_partial_min_pips: float = 30.0,     # “+50/60” -> usa 50 por defecto
        torofx_close_entry_tolerance_pips: float = 10.0,  # para “cierro mi entrada 4330”
        torofx_provider_tag_match: str = "TOROFX",  # substring en provider_tag

        # --- Scaling out config ---
        scaling_tramo_pips: float = 40.0,
        scaling_percent_per_tramo: int = 25,

        default_sl: float = 60.0,  # SL por defecto en pips

        notifier=None, 
        config_provider=None,
        notify_connect: bool | None = None,  # compat
        redis_url: str = None, redis_conn=None):
        self.mt5 = mt5_exec if mt5 is None else mt5
        self.magic = magic
        self.loop_sleep_sec = loop_sleep_sec
        self.config_provider = config_provider
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
        # Already set in __init__

    # ----------------------------
    # Notifier
    # ----------------------------
    def _notify_bg(self, account: dict, message: str):
        # Centraliza notificaciones Telegram usando chat_id
         notifier = TelegramNotifierAdapter(self.notifier)
        # import asyncio
        # chat_id = account.get("chat_id")
        # if chat_id:
        #     asyncio.create_task(notifier.notify(chat_id, message))
        # else:
        #     account_name = account.get("name")
        #     import logging
        #     logging.getLogger("trade_orchestrator.trade_manager").warning(f"No chat_id for account {account_name}, notificación no enviada: {message}")

    async def notify_trade_event(self, event: str, **kwargs):
        notifier = TelegramNotifierAdapter(self.notifier)
        await notifier.notify_trade_event(event, **kwargs)

    def update_trade_signal(self, *, ticket: int, tps: list[float], planned_sl: Optional[float], provider_tag: Optional[str] = None):
        t = self.trades.get(int(ticket))
        if not t:
            return
        # Si planned_sl es None o 0.0, intentar obtener el precio real; si no es posible, abortar el cálculo
        if planned_sl is None or planned_sl == 0.0:
            symbol = getattr(t, 'symbol', None)
            direction = getattr(t, 'direction', 'BUY')
            account_name = getattr(t, 'account_name', None)
            symbol_upper = symbol.upper() if symbol else ""
            default_sl_pips = getattr(self, 'default_sl', None)
            env_override = None
            if symbol_upper == "XAUUSD":
                env_override = self.config_provider.get("DEFAULT_SL_XAUUSD_PIPS") if self.config_provider else os.getenv("DEFAULT_SL_XAUUSD_PIPS")
            if env_override is not None:
                try:
                    default_sl_pips = float(env_override)
                except Exception:
                    pass
            client = getattr(self, 'mt5', None)
            price = None
            point = 0.1 if symbol_upper.startswith('XAU') else 0.00001
            if client is not None:
                try:
                    info = client._client_for({'name': account_name}).symbol_info(symbol)
                    if info and hasattr(info, 'point'):
                        point = float(getattr(info, 'point', point))
                    price = float(getattr(info, 'bid', None))
                except Exception:
                    pass
            if price is None:
                log.error(f"[TM][ERROR] No se pudo obtener el precio actual de {symbol} para calcular el SL por defecto. Abortando update_trade_signal. ticket={ticket}")
                return
            try:
                planned_sl = calcular_sl_default(symbol, direction, price, point, default_sl_pips)
            except Exception as e:
                log.error(f"[TM][PATCH] Error calculando planned_sl centralizado en update: {e}")
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
        
        while True:
            accounts = self.config_provider.get_accounts() if self.config_provider else self.mt5.accounts
            accounts = [a for a in accounts if a.get("active")]
            await asyncio.gather(*(self._tick_once_account(account) for account in accounts))

    async def _tick_once_account(self, account):
        """
        Gestiona los trades de una sola cuenta (idéntico a la lógica previa de _tick_once, pero por cuenta).
        """
        account = self._ensure_account_dict(account)
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
                trade = self.trades[ticket]
                if trade.account_name != account["name"]:
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
            for ticket, trade in list(self.trades.items()):
                if trade.account_name != account["name"]:
                    continue

                pos = pos_by_ticket.get(ticket)
                
                if not pos:
                    continue
                
                if pos.magic != self.mt5.magic:
                    continue

                info = client.symbol_info(trade.symbol)
                tick = client.symbol_info_tick(trade.symbol)
                
                if not info or not tick:
                    continue

                point = float(info.point)
                is_buy = (trade.direction == "BUY")
                current = float(pos.price_current)

                # Guarda entry y volumen inicial si no están
                if trade.entry_price is None:
                    trade.entry_price = float(pos.price_open)
                if trade.initial_volume is None:
                    trade.initial_volume = float(pos.volume)

                # Llamada a la gestión según modalidad, ahora con contexto completo
                await self.gestionar_trade(trade, account, pos=pos, point=point, is_buy=is_buy, current=current)
        
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
        accounts = self.config_provider.get_accounts() if self.config_provider else self.mt5.accounts
        for account in [a for a in accounts if a.get("active")]:
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

    async def _do_be(self, account: dict, ticket: int, point: float, is_buy: bool, override_price: float = None):
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
        # Calcular precio BE: SL = override_price (si existe) o precio de entrada (entry_price) + spread (BUY) o - spread (SELL) + offset
        entry_price = float(override_price) if override_price is not None else float(getattr(pos, 'price_open', 0.0))
        # Offset BE en cero
        offset = 0.0
        info = client.symbol_info(symbol) if client else None
        if not info:
            log.error(f"[BE-DEBUG] No se pudo obtener info de símbolo para {symbol} en _do_be")
            return 100
        spread = float(getattr(info, 'spread', 0.0)) * float(getattr(info, 'point', point))
        # Si el spread es 0, usar un valor mínimo configurable o default
        if spread == 0.0:
            spread = getattr(self, 'be_min_spread', 0.0) * point if hasattr(self, 'be_min_spread') else 0.0
        if is_buy:
            be = entry_price + spread + offset
        else:
            be = entry_price - spread - offset
        # Validar volumen > 0
        v = float(getattr(pos, 'volume', 0.0))
        if v <= 0:
            log.error(f"[BE-DEBUG] No se puede aplicar BE, volumen=0 | ticket={ticket}")
            self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)}\nLa posición ya está cerrada (volumen=0).")
            await self.notify_trade_event(
                'be',
                account_name=account["name"],
                message=f"❌ BE falló | Ticket: {int(ticket)}\nLa posición ya está cerrada (volumen=0)."
            )
            return
        # Validar y ajustar distancia mínima de stop para BE
        # Acceso robusto a stops_level/stop_level
        if hasattr(info, "stops_level"):
            stop_level_raw = getattr(info, "stops_level", 0.0)
        elif hasattr(info, "stop_level"):
            stop_level_raw = getattr(info, "stop_level", 0.0)
        else:
            log.warning(f"[TM][WARN] SymbolInfo for {symbol} no tiene stops_level ni stop_level. Usando 0.0")
            stop_level_raw = 0.0
        min_stop = float(stop_level_raw) * float(getattr(info, 'point', point))
        price_current = float(getattr(pos, 'price_current', entry_price))

        # Ajustar el BE si no cumple la distancia mínima
        be_attempt = be
        max_be_retries = 10
        for be_try in range(max_be_retries):
            dist = abs(be_attempt - price_current)
            if dist < min_stop:
                # Ajustar el SL al valor más cercano permitido
                if is_buy:
                    be_attempt = price_current - min_stop
                else:
                    be_attempt = price_current + min_stop
                log.warning(f"[BE-DEBUG] Ajustando BE para cumplir min_stop | ticket={ticket} intento={be_try+1} nuevo_BE={be_attempt} min_stop={min_stop}")
            else:
                break
        else:
            log.error(f"[BE-DEBUG] No se pudo ajustar BE para cumplir min_stop tras {max_be_retries} intentos | ticket={ticket}")
            self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)}\nNo se pudo ajustar el SL para cumplir la distancia mínima de stop.")
            await self.notify_trade_event(
                'be',
                account_name=account["name"],
                message=f"❌ BE falló | Ticket: {int(ticket)}\nNo se pudo ajustar el SL para cumplir la distancia mínima de stop."
            )
            return
        log.info(f"[BE-DEBUG] BE calculation ajustado | entry_price={entry_price} spread={spread} offset={offset} is_buy={is_buy} => BE={be_attempt}")
        log.info(f"[BE-DEBUG] BE calculation | entry_price={entry_price} spread={spread} offset={offset} is_buy={is_buy} => BE={be}")
        # --- Probar todos los filling modes para modificar SL (BE) ---
        supported_filling_modes = [1, 3, 2]  # IOC, FOK, RETURN
        be_applied = False
        for type_filling in supported_filling_modes:
            pos_info = client.positions_get(ticket=int(ticket))
            if not pos_info or len(pos_info) == 0:
                log.error(f"[BE-DEBUG] No se pudo obtener info de la posición para modificar SL | ticket={ticket}")
                continue
            pos0 = pos_info[0]
            req = {
                "action": 6,  # TRADE_ACTION_SLTP (MT5)
                "position": int(ticket),
                "sl": float(be_attempt),
                "tp": float(getattr(pos0, 'tp', 0.0)),
                "comment": self._safe_comment("BE-general"),
                "type_filling": type_filling
            }
            log.info(f"[BE-DEBUG] Enviando order_send | req={req}")
            res = client.order_send(req)
            log.info(f"[BE-DEBUG] Resultado order_send | res={res}")
            if res and getattr(res, "retcode", None) == 10009:
                await asyncio.sleep(1)
                pos_check = client.positions_get(ticket=int(ticket))
                sl_actual = None
                if pos_check and len(pos_check) > 0:
                    sl_actual = float(getattr(pos_check[0], 'sl', 0.0))
                if sl_actual is not None and abs(sl_actual - float(be_attempt)) < 1e-4:
                    self._notify_bg(account["name"], f"✅ BE aplicado | Ticket: {int(ticket)} | SL: {be_attempt:.5f}")
                    log.info("[TM] BE applied ticket=%s sl=%.5f", int(ticket), be_attempt)
                    await self.notify_trade_event(
                        'be',
                        account_name=account["name"],
                        message=f"✅ BE aplicado | Ticket: {int(ticket)} | SL: {be_attempt:.5f}"
                    )
                    log.info(f"[BE-DEBUG] FIN _do_be OK | account={account.get('name')} ticket={ticket}")
                    be_applied = True
                    break
                else:
                    log.error(f"[BE-DEBUG] SL no cambió tras BE | esperado={be_attempt} actual={sl_actual}")
                    self._notify_bg(
                        account["name"],
                        f"❌ BE falló | Ticket: {int(ticket)}\nSL no cambió tras BE (esperado={be_attempt}, actual={sl_actual})"
                    )
                    await self.notify_trade_event(
                        'be',
                        account_name=account["name"],
                        message=f"❌ BE falló | Ticket: {int(ticket)}\nSL no cambió tras BE (esperado={be_attempt}, actual={sl_actual})"
                    )
                    return
            elif res and getattr(res, "retcode", None) not in [10030, 10013]:
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
        if not be_applied:
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
        account = self._ensure_account_dict(account)
        if not account:
            return
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
        delta_vol = vol_before - vol_after
        log.info(f"[DEBUG] Volumen antes del cierre parcial: {vol_before} | después: {vol_after} | delta: {delta_vol}")
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

        # Validación crítica: ¿el volumen realmente cambió?
        if ok and delta_vol > 0.00001:
            log.info("[TM] 🎯 partial_close ticket=%s percent=%s reason=%s | Volumen cambiado correctamente (delta=%.5f)", int(ticket), int(percent), reason, delta_vol)
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
                closed_volume=delta_vol,
            )
            # Si el cierre es total, auditar solo si el trade existe
            if percent >= 100:
                t_audit = self.trades.get(int(ticket))
                if t_audit is not None:
                    await self.audit_trade_close(account["name"], int(ticket), t_audit, reason, pos)
        else:
            log.error("[TM][CRITICAL] ❌ partial_close NO CAMBIÓ VOLUMEN | ticket=%s percent=%s reason=%s | delta=%.5f | ok=%s", int(ticket), int(percent), reason, delta_vol, ok)
            await self.notify_trade_event(
                'partial',
                account_name=account["name"],
                ticket=int(ticket),
                symbol=symbol,
                close_percent=percent,
                close_price=getattr(pos, 'price_current', 0.0) if pos else 0.0,
                closed_volume=delta_vol,
            )

    # ----------------------------
    # TP / Runner / BE
    # ----------------------------
    async def _maybe_take_profits(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        # Solo loguear eventos relevantes, no cada tick
        buffer_price = self.buffer_pips * point
        if not t.tps:
            return
        # --- Runner y TP logic ---
        # Guardar el precio máximo alcanzado
        if t.mfe_peak_price is None:
            t.mfe_peak_price = current
        else:
            if is_buy and current > t.mfe_peak_price:
                t.mfe_peak_price = current
            if (not is_buy) and current < t.mfe_peak_price:
                t.mfe_peak_price = current

        tp_percents = [
            self.long_tp1_percent if self._is_long_mode(t) else self.scalp_tp1_percent,
            self.long_tp2_percent if self._is_long_mode(t) else self.scalp_tp2_percent,
        ]
        for idx, tp in enumerate(t.tps):
            tp_idx = idx + 1
            if tp_idx not in t.tp_hit and self._tp_hit(is_buy, current, float(tp), buffer_price):
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
                await self._do_partial_close(account, pos.ticket, pct_eff, reason=f"TP{tp_idx} (objetivo={float(tp):.5f} actual={current:.5f})")
                log.info(f"[DEBUG] Finished _do_partial_close for TP{tp_idx} | account={account['name']} ticket={int(pos.ticket)} pct={pct_eff}")
                t.tp_hit.add(tp_idx)
                # BE solo tras TP1
                if tp_idx == 1 and self.enable_be_after_tp1:
                    log.info(f"[BE-DEBUG] Intentando aplicar BE | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol} dir={t.direction} entry={t.entry_price} tp1={float(tp)}")
                    await self._do_be(account, pos.ticket, point, is_buy)
                    log.info(f"[BE-DEBUG] BE ejecutado | account={account['name']} ticket={int(pos.ticket)} symbol={t.symbol}")
                # Runner solo tras TP2 (ahora para cualquier trade con al menos 2 TPs)
                    if tp_idx == 2:
                        # --- Runner momentum filter integration ---
                        candles = self._get_recent_candles(t.symbol) if hasattr(self, '_get_recent_candles') else None
                        if candles and self.runner_momentum_filter(t.symbol, candles):
                            t.runner_enabled = True
                            log.info(f"[RUNNER-FILTER] Runner enabled for {t.symbol} ticket={int(pos.ticket)}")
                        else:
                            t.runner_enabled = False
                            log.info(f"[RUNNER-FILTER] Runner NOT enabled for {t.symbol} ticket={int(pos.ticket)} (momentum filter failed)")
                try:
                    TP_HITS.labels(tp=f"tp{tp_idx}").inc()
                except Exception:
                    pass
                return

        # Robustecer: Si TP2 ya está en tp_hit y la variable de activación está activa, runner_enabled debe estar activo
        if self.trailing_activation_after_tp2 and (2 in t.tp_hit):
            t.runner_enabled = True

        # Runner retrace (ahora para cualquier trade con runner_enabled)
        if t.runner_enabled and t.mfe_peak_price is not None:
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
        if self.config_provider:
            try:
                trailing_cfg = self.config_provider.get('trailing', {})
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

    def handle_hannah_management_message(self, source_chat_id: int, raw_text: str) -> bool:
        """
        Procesa mensajes de gestión de Hannah:
        - Solo ejecuta cierre parcial y BE si NO se ha alcanzado TP1.
        - Si el precio actual está por debajo del entry y no se puede aplicar BE, cierra el trade completamente.
        - Si ya se alcanzó TP1, ignora el mensaje y sigue la gestión normal.
        """
        import re
        text = (raw_text or "").strip()
        if not text:
            return False

        up = text.upper()

        # --- Cierre inmediato de todas las posiciones ---
        close_all_keywords = ["CLOSE ALL", "CLOSE ALL POSITIONS", "PRICE SPIKED"]
        if any(k in up for k in close_all_keywords):
            any_matched_trade = False
            provider_tag_match = "HANNAH"
            for account in [a for a in self.mt5.accounts if a.get("active")]:
                client = self.mt5._client_for(account)
                positions = client.positions_get()
                if not positions:
                    continue
                pos_by_ticket = {p.ticket: p for p in positions}
                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if provider_tag_match not in (t.provider_tag or "").upper():
                        continue
                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue
                    action_key = f"HANNAH_CLOSE_ALL"
                    if hasattr(t, "actions_done") and action_key in t.actions_done:
                        continue
                    if not hasattr(t, "actions_done"):
                        t.actions_done = set()
                    self._do_partial_close(account, ticket, 100, reason="HANNAH close all (alert)")
                    self._notify_bg(
                        account["name"],
                        f"🚨 HANNAH: Cierre inmediato por alerta\nTicket: {ticket}"
                    )
                    t.actions_done.add(action_key)
                    any_matched_trade = True
            return any_matched_trade

        # --- Cierre parcial 50% (sin BE, sin TP1 check) ---
        close_half_keywords = ["CLOSE HALF", "HALF GUYS", "HALF NOW", "HALF ONLY"]
        if any(k in up for k in close_half_keywords):
            any_matched_trade = False
            provider_tag_match = "HANNAH"
            for account in [a for a in self.mt5.accounts if a.get("active")]:
                client = self.mt5._client_for(account)
                positions = client.positions_get()
                if not positions:
                    continue
                pos_by_ticket = {p.ticket: p for p in positions}
                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if provider_tag_match not in (t.provider_tag or "").upper():
                        continue
                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue
                    action_key = f"HANNAH_CLOSE_HALF"
                    if hasattr(t, "actions_done") and action_key in t.actions_done:
                        continue
                    if not hasattr(t, "actions_done"):
                        t.actions_done = set()
                    self._do_partial_close(account, ticket, 50, reason="HANNAH close half (alert)")
                    self._notify_bg(
                        account["name"],
                        f"✂️ HANNAH: Cierre parcial 50% por alerta\nTicket: {ticket}"
                    )
                    t.actions_done.add(action_key)
                    any_matched_trade = True
            return any_matched_trade

        # Detectar mensaje de gestión Hannah (palabras clave)
        has_partial = any(w in up for w in ["SECURE", "HALF", "PROFITS", "COLECT", "COLLECT", "CIERRA", "CIERRE", "PARCIAL"])
        has_be = any(w in up for w in ["BREAKEVEN", "BREAK EVEN", "BREAK-EVEN", "BE", "RISK FREE", "RISKLESS", "SIN RIESGO"])
        # Ejemplo: "Secure half your Profits & set breakeven"
        if not (has_partial and has_be):
            return False

        # Por defecto, cierre parcial 50%
        pct = 50

        # Buscar porcentaje explícito (opcional)
        m_pct = re.search(r"(\d{1,3})\s*%", text)
        if m_pct:
            pct = max(1, min(100, int(m_pct.group(1))))

        any_matched_trade = False
        provider_tag_match = "HANNAH"
        for account in [a for a in self.mt5.accounts if a.get("active")]:
            client = self.mt5._client_for(account)
            positions = client.positions_get()
            if not positions:
                continue
            pos_by_ticket = {p.ticket: p for p in positions}

            for ticket, t in list(self.trades.items()):
                if t.account_name != account["name"]:
                    continue
                if provider_tag_match not in (t.provider_tag or "").upper():
                    continue

                pos = pos_by_ticket.get(ticket)
                if not pos or pos.magic != self.mt5.magic:
                    continue

                # Si ya alcanzó TP1, ignorar el mensaje
                if hasattr(t, "tp_hit") and 1 in getattr(t, "tp_hit", set()):
                    continue

                # Evitar repetir la acción
                action_key = f"HANNAH_PARTIAL_BE_{pct}"
                if hasattr(t, "actions_done") and action_key in t.actions_done:
                    continue
                if not hasattr(t, "actions_done"):
                    t.actions_done = set()

                info = self.mt5.symbol_info(t.symbol)
                if not info:
                    continue
                point = float(info.point)
                entry = float(pos.price_open)
                current = float(pos.price_current)

                # 1. Cierre parcial
                self._do_partial_close(account, ticket, pct, reason="HANNAH partial+BE")
                # 2. Intentar BE
                be_applied = False
                try:
                    # Intentar mover SL a BE (usa _do_be si está disponible)
                    is_buy = (t.direction == "BUY")
                    # Si el precio actual está por debajo del entry, no se puede aplicar BE
                    if (is_buy and current < entry) or ((not is_buy) and current > entry):
                        # Cerrar trade completamente
                        self._do_partial_close(account, ticket, 100, reason="HANNAH close loss (BE not possible)")
                        self._notify_bg(
                            account["name"],
                            f"❌ HANNAH: BE no posible, trade cerrado por debajo del entry\nTicket: {ticket} | Entry: {entry:.2f} | Current: {current:.2f}"
                        )
                        t.actions_done.add(action_key)
                        any_matched_trade = True
                        continue
                    # Si se puede aplicar BE
                    # Si tienes un método async _do_be, deberías llamarlo con create_task o similar, aquí llamo directo por simplicidad
                    self.mt5.set_be(account=account, ticket=int(ticket))
                    self._notify_bg(
                        account["name"],
                        f"🔒 HANNAH: Parcial {pct}% y BE aplicado\nTicket: {ticket} | Entry: {entry:.2f} | Current: {current:.2f}"
                    )
                    be_applied = True
                except Exception as e:
                    self._notify_bg(
                        account["name"],
                        f"⚠️ HANNAH: Error al aplicar BE\nTicket: {ticket} | Error: {e}"
                    )
                t.actions_done.add(action_key)
                any_matched_trade = True

        return any_matched_trade

    async def gestionar_trade(self, trade, cuenta, pos=None, point=None, is_buy=None, current=None):
        """
        Decide y delega la gestión del trade según la modalidad configurada en la cuenta.
        Llama a la función de gestión correspondiente:
        - general: gestión clásica (TP, runner, trailing, BE, etc.)
        - be_pips: mueve SL a BE al alcanzar X pips, luego gestión normal
        - be_pnl: tras parcial y X pips, mueve SL para proteger ganancia, luego gestión normal
        - reentry: cierra 100% en TP1, abre runner con menor lote, SL en entry, TP en TP2
        Si el modo es desconocido, usa la gestión general.
        """
        modo = cuenta.get("trading_mode", TradingMode.GENERAL.value)
        if modo == TradingMode.GENERAL.value:
            return await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)
        elif modo == TradingMode.BE_PIPS.value:
            return await self.gestionar_trade_be_pips(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)
        elif modo == TradingMode.BE_PNL.value:
            return await self.gestionar_trade_be_pnl(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)
        elif modo == TradingMode.REENTRY.value:
            return await self.gestionar_trade_reentry(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)
        else:
            # fallback a general si el modo es desconocido
            return await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)

    async def gestionar_trade_reentry(self, trade, cuenta, pos=None, point=None, is_buy=None, current=None):
        """
        Modalidad reentry:
        - Al alcanzar TP1, cerrar 100% del trade original.
        - Abrir un nuevo trade (runner) con menor lote, SL en entry, TP en TP2.
        - El runner se gestiona con trailing o cierre manual.
        """
        # Obtener datos necesarios si no se pasan
        if pos is None or point is None or is_buy is None or current is None:
            client = self.mt5._client_for(cuenta)
            pos_list = client.positions_get(ticket=int(trade.ticket))
            
            if not pos_list:
                return
            
            pos = pos_list[0]
            info = client.symbol_info(trade.symbol)
            tick = client.symbol_info_tick(trade.symbol)
            
            if not info or not tick:
                return
            
            point = float(info.point)
            is_buy = (trade.direction == "BUY")
            current = float(pos.price_current)


        # Fallback a general solo una vez si no hay TPs suficientes
        if not hasattr(trade, "_reentry_fallback_logged"):
            trade._reentry_fallback_logged = False
        tp1 = trade.tps[0] if trade.tps else None
        tp2 = trade.tps[1] if len(trade.tps) > 1 else None
        if not tp1 or not tp2:
            if not trade._reentry_fallback_logged:
                log.info(f"[REENTRY] No hay suficientes TPs para modalidad reentry en {trade.symbol} ticket={trade.ticket}. Fallback a gestión general.")
                trade._reentry_fallback_logged = True
            return await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)

        # Usar un flag en el trade para evitar múltiples ejecuciones
        if not hasattr(trade, "reentry_done"):
            trade.reentry_done = False

        log.info(f"[REENTRY] Evaluando el contenido de [cuenta] => {cuenta}.")
        # --- Robustecer: asegurar que cuenta sea dict ---
        cuenta_dict = cuenta
        if isinstance(cuenta, str):
            # Buscar el dict de cuenta por nombre
            cuentas = getattr(self.mt5, 'accounts', [])
            match = next((a for a in cuentas if a.get('name') == cuenta), None)
            if match:
                cuenta_dict = match
                log.warning(f"[REENTRY][FIX] 'cuenta' era str, corregido a dict para runner: {cuenta}")
            else:
                log.error(f"[REENTRY][ERROR] No se encontró el dict de cuenta para el nombre: {cuenta}. Abortando apertura de runner.")
                return

        # Si TP1 alcanzado y no se ha hecho reentry
        if (is_buy and current >= tp1) or (not is_buy and current <= tp1):
            if not trade.reentry_done:
                client = self.mt5._client_for(cuenta_dict)
                log.info(f"[REENTRY] TP1 alcanzado para {trade.symbol} ticket={trade.ticket} en cuenta {cuenta_dict['name']}. Cerrando 100% trade original.")
                # Cerrar 100% del trade original
                await self._do_partial_close(cuenta_dict, trade.ticket, 100, reason="REENTRY_TP1")
                # Guardar timestamp de TP1 para ventana de gracia
                trade.reentry_tp1_time = time.time()
                # Intentar abrir runner
                candles = self._get_recent_candles(trade.symbol) if hasattr(self, '_get_recent_candles') else None
                allow_runner = False
                if candles and self.runner_momentum_filter(trade.symbol, candles):
                    allow_runner = True
                else:
                    # Si momentum filter falla, permitir solo si estamos en ventana de gracia (5s desde TP1)
                    if trade.reentry_tp1_time and (time.time() - trade.reentry_tp1_time) <= 3:
                        allow_runner = True
                        log.info(f"[REENTRY][GRACE] Ventana de gracia activa: permitiendo runner para {trade.symbol} ticket={trade.ticket}")
                if allow_runner:
                    original_vol = float(getattr(pos, "volume", 0.01))
                    raw_runner_lot = original_vol * 0.3
                    info = client.symbol_info(trade.symbol)
                    step = float(getattr(info, 'volume_step', 0.01)) if info else 0.01
                    vmin = float(getattr(info, 'volume_min', 0.01)) if info else 0.01
                    runner_lot = max(vmin, step * int(raw_runner_lot / step))
                    entry_price = float(getattr(pos, "price_open", current))
                    sl = entry_price
                    tp = tp2
                    log.info(f"[REENTRY] Abriendo runner para {trade.symbol} ticket={trade.ticket} en cuenta {cuenta_dict['name']}: lot={runner_lot} SL={sl} TP={tp}")
                    await self.mt5.open_runner_trade(
                        cuenta_dict,
                        symbol=trade.symbol,
                        direction=trade.direction,
                        volume=runner_lot,
                        sl=sl,
                        tp=tp,
                        provider_tag=f"{trade.provider_tag}_REENTRY"
                    )
                    trade.reentry_done = True
                    self._notify_bg(cuenta_dict["name"], f"🔁 REENTRY: Cerrado 100% en TP1 y abierto runner {runner_lot} lotes | SL={sl} TP={tp}")
                    log.info(f"[REENTRY] Runner abierto correctamente para {trade.symbol} ticket={trade.ticket} en cuenta {cuenta_dict['name']}.")
                else:
                    self._notify_bg(cuenta_dict["name"], f"⛔ REENTRY: Momentum filter rechazó runner para {trade.symbol} en TP1")
                    log.info(f"[REENTRY] Momentum filter rechazó runner para {trade.symbol} ticket={trade.ticket} en cuenta {cuenta_dict['name']}.")
                return

        # El runner se gestiona con trailing si está habilitado
        #if self.enable_trailing:
        #    log.info(f"[REENTRY] Evaluando trailing para runner {trade.symbol} ticket={trade.ticket} en cuenta {cuenta['name']}.")
        #    await self._maybe_trailing(cuenta, pos, point, is_buy, current, trade)

    async def gestionar_trade_general(self, trade, cuenta, pos=None, point=None, is_buy=None, current=None):
        """
        Lógica de gestión clásica: TP, runner, trailing, BE, etc. usando funciones comunes.
        - Ejecuta toma de ganancias parciales y trailing stop si está habilitado.
        - Usa helpers centralizados para cálculos de precios y pips.
        """
        # Si se pasan los argumentos, úsalos; si no, obténlos
        if pos is None or point is None or is_buy is None or current is None:
            client = self.mt5._client_for(cuenta)
            pos_list = client.positions_get(ticket=int(trade.ticket))
            if not pos_list:
                return
            pos = pos_list[0]
            info = client.symbol_info(trade.symbol)
            tick = client.symbol_info_tick(trade.symbol)
            if not info or not tick:
                return
            point = float(info.point)
            is_buy = (trade.direction == "BUY")
            current = float(pos.price_current)
        # TP y runner
        await self._maybe_take_profits(cuenta, pos, point, is_buy, current, trade)
        # Trailing
        if self.enable_trailing:
            await self._maybe_trailing(cuenta, pos, point, is_buy, current, trade)

        # --- Scaling out para trades sin TP (ej. TOROFX) ---
        provider_tag = getattr(trade, 'provider_tag', '') or ''
        tps = getattr(trade, 'tps', [])
        if (not tps) and ('TOROFX' in provider_tag.upper()):
            await self._maybe_scaling_out_no_tp(cuenta, pos, point, is_buy, current, trade)

    async def gestionar_trade_be_pips(self, trade, cuenta, pos=None, point=None, is_buy=None, current=None):
        """
        Lógica: al alcanzar X pips, cerrar 30% del trade, mover SL a BE, luego gestión normal (TP, runner, trailing).
        - Si el recorrido en pips >= be_pips y no se ha aplicado BE, cierra 30% y mueve SL a BE.
        - Luego ejecuta la gestión general.
        """
        # Fallback a general solo una vez si no hay TPs
        if not hasattr(trade, "_be_pips_fallback_logged"):
            trade._be_pips_fallback_logged = False
        if not trade.tps or len(trade.tps) == 0:
            if not trade._be_pips_fallback_logged:
                log.info(f"[BE_PIPS] No hay TPs para modalidad be_pips en {trade.symbol} ticket={trade.ticket}. Fallback a gestión general.")
                trade._be_pips_fallback_logged = True
            return await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)
        be_pips = cuenta.get("be_pips", 30)
        recorrido = self._get_recorrido_pips(trade, cuenta)
        if recorrido >= be_pips and not getattr(trade, "be_applied", False):
            # Cierre parcial 30%
            client = self.mt5._client_for(cuenta)
            pos_list = client.positions_get(ticket=int(trade.ticket))
            if pos_list:
                await self._do_partial_close(cuenta, trade.ticket, 30, reason=f"BE_PIPS {be_pips}pips")
            # Mover SL a BE
            self._move_sl_to_be(trade, cuenta)
            trade.be_applied = True
        # Reutilizar funciones comunes
        await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)

    async def gestionar_trade_be_pnl(self, trade, cuenta, pos=None, point=None, is_buy=None, current=None):
        """
        Lógica: al alcanzar X pips, cerrar 30% del trade, calcular SL en base al monto ganado en esa parcial, mover el SL, luego gestión normal.
        - Si el recorrido en pips >= be_pips y no se ha aplicado SL PnL, cierra 30%, calcula y mueve el SL.
        - Luego ejecuta la gestión general.
        """
        # Fallback a general solo una vez si no hay TPs
        if not hasattr(trade, "_be_pnl_fallback_logged"):
            trade._be_pnl_fallback_logged = False
        if not trade.tps or len(trade.tps) == 0:
            if not trade._be_pnl_fallback_logged:
                log.info(f"[BE_PNL] No hay TPs para modalidad be_pnl en {trade.symbol} ticket={trade.ticket}. Fallback a gestión general.")
                trade._be_pnl_fallback_logged = True
            return await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)
        be_pips = cuenta.get("be_pips", 30)
        recorrido = self._get_recorrido_pips(trade, cuenta)
        if recorrido >= be_pips and not getattr(trade, "sl_pnl_applied", False):
            # Cierre parcial 30%
            client = self.mt5._client_for(cuenta)
            pos_list = client.positions_get(ticket=int(trade.ticket))
            pnl_ganado = 0.0
            if pos_list:
                await self._do_partial_close(cuenta, trade.ticket, 30, reason=f"BE_PNL {be_pips}pips")
                # Intentar obtener el PnL de la parcial recién cerrada
                pos = pos_list[0]
                if hasattr(pos, "profit"):
                    pnl_ganado = float(getattr(pos, "profit", 0.0)) * 0.3  # Aproximación: 30% del profit actual
            # Calcular y mover SL en base al PnL ganado
            sl_price = self._calcular_sl_por_pnl(trade, cuenta, pnl_ganado)
            self._move_sl(trade, cuenta, sl_price)
            trade.sl_pnl_applied = True
        # Reutilizar funciones comunes
        await self.gestionar_trade_general(trade, cuenta, pos=pos, point=point, is_buy=is_buy, current=current)

    # Métodos auxiliares (esqueleto)
    def _get_current_price(self, symbol, cuenta):
        """
        Obtiene el precio actual (bid) del símbolo para la cuenta dada.
        """
        client = self.mt5._client_for(cuenta)
        tick = client.symbol_info_tick(symbol)
        return getattr(tick, "bid", None) if tick else None

    def _close_partial_and_be(self, trade, cuenta, tp1):
        """
        Realiza cierre parcial (50%) y mueve el SL a BE para el trade dado.
        Llama a early_partial_close y luego a la lógica de BE.
        """
        client = self.mt5._client_for(cuenta)
        ticket = trade.ticket
        # Cierre parcial
        asyncio.create_task(self.mt5.early_partial_close(cuenta, ticket, percent=0.5, provider_tag=trade.provider_tag, reason="TP1"))
        # Mover SL a BE (lógica clásica)
        # ...puedes llamar a modify_sl o lógica interna...
        pass

    def _get_recorrido_pips(self, trade, cuenta):
        """
        Calcula el recorrido en pips desde la entrada hasta el precio actual para el trade dado, usando el valor estándar de pip (ej. 0.10 para XAUUSD).
        """
        client = self.mt5._client_for(cuenta)
        pos_list = client.positions_get(ticket=int(trade.ticket))
        if not pos_list:
            return 0
        pos = pos_list[0]
        entry = float(getattr(pos, "price_open", 0.0))
        current = float(getattr(pos, "price_current", 0.0))
        volume = float(getattr(pos, "volume", 0.01))
        pip_value = valor_pip(trade.symbol, volume)
        if pip_value == 0:
            pip_value = 0.10  # fallback seguro para oro
        if trade.direction.upper() == "BUY":
            recorrido = (current - entry) / pip_value
        else:
            recorrido = (entry - current) / pip_value
        return round(recorrido, 1)

    def _move_sl_to_be(self, trade, cuenta):
        """
        Mueve el SL del trade al precio de entrada (break-even).
        """
        client = self.mt5._client_for(cuenta)
        pos_list = client.positions_get(ticket=int(trade.ticket))
        if not pos_list:
            return
        pos = pos_list[0]
        entry = float(getattr(pos, "price_open", 0.0))
        # Mover SL a precio de entrada
        asyncio.create_task(self.mt5.modify_sl(cuenta, trade.ticket, entry, reason="BE-auto", provider_tag=trade.provider_tag))

    def _calcular_sl_por_pnl(self, trade, cuenta, pnl_ganado):
        """
        Calcula el precio de SL que permite perder solo lo ganado en una parcial para el trade dado.
        Utiliza la función auxiliar centralizada.
        """
        client = self.mt5._client_for(cuenta)
        pos_list = client.positions_get(ticket=int(trade.ticket))
        if not pos_list:
            return 0
        pos = pos_list[0]
        entry = float(getattr(pos, "price_open", 0.0))
        volume = float(getattr(pos, "volume", 0.01))
        point = float(getattr(client.symbol_info(trade.symbol), "point", 0.00001))
        return calcular_sl_por_pnl(entry, trade.direction, pnl_ganado, volume, point, trade.symbol)

    def _valor_pip(self, symbol, volume, cuenta):
        """
        Wrapper para la función auxiliar valor_pip.
        """
        return valor_pip(symbol, volume)

    def _move_sl(self, trade, cuenta, sl_price):
        cuenta = self._ensure_account_dict(cuenta)
        if not cuenta:
            return
        client = self.mt5._client_for(cuenta)
        asyncio.create_task(self.mt5.modify_sl(cuenta, trade.ticket, sl_price, reason="BE-PNL", provider_tag=trade.provider_tag))

