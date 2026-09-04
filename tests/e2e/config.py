"""
Environment configuration for the e2e test suite. All values come from the
same .env the rest of the platform uses (docker-compose env_file), plus a
few e2e-only vars for reaching mt5_acct1/trade_orchestrator by service name
inside the docker-compose network.
"""
import os
from dataclasses import dataclass


REQUIRED_VARS = (
    "REDIS_URL", "TG_TEST_CHAT_ID", "TG_API_ID", "TG_API_HASH", "TG_PHONE",
    "MT5_HOST", "MT5_PORT", "N8N_ACTION_API_KEY", "MGMT_API_PORT",
    "TRADE_ORCHESTRATOR_HOST",
)


@dataclass(frozen=True)
class E2EConfig:
    redis_url: str
    tg_test_chat_id: int
    tg_api_id: str
    tg_api_hash: str
    tg_phone: str
    mt5_host: str
    mt5_port: int
    n8n_action_api_key: str
    mgmt_api_port: int
    trade_orchestrator_host: str


def load_config() -> E2EConfig:
    missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(
            f"Missing required e2e env vars: {', '.join(missing)}"
        )
    return E2EConfig(
        redis_url=os.environ["REDIS_URL"],
        tg_test_chat_id=int(os.environ["TG_TEST_CHAT_ID"]),
        tg_api_id=os.environ["TG_API_ID"],
        tg_api_hash=os.environ["TG_API_HASH"],
        tg_phone=os.environ["TG_PHONE"],
        mt5_host=os.environ["MT5_HOST"],
        mt5_port=int(os.environ["MT5_PORT"]),
        n8n_action_api_key=os.environ["N8N_ACTION_API_KEY"],
        mgmt_api_port=int(os.environ["MGMT_API_PORT"]),
        trade_orchestrator_host=os.environ["TRADE_ORCHESTRATOR_HOST"],
    )
