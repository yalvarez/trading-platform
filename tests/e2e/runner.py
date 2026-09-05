"""
CLI entrypoint for the e2e suite (spec section 3.1/3.2): wires config,
pre-flight, and scenarios together, and prints a report classifying each
result as PASS / FAIL / INCONCLUSIVE_* / EXTERNAL_DEPENDENCY_FAILURE
(spec section 7). Run via:
  docker compose --profile e2e run --rm e2e_runner --scenario a1_fast_only
  docker compose --profile e2e run --rm e2e_runner --all
"""
import argparse
import asyncio
import json
import sys

import httpx

from tests.e2e.config import load_config, E2EConfig
from tests.e2e.preflight import run_preflight
from tests.e2e.price_reader import PriceReader
from tests.e2e.telegram_sender import TelegramSender
from tests.e2e.vps_observer import VpsObserver
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult

from tests.e2e.scenarios import (
    a1_fast_only, a2_fast_then_full_early, a3_fast_then_full_late, a4_full_only,
    b1_be_variant1, b2_be_variant2, b3_be_variant3, b4_forced_close,
    b5_signal_correction, b6_milestone_noop, b7_sl_hit_note, b8_spam_noop,
    c1_dedup, c1b_reopen_after_cooldown, c2_unrecognized_to_n8n, c3_entry_range_dash_variants,
    d1_restart_reconciliation,
)

SCENARIOS = {
    "a1_fast_only": a1_fast_only.run,
    "a2_fast_then_full_early": a2_fast_then_full_early.run,
    "a3_fast_then_full_late": a3_fast_then_full_late.run,
    "a4_full_only": a4_full_only.run,
    "b1_be_variant1": b1_be_variant1.run,
    "b2_be_variant2": b2_be_variant2.run,
    "b3_be_variant3": b3_be_variant3.run,
    "b4_forced_close": b4_forced_close.run,
    "b5_signal_correction": b5_signal_correction.run,
    "b6_milestone_noop": b6_milestone_noop.run,
    "b7_sl_hit_note": b7_sl_hit_note.run,
    "b8_spam_noop": b8_spam_noop.run,
    "c1_dedup": c1_dedup.run,
    "c1b_reopen_after_cooldown": c1b_reopen_after_cooldown.run,
    "c2_unrecognized_to_n8n": c2_unrecognized_to_n8n.run,
    "c3_entry_range_dash_variants": c3_entry_range_dash_variants.run,
    "d1_restart_reconciliation": d1_restart_reconciliation.run,
}

# Standing limitation of run_preflight (see its module docstring): it cannot
# verify that the operator's n8n/Ollama instance's inbound webhook and
# /mgmt/action callback actually target THIS VPS rather than production.
# Surfaced here so an operator who only ever sees printed preflight output
# (not preflight.py's docstring) still learns about it.
PREFLIGHT_WEBHOOK_NOTE = (
    "Note: pre-flight cannot verify that your n8n/Ollama instance's inbound "
    "webhook and /mgmt/action callback actually point at THIS VPS (not "
    "production) — confirm that manually; see "
    "docs/superpowers/specs/2026-09-04-e2e-test-suite-design.md section 4."
)


async def run_scenario(name: str, ctx: ScenarioContext) -> ScenarioResult:
    return await SCENARIOS[name](ctx)


def format_report(results: list) -> str:
    lines = ["=== e2e suite report ==="]
    for outcome in (ScenarioOutcome.PASS, ScenarioOutcome.FAIL,
                    ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                    ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                    ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE):
        matching = [r for r in results if r.outcome == outcome]
        if not matching:
            continue
        label = "INCONCLUSIVE" if "INCONCLUSIVE" in outcome.value.upper() else outcome.value.upper()
        lines.append(f"\n{label}:")
        for r in matching:
            lines.append(f"  - {r.name}: {r.detail}")
    return "\n".join(lines)


def _print_preflight_report(preflight) -> None:
    """
    Prints the pre-flight report to the operator. Always prints
    PREFLIGHT_WEBHOOK_NOTE — a standing limitation of the check itself, not
    a symptom of a specific failure — regardless of whether pre-flight
    passed or failed.
    """
    if not preflight.ok:
        print("Pre-flight checks failed:")
        for p in preflight.problems:
            print(f"  - {p}")
    else:
        print("Pre-flight checks passed.")
    print(PREFLIGHT_WEBHOOK_NOTE)


async def _build_context(cfg: E2EConfig) -> ScenarioContext:
    import redis.asyncio as redis_asyncio
    redis_client = redis_asyncio.from_url(cfg.redis_url, decode_responses=True)
    return ScenarioContext(
        cfg=cfg,
        price_reader=PriceReader(host=cfg.mt5_host, port=cfg.mt5_port),
        sender=TelegramSender(api_id=cfg.tg_api_id, api_hash=cfg.tg_api_hash, phone=cfg.tg_phone),
        observer=VpsObserver(redis_client=redis_client, mt5_host=cfg.mt5_host, mt5_port=cfg.mt5_port),
    )


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the trading-platform e2e test suite against a live VPS.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", choices=sorted(SCENARIOS.keys()), help="run a single scenario")
    group.add_argument("--all", action="store_true", help="run every scenario in serial")
    args = parser.parse_args(argv)

    cfg = load_config()

    accounts_json = []  # loaded from ACCOUNTS_JSON env the same way services/common/config.py does
    import os
    accounts_json = json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

    async with httpx.AsyncClient() as http_client:
        preflight = await run_preflight(cfg, accounts_json, http_client)
        _print_preflight_report(preflight)
        if not preflight.ok:
            return 2

    ctx = await _build_context(cfg)
    names = [args.scenario] if args.scenario else sorted(SCENARIOS.keys())

    results = []
    try:
        for name in names:
            print(f"--- running {name} ---")
            result = await run_scenario(name, ctx)
            results.append(result)
            print(f"{result.outcome.value}: {result.detail}")
    finally:
        await ctx.sender.close()

    print(format_report(results))

    if any(r.outcome == ScenarioOutcome.FAIL for r in results):
        return 1
    if any(r.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE for r in results):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
