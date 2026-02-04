"""
trade_utils.py - Funciones auxiliares y comunes para la gestión de trades.

Centraliza lógica repetida y utilidades para mantener el código mantenible y documentado.

Funciones principales:
- pips_to_price: Conversión de pips a precio según el símbolo (oro o FX).
- safe_comment: Genera comentarios seguros para órdenes, truncados y sin caracteres especiales.
- valor_pip: Estima el valor de un pip para un símbolo y volumen dados.
- calcular_sl_por_pnl: Calcula el precio de SL que permite perder solo lo ganado en una parcial.
"""
import re
import os
from typing import Optional

def calcular_lotaje(balance: float, risk_money: float, sl_distance: float, tick_value: float, tick_size: float, lot_step: float, min_lot: float, fixed_lot: float = 0.0) -> float:
    """
    Calcula el lotaje a usar para una operación, usando lotaje fijo si se especifica, o dinámico según riesgo.
    Args:
        balance: Balance de la cuenta
        risk_money: Dinero a arriesgar
        sl_distance: Distancia al SL en precio
        tick_value: Valor del tick
        tick_size: Tamaño del tick
        lot_step: Paso mínimo de lotaje
        min_lot: Lotaje mínimo permitido
        fixed_lot: Lotaje fijo (si > 0)
    Returns:
        Lotaje calculado (ajustado)
    """
    if fixed_lot > 0:
        return fixed_lot
    if sl_distance <= 0 or tick_value <= 0 or tick_size <= 0:
        return min_lot
    lot = risk_money / (sl_distance * (tick_value / tick_size))
    lot = max(min_lot, round(lot / lot_step) * lot_step)
    return lot

def calcular_volumen_parcial(current_volume: float, close_percent: float, step: float = 0.0, min_vol: float = 0.0) -> float:
    """
    Calcula el volumen a cerrar en un cierre parcial, ajustando al múltiplo de step y respetando el mínimo.
    Args:
        current_volume: Volumen actual de la posición
        close_percent: Porcentaje a cerrar (0-100)
        step: Paso mínimo de volumen (opcional)
        min_vol: Volumen mínimo permitido (opcional)
    Returns:
        Volumen a cerrar (ajustado)
    """
    raw_close = current_volume * (close_percent / 100.0)
    close_vol = raw_close
    if step > 0:
        close_vol = step * int(raw_close / step)
    if min_vol > 0 and close_vol < min_vol:
        if current_volume > min_vol:
            close_vol = min_vol
        else:
            close_vol = current_volume
    if close_vol > current_volume:
        close_vol = current_volume
    return close_vol

def calcular_trailing_retroceso(peak: float, current: float, point: float, is_buy: bool) -> float:
    """
    Calcula el retroceso en pips desde el peak para trailing stop.
    Args:
        peak: Precio máximo/min alcanzado
        current: Precio actual
        point: Valor de un punto para el símbolo
        is_buy: True si es BUY, False si es SELL
    Returns:
        Retroceso en pips (float)
    """
    if is_buy:
        return (peak - current) / 0.01
    else:
        return (current - peak) / 0.1

def calcular_be_price(entry_price: float, direction: str, be_offset_pips: float, point: float, symbol: str) -> float:
    """
    Calcula el precio de break-even (BE) con offset, centralizando la lógica para BUY/SELL y oro/FX.
    Args:
        entry_price: Precio de entrada de la posición
        direction: Dirección de la operación ('BUY' o 'SELL')
        be_offset_pips: Offset en pips para el BE (ej: 3.0)
        point: Valor de un punto para el símbolo
        symbol: Símbolo del instrumento
    Returns:
        Precio de BE recomendado según lógica centralizada
    """
    offset = pips_to_price(symbol, be_offset_pips, point)
    if direction.upper() == "BUY":
        return round(entry_price + offset, 2 if symbol.upper().startswith("XAU") else 5)
    else:
        return round(entry_price - offset, 2 if symbol.upper().startswith("XAU") else 5)

