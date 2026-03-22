"""
TradePulse signal parser

Full signal format:
    ‼️SIGNAL ALERT‼️

    PAIR: XAUUSD
    ORDER TYPE: BUY
    ENTRY PRICE: 4999 -4992

    ❌STOP LOSS: 4982

    ✅TAKE PROFIT 1:5020
    ✅TAKE PROFIT 2:5040

    ‼️Follow Risk Management rules‼️

Fast signal format:
    XAUUSD BUY NOW
    XAUUSD SELL NOW
"""

import re
from typing import Optional
from parsers_base import SignalParser, ParseResult


class TradePulseParser(SignalParser):
    format_tag = "TRADEPULSE"
    provider_tag = "TRADE_PULSE"

    # Full signal anchor — very distinctive
    SIGNAL_ALERT = re.compile(r'SIGNAL\s+ALERT', re.IGNORECASE)

    # Full signal fields
    PAIR_RE = re.compile(r'PAIR\s*:\s*([A-Z0-9/]{3,10})', re.IGNORECASE)
    ORDER_TYPE_RE = re.compile(r'ORDER\s+TYPE\s*:\s*(BUY|SELL)', re.IGNORECASE)
    ENTRY_RE = re.compile(r'ENTRY\s+PRICE\s*:\s*([\d.]+)\s*[-–]\s*([\d.]+)', re.IGNORECASE)
    SL_RE = re.compile(r'STOP\s+LOSS\s*:\s*([\d.]+)', re.IGNORECASE)
    TP_RE = re.compile(r'TAKE\s+PROFIT\s*\d*\s*:\s*([\d.]+)', re.IGNORECASE)

    # Fast signal: "XAUUSD BUY NOW" / "XAUUSD SELL NOW" (full line, no SL/TP)
    FAST_RE = re.compile(
        r'(?:^|\n)\s*(XAUUSD|GOLD|XAU)\s+(BUY|SELL)\s+NOW\s*(?:\n|$)',
        re.IGNORECASE,
    )

    _SYMBOL_ALIASES = {"GOLD", "XAU", "XAUUSD"}

    def _normalize_symbol(self, raw: str) -> str:
        return "XAUUSD" if raw.upper() in self._SYMBOL_ALIASES else raw.upper()

    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)

        # --- Fast signal (check first — cheap) ---
        fast_m = self.FAST_RE.search(norm)
        if fast_m and not self.SL_RE.search(norm):
            return ParseResult(
                format_tag=self.format_tag,
                provider_tag=self.provider_tag,
                symbol=self._normalize_symbol(fast_m.group(1)),
                direction=fast_m.group(2).upper(),
                is_fast=True,
            )

        # --- Full signal ---
        if not self.SIGNAL_ALERT.search(norm):
            return None

        pair_m = self.PAIR_RE.search(norm)
        if not pair_m:
            return None
        symbol = self._normalize_symbol(pair_m.group(1))

        order_m = self.ORDER_TYPE_RE.search(norm)
        if not order_m:
            return None
        direction = order_m.group(1).upper()

        entry_range = None
        entry_m = self.ENTRY_RE.search(norm)
        if entry_m:
            a, b = float(entry_m.group(1)), float(entry_m.group(2))
            entry_range = (min(a, b), max(a, b))

        sl = None
        sl_m = self.SL_RE.search(norm)
        if sl_m:
            sl = float(sl_m.group(1))

        tps = [float(m.group(1)) for m in self.TP_RE.finditer(norm)] or None

        return ParseResult(
            format_tag=self.format_tag,
            provider_tag=self.provider_tag,
            symbol=symbol,
            direction=direction,
            entry_range=entry_range,
            sl=sl,
            tps=tps,
        )
