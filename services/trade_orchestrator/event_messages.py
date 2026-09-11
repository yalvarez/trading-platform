"""
event_messages.py
Construye el texto legible en espanol (`message`) de cada evento
user-facing, listo para reenviar tal cual a Telegram via n8n. Toda la
redaccion vive aqui, separada de la logica de negocio de trade_manager.py,
para poder ajustar el tono/formato sin tocar la logica de gestion.
"""
from typing import Optional


def _fmt_price(value: Optional[float]) -> str:
    return f"{value:.5f}" if value is not None else "N/D"


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "N/D"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}"


def _fmt_direction(direction: Optional[str]) -> str:
    return direction.upper() if direction is not None else "N/D"


def _fmt_leg_results(leg_results: list) -> str:
    return ", ".join(
        f"{lr['leg']} ({lr['close_volume']} lots @ {_fmt_price(lr['close_price'])}, {_fmt_money(lr['pnl_money'])})"
        for lr in leg_results
    )


def build_group_opened_message(*, channel_name, group_id, symbol, direction, entry_price, sl, tp1, tp2, volume) -> str:
    return (
        f"\U0001F7E2 APERTURA — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"Entrada: {_fmt_price(entry_price)}\n"
        f"SL: {_fmt_price(sl)} | TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)}\n"
        f"Volumen: {volume} lots"
    )


def build_tp1_hit_message(*, channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money, account_currency) -> str:
    return (
        f"✅ TP1 ALCANZADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"Cerrado: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)} {account_currency}\n"
        f"SL movido a break-even"
    )


def build_tp1_hit_be_failed_message(*, channel_name, group_id, symbol, direction, runner_ticket) -> str:
    """
    TP1 se alcanzo pero el runner NO pudo moverse a breakeven tras 3 intentos:
    queda vivo con su SL ORIGINAL, es decir MAS expuesto que en cualquier otro
    evento del catalogo (en todos los demas el riesgo baja o la posicion se
    cierra). Por eso el tono es de alerta explicita y pide revision manual —
    nadie mas va a reintentar el BE por su cuenta.
    """
    return (
        f"\U0001F6A8 ATENCION — TP1 alcanzado pero BE NO aplicado — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"El runner (ticket {runner_ticket}) NO pudo moverse a breakeven tras 3 intentos.\n"
        f"Sigue abierto con su SL ORIGINAL — el riesgo NO se redujo.\n"
        f"Requiere revision manual inmediata."
    )


def build_tp2_partial_closed_message(*, channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money, remaining_volume) -> str:
    return (
        f"✅ TP2 ALCANZADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"Cerrado 50%: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)}\n"
        f"Runner sigue abierto con trailing ({remaining_volume} lots restantes)"
    )


def build_sl_hit_message(*, channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money) -> str:
    return (
        f"\U0001F534 STOP LOSS — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)}\n"
        f"Cerrado: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)}"
    )


def build_external_close_message(*, channel_name, group_id, symbol, direction, leg, close_price, close_volume, pnl_money) -> str:
    return (
        f"\U0001F6A8 CIERRE EXTERNO DETECTADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {_fmt_direction(direction)} ({leg})\n"
        f"Cerrado por fuera del sistema: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)}\n"
        f"Revisar la cuenta — este cierre no fue TP, SL ni una orden via Telegram."
    )


def build_close_now_message(*, channel_name, group_id, raw_text, leg_results, total_pnl_money) -> str:
    legs_text = _fmt_leg_results(leg_results)
    return (
        f"⚠️ CIERRE MANUAL — Canal: {channel_name} (grupo {group_id})\n"
        f"Motivo: \"{raw_text}\"\n"
        f"{legs_text}\n"
        f"Total: {_fmt_money(total_pnl_money)}"
    )


def build_close_partial_now_message(*, channel_name, group_id, raw_text, percent_requested, leg_results) -> str:
    legs_text = _fmt_leg_results(leg_results)
    return (
        f"⚠️ CIERRE PARCIAL MANUAL ({percent_requested:.0f}%) — Canal: {channel_name} (grupo {group_id})\n"
        f"Motivo: \"{raw_text}\"\n"
        f"{legs_text}"
    )


def build_move_sl_be_applied_message(*, channel_name, group_id, new_sl, raw_text) -> str:
    return (
        f"\U0001F6E1️ BREAKEVEN — Canal: {channel_name} (grupo {group_id})\n"
        f"SL movido a breakeven manualmente: {_fmt_price(new_sl)}\n"
        f"Motivo: \"{raw_text}\""
    )


def build_partial_failure_message(*, channel_name, group_id, leg_summaries) -> str:
    legs_text = ", ".join(leg_summaries) if leg_summaries else "ninguna pierna confirmada"
    return (
        f"⚠️ CIERRE INCOMPLETO — Canal: {channel_name} (grupo {group_id})\n"
        f"Al menos una pierna fue rechazada por el broker. Piernas: {legs_text}\n"
        f"Revisar manualmente — puede quedar una posicion abierta."
    )
