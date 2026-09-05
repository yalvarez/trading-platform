"""
conftest.py for tests/ — stubs out Docker-only dependencies (mt5linux, psycopg2)
so that tests that DON'T use them can still be collected, and tests that DO are
skipped gracefully when the modules are absent.
"""
import sys
import types
from unittest.mock import MagicMock


def _stub_module(name: str, **attrs):
    """Create a lightweight stub module and register it in sys.modules."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ── mt5linux ─────────────────────────────────────────────────────────────────
class _StubConstants:
    # Mirrors mt5linux.constants.Constants' account trade-mode values (real
    # MT5 API constants) — enough for tests/e2e/preflight.py's demo-account
    # check to import Constants without needing the real Docker-only package.
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2


if "mt5linux" not in sys.modules:
    _stub_module(
        "mt5linux",
        MetaTrader5=MagicMock(),
        Constants=_StubConstants,
    )

# ── psycopg2 ─────────────────────────────────────────────────────────────────
if "psycopg2" not in sys.modules:
    psycopg2_stub = _stub_module("psycopg2")
    psycopg2_stub.connect = MagicMock(side_effect=RuntimeError("psycopg2 not available in local env"))
    psycopg2_stub.OperationalError = Exception
    psycopg2_stub.extras = _stub_module("psycopg2.extras")
    psycopg2_stub.extensions = _stub_module("psycopg2.extensions")
