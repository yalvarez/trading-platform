"""
Hannah signal parser - detects signals from Hannah provider
Format example:
GOLD BUY NOW
@4460-4457
SL 4454
TP1 4463
TP2 4466
"""

import re
from typing import Optional
from parsers_base import SignalParser, ParseResult

class HannahParser(SignalParser):
    format_tag = "HANNAH"
    provider_tag = "hannah"

    SYMBOL_PATTERN = re.compile(r'\b(GOLD|XAUUSD|ORO)\b', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\b(BUY|LONG|COMPRA)\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\b(SELL|SHORT|VENDE|VENTA)\b', re.IGNORECASE)
    ENTRY_PATTERN = re.compile(r'@([\d.]+)-(\d+)', re.IGNORECASE)
    SL_PATTERN = re.compile(r'SL\s*(\d+)', re.IGNORECASE)
    TP_PATTERN = re.compile(r'TP\d*\s*(\d+)', re.IGNORECASE)

    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        lines = [l.strip() for l in norm.splitlines() if l.strip()]
        if not lines:
            return None
        # Hannah signals must start with 'BUY NOW' or 'SELL NOW'
        first_line = lines[0].upper()
        if not (first_line.startswith('GOLD BUY NOW') or first_line.startswith('GOLD SELL NOW')):
            return None
        # Symbol
        symbol = None
        for l in lines:
            m = self.SYMBOL_PATTERN.search(l)
            if m:
                symbol = "XAUUSD"
                break
        if not symbol:
            return None
        # Direction
        direction = None
        if 'BUY' in first_line:
            direction = "BUY"
        elif 'SELL' in first_line:
            direction = "SELL"
        if not direction:
            return None
        # Entry range
        entry_min, entry_max = None, None
        for l in lines:
            m = self.ENTRY_PATTERN.search(l)
            if m:
                entry_max = float(m.group(1))
                entry_min = float(m.group(2))
                break
        if entry_min is None or entry_max is None:
            return None
        # SL
        sl = None
        for l in lines:
            m = self.SL_PATTERN.search(l)
            if m:
                sl = float(m.group(1))
                break
        # TPs
        tps = []
        for l in lines:
            for m in self.TP_PATTERN.finditer(l):
                tps.append(float(m.group(1)))
        if not tps:
            tps = None
        return ParseResult(
            format_tag=self.format_tag,
            provider_tag=self.provider_tag,
            symbol=symbol,
            direction=direction,
            entry_range=(min(entry_min, entry_max), max(entry_min, entry_max)),
            sl=sl,
            tps=tps
        )
