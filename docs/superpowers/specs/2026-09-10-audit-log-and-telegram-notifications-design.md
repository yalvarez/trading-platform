# Diseño: Log de auditoría real + notificaciones legibles a Telegram vía n8n

Fecha: 2026-09-10
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

## 1. Contexto y problema

Hoy `trade_orchestrator` no tiene ni un log de auditoría real ni un canal
de notificaciones legibles hacia Telegram. Lo que existe:

- **Logging de aplicación**: `logging` estándar de Python, texto plano, a
  stdout únicamente (`services/trade_orchestrator/app.py:14-17`). Sin
  formato estructurado (JSON), sin persistencia propia — depende de lo que
  Docker retenga.
- **Un único punto de eventos de negocio**: `TradeManager._notify(event,
  **kwargs)` (`trade_manager.py:57-64`). Siempre hace `log.info`, y si hay
  un notifier configurado reenvía a n8n vía POST.
- **Esquema de salida hacia n8n rígido y aplanado**: `N8N_SCHEMA_FIELDS =
  ("group_id", "leg", "symbol", "action", "message")`
  (`services/common/n8n_notifier.py:14-16`). Cualquier otro campo se
  concatena como texto dentro de `message`. No hay `payload` JSON
  estructurado.
- **No hay P&L en dinero en ningún evento** — ni volumen de cierre, ni
  profit. Solo precio (`_get_close_price`, `trade_manager.py:649-668`, que
  solo lee `entry` y `price` del deal de MT5).
- **No hay auditoría real separada del log de aplicación.**
  `data/trade_state.jsonl` es el snapshot vivo de gestión (se compacta,
  no es historial).
- **No hay mapeo `chat_id` → nombre legible de canal.**
  `Settings.channel_providers()` y `ConfigProvider.get_channel_providers()`
  devuelven `{}` hardcodeado (`services/common/config.py:40-42`,
  `services/common/config_db.py:36-37`).
- **Si el POST a n8n falla, el evento se pierde** — solo un `warning` en
  el log, sin reintento ni cola (`n8n_notifier.py:36-38`).
- **La detección de causa de cierre es heurística, no usa `deal.reason`**:
  `_closed_at_tp1` (`trade_manager.py:570-599`) decide "fue TP1" si el
  precio de cierre avanzó al menos el 50% de la distancia `entry→tp1`;
  cualquier otra causa (SL, cierre manual, liquidación) cae en un mismo
  `else` genérico sin distinguir cuál fue. El campo `reason` del deal de
  MT5 (`DEAL_REASON_SL`/`DEAL_REASON_TP`/`DEAL_REASON_CLIENT`) nunca se
  lee — el wrapper (`PooledMT5Client.history_deals_get`,
  `mt5_pool.py:177-185`) es passthrough total y sí lo expondría.
- **`move_sl_be_now` (BE manual) ya funciona hoy incluso antes de que TP1
  se toque** — no hay ninguna validación que lo bloquee
  (`trade_manager.py:930-992`) — pero no genera ningún mensaje legible
  hacia Telegram, solo el evento plano de siempre.
- **No existe cierre parcial manual con porcentaje arbitrario.**
  `close_now` (`trade_manager.py:878-928`) siempre cierra el 100%
  (`client.partial_close(account, t.ticket, 100)`, línea 897). El único
  cierre parcial que existe es el automático de TP2, con 50% fijo
  (`_apply_tp2_partial_close`, líneas 705-749). La capa MT5
  (`partial_close(account, ticket, percent)`, `mt5_pool.py:171-172`) sí
  soporta cualquier porcentaje, pero ningún endpoint lo expone.

El usuario necesita dos cosas que hoy no existen:

1. Un **log de auditoría real**, consultable por él y por Claude, con
   toda la información disponible de cada evento del sistema (incluyendo
   ruido interno/errores) — para trazabilidad y seguridad.
2. Un **canal de mensajes legibles hacia su Telegram**, vía n8n, con
   solo los eventos que le importan como trader (apertura, TP1, TP2, SL,
   cierre manual, BE), en un formato de texto claro, incluyendo P&L en
   dinero al cerrar.

