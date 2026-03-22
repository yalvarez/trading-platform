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
if "mt5linux" not in sys.modules:
    _stub_module(
        "mt5linux",
        MetaTrader5=MagicMock(),
    )

# ── psycopg2 ─────────────────────────────────────────────────────────────────
if "psycopg2" not in sys.modules:
    psycopg2_stub = _stub_module("psycopg2")
    psycopg2_stub.connect = MagicMock(side_effect=RuntimeError("psycopg2 not available in local env"))
    psycopg2_stub.OperationalError = Exception
    psycopg2_stub.extras = _stub_module("psycopg2.extras")
    psycopg2_stub.extensions = _stub_module("psycopg2.extensions")
