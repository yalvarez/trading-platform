"""
ToroFX parser - detects ToroFX signal format
Also handles position management commands: "tomar parcial", "cierro mi entrada", etc.
"""

import re
from typing import Optional
from .parsers_base import SignalParser, ParseResult
import logging

log = logging.getLogger("router_parser")


class ToroFxParser(SignalParser):
    format_tag = "TOROFX"
    
    # ToroFX signals mention currency pairs and sometimes crypto (BTC/ETH)
    # Soporta símbolos con barra (XAU/USD, BTC/USD, etc) y sin barra
    SYMBOL_PATTERN = re.compile(r'([A-Z]{3,5}/[A-Z]{3,5}|[A-Z]{6,7}|eur|gbp|usd|nzd|cad|jpy|aud|chf|btc|eth)', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\bBUY\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\bSELL\b', re.IGNORECASE)
    
    # Entry: 1.2500-1.2510 or integers like 90000-90200
    ENTRY_PATTERN = re.compile(
        r'entry[\s:]*(\d+(?:\.\d{1,5})?)\s*[-–]\s*(\d+(?:\.\d{1,5})?)',
        re.IGNORECASE
    )
    
    # SL: 1.2490 o Stop Loss: 1.2490
    SL_PATTERN = re.compile(r'(?:sl|stop\s*loss)[\s:]*(\d+(?:\.\d{1,5})?)', re.IGNORECASE)
    
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
        log.debug("[TOROFX_PARSE] norm=%r", norm[:400])
        
        # Check for management commands first (these are not entry signals)
        if self.PARTIAL_PATTERN.search(norm) or (self.CLOSE_PATTERN.search(norm) and not self.BUY_PATTERN.search(norm) and not self.SELL_PATTERN.search(norm)):
            log.debug("[TOROFX_PARSE] management command detected")
            return None
        
        # Must have a known currency pair
        has_symbol = bool(self.SYMBOL_PATTERN.search(norm))
        log.debug("[TOROFX_PARSE] has_symbol=%s", has_symbol)
        if not has_symbol:
            return None
        
        # Must have direction
        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        if not (is_buy or is_sell):
            return None
        
        # Must have entry range (allow integer ranges or decimal ranges)
        entry_match = self.ENTRY_PATTERN.search(norm)
        if not entry_match:
            alt = re.search(r'@?(\d+(?:\.\d{1,5})?)\s*[-–]\s*(\d+(?:\.\d{1,5})?)', norm)
            if alt:
                entry_match = alt
        # fallback: single entry price like 'Entry Price: 90187' or 'Entry: 1.2500'
        single_entry = None
        if not entry_match:
            se = re.search(r'entry(?:\s*price)?[\s:\-]*@?(\d+(?:\.\d{1,5})?)', norm, re.IGNORECASE)
            if se:
                single_entry = float(se.group(1))
                entry_match = se

        log.debug("[TOROFX_PARSE] entry_match=%s single_entry=%s", bool(entry_match), single_entry)
        if not entry_match:
            return None
        
        try:
            # if single entry, group(2) may be None; treat as range with same value
            entry_min = float(entry_match.group(1))
            entry_max = float(entry_match.group(2)) if entry_match.lastindex and entry_match.lastindex >= 2 and entry_match.group(2) else entry_min
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
        
        # ToroFx nunca debe devolver TP, aunque los encuentre en el texto
        tps = None
        

        # Extraer símbolo: si hay 'BUY MARKET XXX' o 'SELL MARKET XXX', tomar la palabra después de MARKET
        market_symbol = re.search(r'(?:BUY|SELL)\s+MARKET\s+([A-Z]{3,10})', norm.upper())
        if market_symbol:
            symbol = market_symbol.group(1)
        else:
            symbol_match = re.search(r'([A-Z]{3,5}/[A-Z]{3,5}|[A-Z]{6,7})', norm.upper())
            symbol = symbol_match.group(1).replace("/", "") if symbol_match else None
        if not symbol:
            symbol = "NO-SYMBOL"
        
        direction = "BUY" if is_buy else "SELL"
        # Si Target: open, no hay TP
        if re.search(r'target\s*:\s*open', norm, re.IGNORECASE):
            tps = []

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