## 2. Alcance

**Dentro de alcance:**

- Un `EventBus` en `trade_orchestrator` que reemplaza `_notify`, con un
  envelope de evento único (`event_id`, `event_type`, `channel`,
  `timestamp`, `message`, `payload`).
- Persistencia local **siempre**, síncrona, en un archivo JSONL append-only
  dedicado (`data/audit_log.jsonl`), nunca compactado — la fuente de
  verdad primaria, independiente de que Redis o n8n estén disponibles.
- Envío a un **único webhook de n8n** con reintento vía cola en Redis;
  cada evento lleva `channel: "audit" | "both"` para que el flujo de n8n
  decida si va solo al Data Table o también a Telegram.
- Ampliar `_get_close_price` para también capturar `profit`, `volume`,
  `commission`, `swap` del deal de cierre — P&L en dinero sale de ahí
  (fuente: MT5 `history_deals_get`, no cálculo manual).
- Reemplazar la heurística de tolerancia de `_closed_at_tp1` por lectura
  directa de `deal.reason` (`DEAL_REASON_TP`/`DEAL_REASON_SL`/
  `DEAL_REASON_CLIENT`) para distinguir con certeza TP1 / SL / cierre
  manual, tanto para la pierna tp1 como para el runner.
- Nuevo evento automático `sl_hit_detected`, disparado por el loop de
  gestión al detectar (vía `deal.reason == DEAL_REASON_SL`) que una
  pierna se cerró por stop loss, sin depender de que el usuario avise
  manualmente.
- Nuevo evento automático `external_close_detected`, disparado cuando el
  loop de gestión detecta que una pierna se cerró sin que el sistema lo
  haya ordenado y sin ser TP/SL — típicamente alguien cerrando a mano
  directamente en MT5/el broker. Se notifica como alerta de seguridad.
- Mapeo simple `chat_id → nombre de canal` vía config
  (`CHANNEL_NAMES_JSON` o similar), con fallback al `chat_id` crudo.
- Nueva acción `close_partial_now` en `/mgmt/action`: cierre parcial
  manual con porcentaje arbitrario (default 50% si Ollama no extrae uno),
  aplicado a **todas** las piernas activas del grupo proporcionalmente.
- Mensajes de Telegram (vía `message`, texto ya armado en Python) para:
  apertura, TP1, TP2 (partial automático), SL (detectado automático),
  cierre manual (`close_now` y `close_partial_now`), BE manual y BE
  automático.
- Extender el simulador de test (`SimuladorMT5`) para modelar `reason`,
  `profit`, `volume`, `commission`, `swap` en los deals sintéticos.

**Fuera de alcance (explícitamente):**

- Cambios al flujo interno de n8n (el nodo Ollama, el prompt de
  clasificación, cómo arma el Data Table o el mensaje final a Telegram) —
  eso lo configura el usuario en su instancia de n8n; este spec solo
  define el contrato HTTP que `trade_orchestrator` expone.
- Prometheus/Grafana/Loki — evaluado y descartado; ya habían sido
  removidos del proyecto por simplicidad y no resuelven bien eventos
  discretos con texto/JSON rico.
- Dead-letter queue con UI o alerta proactiva — si un evento agota
  reintentos, se marca `delivery_status: "dead_letter"` en el JSONL local
  para revisión manual; no se construye ninguna alerta automática sobre
  eso en esta iteración.
- Cambiar `find_active_groups_for_chat` u otra lógica de resolución de
  grupos no relacionada con logging/notificaciones.
- Reintentos para el JSONL local en sí (si escribir a disco falla, es un
  problema de infraestructura fuera de este diseño).

## 3. Arquitectura

