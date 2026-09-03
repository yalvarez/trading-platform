# Diseño: Apertura dual-TP, gestión mecánica, y excepciones vía n8n/Ollama

Fecha: 2026-09-03
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

**Relación con el spec anterior:** este documento **reemplaza por completo**
la sección 5 (`trade_orchestrator`) de
[2026-09-03-tradepulse-only-simplification-design.md](2026-09-03-tradepulse-only-simplification-design.md)
— el modelo de apertura de trades y toda la lógica de gestión
(`general`/`be_pips`/`be_pnl`/`reentry`/addon) descrita ahí queda obsoleta y
se sustituye por el modelo dual-TP descrito aquí. Las demás secciones del
spec anterior (§1-4, §6-10: proveedor único, servicios mínimos eliminados,
`trade_api` para control externo, config env-only, testing, docs) **siguen
vigentes sin cambios** y no se repiten en este documento.

## 1. Contexto y objetivo

Un análisis del último mes de mensajes reales del canal TradePulse (290
mensajes, ver memoria de proyecto `tradepulse-channel-message-patterns`)
mostró que los mensajes de gestión del canal **no tienen un vocabulario de
comandos fijo** — son texto de hype/narrativa humano, variable en redacción,
mezclado con ~40% de spam promocional. El enfoque de keyword-matching
(`FOLLOWUP_KEYWORDS`, handlers por proveedor) no puede capturar esto de
forma confiable.

Objetivo: rediseñar la apertura y gestión de trades para que la mayor parte
del comportamiento sea **mecánico** (dirigido por precio, sin depender de
interpretar texto), y reservar la interpretación de lenguaje natural
—delegada a un modelo Ollama ya desplegado y accesible desde un flujo n8n
ya operativo, ambos fuera del alcance de este repo— únicamente para
**excepciones explícitas** que la mecánica de precio no puede anticipar
(cierre forzado por cambio de estructura de mercado, corrección manual de
una señal, etc).

## 2. Alcance

**Dentro de alcance:**
- Nuevo modelo de apertura: cada señal completa de TradePulse abre **dos
  posiciones** en MT5 (`tp1_leg`, `runner_leg`), vinculadas por `group_id`.
- Nueva gestión mecánica: BE automático en el runner cuando `tp1_leg`
  cierra, trailing proporcional en el runner sin techo de TP fijo.
- Eliminación completa de los modos `general`/`be_pips`/`be_pnl`/`reentry`
  y de la lógica addon/pirámide — el modelo dual-TP es el único
  comportamiento de gestión.
- Nuevo endpoint `POST /mgmt/action` en `trade_orchestrator` (mismo
  proceso, HTTP junto al consumer de Redis) que recibe decisiones de n8n
  y las ejecuta contra el grupo activo correspondiente.
- `router_parser` deja de filtrar/rutear mensajes de gestión por keyword;
  todo texto no reconocido como señal se reenvía por HTTP a un webhook n8n
  de entrada. Se elimina el stream Redis `Streams.MGMT` y
  `tradepulse_filters.looks_like_followup` como mecanismo de ruteo (el
  archivo puede conservarse solo si algo más lo usa; si no, se elimina).

**Fuera de alcance (explícitamente):**
- Construir o desplegar Ollama o el flujo n8n — ambos ya existen y están
  operativos; este spec define solo el contrato HTTP en los dos extremos.
- Piramidar/agregar una segunda entrada cuando el precio ofrece un "mejor"
  punto de entrada antes de que la orden original se llene — pendiente de
  simulación/backtesting en una iteración futura separada.
- El resto del proyecto (proveedor único, `trade_api`, eliminación de
  Postgres/backend_admin/monitoring, config env-only) — cubierto por el
  spec anterior, sin cambios.

## 3. Apertura de trade

Al recibir una señal de TradePulse:

- **Señal fast** (`XAUUSD BUY NOW`, sin SL/TP) → abrir inmediatamente
  **dos posiciones** en MT5, mismo symbol/dirección/precio de mercado, con
  un SL temporal calculado igual que hoy (`calcular_sl_default`,
  `DEFAULT_SL_XAUUSD_PIPS`/`DEFAULT_SL_PIPS`), sin TP fijo todavía. Ambas
  quedan vinculadas por un `group_id` compartido (nuevo grupo, no ligado a
  ningún trade previo). Etiquetadas `provider_tag="FAST"` como hoy.
