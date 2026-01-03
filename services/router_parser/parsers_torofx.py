"""
ToroFX parser - detects ToroFX signal format
Also handles position management commands: "tomar parcial", "cierro mi entrada", etc.
"""

import re
from typing import Optional
from parsers_base import SignalParser, ParseResult


class ToroFxParser(SignalParser):
    format_tag = "TOROFX"
    
    # ToroFX signals mention specific brokers or FOREX pairs (not gold)
    SYMBOL_PATTERN = re.compile(r'\b(eur|gbp|usd|nzd|cad|jpy|aud|chf)\w*\b', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\bBUY\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\bSELL\b', re.IGNORECASE)
    
    # Entry: 1.2500-1.2510
    ENTRY_PATTERN = re.compile(
        r'entry[\s:]*(\d+\.\d{3,5})\s*[-–]\s*(\d+\.\d{3,5})',
        re.IGNORECASE
    )
    
    # SL: 1.2490
    SL_PATTERN = re.compile(r'sl[\s:]*(\d+\.\d{3,5})', re.IGNORECASE)
    
    # TP: 1.2550, 1.2600
    TP_PATTERN = re.compile(r'tp[\s:]*(\d+\.\d{3,5})', re.IGNORECASE)
    
    # Management commands
    PARTIAL_PATTERN = re.compile(r'\b(tomar\s*parcial|take\s*partial|partial\s*profit)\b', re.IGNORECASE)
    CLOSE_PATTERN = re.compile(r'\b(cierro|cerrar|close)\b', re.IGNORECASE)
    ENTRY_CLOSE_PATTERN = re.compile(
        r'(cierro\s*mi\s*entrada|cierre\s*entrada|close\s*entry)\s*(@|en|at)?\s*(\d+\.\d{3,5})?',
        re.IGNORECASE
    )
    
    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        
        # Check for management commands first (these are not entry signals)
        if self.PARTIAL_PATTERN.search(norm) or (self.CLOSE_PATTERN.search(norm) and not self.BUY_PATTERN.search(norm) and not self.SELL_PATTERN.search(norm)):
            # These should be handled as management messages, not entry signals
            return None
        
        # Must have a known currency pair
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
        
        # Extract symbol from text (e.g., EURUSD, GBPUSD)
        symbol_match = re.search(r'\b([A-Z]{3}[A-Z]{3})\b', norm.upper())
        symbol = symbol_match.group(1) if symbol_match else "EURUSD"
        
        direction = "BUY" if is_buy else "SELL"
        return ParseResult(
            format_tag=self.format_tag,
            provider_tag="TOROFX",
            symbol=symbol,
            direction=direction,
            entry_range=(entry_min, entry_max),
            sl=sl,
            tps=tps if tps else None,
        )
    
    def is_management_message(self, text: str) -> bool:
        """Check if message is a management command, not an entry signal"""
        norm = self.normalize(text)
        return bool(
            self.PARTIAL_PATTERN.search(norm) or 
            self.ENTRY_CLOSE_PATTERN.search(norm)
        )
    
    def extract_management_command(self, text: str) -> Optional[dict]:
        """Extract management command details"""
        norm = self.normalize(text)
        
        # Partial profit
        if self.PARTIAL_PATTERN.search(norm):
            pct_match = re.search(r'(\d+)%?', norm)
            percent = int(pct_match.group(1)) if pct_match else 30
            return {
                "type": "partial_profit",
                "percent": percent,
            }
        
        # Close entry
        entry_match = self.ENTRY_CLOSE_PATTERN.search(norm)
        if entry_match:
            price = None
            try:
                price = float(entry_match.group(3))
            except (ValueError, TypeError, IndexError):
                pass
            return {
                "type": "close_entry",
                "at_price": price,
            }
        
        return None
