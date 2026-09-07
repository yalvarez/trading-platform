"""
Shared types for every e2e scenario: the context each scenario receives,
the result it must return, and the emergency cleanup helper (spec section 6)
scenarios call from a `finally` block or the runner calls on unexpected
failure.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from tests.e2e.config import E2EConfig
from tests.e2e.price_reader import PriceReader
from tests.e2e.telegram_sender import TelegramSender
from tests.e2e.vps_observer import VpsObserver


class ScenarioOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE_TP1_NOT_REACHED = "inconclusive_tp1_not_reached"
    INCONCLUSIVE_ENTRY_RANGE_TIMEOUT = "inconclusive_entry_range_timeout"
    EXTERNAL_DEPENDENCY_FAILURE = "external_dependency_failure"


@dataclass
class ScenarioContext:
    cfg: E2EConfig
    price_reader: PriceReader
    sender: TelegramSender
    observer: VpsObserver


@dataclass
class ScenarioResult:
    name: str
    outcome: ScenarioOutcome
    evidence: dict = field(default_factory=dict)
    detail: str = ""


async def cleanup_group(
    ctx: ScenarioContext,
    symbol: str,
    close_fn: Optional[Callable] = None,
    preexisting_tickets: Optional[set] = None,
) -> None:
    """
    Best-effort emergency cleanup: closes any position still open for
    `symbol` that the scenario itself opened. `close_fn(ticket, volume)`
    defaults to a direct RPyC partial_close against ctx.observer's MT5
    connection; a scenario's unit test injects a fake to avoid touching a
    real MT5 connection. Never raises — a cleanup failure is logged, not
    propagated, so it never masks the scenario's own result.

    `preexisting_tickets`: tickets already open for `symbol` BEFORE the
    scenario sent anything (a snapshot taken at the very start of `run()`).
    This demo account is shared with real, unrelated live trading on this
    VPS — without this exclusion, cleanup would close ANY open position of
    the symbol, including a real position that has nothing to do with the
    test. Every ticket in this set is left untouched; only tickets NOT in
    it (i.e. genuinely new, opened by this scenario) are closed. Pass None
    (or omit) only when the scenario is certain it owns every position for
    `symbol` — e.g. a hermetic unit test — never in a real run.
    """
    import logging
    log = logging.getLogger("e2e.cleanup")
    preexisting_tickets = preexisting_tickets or set()
    try:
        positions = await ctx.observer.positions_for_symbol(symbol)
    except Exception as e:
        log.warning("cleanup_group: could not read positions for %s: %s", symbol, e)
        return
    positions = [p for p in positions if p["ticket"] not in preexisting_tickets]
    for pos in positions:
        try:
            if close_fn is not None:
                await close_fn(pos["ticket"], pos["volume"])
            else:
                from tests.e2e.mt5_client_factory import build_mt5_client

                client = build_mt5_client(ctx.observer.mt5_host, ctx.observer.mt5_port)
                account = {"host": ctx.observer.mt5_host, "port": ctx.observer.mt5_port}
                client.partial_close(account, pos["ticket"], 100)
        except Exception as e:
            log.warning("cleanup_group: failed to close ticket=%s: %s", pos["ticket"], e)