- **Señal completa** que sigue a un fast reciente para el mismo
  symbol/dirección → localizar el `group_id` abierto por el fast y
  actualizar ambas posiciones: SL real (el de la señal) en ambas, TP1 en
  `tp1_leg`, sin TP fijo en `runner_leg` (ver §4 — el runner nunca lleva un
  TP real en MT5, TP2 solo se usa como referencia de escala para el
  trailing). Esto reemplaza el mecanismo actual "fast→full update"
  (`provider_tag == "FAST"` lookup), aplicado ahora a las dos piernas del
  grupo en vez de a una sola posición.
- **Señal completa sin fast previo** (llega directa) → abrir ambas
  posiciones de una vez, ya con el SL y TP1 reales desde el inicio.

Si no hay una cuenta activa o el precio no se puede obtener, no se abre
nada (mismo comportamiento defensivo que existe hoy).

## 4. Gestión mecánica (por precio, sin depender de texto)

Reemplaza íntegramente `gestionar_trade`/`gestionar_trade_general`/
`gestionar_trade_be_pips`/`gestionar_trade_be_pnl`/`gestionar_trade_reentry`
y `_maybe_addon_midpoint` de `trade_manager.py`.

- **BE automático:** cuando `tp1_leg` se cierra por TP hit en MT5, mover el
  SL de `runner_leg` al precio de entrada (BE) inmediatamente.
- **Runner sin techo:** `runner_leg` nunca lleva un TP fijo en MT5 (o, si
  la API de MT5 exige un valor, uno de seguridad muy lejano sin relevancia
  práctica) — su única salida mecánica es el trailing stop. Esto permite
  capturar movimientos muy por encima de TP2 sin cerrar prematuramente.
- **Trailing proporcional, escala fija, sin techo:**
  - `unit = TP2_price − TP1_price` (distancia en precio entre TP1 y TP2 de
    la señal; constante por grupo, ajustable solo por `signal_correction`,
    ver §5).
  - `peak` = múltiplo máximo histórico de `unit` que el precio haya
    alcanzado desde TP1 (puede superar 1.0 indefinidamente; nunca
    disminuye).
  - `SL_price = TP1_price + (peak × unit) / 3` — el SL solo puede subir,
    nunca baja, incluso si el precio retrocede desde el peak.
  - Antes de que `tp1_leg` cierre, `runner_leg` no tiene trailing activo —
    corre con el mismo SL original que `tp1_leg`.
- **Comportamiento por defecto sin intervención externa:** si el precio
  nunca alcanza TP1, ambas posiciones cierran en el SL original cuando el
  mercado lo golpea — pérdida simétrica normal. Este es el comportamiento
  *por defecto*; queda siempre subordinado a §5 — una acción `close_now`
  de n8n/Ollama puede cerrar ambas posiciones en cualquier momento del
  ciclo de vida del grupo, sin esperar a que se toque TP1 o el SL.
- **Piramidar en mejor precio:** no implementado — fuera de alcance (§2).

## 5. Excepciones vía n8n/Ollama

### 5.1 Salida: `router_parser` → webhook n8n de entrada

Cuando el parser de señales (`TradePulseParser`) **no** reconoce un
mensaje entrante como señal fast/completa, `router_parser` hace
`POST` directo (HTTP, `httpx`) a un webhook n8n configurado por
`N8N_INBOUND_WEBHOOK_URL`, con:
```json
{"chat_id": "<chat_id>", "text": "<raw message text>", "timestamp": "<ISO8601>"}
```
n8n es responsable de: pasar el texto a Ollama, interpretar la respuesta,
y —si corresponde a una acción ejecutable— hacer `POST` a
`/mgmt/action` en `trade_orchestrator` (§5.2). Mensajes que Ollama
clasifica como ruido/spam simplemente no generan ese segundo POST.

Esto reemplaza el flujo actual `looks_like_followup` → `Streams.MGMT`
(Redis) → `trade_orchestrator.handle_mgmt`. El stream `Streams.MGMT` se
elimina; `tradepulse_filters.looks_like_followup` se elimina si nada más
lo referencia tras este cambio.

### 5.2 Entrada: `POST /mgmt/action` en `trade_orchestrator`

Servidor HTTP (FastAPI) montado **en el mismo proceso** que el consumer de
Redis Streams de `trade_orchestrator` (no un servicio Docker separado) —
necesita acceso directo al `TradeManager` en memoria para resolver el
grupo activo por símbolo sin duplicar ese estado en otro lugar.

