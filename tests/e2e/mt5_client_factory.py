"""
Constructs an MT5Client (services/common/mt5_client.MT5Client) for the e2e
suite to reuse — this IS the correct way to talk to the mt5_acct1 RPyC
server (a classic-mode RPyC server via mt5linux.MetaTrader5), unlike a raw
rpyc.connect(host, port).root.* call, which does not work against this
server at all (see final-review ledger entry, 2026-09-04-e2e-test-suite).
"""
from services.common.mt5_client import MT5Client


def build_mt5_client(host: str, port: int) -> MT5Client:
    return MT5Client(host=host, port=port)
