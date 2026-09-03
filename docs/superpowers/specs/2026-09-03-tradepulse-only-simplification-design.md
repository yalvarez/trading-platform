# Diseño: Simplificación a proveedor único TradePulse

Fecha: 2026-09-03
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

## 1. Contexto y objetivo

El proyecto `trading-platform` maneja hoy múltiples proveedores de señales
(GoldBro fast/long/scalp, Hannah, Limitless, ToroFX, TradePulse), múltiples
cuentas MT5, un backend de administración con CRUD sobre Postgres, un
servicio de market_data y una pila de monitoring (Prometheus/Alertmanager/
Promtail). El working tree ya tiene trabajo iniciado hacia TradePulse-only
(`router_parser/app.py` con los demás parsers comentados, nuevo
`tradepulse_filters.py`).

Objetivo de esta reformulación: dejar el proyecto operando **solo** con el
proveedor de señales TradePulse (que emite señales fast, señales completas y
mensajes de gestión), **una sola cuenta MT5** activa, y el pipeline mínimo
necesario: telegram_ingestor → router_parser → trade_orchestrator → MT5, más
un nuevo servicio `trade_api` para control externo, con notificaciones y
conciliación delegadas a un flujo de **n8n** ya existente vía webhook.
Prioridad: ligereza y simplicidad de mantenimiento sobre flexibilidad
multi-proveedor.

## 2. Alcance

**Dentro de alcance:**
- Eliminar todo el código de proveedores que no sean TradePulse.
- Eliminar servicios no esenciales: `backend_admin`, `market_data`,
  `monitoring/*`, Postgres, y las variantes MT5 huérfanas
  (`mt5_custom`, `mt5_extended`, `mt5linux`).
- Simplificar `router_parser` (quitar ruteo por canal multi-parser).
- Limpiar `trade_orchestrator`/`trade_manager.py` de ramas específicas de
  otros proveedores (Hannah, ToroFX, GB_FAST), preservando la lógica
  genérica de gestión (parcial, BE, trailing, addon, modos
  general/be_pips/be_pnl) y el mecanismo fast→full de TradePulse.
- Reemplazar las notificaciones vía Telegram (desde `trade_orchestrator`)
  por un POST a un webhook único de n8n.
- Crear el nuevo servicio `trade_api` (FastAPI) para abrir/modificar/
  cerrar/consultar trades desde aplicaciones externas, operando MT5
  directamente (sin depender del estado en memoria del orchestrator).
- Simplificar `services/common/config.py`/`config_db.py` a env-only
  (sin Postgres).
- Actualizar `docker-compose.yml`, `.env.example`, `README.md`, tests.

**Fuera de alcance (explícitamente):**
- Construir el flujo de n8n en sí (vive fuera de este repo).
- Un dashboard o ledger propio en este proyecto (delegado a n8n).
- Quitar el soporte de `ACCOUNTS_JSON` como lista (se mantiene por
  flexibilidad futura; solo se configura/corre una entrada activa).
- Cambiar el sabor de imagen MT5 (se mantiene `gmag11/metatrader5_vnc:latest`
  tal como está wireado hoy en `docker-compose.yml`, sin build propio).

## 3. Arquitectura final

```
Telegram (canal TradePulse)
        │
        ▼
telegram_ingestor  ──► Redis Stream (raw_messages)
        │                       │
        │                       ▼
        │              router_parser (solo TradePulseParser)
        │                       │
        │           ┌───────────┴───────────┐
        │           ▼                       ▼
        │   Redis Stream (signals)   Redis Stream (mgmt)
        │           │                       │
        │           └───────────┬───────────┘
        │                       ▼
        │             trade_orchestrator ──► MT5 (mt5_acct1, RPyC)
        │                       │
        │                       └──► webhook n8n (eventos: aperturas,
        │                            BE, parciales, trailing, cierres,
        │                            errores)
        │
        └── (ya no expone /notify; vuelve a su único rol: leer Telegram
             y publicar a Redis)

trade_api (nuevo, FastAPI, servicio independiente)
        │
        ├──► MT5 (mismo host/puerto RPyC que trade_orchestrator, vía
        │     mt5_client compartido en services/common)
        │
        └──► expuesto a aplicaciones externas: open/modify/close/status
```

