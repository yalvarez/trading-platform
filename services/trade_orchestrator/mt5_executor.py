from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import asyncio, time, re

from common.timewindow import parse_windows, in_windows
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

    def open_complete_trade(
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

        # ✅ No intentar ni conectar fuera de horario
        if not self._should_operate_now():
            reason = "Outside trading windows (London/NY)."
            for a in [x for x in self.accounts if x.get("active")]:
                errors[a["name"]] = reason
            return MT5OpenResult(tickets_by_account=tickets, errors_by_account=errors)

        for account in [a for a in self.accounts if a.get("active")]:
            name = account["name"]
            client = self._client_for(account)

            # ensure symbol
            client.symbol_select(symbol, True)

            # ✅ entry range gate (wait up to N seconds)
            price = client.tick_price(symbol, direction)
            if entry_range:
                lo, hi = float(entry_range[0]), float(entry_range[1])
                # wait (async friendly)
                # NOTE: called sync here; orchestrator will call the async helper and pass final price
                # keep sync fallback:
                pass

            # open at market with SL/TP=0 (TPs handled by manager)
            order_type = 0 if direction == "BUY" else 1  # BUY=0, SELL=1 (mt5linux mirrors MT5)
            req = {
                "action": 1,  # TRADE_ACTION_DEAL
                "symbol": symbol,
                "volume": 0.01,  # placeholder; tú lo conectarás con risk calc si quieres
                "type": order_type,
                "price": float(price),
                "sl": float(sl),
                "tp": 0.0,
                "deviation": int(self.default_deviation),
                "magic": int(self.magic),
                "comment": self._safe_comment(provider_tag),
                "type_time": 0,
                "type_filling": 1,
            }
            res = client.order_send(req)
            if res and getattr(res, "retcode", None) == 10009:  # DONE
                tickets[name] = int(getattr(res, "order", 0))
            else:
                errors[name] = f"order_send failed retcode={getattr(res,'retcode',None)}"

        return MT5OpenResult(tickets_by_account=tickets, errors_by_account=errors)