**Payload de entrada:**
```json
{
  "action": "close_now",
  "symbol": "XAUUSD",
  "raw_text": "MARKET STRUCTURE SHIFTED! DON'T HOLD SELL. Close now",
  "correction": null
}
```
`action` ∈ `{"close_now", "move_sl_be_now", "note_sl_hit", "signal_correction", "ignore"}`.
`correction` solo se usa con `action == "signal_correction"`:
```json
{"field": "tp2", "value": 4687.0}
```
`field` ∈ `{"sl", "tp1", "tp2"}` — únicamente estos tres campos son
corregibles; symbol/direction/entry no lo son (una corrección a esos
valores se trata como señal nueva, no como corrección).

**Resolución del grupo objetivo:** el endpoint busca el grupo activo más
reciente para `symbol` dentro del `TradeManager` en memoria (una sola
cuenta/canal activo — no hay ambigüedad de a qué cuenta aplica).

**Semántica por acción:**
- `close_now`: cierra `tp1_leg` y `runner_leg` (los que sigan abiertos) al
  precio de mercado actual, sin importar en qué punto del ciclo de vida
  estén (antes de TP1, en BE, en trailing).
- `move_sl_be_now`: verifica el SL actual de `runner_leg` contra BE (precio
  de entrada). Si el SL actual está **peor** que BE (ej. el trigger
  mecánico de §4 no se disparó por algún motivo), lo fuerza a BE ahora —
  es un fallback de seguridad, nunca reduce una protección ya alcanzada
  por el trailing. Si el SL actual ya está en BE o mejor, no hace nada,
  solo registra que la instrucción ya estaba cumplida.
- `note_sl_hit`: puramente informativo — no ejecuta ninguna acción sobre
  MT5. Se registra/loggea (y puede reenviarse a n8n como confirmación) y
  nada más.
- `signal_correction`: aplica el nuevo valor de `sl`/`tp1` directamente en
  MT5 sobre la pierna correspondiente (`sl` en ambas piernas; `tp1` en
  `tp1_leg`). Una corrección de `tp2` no toca MT5 directamente (el runner
  no lleva TP real) — actualiza el `unit`/`TP2_price` de referencia
  usado por la fórmula de trailing en §4.
- `ignore`: no-op explícito (n8n decide no reenviar mensajes de `ignore`
  en absoluto normalmente; este valor existe para que el contrato quede
  completo si n8n prefiere loggear centralizadamente en vez de
  descartar en su propio flujo).

**Sin grupo activo para el símbolo:** el endpoint responde `200 OK` con
`{"status": "no_active_trade"}` — no es un error; es un evento legítimo
(el canal habló de un trade que ya no está abierto). No se reintenta ni
se propaga como fallo.

## 6. `trade_manager.py` — alcance final tras esta reformulación

Queda reducido a:
- Estructura `ManagedTrade` (extendida con `group_id`, `leg` ∈
  `{"tp1", "runner"}`, `peak_multiple` para el trailing).
- Apertura dual-TP (§3).
- Loop de gestión mecánica: BE en TP1 hit, trailing proporcional en el
  runner (§4).
- Manejador de acciones `/mgmt/action` (§5.2), montado como rutas FastAPI
  adicionales en el mismo proceso.
- Notificación de eventos vía `N8nWebhookNotifier`/`N8nNotifierAdapter`
  (sin cambios respecto al spec anterior §5) para aperturas, BE, trailing
  updates, cierres.

Se elimina: `TradingMode` enum y todos los `gestionar_trade_*` salvo el
loop mecánico nuevo, `_maybe_addon_midpoint` y todo el código de addon
(`enable_addon`, `addon_max_count`, `addon_lot_factor`, `group_addon_count`),
`torofx_provider_tag_match` (ya cubierto por el spec anterior).

## 7. Riesgos / notas

- El cálculo de `unit = TP2 − TP1` asume TP2 > TP1 en la dirección del
  trade (ya garantizado por el parser existente, que ordena
  `entry_range`/valida TPs). Si una señal llega con TP1/TP2 invertidos o
  iguales, abortar la apertura y notificar error — no se abre un grupo con
  `unit <= 0`.
- El endpoint `/mgmt/action` no tiene autenticación explícita definida en
  este spec — dado que solo el flujo n8n (infraestructura propia, no
  pública) lo invoca, se protege igual que los demás endpoints internos
  del proyecto: un API key simple (`N8N_ACTION_API_KEY`) vía header,
  mismo patrón que `trade_api`/`notify_api.py` ya usan.
- Backtesting/simulación de "piramidar en mejor precio" y validación
  numérica de la fórmula de trailing (`peak/3`) contra el corpus histórico
  real quedan como trabajo de investigación separado, no bloqueante para
  implementar este diseño.