**Servicios en `docker-compose.yml` tras la reformulación:**
`redis`, `mt5_acct1`, `telegram_ingestor`, `router_parser`,
`trade_orchestrator`, `trade_api`.

**Servicios/piezas eliminados:**
`postgres`, `backend_admin`, `market_data`, `monitoring/` (prometheus,
alertmanager, promtail) + `prometheus.yml`/`promtail-config.yml` en la
raíz, `services/mt5_custom/`, `services/mt5_extended/`, `services/mt5linux/`.

## 4. `router_parser`

- Eliminar archivos: `parsers_goldbro_fast.py`, `parsers_goldbro_long.py`,
  `parsers_goldbro_scalp.py`, `parsers_hannah.py`, `parsers_limitless.py`,
  `parsers_torofx.py`, `parsers_daily_signal.py`, `gb_filters.py`,
  `torofx_filters.py`.
- Eliminar tests asociados: `test_parsers_hannah.py`,
  `test_parsers_hannah_only.py`, `test_parsers_all_providers.py`, y
  cualquier caso multi-proveedor dentro de `test_parsers.py`/
  `test_parsers_cases.py` en `tests/` (conservando únicamente casos
  TradePulse; `test_parsers_tradepulse.py` se mantiene).
- `app.py`: quitar imports comentados de otros parsers, quitar
  `looks_like_torofx_management` y su import; dejar solo
  `looks_like_followup` (TradePulse) para detectar mensajes de gestión.
- `SignalRouter`: eliminar el parámetro `channels_config` y el ruteo por
  canal (`CHANNELS_CONFIG_JSON`) — con un solo parser no filtra nada útil.
  `parse_signal` prueba directo con `TradePulseParser`.
- `parsers_base.py` se mantiene (interfaz `SignalParser`/`ParseResult`
  genérica, sigue siendo necesaria).

## 5. `trade_orchestrator`

- **`trade_manager.py`**: eliminar `handle_hannah_management_message`
  completo y su ruteo; eliminar lógica específica de TOROFX
  (`torofx_provider_tag_match` y los chequeos que lo usan). Conservar la
  lógica de `-ADDON` (pirámide midpoint entrada-SL): es una funcionalidad
  genérica del bot, no específica de otro proveedor. Conservar gestión
  general, `be_pips`, `be_pnl`, trailing, cierres parciales, BE.
- **`app.py`**: renombrar el tag hardcodeado `GB_FAST` a un nombre neutral
  (p. ej. `FAST`) ya que el mecanismo fast→full ahora es exclusivamente de
  TradePulse.
- **Notificaciones**: sustituir `_notify_bg` /
  `TelegramNotifierAdapter` / `RemoteTelegramNotifier` por un cliente HTTP
  simple que hace `POST` a un único webhook n8n (`N8N_WEBHOOK_URL`), con
  payload JSON estructurado (evento, ticket, cuenta, símbolo, dirección,
  detalle). Se mantiene el mismo punto de llamada (`_notify_bg`) internamente
  para minimizar el diff en el resto del archivo, pero su implementación
  deja de hablar con Telegram.
- Eliminar: `services/trade_orchestrator/common/telegram_notifier.py`,
  `services/trade_orchestrator/notifications/telegram.py`,
  `services/common/telegram_notifier.py`,
  `services/telegram_ingestor/notify_api.py`.
- `telegram_ingestor` vuelve a su único rol: leer Telegram y publicar a
  Redis (ya no expone `/notify`).

## 6. `trade_api` (nuevo servicio)

- FastAPI ligero, mismo patrón de Dockerfile/`env_file: .env` que los
  demás servicios.
- **No comparte proceso ni estado en memoria** con `trade_orchestrator`.
  Para status/open/modify/close, opera MT5 directamente vía RPyC (mismo
  host/puerto que usa `trade_orchestrator`), sin depender del `TradeManager`
  en memoria del orchestrator. Como consecuencia, los trades abiertos vía
  `trade_api` no llevan el tracking rico del pipeline de señales
  (`provider_tag`, TPs planeados, modo de gestión) — son operaciones MT5
  directas para control manual/externo, en paralelo al pipeline automático
  de señales.
