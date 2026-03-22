"""
env_validator.py
Valida variables de entorno criticas al arranque de cada servicio.
Llama a validate() al inicio de main() para detectar configuraciones invalidas
antes de que el servicio empiece a operar.
"""
import os
import logging

log = logging.getLogger("env_validator")


class EnvError(Exception):
    """Lanzada cuando una variable de entorno requerida falta o es invalida."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvError(f"Variable de entorno requerida no configurada: {name}")
    return value


def _require_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        val = float(raw)
    except ValueError:
        raise EnvError(f"{name}='{raw}' no es un numero valido")
    if val <= 0:
        raise EnvError(f"{name}={val} debe ser mayor que 0")
    return val


def _require_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        val = int(raw)
    except ValueError:
        raise EnvError(f"{name}='{raw}' no es un entero valido")
    if val <= 0:
        raise EnvError(f"{name}={val} debe ser mayor que 0")
    return val


def validate_telegram_ingestor() -> None:
    """Valida variables requeridas por telegram_ingestor."""
    errors = []
    for name in ("TG_API_ID", "TG_API_HASH", "TG_PHONE", "REDIS_URL"):
        try:
            _require(name)
        except EnvError as e:
            errors.append(str(e))
    # TG_API_ID debe ser entero
    api_id = os.getenv("TG_API_ID", "")
    if api_id and not api_id.strip().isdigit():
        errors.append(f"TG_API_ID='{api_id}' debe ser un entero")
    _report(errors, "telegram_ingestor")


def validate_trade_orchestrator() -> None:
    """Valida variables requeridas por trade_orchestrator."""
    errors = []
    for name in ("REDIS_URL",):
        try:
            _require(name)
        except EnvError as e:
            errors.append(str(e))
    try:
        _require_positive_int("ENTRY_WAIT_SECONDS", 90)
    except EnvError as e:
        errors.append(str(e))
    try:
        _require_positive_int("ENTRY_POLL_MS", 200)
    except EnvError as e:
        errors.append(str(e))
    try:
        _require_positive_float("DEDUP_TTL_SECONDS", 120.0)
    except EnvError as e:
        errors.append(str(e))
    try:
        _require_positive_float("DEFAULT_SL_XAUUSD_PIPS", 60.0)
    except EnvError as e:
        errors.append(str(e))
    _report(errors, "trade_orchestrator")


def validate_router_parser() -> None:
    """Valida variables requeridas por router_parser."""
    errors = []
    for name in ("REDIS_URL",):
        try:
            _require(name)
        except EnvError as e:
            errors.append(str(e))
    try:
        _require_positive_float("DEDUP_TTL_SECONDS", 120.0)
    except EnvError as e:
        errors.append(str(e))
    _report(errors, "router_parser")


def validate_backend_admin() -> None:
    """Valida variables requeridas por backend_admin."""
    errors = []
    for name in ("ADMIN_USER", "ADMIN_PASS", "CONFIG_DB_URL"):
        try:
            val = _require(name)
            if name in ("ADMIN_USER", "ADMIN_PASS") and val.lower() in ("admin", "password", "admin123", "changeme"):
                log.warning("[ENV] %s usa un valor inseguro por defecto. Cambiar en produccion.", name)
        except EnvError as e:
            errors.append(str(e))
    _report(errors, "backend_admin")


def validate_market_data() -> None:
    """Valida variables requeridas por market_data."""
    errors = []
    for name in ("REDIS_URL",):
        try:
            _require(name)
        except EnvError as e:
            errors.append(str(e))
    _report(errors, "market_data")


def _report(errors: list[str], service: str) -> None:
    if errors:
        for e in errors:
            log.critical("[ENV][%s] %s", service, e)
        raise EnvError(
            f"[{service}] {len(errors)} error(es) de configuracion al arrancar:\n  " +
            "\n  ".join(errors)
        )
    log.info("[ENV][%s] Configuracion validada correctamente.", service)