```
TradeManager (evento de negocio: open, tp1_hit, tp2_partial, sl_hit_detected,
              external_close_detected, close_now, close_partial_now,
              be_applied, ...)
        │
        ▼
   EventBus.emit(event_type, channel, message, payload)
        │
        ├──► escribir línea en data/audit_log.jsonl   (SIEMPRE, síncrono, primero)
        │
        └──► push a Redis list "n8n_event_queue"
                    │
                    ▼
             worker de reintento (asyncio, dentro de trade_orchestrator)
                    │  backoff exponencial, tope de intentos
                    ▼
             POST único a N8N_EVENT_WEBHOOK_URL
                    │
          ┌─────────┴─────────┐
          │   n8n: IF/Switch por `payload.channel`   │
          └─────────┬─────────┘
           channel="audit"        channel="both"
                │                       │
                ▼                       ▼
          Data Table n8n         Data Table n8n + Telegram
```

Puntos clave:

- El **JSONL local es la fuente de verdad primaria**: se escribe siempre,
  de forma síncrona, antes de intentar nada por red. Ningún evento se
  pierde por caída de Redis o de n8n.
- **Un solo webhook HTTP** hacia n8n, con el payload completo siempre
  (incluye el JSON técnico completo); el campo `channel` le dice a n8n si
  bifurca solo a auditoría o también a Telegram. Se prefirió esto sobre
  dos webhooks separados por simplicidad de mantenimiento (un solo
  endpoint, un solo cliente HTTP, un solo punto de fallo que vigilar).
- **Reintentos vía cola en Redis**, reusando la infraestructura que ya
  existe en el proyecto (Redis Streams para el pipeline de señales). Un
  worker de fondo (mismo patrón async que `run_forever`) consume la cola,
  reintenta con backoff, y si agota intentos marca el evento como
  `dead_letter` en el JSONL para revisión manual — nunca se descarta en
  silencio.

## 4. Envelope de evento

```json
{
  "event_id": "uuid4",
  "event_type": "group_opened",
  "channel": "both",
  "timestamp": "2026-09-10T14:32:01.123Z",
  "message": "🟢 APERTURA — Canal: Oro Premium (grupo 61)\nEURUSD BUY\nEntrada: 1.09345\nSL: 1.09100 | TP1: 1.09500 | TP2: 1.09800\nVolumen: 0.02 lots",
  "payload": { "...": "campos específicos del evento, ver §6" }
}
```

- `event_id`: UUID4 generado en Python al crear el evento. Permite
  correlacionar el mismo evento entre el JSONL local, el Data Table de
  n8n, y los logs de n8n si hace falta debug.
- `event_type`: mismo vocabulario de eventos que ya existe hoy
  (`group_opened`, `tp1_hit`, `tp2_partial_closed`, `mgmt_close_now`,
  `mgmt_move_sl_be_applied`, etc.) más los nuevos (`sl_hit_detected`,
  `mgmt_close_partial_now`). No se renombra nada existente.
- `channel`: `"audit"` (solo Data Table) o `"both"` (Data Table +
  Telegram). Decidido en Python por tipo de evento (ver tabla §6), no
  configurable desde n8n.
- `message`: texto legible en español, ya armado por `trade_orchestrator`.
  n8n lo reenvía tal cual a Telegram cuando `channel` es `"both"` — la
  responsabilidad del formato final vive en Python para mantenerlo
  consistente en un solo lugar; n8n solo reenvía.
- `payload`: JSON con los campos estructurados específicos del evento
  (ver §6), siempre presente sin importar el `channel`.

## 5. Detección de causa de cierre vía `deal.reason`

Reemplaza la heurística de tolerancia de precio de `_closed_at_tp1` por
lectura directa del campo `reason` del deal de salida (`DEAL_ENTRY_OUT`)
en `history_deals_get`. MT5 expone `DEAL_REASON_SL`, `DEAL_REASON_TP`,
`DEAL_REASON_CLIENT` (cierre manual/API), entre otros.

- `_get_close_price` se amplía (o se agrega una función hermana,
  `_get_close_deal_info`) para devolver también `reason`, `profit`,
  `volume`, `commission`, `swap` del mismo deal — sin llamada adicional a
  MT5, ya está en el mismo objeto.
