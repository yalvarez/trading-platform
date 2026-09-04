#!/bin/sh
# fix-mt5-linux-version.sh
#
# The gmag11/metatrader5_vnc:2.3 image installs the latest mt5linux release
# on both the Linux side (the socket server clients connect to) and the
# Wine/Windows side (where MT5 itself and the RPyC client-facing service
# run) without pinning a version. Two related problems observed:
#
# 1. mt5linux>=1.1.0 uses f-string syntax (PEP 701, nested braces/multiline
#    expressions inside f-strings) that is invalid on Python < 3.12 -- and
#    this image ships Python 3.11, so the mt5linux server fails at startup:
#
#      SyntaxError: unterminated string literal (detected at line 1755)
#      (in mt5linux/metatrader5.py, inside copy_rates_from)
#
# 2. mt5linux>=1.1.0 pulls in rpyc>=6.0, which is not wire-protocol
#    compatible with the rpyc version our clients use (trade_orchestrator/
#    trade_api, via mt5linux==0.1.9). Fixing mt5linux on only ONE side
#    (e.g. just the Linux server) leaves rpyc mismatched between client and
#    server, which breaks the RPyC handshake with confusing symptoms that
#    look unrelated to a version problem:
#
#      ValueError: not enough values to unpack (expected 3, got 0)
#        (in rpyc/core/protocol.py, brine.load)
#      TimeoutError: result expired
#        (in rpyc/core/async_.py, AsyncResultTimeout)
#
# The image's own start.sh never reinstalls mt5linux once any version is
# detected as present (on either side), so once this script pins both
# mt5linux==0.1.9 and rpyc==5.0.1 in BOTH places inside the mt5_acct1_config
# persistent volume, the fix survives normal container restarts. Re-run
# this script only if that volume is ever recreated from scratch (e.g.
# after `docker compose down -v`).
#
# Usage: ./scripts/fix-mt5-linux-version.sh [container_name]
# Default container_name: atp-mt5-acct1

set -e

CONTAINER="${1:-atp-mt5-acct1}"

echo "Checking mt5linux/rpyc versions inside $CONTAINER (Linux side)..."
docker exec "$CONTAINER" pip show mt5linux rpyc 2>/dev/null | grep -i "^name\|^version" || {
    echo "mt5linux/rpyc not found or container not running — is $CONTAINER up?"
    exit 1
}

echo "Pinning mt5linux==0.1.9 and rpyc==5.0.1 on the Linux side..."
docker exec "$CONTAINER" pip install --break-system-packages --no-cache-dir --no-deps "mt5linux==0.1.9"
docker exec "$CONTAINER" pip install --break-system-packages --no-cache-dir "rpyc==5.0.1"

echo "Pinning mt5linux==0.1.9 (and its rpyc dependency) on the Wine/Windows side..."
docker exec -u abc "$CONTAINER" wine python -m pip install --no-cache-dir "mt5linux==0.1.9"

echo "Restarting $CONTAINER to pick up the pinned versions..."
docker restart "$CONTAINER"

echo "Done. Check health with: docker ps --filter name=$CONTAINER --format '{{.Status}}'"
echo "Give it a minute or two to fully settle (Wine/MT5 startup + broker login) before"
echo "restarting any dependent service (trade_orchestrator, trade_api) that connects to it."
