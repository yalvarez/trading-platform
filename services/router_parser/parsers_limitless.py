import re
from typing import Optional
from .parsers_base import SignalParser, ParseResult
import logging

log = logging.getLogger("router_parser")

class LimitlessParser(SignalParser):
    format_tag = "LIMITLESS"

    # Accept GOLD, XAUUSD, ORO, and any symbol like BTCUSD, ETHUSD, etc.
    SYMBOL_PATTERN = re.compile(r'\b([A-Z]{3,6}USD|GOLD|XAUUSD|XAU|ORO)\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\bSELL\b', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\bBUY\b', re.IGNORECASE)
    ZONE_PATTERN = re.compile(r'Zone[:\s]*([\d.]+)\s*[-–]\s*([\d.]+)', re.IGNORECASE)
    TP_PATTERN = re.compile(r'TP\s*\d*[:]?\s*([\d.]+)', re.IGNORECASE)
    SL_PATTERN = re.compile(r'Risk Price[:\s]*([\d.]+)', re.IGNORECASE)

    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        log.debug("[LIMITLESS_PARSE] norm=%r", norm[:400])

        symbol_match = self.SYMBOL_PATTERN.search(norm)
        if not symbol_match:
            return None
        symbol_raw = symbol_match.group(1).upper()
        # Normalizar GOLD, ORO, XAU, XAU/USD a XAUUSD
        if symbol_raw in ["GOLD", "ORO", "XAU", "XAU/USD", "XAUUSD"]:
            symbol = "XAUUSD"
        else:
            symbol = symbol_raw

        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        if not (is_buy or is_sell):
            return None
        direction = "BUY" if is_buy else "SELL"

        zone_match = self.ZONE_PATTERN.search(norm)
        if not zone_match:
            return None
        entry_min = float(zone_match.group(1))
        entry_max = float(zone_match.group(2))

        sl = None
        sl_match = self.SL_PATTERN.search(norm)
        if sl_match:
            try:
                sl = float(sl_match.group(1))
            except (ValueError, IndexError):
                pass

        tps = []
        for tp_match in self.TP_PATTERN.finditer(norm):
            try:
                tp = float(tp_match.group(1))
                if tp not in tps:
                    tps.append(tp)
            except (ValueError, IndexError):
                pass

        return ParseResult(
            format_tag=self.format_tag,
            provider_tag="LIMITLESS",
            symbol=symbol,
            direction=direction,
            entry_range=(entry_min, entry_max),
            sl=sl,
            tps=tps if tps else None,
        )
