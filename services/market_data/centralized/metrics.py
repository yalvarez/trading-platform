# Unificación de métricas y reporting centralizado
from prometheus_client import Counter, Gauge

TRADES_OPENED = Counter('trades_opened_total', 'Total trades opened', ['account', 'symbol'])
TRADES_FAILED = Counter('trades_failed_total', 'Total trades failed', ['account', 'symbol'])
TP_HITS = Counter('trade_tp_hits_total', 'TP hits', ['account', 'symbol', 'tp'])
PARTIAL_CLOSES = Counter('trade_partial_closes_total', 'Partial closes', ['account', 'symbol'])
ACTIVE_TRADES = Gauge('active_trades', 'Active trades', ['account', 'symbol'])
TRAILING_ACTIVATED = Counter('trailing_activated_total', 'Trailing activations', ['account', 'symbol'])
BE_ACTIVATED = Counter('be_activated_total', 'Break-even activations', ['account', 'symbol'])

# Estos contadores serán actualizados tanto por market_data (al decidir) como por trade_orchestrator (al ejecutar)
