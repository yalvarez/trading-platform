
"""
trade_advanced.py - Funciones avanzadas para la gestión de trades.

Incluye:
- Partial take profits: gestión de cierres parciales en diferentes niveles de TP.
- Breakeven automation: automatización del movimiento de SL a BE.
- Trailing stops: trailing dinámico del SL.
- Addon entries: entradas adicionales en niveles definidos.
- Position scaling: escalado de posición según beneficio.
"""

import time
from dataclasses import dataclass, field
from typing import Optional
import logging

log = logging.getLogger("trade_advanced")


@dataclass
class AdvancedTradeSettings:
    """Settings for advanced trade management strategies"""
    
    # Partial Take Profit Configuration
    tp_partial_levels: list[dict] = field(default_factory=lambda: [
        {"tp_price": None, "close_percent": 70},  # Close 70% at TP1
        {"tp_price": None, "close_percent": 100},  # Close remaining at TP2
    ])
    
    # Breakeven Settings
    enable_breakeven: bool = True
    breakeven_after_tp_hit: int = 1  # Move to BE after 1st TP hits
    breakeven_offset_pips: float = 3.0  # How far above entry to set BE
    
    # Trailing Stop Settings
    enable_trailing: bool = True
    trailing_activation_pips: float = 30.0  # Activate trail after X pips profit
    trailing_stop_pips: float = 15.0  # Trail by X pips
    trailing_min_change_pips: float = 1.0  # Min change to update trail
    trailing_cooldown_sec: float = 2.0  # Cooldown between updates
    
    # Addon Entry Settings
    enable_addon: bool = True
    addon_max_count: int = 2  # Max addon entries per trade
    addon_entry_levels: list[float] = field(default_factory=list)  # Price levels for addons
    addon_lot_factor: float = 0.5  # Addon lot = original_lot * factor
    addon_entry_delay_sec: int = 5  # Min seconds from entry before addon
    
    # Runner Strategy (trailing for scalps)
    enable_runner: bool = True
    runner_activation_pips: float = 50.0  # Activate after X pips
    runner_retrace_pips: float = 25.0  # Trail with X pips retrace
    runner_min_profit_pips: float = 20.0  # Min profit before activating
    
    # Position Scaling
    enable_scaling: bool = True
    scale_down_percent: float = 50.0  # Close X% of position
    scale_down_profit_pips: float = 100.0  # After this much profit


@dataclass
class PartialCloseRecord:
    """Record of a partial close action"""
    timestamp: float = field(default_factory=time.time)
    tp_index: int = 0
    close_percent: float = 100.0
    closed_volume: float = 0.0
    close_price: float = 0.0


