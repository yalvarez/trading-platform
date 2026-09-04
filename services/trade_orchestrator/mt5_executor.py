
from __future__ import annotations
from dataclasses import dataclass
import asyncio
import logging

from services.common.timewindow import parse_windows

log = logging.getLogger("trade_orchestrator.mt5_executor")

from .trade_utils import safe_comment
from .notifications.n8n import N8nNotifierAdapter


@dataclass
class MT5OpenResult:
    tickets_by_account: dict[str, int]
    errors_by_account: dict[str, str]


class MT5Executor:
    """
    Reducido tras la reescritura dual-TP (ver docs/superpowers/plans/
    2026-09-03-tradepulse-only-simplification.md, seccion final-whole-branch-review).
    TradeManager solo usa `_client_for(account)` y `.accounts` de esta clase —
    toda su antigua logica de apertura/gestion de ordenes (open_complete_trade,
    open_for_accounts, modify_sl, el gate de entry-range) fue reemplazada por
    TradeManager.open_group/_wait_for_entry_range/_force_runner_sl, que hablan
    MT5 directo via order_send. El codigo removido llamaba a metodos de
    TradeManager (register_trade, update_trade_signal) que ya no existen en
    la API dual-TP y por lo tanto estaba muerto Y roto, no solo sin uso.
    """

    def _notify_bg(self, account_name, message):
        try:
            if hasattr(self, 'notifier') and self.notifier:
                asyncio.create_task(self.notifier.notify(account_name, message))
        except Exception as e:
            log.error(f"[NOTIFY][ERROR] {account_name}: {e}")

    def _safe_comment(self, tag: str) -> str:
        """Wrapper para safe_comment centralizado."""
        return safe_comment(tag, getattr(self, 'comment_prefix', 'TM'))

    def _client_for(self, account):
        """
        Devuelve el cliente MT5 para la cuenta dada.
        Usa el pool global — un solo cliente por (host, port) reutilizado en toda
        la vida del proceso. Elimina los 50-100ms de MT5.initialize() por llamada.
        """
        from .mt5_pool import MT5ClientPool
        return MT5ClientPool.get_for_account(account)

    def __init__(
        self,
        accounts: list[dict],
        *,
        default_deviation: int = 50,
        magic: int = 987654,
        comment_prefix: str = "YsaCopyNew",
        notifier=None,
        trading_windows: str = "03:00-12:00,08:00-17:00",
        entry_wait_seconds: int = 60,
        entry_poll_ms: int = 500,
        entry_buffer_points: float = 0.0,
        config_provider=None,
    ):
        self.accounts = accounts
        self.default_deviation = default_deviation
        self.magic = magic
        self.comment_prefix = comment_prefix
        self.notifier = notifier
        self.windows = parse_windows(trading_windows)
        self.entry_buffer_points = entry_buffer_points
        self.entry_wait_seconds = entry_wait_seconds
        self.entry_poll_ms = entry_poll_ms
        self.config_provider = config_provider
