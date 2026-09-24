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

import asyncio
import logging
import os
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
            # Via el lock, como el resto de llamadas: antes iba directo a
            # client.mt5, en concurrencia con cualquier otra llamada en curso.
            info = client._call("symbol_info", symbol)
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


class MT5ConnectionStuckError(asyncio.TimeoutError):
    """
    La conexion MT5 de la cuenta esta atascada (una llamada anterior colgada
    retiene el lock) y no se pudo, o no se debia, reemplazar. Hereda de
    asyncio.TimeoutError a proposito: todo el manejo de timeouts que ya existe
    en TradeManager (notificaciones open_failed, tp1_hit_be_timeout,
    mgmt_*_failure...) la trata igual que una llamada colgada, que es lo que es.
    """


class _Connection:
    """Una conexion RPyC a la terminal + el lock que serializa su uso."""

    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()


class PooledMT5Client:
    """
    Cliente MT5 con reconexion automatica.
    Wrappea MT5Client con logica de reconexion si la conexion cae.

    Recuperacion de lock atascado (incidente real 2026-09-23: una llamada RPyC
    colgada retuvo el lock y TODAS las llamadas siguientes de la cuenta fallaron
    por timeout durante hasta 42 min -- trailing, BE tras TP1, aperturas y
    cierres detenidos). TradeManager._call deja de ESPERAR a los
    MT5_CALL_TIMEOUT_SECONDS, pero no puede matar el hilo, que sigue con el lock.
    Ahora:
      - el lock se espera como maximo _lock_timeout() (< timeout de la llamada),
        asi una llamada cuyo llamador ya se rindio nunca se ejecuta tarde (p. ej.
        un order_send ya revertido que dejaria una posicion huerfana);
      - si no se obtiene, la conexion se da por atascada y se reemplaza por una
        nueva (el hilo colgado se queda con la vieja);
      - como maximo un reemplazo cada REPLACE_COOLDOWN_SECONDS: si la terminal
        entera esta colgada, se falla rapido con MT5ConnectionStuckError en vez
        de acumular conexiones e hilos.
    """

    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY = 0.5  # segundos
    REPLACE_COOLDOWN_SECONDS = 60.0
    DEFAULT_CALL_TIMEOUT_SECONDS = 20.0  # mismo default que TradeManager._call

    def __init__(self, host: str, port: int):
        from services.common.mt5_client import MT5Client
        self.host = host
        self.port = port
        self._conn = _Connection(MT5Client(host, port))
        self._swap_lock = threading.Lock()
        self._last_replace: Optional[float] = None
        log.info("[PooledMT5Client] Inicializado %s:%s", host, port)

    @property
    def _client(self):
        return self._conn.client

    @property
    def mt5(self):
        return self._client.mt5

    @staticmethod
    def _lock_timeout() -> float:
        """MT5_LOCK_TIMEOUT_SECONDS, o por defecto el 80% de MT5_CALL_TIMEOUT_SECONDS:
        siempre por debajo del timeout con el que TradeManager deja de esperar."""
        raw = os.getenv("MT5_LOCK_TIMEOUT_SECONDS", "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                log.warning("[PooledMT5Client] MT5_LOCK_TIMEOUT_SECONDS='%s' invalido, usando default", raw)
        call_timeout = PooledMT5Client.DEFAULT_CALL_TIMEOUT_SECONDS
        raw_call = os.getenv("MT5_CALL_TIMEOUT_SECONDS", "")
        if raw_call:
            try:
                call_timeout = float(raw_call)
            except ValueError:
                pass
        return call_timeout * 0.8

    def _reconnect(self, conn: _Connection) -> bool:
        """Intenta reconectar si la conexion cayo (se llama con conn.lock tomado)."""
        from services.common.mt5_client import MT5Client
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            try:
                log.warning("[PooledMT5Client] Reconectando %s:%s (intento %d/%d)",
                            self.host, self.port, attempt, self.MAX_RECONNECT_ATTEMPTS)
                conn.client = MT5Client(self.host, self.port)
                log.info("[PooledMT5Client] Reconectado %s:%s", self.host, self.port)
                return True
            except Exception as e:
                log.error("[PooledMT5Client] Fallo reconexion %s:%s: %s", self.host, self.port, e)
                if attempt < self.MAX_RECONNECT_ATTEMPTS:
                    time.sleep(self.RECONNECT_DELAY * attempt)
        return False

    def _acquire_replacing_if_stuck(self, method: str) -> _Connection:
        """Devuelve una conexion con su lock ya tomado, reemplazando la actual si su
        lock sigue retenido por una llamada colgada. Lanza MT5ConnectionStuckError si
        no hay conexion utilizable dentro del tiempo."""
        from services.common.mt5_client import MT5Client
        timeout = self._lock_timeout()
        conn = self._conn
        if conn.lock.acquire(timeout=timeout):
            return conn
        if not self._swap_lock.acquire(timeout=timeout):
            raise MT5ConnectionStuckError(f"MT5 {self.host}:{self.port} atascado ({method}): reemplazo en curso no termino")
        try:
            if self._conn is conn:  # nadie la reemplazo mientras esperabamos
                now = time.monotonic()
                if self._last_replace is not None and now - self._last_replace < self.REPLACE_COOLDOWN_SECONDS:
                    raise MT5ConnectionStuckError(
                        f"MT5 {self.host}:{self.port} atascado ({method}) y la conexion ya se reemplazo "
                        f"hace {now - self._last_replace:.0f}s -- la terminal parece colgada, fallando rapido")
                log.error("[PooledMT5Client] Lock de %s:%s retenido >%.1fs por una llamada colgada -- "
                          "reemplazando la conexion (llamada en espera: %s)", self.host, self.port, timeout, method)
                try:
                    self._conn = _Connection(MT5Client(self.host, self.port))
                except Exception as e:
                    raise MT5ConnectionStuckError(
                        f"MT5 {self.host}:{self.port} atascado ({method}) y no se pudo abrir una conexion nueva: {e}") from e
                self._last_replace = now
            conn = self._conn
        finally:
            self._swap_lock.release()
        if not conn.lock.acquire(timeout=timeout):
            raise MT5ConnectionStuckError(f"MT5 {self.host}:{self.port} atascado ({method}) tambien en la conexion nueva")
        return conn

    def _call(self, method: str, *args, **kwargs):
        """Ejecuta un metodo del cliente, reconectando si es necesario."""
        conn = self._acquire_replacing_if_stuck(method)
        try:
            try:
                return getattr(conn.client, method)(*args, **kwargs)
            except Exception as e:
                log.warning("[PooledMT5Client] Error en %s.%s: %s — intentando reconexion", self.host, method, e)
                if self._reconnect(conn):
                    try:
                        return getattr(conn.client, method)(*args, **kwargs)
                    except Exception as e2:
                        log.error("[PooledMT5Client] Error tras reconexion en %s.%s: %s", self.host, method, e2)
                        raise
                raise
        finally:
            conn.lock.release()

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

    def history_deals_get(self, *args, **kwargs):
        # Bug real de produccion: este passthrough faltaba, asi que todo
        # llamador de TradeManager._get_close_price contra la cuenta real
        # (PooledMT5Client, no el MT5Client directo que usan los tests)
        # siempre fallaba con AttributeError, silenciado por el try/except
        # de _get_close_price -- cada mensaje de cierre a n8n mostraba
        # "N/D" en vez del precio real, y _tick_once_account no podia
        # verificar si un cierre de tp1_leg realmente toco el TP1.
        return self._call("history_deals_get", *args, **kwargs)
