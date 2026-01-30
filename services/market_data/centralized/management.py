"""
management.py: Lógica centralizada de gestión de trades (BE, trailing, cierre parcial, etc.)
Produce comandos al bus centralizado según reglas de gestión y eventos recibidos.
"""
import asyncio
import logging
from .bus import TradeBus
from .schema import TRADE_COMMANDS_STREAM, TRADE_EVENTS_STREAM

log = logging.getLogger("centralized.management")


from ..common.config import Settings
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class AccountTradeSettings:
    # All advanced management settings per account
    enable_breakeven: bool = True
    breakeven_after_tp_hit: int = 1
    breakeven_offset_pips: float = 3.0
    enable_trailing: bool = True
    trailing_activation_pips: float = 30.0
    trailing_stop_pips: float = 15.0
    trailing_min_change_pips: float = 1.0
    trailing_cooldown_sec: float = 2.0
    enable_addon: bool = True
    addon_max_count: int = 2
    addon_lot_factor: float = 0.5
    addon_entry_delay_sec: int = 5
    enable_scaling: bool = True
    scale_down_percent: float = 50.0
    scale_down_profit_pips: float = 100.0
    # Add more as needed

class CentralizedTradeManager:
    def __init__(self, bus: TradeBus):
        self.bus = bus
        self.settings = Settings.load()
        # Map account name to settings
        self.account_settings: Dict[str, AccountTradeSettings] = self._load_account_settings()

    def _load_account_settings(self) -> Dict[str, AccountTradeSettings]:
        """Load per-account advanced management settings from config/accounts_json."""
        accounts = self.settings.accounts()
        mapping = {}
        for acc in accounts:
            # Merge global defaults with per-account overrides
            mapping[acc["name"]] = AccountTradeSettings(
                enable_breakeven=acc.get("enable_breakeven", self.settings.enable_advanced_trade_mgmt),
                breakeven_after_tp_hit=acc.get("breakeven_after_tp_hit", 1),
                breakeven_offset_pips=acc.get("breakeven_offset_pips", 3.0),
                enable_trailing=acc.get("enable_trailing", self.settings.enable_trailing),
                trailing_activation_pips=acc.get("trailing_activation_pips", self.settings.trailing_activation_pips),
                trailing_stop_pips=acc.get("trailing_stop_pips", self.settings.trailing_stop_pips),
                trailing_min_change_pips=acc.get("trailing_min_change_pips", 1.0),
                trailing_cooldown_sec=acc.get("trailing_cooldown_sec", 2.0),
                enable_addon=acc.get("enable_addon", self.settings.enable_addon),
                addon_max_count=acc.get("addon_max_count", self.settings.addon_max_count),
                addon_lot_factor=acc.get("addon_lot_factor", self.settings.addon_lot_factor),
                addon_entry_delay_sec=acc.get("addon_entry_delay_sec", 5),
                enable_scaling=acc.get("enable_scaling", True),
                scale_down_percent=acc.get("scale_down_percent", 50.0),
                scale_down_profit_pips=acc.get("scale_down_profit_pips", 100.0),
            )
        return mapping

    async def handle_event(self, event: dict):
        """
        Procesa eventos de ejecución y decide acciones de gestión avanzadas:
        - break-even
        - trailing stop
        - cierre parcial
        - scaling
        - addon
        """
        account = event.get("account")
        if not account or account not in self.account_settings:
            log.warning(f"[MGMT] Cuenta no encontrada en settings: {account}")
            return
        acc_settings = self.account_settings[account]

        # --- Break-even logic ---
        if self._should_apply_breakeven(event, acc_settings):
            await self._apply_breakeven(event, acc_settings)

        # --- Trailing stop logic ---
        if self._should_apply_trailing(event, acc_settings):
            await self._apply_trailing(event, acc_settings)

        # --- Partial close logic ---
        if self._should_apply_partial_close(event, acc_settings):
            await self._apply_partial_close(event, acc_settings)

        # --- Scaling logic ---
        if self._should_apply_scaling(event, acc_settings):
            await self._apply_scaling(event, acc_settings)

        # --- Addon logic ---
        if self._should_apply_addon(event, acc_settings):
            await self._apply_addon(event, acc_settings)

    # --- Advanced management logic stubs ---
    def _should_apply_breakeven(self, event: dict, acc_settings: AccountTradeSettings) -> bool:
        return (
            event["type"] == "tp_hit"
            and acc_settings.enable_breakeven
            and event.get("tp_index", 0) >= acc_settings.breakeven_after_tp_hit
        )

    async def _apply_breakeven(self, event: dict, acc_settings: AccountTradeSettings):
        log.info(f"[BE] Activando break-even para {event['account']} ticket={event['ticket']}")
        cmd = {
            "signal_id": event["signal_id"],
            "type": "be",
            "symbol": event["symbol"],
            "accounts": [event["account"]],
            "ticket": event["ticket"],
            "be": {"enabled": True, "offset": acc_settings.breakeven_offset_pips},
            "timestamp": event["timestamp"]
        }
        await self.bus.publish_command(cmd)

    def _should_apply_trailing(self, event: dict, acc_settings: AccountTradeSettings) -> bool:
        return event["type"] == "trailing_activated" and acc_settings.enable_trailing

    async def _apply_trailing(self, event: dict, acc_settings: AccountTradeSettings):
        log.info(f"[TRAILING] Reforzando trailing para {event['account']} ticket={event['ticket']}")
        cmd = {
            "signal_id": event["signal_id"],
            "type": "trailing",
            "symbol": event["symbol"],
            "accounts": [event["account"]],
            "ticket": event["ticket"],
            "trailing": {
                "enabled": True,
                "distance": acc_settings.trailing_stop_pips,
                "activation": acc_settings.trailing_activation_pips
            },
            "timestamp": event["timestamp"]
        }
        await self.bus.publish_command(cmd)

    def _should_apply_partial_close(self, event: dict, acc_settings: AccountTradeSettings) -> bool:
        # Placeholder: implement real condition for partial close
        return event["type"] == "partial_close" and acc_settings.enable_scaling

    async def _apply_partial_close(self, event: dict, acc_settings: AccountTradeSettings):
        log.info(f"[PARTIAL] Ejecutando cierre parcial para {event['account']} ticket={event['ticket']}")
        cmd = {
            "signal_id": event["signal_id"],
            "type": "partial_close",
            "symbol": event["symbol"],
            "accounts": [event["account"]],
            "ticket": event["ticket"],
            "partial": {
                "percent": acc_settings.scale_down_percent
            },
            "timestamp": event["timestamp"]
        }
        await self.bus.publish_command(cmd)

    def _should_apply_scaling(self, event: dict, acc_settings: AccountTradeSettings) -> bool:
        # Placeholder: implement real condition for scaling
        return event["type"] == "scaling" and acc_settings.enable_scaling

    async def _apply_scaling(self, event: dict, acc_settings: AccountTradeSettings):
        log.info(f"[SCALING] Ejecutando scaling para {event['account']} ticket={event['ticket']}")
        cmd = {
            "signal_id": event["signal_id"],
            "type": "scaling",
            "symbol": event["symbol"],
            "accounts": [event["account"]],
            "ticket": event["ticket"],
            "scaling": {
                "percent": acc_settings.scale_down_percent,
                "profit_pips": acc_settings.scale_down_profit_pips
            },
            "timestamp": event["timestamp"]
        }
        await self.bus.publish_command(cmd)

    def _should_apply_addon(self, event: dict, acc_settings: AccountTradeSettings) -> bool:
        # Placeholder: implement real condition for addon
        return event["type"] == "addon" and acc_settings.enable_addon

    async def _apply_addon(self, event: dict, acc_settings: AccountTradeSettings):
        log.info(f"[ADDON] Ejecutando addon para {event['account']} ticket={event['ticket']}")
        cmd = {
            "signal_id": event["signal_id"],
            "type": "addon",
            "symbol": event["symbol"],
            "accounts": [event["account"]],
            "ticket": event["ticket"],
            "addon": {
                "max_count": acc_settings.addon_max_count,
                "lot_factor": acc_settings.addon_lot_factor,
                "delay_sec": acc_settings.addon_entry_delay_sec
            },
            "timestamp": event["timestamp"]
        }
        await self.bus.publish_command(cmd)

    async def run(self):
        """
        Loop principal: escucha eventos y decide acciones de gestión.
        """
        await self.bus.connect()
        async for msg_id, event in self.bus.listen_events():
            await self.handle_event(event)