class AdvancedTradeManager:
    """
    Gestiona las funciones avanzadas de trade para optimizar beneficios.
    Trabaja junto a MT5Executor para la gestión de posiciones.
    Métodos principales:
    - should_close_partial: Determina si debe hacerse un cierre parcial en un TP.
    - calculate_close_volume: Calcula el volumen a cerrar en un parcial.
    - should_activate_trailing: Determina si debe activarse el trailing stop.
    - should_update_trailing: Verifica si ha pasado suficiente tiempo para actualizar el trailing.
    - calculate_trailing_sl: Calcula el nuevo SL para trailing.
    - should_move_to_breakeven: Determina si debe moverse el SL a BE.
    - calculate_breakeven_price: Calcula el precio de BE con offset.
    - suggest_addon_prices: Sugiere precios para addons entre entry y SL.
    - calculate_addon_lot: Calcula el lotaje para addons.
    - record_partial_close: Registra un cierre parcial.
    - update_trailing_timestamp: Actualiza el timestamp del trailing.
    - cleanup_ticket: Limpia datos de seguimiento de un ticket cerrado.
    """
    
    def __init__(self, settings: Optional[AdvancedTradeSettings] = None):
        self.settings = settings or AdvancedTradeSettings()
        self.partial_closes: dict[int, list[PartialCloseRecord]] = {}  # ticket -> [closes]
        self.trailing_last_update: dict[int, float] = {}  # ticket -> timestamp
    
    def should_close_partial(
        self,
        ticket: int,
        tp_index: int,
        current_price: float,
        tp_prices: list[float]
    ) -> bool:
        """
        Determine if a partial close should occur at current TP.
        
        Args:
            ticket: Trade ticket
            tp_index: Which TP was hit (0-based)
            current_price: Current market price
            tp_prices: List of TP prices for this trade
        
        Returns:
            True if should partially close
        """
        if not self.settings.enable_addon or tp_index >= len(self.settings.tp_partial_levels):
            return False
        
        # Check if already closed at this level
        closes = self.partial_closes.get(ticket, [])
        if any(c.tp_index == tp_index for c in closes):
            return False
        
        return True
    
    def calculate_close_volume(
        self,
        current_volume: float,
        tp_index: int,
        total_tps: int,
        step: float = 0.0,
        min_vol: float = 0.0
    ) -> float:
        """
        Calcula el volumen a cerrar en un parcial usando la función centralizada.
        """
        from .trade_utils import calcular_volumen_parcial
        if not self.settings.tp_partial_levels or tp_index >= len(self.settings.tp_partial_levels):
            return current_volume
        level = self.settings.tp_partial_levels[tp_index]
        close_percent = level.get("close_percent", 100.0)
        return calcular_volumen_parcial(current_volume, close_percent, step, min_vol)
    
    def should_activate_trailing(
        self,
        current_profit_pips: float
    ) -> bool:
        """Check if trailing stop should be activated"""
        if not self.settings.enable_trailing:
            return False
        
        return current_profit_pips >= self.settings.trailing_activation_pips
    
    def should_update_trailing(
        self,
        ticket: int,
        time_now: float
    ) -> bool:
        """Check if enough time passed since last trailing update"""
        last = self.trailing_last_update.get(ticket, 0)
        return (time_now - last) >= self.settings.trailing_cooldown_sec
    
    def calculate_trailing_sl(
        self,
        peak_price: float,
        direction: str
    ) -> float:
        """
        Calculate new trailing stop loss.
        
        Args:
            peak_price: Highest/lowest price reached
            direction: BUY or SELL
        
        Returns:
            New SL price
        """
        pips = self.settings.trailing_stop_pips * 0.01  # Convert to decimal
        
        if direction == "BUY":
            return peak_price - pips
        else:
            return peak_price + pips
    
    def should_move_to_breakeven(
        self,
        tps_hit: set[int],
        tp_count: int,
    ) -> bool:
        """Check if should move to breakeven"""
        if not self.settings.enable_breakeven:
            return False
        
        # Activate after hitting first TP
        return len(tps_hit) >= self.settings.breakeven_after_tp_hit
    
    def calculate_breakeven_price(
        self,
        entry_price: float,
        direction: str,
        point: float,
        symbol: str
    ) -> float:
        """
        Calcula el precio de break-even (BE) usando la función centralizada.
        """
        from .trade_utils import calcular_be_price
        return calcular_be_price(
            entry_price,
            direction,
            self.settings.breakeven_offset_pips,
            point,
            symbol
        )
    
    def suggest_addon_prices(
        self,
        entry_price: float,
        sl_price: float,
        direction: str,
        addon_count: int = 1
    ) -> list[float]:
        """
        Calculate addon entry prices between entry and SL.
        
        Args:
            entry_price: Original entry price
            sl_price: Stop loss price
            direction: BUY or SELL
            addon_count: Number of addon levels to generate
        
        Returns:
            List of suggested addon prices
        """
        if addon_count <= 0:
            return []
        
        prices = []
        distance = abs(entry_price - sl_price)
        step = distance / (addon_count + 1)  # Divide into equal parts
        
        if direction == "BUY":
            if entry_price is None:
                log.error(f"[ADVANCED][ERROR] No se pudo obtener entry_price para addons BUY. Abortando cálculo de precios.")
                return []
            for i in range(1, addon_count + 1):
                price = entry_price - (step * i)
                prices.append(price)
        else:
            if entry_price is None:
                log.error(f"[ADVANCED][ERROR] No se pudo obtener entry_price para addons SELL. Abortando cálculo de precios.")
                return []
            for i in range(1, addon_count + 1):
                price = entry_price + (step * i)
                prices.append(price)
        return prices
    
    def calculate_addon_lot(
        self,
        original_lot: float
    ) -> float:
        """Calculate addon position size"""
        return original_lot * self.settings.addon_lot_factor
    
    def record_partial_close(
        self,
        ticket: int,
        tp_index: int,
        close_percent: float,
        closed_volume: float,
        close_price: float
    ):
        """Record a partial close action"""
        if ticket not in self.partial_closes:
            self.partial_closes[ticket] = []
        
        record = PartialCloseRecord(
            tp_index=tp_index,
            close_percent=close_percent,
            closed_volume=closed_volume,
            close_price=close_price,
        )
        self.partial_closes[ticket].append(record)
        log.info(f"[PARTIAL] Ticket {ticket}: Closed {closed_volume} at {close_price} (TP{tp_index+1})")
    
    def update_trailing_timestamp(self, ticket: int):
        """
        Actualiza el timestamp del último update de trailing stop para el ticket dado.
        """
        self.trailing_last_update[ticket] = time.time()
    
    def cleanup_ticket(self, ticket: int):
        """
        Limpia los datos de seguimiento (cierres parciales y trailing) para un ticket cerrado.
        """
        self.partial_closes.pop(ticket, None)
        self.trailing_last_update.pop(ticket, None)