- Para evitar duplicar lógica de conexión MT5, la parte de
  `mt5_client.py`/`mt5_executor.py` que es pura ejecución (no gestión de
  streams) se mueve a `services/common/` para que tanto
  `trade_orchestrator` como `trade_api` la importen desde un solo lugar.
- Endpoints mínimos:
  - `POST /trades` — abrir trade (symbol, direction, volume, sl, tp opcional)
  - `PATCH /trades/{ticket}` — modificar SL/TP o cerrar parcial
  - `DELETE /trades/{ticket}` — cerrar trade
  - `GET /trades` / `GET /trades/{ticket}` — consultar posiciones abiertas
- Autenticación por API key simple (header), mismo patrón que tenía
  `notify_api.py` (`TRADE_API_KEY` en env).
- Puerto propio en `docker-compose.yml`, expuesto para consumo externo.

## 7. Config / env vars

- **Quitar** de `.env.example` y `services/common/config.py`:
  `CHANNELS_CONFIG_JSON`, `CONFIG_DB_URL`, `ADMIN_USER`, `ADMIN_PASS`,
  cualquier var exclusiva de Telegram-como-notificador si no se reusa.
- **Agregar**: `N8N_WEBHOOK_URL` (y `N8N_WEBHOOK_TOKEN` opcional si el
  flujo n8n requiere auth), `TRADE_API_KEY`.
- **Mantener**: `ACCOUNTS_JSON` como lista (documentar que por ahora solo
  se define/activa una entrada).
- `services/common/config_db.py`: eliminar toda la rama `psycopg2`/
  Postgres; queda como lectura env-only (elimina también
  `config_db_loader.py`, `config_db_migration.py`,
  `config_db_schema.sql`, `config_db_schema_full.sql`). `Settings.accounts()`
  / `signal_providers()` / `channel_providers()` en `config.py` se
  simplifican para leer directo de env (quitando las ramas `db_url`).

## 8. Testing

- Mantener/adaptar: `test_parsers_tradepulse.py`,
  `test_trade_management_logic.py`, `test_trading_modes.py`,
  `test_gestion_completa.py`, `test_simulador_mt5.py`,
  `test_trade_manager.py`, `test_trading_logic*.py`, `test_deduplication*.py`
  — revisando que ningún fixture/caso dependa de Hannah/TOROFX/GoldBro y
  quitando esos casos si los hay.
  eliminar `test_backend_endpoints.py` (backend_admin desaparece).
- Agregar tests nuevos básicos (smoke) para los endpoints de `trade_api`
  con MT5 mockeado.
- `conftest.py` (raíz y `tests/`): revisar fixtures que monten Postgres o
  multi-proveedor y limpiarlas.

## 9. Documentación

- Actualizar `README.md`: un solo proveedor (TradePulse), una cuenta
  activa, sin backend_admin/Postgres, notificaciones vía webhook n8n en
  vez de Telegram, documentar `trade_api` y sus endpoints.
- `DEPLOYMENT.md`/`IMPLEMENTATION_SUMMARY.md`: revisar y actualizar si
  referencian servicios eliminados.

## 10. Riesgos / notas

- El diff en `trade_manager.py` (2059 líneas) es el más delicado: hay que
  separar con cuidado qué es "genérico" (addon, BE, trailing, be_pips/
  be_pnl) de qué es "específico de otro proveedor" (Hannah, TOROFX) antes
  de borrar, para no romper gestión que TradePulse sí necesita.
- `trade_api` operando MT5 sin pasar por `trade_manager` puede generar
  posiciones "invisibles" para la gestión automática del orchestrator
  (no tendrán BE/trailing/parciales automáticos). Esto es aceptado como
  comportamiento esperado: son trades de control externo/manual.
- Confirmar con el flujo de n8n el contrato exacto del payload del webhook
  antes de fijarlo en código (esta spec deja el payload como JSON
  estructurado abierto a ajuste fino en implementación).
