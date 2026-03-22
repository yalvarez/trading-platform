"""
mt5_pool.py
Pool de conexiones MT5 — un cliente por cuenta, reutilizado en toda la vida del proceso.

Problema previo: MT5Client se creaba 5-8 veces por señal, cada vez ejecutando
mt5.initialize() que cuesta 50-100ms. Con 4 cuentas y una señal de oro, eso sumaba
200-800ms solo en inicializaciones.

Solucion: Singleton pool indexado por (host, port). El cliente se crea una sola vez
y se reutiliza. Si la conexion cae, se reconecta automaticamente.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("trade_orchestrator.mt5_pool")

# Cache de symbol_info por cliente: {(host, port, symbol): (info, timestamp)}
_SYMBOL_INFO_TTL = 2.0  # segundos — suficiente para XAUUSD que se mueve rapido


class MT5ClientPool:
    """
    Pool global de clientes MT5, uno por cuenta (host:port).
    Thread-safe para uso desde asyncio (el executor corre en threads).
    """

    _lock = threading.Lock()
    _clients: dict[tuple[str, int], "PooledMT5Client"] = {}
    _symbol_cache: dict[tuple[str, int, str], tuple] = {}  # (host, port, symbol) -> (info, ts)

    @classmethod
    def get(cls, host: str, port: int) -> "PooledMT5Client":
        """
        Devuelve el cliente para (host, port), creandolo si no existe.
        El cliente se inicializa una sola vez y se reutiliza en llamadas posteriores.
        """
        key = (host, port)
        with cls._lock:
            if key not in cls._clients:
                log.info("[MT5Pool] Creando cliente nuevo para %s:%s", host, port)
                cls._clients[key] = PooledMT5Client(host, port)
            return cls._clients[key]

    @classmethod
    def get_for_account(cls, account: dict) -> "PooledMT5Client":
        """Atajo para obtener cliente desde un dict de cuenta."""
        if "client" in account:
            return account["client"]
        host = account.get("host", "localhost")
        port = int(account.get("port", 18812))
        return cls.get(host, port)

    @classmethod
    def get_symbol_info(cls, host: str, port: int, symbol: str, client: "PooledMT5Client"):
        """
        Devuelve symbol_info cacheado. TTL de 2s — rapido para XAUUSD.
        Evita las 4 llamadas repetidas a symbol_info() por señal.
        """
        key = (host, port, symbol)
        now = time.monotonic()
        cached = cls._symbol_cache.get(key)
        if cached is not None:
            info, ts = cached
            if now - ts < _SYMBOL_INFO_TTL:
                return info
        # Cache miss o expirado — consultar MT5
        try:
            info = client.mt5.symbol_info(symbol)
            cls._symbol_cache[key] = (info, now)
            return info
        except Exception as e:
            log.warning("[MT5Pool] symbol_info falló para %s: %s", symbol, e)
            return None

    @classmethod
    def invalidate_symbol(cls, host: str, port: int, symbol: str) -> None:
        """Invalida la cache de un simbolo especifico (util tras errores)."""
        cls._symbol_cache.pop((host, port, symbol), None)

    @classmethod
    def close_all(cls) -> None:
        """Cierra todas las conexiones del pool (para shutdown limpio)."""
        with cls._lock:
            for client in cls._clients.values():
                try:
                    client.mt5.shutdown()
                except Exception:
                    pass
            cls._clients.clear()
            cls._symbol_cache.clear()
            log.info("[MT5Pool] Todas las conexiones cerradas.")


class PooledMT5Client:
    """
    Cliente MT5 con reconexion automatica.
    Wrappea MT5Client con logica de reconexion si la conexion cae.
    """

    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY = 0.5  # segundos

    def __init__(self, host: str, port: int):
        from .mt5_client import MT5Client
        self.host = host
        self.port = port
        self._client = MT5Client(host, port)
        self._lock = threading.Lock()
        log.info("[PooledMT5Client] Inicializado %s:%s", host, port)

    @property
    def mt5(self):
        return self._client.mt5

    def _reconnect(self) -> bool:
        """Intenta reconectar si la conexion cayo."""
        from .mt5_client import MT5Client
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            try:
                log.warning("[PooledMT5Client] Reconectando %s:%s (intento %d/%d)",
                            self.host, self.port, attempt, self.MAX_RECONNECT_ATTEMPTS)
                self._client = MT5Client(self.host, self.port)
                log.info("[PooledMT5Client] Reconectado %s:%s", self.host, self.port)
                return True
            except Exception as e:
                log.error("[PooledMT5Client] Fallo reconexion %s:%s: %s", self.host, self.port, e)
                if attempt < self.MAX_RECONNECT_ATTEMPTS:
                    time.sleep(self.RECONNECT_DELAY * attempt)
        return False

    def _call(self, method: str, *args, **kwargs):
        """Ejecuta un metodo del cliente, reconectando si es necesario."""
        with self._lock:
            try:
                return getattr(self._client, method)(*args, **kwargs)
            except Exception as e:
                log.warning("[PooledMT5Client] Error en %s.%s: %s — intentando reconexion", self.host, method, e)
                if self._reconnect():
                    try:
                        return getattr(self._client, method)(*args, **kwargs)
                    except Exception as e2:
                        log.error("[PooledMT5Client] Error tras reconexion en %s.%s: %s", self.host, method, e2)
                        raise
                raise

    # ---- API publica (misma interfaz que MT5Client) ----

    def tick_price(self, symbol: str, direction: str) -> float:
        return self._call("tick_price", symbol, direction)

    def symbol_info(self, symbol: str):
        # Usa cache del pool para evitar llamadas repetidas
        return MT5ClientPool.get_symbol_info(self.host, self.port, symbol, self)

    def symbol_info_tick(self, symbol: str):
        return self._call("symbol_info_tick", symbol)

    def symbol_select(self, symbol: str, enable: bool = True):
        return self._call("symbol_select", symbol, enable)

    def positions_get(self, *args, **kwargs):
        return self._call("positions_get", *args, **kwargs)

    def order_send(self, req: dict):
        return self._call("order_send", req)

    def partial_close(self, account: dict, ticket: int, percent: int) -> bool:
        return self._call("partial_close", account, ticket, percent)

    def get_pip_size(self, symbol: str) -> float:
        return self._call("get_pip_size", symbol)
