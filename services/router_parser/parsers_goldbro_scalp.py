"""
Gold Brother SCALP parser - detects scalping signals with tight TP/SL
Format: ORO SCALP BUY Entry: 2500, SL: 2495, TP1: 2505 (70%), TP2: 2510 (100%)
"""

import re
from typing import Optional, List
from parsers_base import SignalParser, ParseResult


class GoldBroScalpParser(SignalParser):
    format_tag = "GB_SCALP"
    
    SYMBOL_PATTERN = re.compile(r'\b(oro|gold|xau)\b', re.IGNORECASE)
    SCALP_PATTERN = re.compile(r'\bSCALP\b', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\bBUY\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\bSELL\b', re.IGNORECASE)
    
    # Entry: 2500
    ENTRY_PATTERN = re.compile(r'entry[\s:]*(\d{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)
    
    # SL: 2495
    SL_PATTERN = re.compile(r'sl[\s:]*(\d{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)
    
    # TP1: 2505 (70%) or TP1: 2505
    TP_PATTERN = re.compile(
        r'tp[1-3]?[\s:]*(\d{3,5}(?:\.\d{1,2})?)\s*(?:\((\d+)%?\))?',
        re.IGNORECASE
    )
    
    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        
        # Must have symbol
        if not self.SYMBOL_PATTERN.search(norm):
            return None
        
        # Must have SCALP keyword
        if not self.SCALP_PATTERN.search(norm):
            return None
        
        # Must have direction
        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        if not (is_buy or is_sell):
            return None
        
        # Must have entry price
        entry_match = self.ENTRY_PATTERN.search(norm)
        if not entry_match:
            return None
        
        try:
            entry = float(entry_match.group(1))
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
        
        # Extract TPs with percentages
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
            provider_tag="GB_SCALP",
            symbol="XAUUSD",
            direction=direction,
            entry_range=(entry, entry),  # Single entry for scalp
            sl=sl,
            tps=tps if tps else None,
        )