- La detección pasiva en `_tick_once_account` (línea ~524, "ticket ya no
  está en `positions_get`") pasa a resolver la causa así:
  - `reason == DEAL_REASON_TP` → TP1 genuino (pierna tp1) → dispara
    `_on_tp1_leg_closed` como hoy.
  - `reason == DEAL_REASON_SL` → nuevo evento `sl_hit_detected`, con P&L
    real, para **cualquier** pierna (tp1 o runner).
  - `reason == DEAL_REASON_CLIENT` (o cualquier otra causa) cuando el
    ticket **no** fue removido de forma síncrona por `apply_mgmt_action`
    (es decir, nadie desde Telegram pidió este cierre) → nuevo evento
    `external_close_detected`, `channel: "both"`, con P&L real. Es
    información de seguridad: alguien tocó la posición directamente en
    MT5/el broker, por fuera del sistema — se avisa igual que un SL o TP,
    con un tono de alerta explícito ("cierre no originado por el
    sistema").
- Los cierres que el propio `TradeManager` origina de forma síncrona
  (`close_now`, `close_partial_now`, TP2 partial) **no cambian** — ya
  conocen su causa con certeza porque el código la originó; no dependen
  de esta detección pasiva. Concretamente: `close_now`/`close_partial_now`
  ya remueven el ticket de `self.trades` de forma síncrona dentro de la
  misma llamada (como hoy hace `close_now`, `trade_manager.py:908`) antes
  de que `_tick_once_account` pueda verlo "desaparecido" — por eso la
  detección pasiva por `reason` nunca compite con estos casos ni los
  reclasifica erróneamente como `external_close_detected`.
- `SimuladorMT5` (`tests/test_simulador_mt5.py`, `_record_deal`) se
  extiende para incluir `reason`, `profit`, `volume`, `commission`,
  `swap` en los deals sintéticos que genera, para poder ejercitar esta
  lógica en tests.

## 6. Catálogo de eventos

Todos con `payload` incluyendo como mínimo: `group_id`, `chat_id`,
`channel_name` (resuelto vía mapeo, fallback a `chat_id` crudo),
`symbol`, `direction`.

| `event_type` | `channel` | Campos adicionales en `payload` | Mensaje a Telegram |
|---|---|---|---|
| `group_opened` | both | `entry_price`, `sl`, `tp1`, `tp2`, `volume` | 🟢 Apertura — canal, símbolo, dirección, entrada, SL, TP1/TP2, volumen |
| `tp1_hit` | both | `close_price`, `close_volume`, `pnl_money`, `account_currency` | ✅ TP1 alcanzado — precio, volumen cerrado, ganancia, nota de BE aplicado |
| `tp1_hit_be_failed` | both | igual que `tp1_hit` + `be_error` | ✅ TP1 alcanzado pero ⚠️ BE no se pudo aplicar (revisar manualmente) |
| `tp2_partial_closed` | both | `close_price`, `close_volume` (50%), `pnl_money`, `remaining_volume` | ✅ TP2 alcanzado — 50% cerrado, ganancia, runner sigue con trailing |
| `sl_hit_detected` (nuevo) | both | `leg`, `close_price`, `close_volume`, `pnl_money` | 🔴 Stop Loss — precio, volumen, pérdida/ganancia real |
| `external_close_detected` (nuevo) | both | `leg`, `close_price`, `close_volume`, `pnl_money`, `deal_reason` | 🚨 Cierre externo detectado — pierna cerrada por fuera del sistema (no fue TP/SL/orden de Telegram), revisar la cuenta |
| `mgmt_close_now` | both | por leg: `close_price`, `close_volume`, `pnl_money`; `total_pnl_money`; `raw_text` | ⚠️ Cierre manual — motivo (raw_text), piernas cerradas, total |
| `mgmt_close_partial_now` (nuevo) | both | `percent_requested`, por leg: `close_price`, `close_volume`, `pnl_money`; `raw_text` | ⚠️ Cierre parcial manual — % solicitado, piernas afectadas, ganancia parcial |
| `mgmt_move_sl_be_applied` | both | `new_sl`, `raw_text` | 🛡️ SL movido a breakeven manualmente |
| `mgmt_move_sl_be_already_satisfied` | audit | — | (sin mensaje, es un no-op informativo) |
| `mgmt_close_now_partial_failure` | both | `leg_summaries` | ⚠️ Cierre incompleto — al menos una pierna fue rechazada por el broker, revisar manualmente (posición puede seguir abierta) |
| `mgmt_close_partial_now_failure` (nuevo) | both | `percent_requested`, `leg_summaries` | ⚠️ Cierre parcial incompleto — al menos una pierna fue rechazada por el broker al aplicar el % solicitado, revisar manualmente |
| `mgmt_no_active_trade`, `mgmt_account_unresolved`, `mgmt_no_runner_leg`, `mgmt_invalid_correction`, `mgmt_unknown_action` | audit | los que ya existen hoy | — (ruido operativo, solo auditoría) |
| `mgmt_note_sl_hit` | audit | los que ya existen hoy | — (ahora redundante con `sl_hit_detected` automático, se mantiene para compatibilidad pero deja de ser la vía principal) |
| `reconciliation_summary` | audit | los que ya existen hoy | — |
| `trailing_updated` (si se decide reactivar) | audit | `new_sl`, `peak_multiple` | — (no es user-facing, es mecánico y frecuente) |

## 7. Nueva acción `close_partial_now`

Extiende `/mgmt/action`:

```python
class MgmtActionRequest(BaseModel):
    action: str
    chat_id: str
    raw_text: str
    correction: Optional[Correction] = None
    percent: Optional[float] = None   # nuevo, solo relevante para close_partial_now
```

Nueva rama en `apply_mgmt_action`, paralela a `close_now`:

- `percent = req.percent if req.percent is not None else 50.0` (default
  acordado si Ollama no extrae un valor explícito del texto).
- Se aplica a **todas** las piernas activas del grupo (tp1_leg si aún no
  se cerró, y/o runner) — cada una recibe `partial_close(account, ticket,
  percent)` con el mismo porcentaje.
- Si una pierna falla el `partial_close`, se reporta como
  `mgmt_close_partial_now_failure` (mismo patrón de manejo de error que
  `mgmt_close_now_partial_failure`), sin abortar las demás piernas.
- El grupo permanece activo (a diferencia de `close_now`, que sí lo
  cierra en el store) porque queda volumen remanente en cada pierna
  parcialmente cerrada.

## 8. Errores y resiliencia

- **Fallo al escribir el JSONL local**: no se reintenta (fuera de
  alcance) — se asume que un fallo de disco es un problema de
  infraestructura más amplio que este diseño no resuelve.
- **Fallo del POST a n8n**: el evento ya está en el JSONL antes de
  intentar la red, así que no se pierde. Se encola en Redis
  (`n8n_event_queue`), un worker de fondo reintenta con backoff
  exponencial (ej. 1s, 5s, 30s, 2min — parámetros a definir en el plan de
  implementación) hasta un tope de intentos; agotado el tope, se marca
  `delivery_status: "dead_letter"` en la línea del JSONL correspondiente
  (buscada por `event_id`) para revisión manual.
- **Redis caído al momento de encolar**: se loguea un `warning` (como
  hoy) — el JSONL local sigue siendo la red de seguridad; no se bloquea
  el flujo de negocio principal de `TradeManager` por esto.

## 9. Testing

- Extender `SimuladorMT5` para modelar `reason`/`profit`/`volume`/
  `commission`/`swap` en deals sintéticos (necesario para ejercitar §5).
- Tests para: escritura del JSONL en cada evento nuevo/existente, el
  worker de reintento (éxito, fallo transitorio, dead-letter tras agotar
  intentos), la detección de `sl_hit_detected` y `external_close_detected`
  vía `reason` (incluyendo el caso borde de distinguir "cerrado por
  `apply_mgmt_action`, no debe disparar `external_close_detected`" vs.
  "cerrado por fuera del sistema, sí debe dispararlo"), y la nueva rama
  `close_partial_now` (con y sin `percent` explícito, con una y con dos
  piernas activas, y su fallo parcial `mgmt_close_partial_now_failure`).
- Verificar que `tests/test_orchestrator.py` no tenga el import roto
  documentado en una memoria anterior — confirmar antes de sumarle
  casos nuevos.
