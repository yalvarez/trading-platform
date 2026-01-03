"""
Gold Brother LONG parser - detects longer-term trade signals
Format: ORO/GOLD BUY/SELL Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530
"""

import re
from typing import Optional, Tuple, List
from parsers_base import SignalParser, ParseResult


class GoldBroLongParser(SignalParser):
    format_tag = "GB_LONG"
    
    SYMBOL_PATTERN = re.compile(r'\b(oro|gold|xau)\b', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\bBUY\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\bSELL\b', re.IGNORECASE)
    
    # Entry: 2500-2505 or Entry 2500-2505
    ENTRY_PATTERN = re.compile(
        r'entry[\s:]*(\d{3,5}(?:\.\d{1,2})?)\s*[-–]\s*(\d{3,5}(?:\.\d{1,2})?)',
        re.IGNORECASE
    )
    
    # SL: 2490
    SL_PATTERN = re.compile(r'sl[\s:]*(\d{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)
    
    # TP1/TP2/TP3: 2515, 2530, etc
    TP_PATTERN = re.compile(r'tp[1-3]?[\s:]*(\d{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)
    
    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        
        # Must have symbol
        if not self.SYMBOL_PATTERN.search(norm):
            return None
        
        # Must have direction
        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        if not (is_buy or is_sell):
            return None
        
        # Must have entry range
        entry_match = self.ENTRY_PATTERN.search(norm)
        if not entry_match:
            return None
        
        try:
            entry_min = float(entry_match.group(1))
            entry_max = float(entry_match.group(2))
        except (ValueError, IndexError):
            return None
        
        # Extract SL
        sl = None
        sl_match = self.SL_PATTERN.search(norm)
        if sl_match:
            try:
                sl = float(sl_match.group(1))
            except (ValueError, IndexError):
                pass
        
        # Extract TPs
        tps = []
        for tp_match in self.TP_PATTERN.finditer(norm):
            try:
                tp = float(tp_match.group(1))
                if tp not in tps:
                    tps.append(tp)
            except (ValueError, IndexError):
                pass
        
        direction = "BUY" if is_buy else "SELL"
        return ParseResult(
            format_tag=self.format_tag,
            provider_tag="GB_LONG",
            symbol="XAUUSD",
            direction=direction,
            entry_range=(entry_min, entry_max),
            sl=sl,
            tps=tps if tps else None,
        )
