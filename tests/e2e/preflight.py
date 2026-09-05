"""
Pre-flight checks the e2e runner performs before executing scenarios
(spec section 7): confirm the test channel is actually reachable by the
pipeline, and that trade_orchestrator's mgmt API (n8n's callback target)
is up. This cannot confirm n8n's own webhook configuration points at this
VPS — that stays a documented operator precondition (spec section 4).
"""
from dataclasses import dataclass, field

from tests.e2e.config import E2EConfig


@dataclass
class PreflightResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


def _build_allowed_channels(accounts_json: list[dict]) -> set[str]:
    allowed: set[str] = set()
    for acct in accounts_json:
        for ch in acct.get("allowed_channels") or []:
            allowed.add(str(ch))
    return allowed


async def run_preflight(cfg: E2EConfig, accounts_json: list[dict], http_client) -> PreflightResult:
    problems: list[str] = []

    allowed = _build_allowed_channels(accounts_json)
    if allowed and str(cfg.tg_test_chat_id) not in allowed:
        problems.append(
            f"TG_TEST_CHAT_ID={cfg.tg_test_chat_id} is not in any account's "
            f"allowed_channels ({sorted(allowed)}) — telegram_ingestor will "
            f"silently drop test messages. Add it to ACCOUNTS_JSON."
        )

    try:
        resp = await http_client.get(
            f"http://{cfg.trade_orchestrator_host}:{cfg.mgmt_api_port}/health"
        )
        if resp.status_code != 200:
            problems.append(
                f"trade_orchestrator /health returned {resp.status_code}, expected 200"
            )
    except Exception as e:
        problems.append(
            f"trade_orchestrator unreachable at {cfg.trade_orchestrator_host}:{cfg.mgmt_api_port}: {e}"
        )

    return PreflightResult(ok=len(problems) == 0, problems=problems)