def pips_to_price(symbol: str, pips: float, point: float) -> float:
    """
    Convierte pips a precio para cualquier símbolo.
    - Para XAUUSD (o símbolos que empiezan con XAU), 1 pip = 0.1 dólares.
    - Para otros, usa el point del símbolo (típico en FX).
    Args:
        symbol: Símbolo del instrumento (ej: 'XAUUSD', 'EURUSD')
        pips: Cantidad de pips a convertir
        point: Valor de un punto para el símbolo
    Returns:
        Precio equivalente a los pips dados
    """
    if symbol.upper().startswith("XAU"):
        return round(pips * 0.1, 2)
    return round(pips * point, 5)

def safe_comment(tag: str, comment_prefix: str = "TM") -> str:
    """
    Genera un comentario seguro para órdenes, truncado a 31 caracteres y sin caracteres especiales.
    Args:
        tag: Etiqueta o texto base para el comentario
        comment_prefix: Prefijo identificador (por defecto 'TM')
    Returns:
        Comentario seguro para MetaTrader/orden
    """
    base = f"{comment_prefix}-{tag}"
    base = re.sub(r"[^A-Za-z0-9\-_.]", "", base)
    return base[:31]

def valor_pip(symbol: str, volume: float) -> float:
    """
    Estima el valor de un pip para el símbolo y volumen dados.
    Args:
        symbol: Símbolo del instrumento
        volume: Volumen de la posición (en lotes)
    Returns:
        Valor monetario de un pip para ese símbolo y volumen
    Nota: Para XAUUSD, 1 pip = $1 por lote. Para FX, 1 pip = $0.1 por lote (ajustar según broker si es necesario).
    """
    if symbol.upper().startswith("XAU"):
        return 1.0 * volume  # 1 pip = $1 por lote en oro
    else:
        return 0.1 * volume  # Ejemplo para FX, ajustar según broker

def calcular_sl_por_pnl(entry: float, direction: str, pnl_ganado: float, volume: float, point: float, symbol: str) -> float:
    """
    Calcula el precio de SL que permite perder solo lo ganado en una parcial.
    Args:
        entry: Precio de entrada de la posición
        direction: Dirección de la operación ('BUY' o 'SELL')
        pnl_ganado: Ganancia acumulada en la parcial
        volume: Volumen de la posición
        point: Valor de un punto para el símbolo
        symbol: Símbolo del instrumento
    Returns:
        Precio de SL que, si se ejecuta, deja la ganancia neta en cero
    """
    v_pip = valor_pip(symbol, volume)
    pips_equivalentes = abs(pnl_ganado / v_pip) if v_pip else 0
    if direction.upper() == "BUY":
        sl_price = entry + (pips_equivalentes * point)
    else:
        sl_price = entry - (pips_equivalentes * point)
    return round(sl_price, 5)

def calcular_sl_default(symbol: str, direction: str, price: float, point: float, default_sl_pips: float) -> float:
    """
    Calcula el precio de SL por defecto para una operación, centralizando la lógica de BUY/SELL y oro/FX.
    Args:
        symbol: Símbolo del instrumento (ej: 'XAUUSD', 'EURUSD')
        direction: Dirección de la operación ('BUY' o 'SELL')
        price: Precio de entrada actual
        point: Valor de un punto para el símbolo
        default_sl_pips: SL por defecto en pips (según config o entorno)
    Returns:
        Precio de SL recomendado según lógica centralizada
    """
    if symbol.upper().startswith('XAU'):
        sl_offset = default_sl_pips * (point if point else 0.1)
        if direction.upper() == 'BUY':
            return round(price - sl_offset, 2)
        else:
            return round(price + sl_offset, 2)
    else:
        sl_offset = default_sl_pips * (point if point else 0.00001)
        if direction.upper() == 'BUY':
            return round(price - sl_offset, 5)
        else:
            return round(price + sl_offset, 5)