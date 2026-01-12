from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import asyncio, time, re

from common.timewindow import parse_windows, in_windows
import logging

log = logging.getLogger("trade_orchestrator.mt5_executor")
from mt5_client import MT5Client

@dataclass
class MT5OpenResult:
    tickets_by_account: dict[str, int]
    errors_by_account: dict[str, str]

class MT5Executor:
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
        self.entry_wait_seconds = int(entry_wait_seconds)
        self.entry_poll_ms = int(entry_poll_ms)
        self.entry_buffer_points = float(entry_buffer_points)

        self._clients: dict[str, MT5Client] = {}

    def _notify_bg(self, account_name: str, message: str):
        if not self.notifier:
            return
        try:
            asyncio.create_task(self.notifier(account_name, message))
        except RuntimeError:
            print(f"[NOTIFY][NO_LOOP] {account_name}: {message}")

    def _safe_comment(self, tag: str) -> str:
        base = f"{self.comment_prefix}-{tag}"
        base = re.sub(r"[^A-Za-z0-9\-_.]", "", base)
        return base[:31]

    def _client_for(self, account: dict) -> MT5Client:
        key = account["name"]
        if key not in self._clients:
            self._clients[key] = MT5Client(account["host"], int(account["port"]))
        return self._clients[key]

    def _should_operate_now(self) -> bool:
        return in_windows(self.windows)

    async def wait_price_in_range(self, client: MT5Client, symbol: str, direction: str, lo: float, hi: float) -> float:
        deadline = time.time() + self.entry_wait_seconds
        buffer = self.entry_buffer_points
        while time.time() <= deadline:
            px = client.tick_price(symbol, direction)
            if px > 0 and (lo - buffer) <= px <= (hi + buffer):
                return px
            await asyncio.sleep(self.entry_poll_ms / 1000.0)
        return 0.0


    async def open_complete_trade(
        self,
        *,
        provider_tag: str,
        symbol: str,
        direction: str,
        entry_range: Optional[Tuple[float, float]],
        sl: float,
        tps: list[float],
    ) -> MT5OpenResult:
        tickets: dict[str, int] = {}
        errors: dict[str, str] = {}

        log.info("open_complete_trade start provider=%s symbol=%s direction=%s entry=%s sl=%s tps=%s", provider_tag, symbol, direction, str(entry_range), str(sl), str(tps))


        # --- Forzar SL por defecto si no viene ---
        async def get_forced_sl(client, symbol, direction, price):
            info = client.symbol_info(symbol)
            # Ajustar default_sl_pips según el instrumento
            if symbol.upper().startswith("XAU"):  # Oro
                default_sl_pips = 150.0
            else:
                default_sl_pips = 60.0
            point = float(getattr(info, "point", 0.01)) if info else 0.01
            # Validar que el point sea razonable para oro
            if symbol.upper().startswith("XAU") and point < 0.1:
                point = 0.1
            sl_distance = default_sl_pips * point
            if direction == "BUY":
                return price - sl_distance
            else:
                return price + sl_distance

        if not self._should_operate_now():
            reason = "Outside trading windows (London/NY)."
            for a in [x for x in self.accounts if x.get("active")]:
                errors[a["name"]] = reason
            return MT5OpenResult(tickets_by_account=tickets, errors_by_account=errors)


        async def send_order(account):
            name = account["name"]
            try:
                client = self._client_for(account)
                # Ensure symbol is selected before any info/price fetch
                client.symbol_select(symbol, True)
                symbol_info = client.symbol_info(symbol)
                if not symbol_info:
                    log.warning(f"[SYMBOL] No symbol_info for {symbol} ({name}) after select. Symbol may not be available in MT5.")
                price = client.tick_price(symbol, direction)
                if price == 0.0:
                    log.warning(f"[PRICE] Price is 0.0 for {symbol} ({name}) - symbol may not be available, not selected, or market is closed.")
                order_type = 0 if direction == "BUY" else 1

                # --- Forzar SL si es necesario ---
                forced_sl = sl
                if not forced_sl or float(forced_sl) == 0.0:
                    forced_sl = await get_forced_sl(client, symbol, direction, price)
                    log.warning(f"[SL-FORCED] SL forzado para {name}: {forced_sl}")

                # --- LOTE DINÁMICO O FIJO ---
                lot = 0.01
                fixed_lot = float(account.get("fixed_lot", 0))
                risk_percent = float(account.get("risk_percent", 0))
                balance = 0.0
                if fixed_lot > 0:
                    lot = fixed_lot
                elif risk_percent > 0 and forced_sl and float(forced_sl) > 0:
                    # Obtener balance actual
                    try:
                        acc_info = client.mt5.account_info()
                        if acc_info and hasattr(acc_info, "balance"):
                            balance = float(acc_info.balance)
                    except Exception as e:
                        log.warning(f"[LOTE] No se pudo obtener balance para {name}: {e}")
                    # Calcular riesgo monetario
                    risk_money = balance * (risk_percent / 100.0)
                    # Calcular distancia SL en precio
                    sl_distance = abs(float(price) - float(forced_sl))
                    # Obtener info de símbolo
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
                # --- Selección dinámica y fallback de filling mode ---
                supported_filling_modes = []
                try:
                    if symbol_info is not None and hasattr(symbol_info, 'filling_mode'):
                        supported_filling_modes.append(symbol_info.filling_mode)
                    if not supported_filling_modes:
                        supported_filling_modes = [1, 3, 2]  # fallback: IOC, FOK, RETURN
                except Exception as e:
                    log.warning(f"[FILLING] No se pudo obtener filling mode para {name}: {e}")
                    supported_filling_modes = [2, 1, 3]
                log.info(f"[FILLING] {symbol} ({name}) filling fallback orden: {supported_filling_modes}")

                # Probar cada filling mode hasta que uno funcione
                res = None
                for type_filling in supported_filling_modes:
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
                        "type_filling": type_filling,
                    }
                    import asyncio
                    loop = asyncio.get_running_loop()
                    res = await loop.run_in_executor(None, client.order_send, req)
                    log.warning("order_send response acct=%s (filling=%s): %s", name, type_filling, res)
                    if res and getattr(res, "retcode", None) == 10009:
                        tickets[name] = int(getattr(res, "order", 0))
                        log.info("open_complete_trade success acct=%s ticket=%s (filling=%s)", name, tickets[name], type_filling)
                        break
                    elif res and getattr(res, "retcode", None) not in [10030, 10013]:
                        # Si el error no es de filling mode, no seguir probando
                        errors[name] = f"order_send failed retcode={getattr(res,'retcode',None)}"
                        log.warning("open_complete_trade failed acct=%s retcode=%s (filling=%s)", name, getattr(res,'retcode',None), type_filling)
                        break
                else:
                    # Si ninguno funcionó
                    errors[name] = f"order_send failed retcode={getattr(res,'retcode',None)}"
                    log.warning("open_complete_trade failed acct=%s retcode=%s (all fillings)", name, getattr(res,'retcode',None))
            except Exception as e:
                errors[name] = f"Exception: {e}"
                log.error(f"[EXCEPTION] open_complete_trade failed acct={name}: {e}")

        accounts = [a for a in self.accounts if a.get("active")]
        await asyncio.gather(*(send_order(account) for account in accounts), return_exceptions=True)
        return MT5OpenResult(tickets_by_account=tickets, errors_by_account=errors)
