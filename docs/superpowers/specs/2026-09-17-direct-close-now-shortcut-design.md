# Diseño: Atajo directo para "TRADE INVALID / Close now" y filtro por dirección

Fecha: 2026-09-17
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

## 1. Contexto y problema

El 2026-09-17 se cerró el grupo 150 (XAUUSD BUY) cuarenta segundos después
de abrirse, por un mensaje de gestión que no tenía nada que ver con él. La
secuencia real, reconstruida de los logs del VPS:

| Hora (UTC) | Evento |
|---|---|
| 05:28:28.804 | Telegram entrega `XAUUSD SELL NOW` → se abre el grupo 149 (SELL) |
| **05:36:21.861** | **Telegram entrega `XAUUSD SELL TRADE INVALID ❌ / Close now`** |
| 05:36:22.903 | `router_parser` lo reenvía al webhook de clasificación de n8n |
| 05:37:19.573 | Telegram entrega `XAUUSD BUY NOW` |
| 05:37:20.557 | Se abre el grupo 150 (BUY) |
| **05:37:58.642** | **n8n responde por fin: `close_now` → cierra el grupo 149** |
| **05:38:02.687** | **El mismo `close_now` cierra también el grupo 150** |

Dos defectos independientes se combinaron:

1. **Latencia del pipeline n8n/Ollama: ~97 segundos** entre que Telegram
   entregó el mensaje y que la acción se ejecutó. En esa ventana se abrió
   un grupo nuevo. `apply_mgmt_action` resuelve los grupos por
   `find_active_groups_for_chat(chat_id)` **en el momento de ejecución**, no
   en el momento en que se originó el mensaje, así que el grupo 150 —
   inexistente cuando el operador escribió "Close now" — quedó incluido.

2. **Ningún filtro por dirección.** El texto decía `XAUUSD **SELL** TRADE
   INVALID`, pero `close_now` cierra todos los grupos activos del `chat_id`
   sin mirar símbolo ni dirección (comportamiento deliberado del spec
   `2026-09-08-mgmt-action-chat-id-scoping-design.md`, §5). El grupo 150 era
   BUY: la dirección opuesta a la que el mensaje nombraba.

El diseño de chat_id-scoping asumió implícitamente que los grupos activos al
ejecutar son los mismos que al recibir el mensaje. Con un pipeline de ~97s y
un canal que reabre señales, esa suposición no se sostiene.

## 2. Enfoque

El patrón `TRADE INVALID ... Close now` es literal, distintivo y no requiere
un LLM para reconocerse — igual que las señales rápidas (`XAUUSD BUY NOW`)
ya se reconocen con un regex en `parsers_tradepulse.py`. Para ese patrón,
`router_parser` ejecuta la acción directamente contra `/mgmt/action` y **no
reenvía el mensaje a n8n en absoluto**.

Excluir n8n no es una optimización de latencia: es lo que elimina la ventana
de carrera. Si n8n nunca ve el mensaje, nunca existe un callback tardío que
pueda llegar 97 segundos después sobre un grupo abierto mientras tanto.

Un fallback a n8n reintroduciría exactamente ese riesgo — si la llamada
directa falla y el mensaje se reenvía, la respuesta tardía puede aterrizar
sobre un grupo nuevo. Por eso el fallo del atajo se maneja con reintentos y
una notificación al operador, nunca delegando a n8n.

El filtro por dirección se agrega como protección independiente: aplica al
atajo directo (que siempre extrae la dirección del texto) y queda disponible
para el flujo n8n, que sigue existiendo sin cambios para todos los demás
mensajes de gestión.

### Alternativa descartada: snapshot de grupos por `trace_id`

Se consideró capturar los `group_ids` activos en el instante en que llega el
mensaje, guardarlos indexados por un `trace_id` que viajara hasta n8n y
volviera en su callback, para que `apply_mgmt_action` filtrara contra ese
snapshot. Requería: un stream nuevo (`MGMT_CANDIDATES`), estado con TTL en
`TradeManager`, un consumer loop adicional, `trace_id` en el contrato de
`/mgmt/action`, y modificar el workflow de n8n para devolverlo intacto.

Se descartó porque toda esa maquinaria protege una ruta que, al excluir n8n
del patrón `close_now`, deja de recorrerse. Los demás mensajes de gestión que
sí pasan por n8n (correcciones de señal, BE, cierres parciales) no presentan
el mismo riesgo: no cierran posiciones enteras de grupos que no nombran.

## 3. Alcance

**Dentro de alcance:**
- Reconocimiento por regex del patrón `TRADE INVALID ... Close now` en
  `router_parser`, con extracción de la dirección (`BUY`/`SELL`) del texto.
- Llamada HTTP directa a `POST /mgmt/action` de `trade_orchestrator`, con
  reintentos, sin pasar por n8n.
- Notificación al operador cuando el atajo agota sus reintentos.
- Parámetro `direction_hint` en `apply_mgmt_action` y en
  `MgmtActionRequest`, que filtra los grupos por dirección.
- Notificación de los grupos excluidos por ese filtro.

**Fuera de alcance:**
- Cualquier otro patrón de gestión (BE, cierres parciales, correcciones de
  señal) — siguen yendo por n8n/Ollama sin cambios.
- La latencia del pipeline de n8n en sí (por qué tardó 97s: cola, cold start
  de Ollama, reintentos). No se investigó; n8n corre fuera de este VPS
  (`workflows.ysalabs.work`) y sus logs no son accesibles desde aquí.
- Multi-símbolo. El sistema sigue operando solo XAUUSD; `direction_hint`
  filtra por dirección, no por símbolo.

## 4. Reconocimiento del patrón

Módulo nuevo `services/router_parser/parsers_management.py` — separado de
`parsers_tradepulse.py` porque no produce un `ParseResult` de señal: no es
una apertura, es una acción de gestión.

```python
CLOSE_NOW_RE = re.compile(r'TRADE\s+INVALID.*?CLOSE\s+NOW', re.IGNORECASE | re.DOTALL)
DIRECTION_RE = re.compile(r'\b(BUY|SELL)\b', re.IGNORECASE)
```

`CLOSE_NOW_RE` exige ambas frases: `TRADE INVALID` y `CLOSE NOW`, en ese
orden, con cualquier contenido entre medio (`DOTALL` cubre los saltos de
línea del mensaje real, que trae `❌\n\n` entre las dos partes). La decisión
de exigir ambas — en vez de anclar solo en `Close now` — es deliberada: un
"close now" suelto en otro contexto no debe disparar un cierre total sin
que un clasificador lo evalúe.

`DIRECTION_RE` toma la **primera** ocurrencia de `BUY` o `SELL` en el texto.
En el mensaje real (`XAUUSD SELL TRADE INVALID ❌ / Close now`) eso da
`SELL`. Si no hay ninguna, `direction_hint` viaja como `None` y el cierre
aplica a todos los grupos activos del chat (comportamiento actual).

### Función de reconocimiento

```python
def match_close_now(text: str) -> Optional[dict]:
    """
    Retorna {"action": "close_now", "direction_hint": "SELL"|"BUY"|None} si
    el texto es una orden de cierre total reconocible sin LLM, o None si no
    lo es (el texto sigue su curso normal hacia n8n).
    """
```

## 5. Flujo en `router_parser`

El loop principal (`services/router_parser/app.py:161-177`) ya distingue tres
casos. Se agrega un cuarto, hermano de los existentes:

```
sig is DUPLICATE_SIGNAL  → ya procesado, no reenviar a n8n        (existente)
sig                      → señal válida, publicar a SIGNALS        (existente)
match_close_now(text)    → POST directo a /mgmt/action, NO a n8n   (nuevo)
text.strip()             → no reconocido, reenviar a n8n           (existente)
```

La rama nueva va **después** de `process_raw_signal` y **antes** de
`forward_to_n8n`: un texto que ya fue reconocido como señal de apertura
nunca debe evaluarse como orden de cierre.

El precedente de no reenviar a n8n ya existe en el mismo bloque: el caso
`DUPLICATE_SIGNAL` (líneas 162-165) tiene el comentario *"ya se proceso la
primera vez, no reenviar a n8n como si fuera texto no reconocido"*. Un
mensaje que el atajo ya ejecutó está en la misma situación.

### Llamada directa

```python
POST {TRADE_ORCHESTRATOR_MGMT_URL}
Headers: X-N8N-Action-Key: {N8N_ACTION_API_KEY}
Body: {
    "action": "close_now",
    "chat_id": chat_id,
    "raw_text": text,
    "direction_hint": "SELL"   # o ausente si no se pudo extraer
}
```

Se reutiliza `N8N_ACTION_API_KEY` — es la clave que ya protege ese endpoint;
introducir una segunda credencial para el mismo endpoint no agrega
seguridad y sí una variable más que mantener sincronizada.

### Configuración nueva

`TRADE_ORCHESTRATOR_MGMT_URL` (ej. `http://trade_orchestrator:8000/mgmt/action`),
en `services/common/config.py`, `.env.example` y `docker-compose`. Sin ella
configurada, el atajo no puede operar: se registra un error y el mensaje se
reenvía a n8n como antes (degradación explícita, no silenciosa — sin esta
variable el sistema se comporta como hoy, con su ventana de carrera, pero
al menos el mensaje no se pierde).

Esta es la única situación en que un mensaje que matchea el patrón llega a
n8n, y ocurre por configuración ausente, no por un fallo en tiempo de
ejecución.

### Reintentos y fallo

Tres intentos con backoff 1s / 2s / 4s. Se reintenta ante error de red,
timeout, y respuestas 5xx. **No** se reintenta ante 4xx (401 por clave mal
configurada, 422 por payload inválido): son errores de configuración que un
reintento no resuelve.

Si los tres intentos fallan, o ante un 4xx, `router_parser` emite una
notificación directa al operador por `N8N_EVENT_WEBHOOK_URL` — el webhook de
eventos que ya alimenta los mensajes de Telegram, independiente de
`trade_orchestrator`, así que sigue disponible aunque ese servicio esté
caído:

```
🚨 CIERRE AUTOMÁTICO FALLIDO — Canal: {chat_id}
Motivo: "{raw_text}"
No se pudo ejecutar el cierre tras 3 intentos: {último error}
REVISAR LA CUENTA MANUALMENTE — las posiciones pueden seguir abiertas.
```

El mensaje **no** cae a n8n. Si `trade_orchestrator` está caído, n8n tampoco
podría cerrar nada: llama al mismo endpoint. Lo único que agregaría el
fallback es la posibilidad de un callback tardío sobre un grupo nuevo —
precisamente el bug que este diseño elimina.

## 6. Filtro por dirección en `apply_mgmt_action`

`MgmtActionRequest` (`services/trade_orchestrator/mgmt_api.py`) agrega:

```python
direction_hint: Optional[str] = Field(default=None, pattern="^(BUY|SELL)$")
```

Validado en el borde por el mismo motivo que `percent` ya lo está: el valor
puede venir de una extracción sobre texto libre. Un valor fuera de
`{BUY, SELL}` se rechaza con 422 antes de tocar posiciones.

`apply_mgmt_action` agrega el kwarg `direction_hint: Optional[str] = None` y
lo aplica **una sola vez**, justo después de resolver `group_ids` y antes
del switch por acción:

```python
group_ids = self.find_active_groups_for_chat(chat_id)
if direction_hint:
    group_ids, excluded = self._filter_groups_by_direction(group_ids, direction_hint)
```

Filtrar ahí — y no dentro de cada rama — hace que toda acción se beneficie
sin duplicar lógica, y mantiene una sola definición de "qué grupos toca esta
acción".

`_filter_groups_by_direction` compara contra `ManagedTrade.direction`
(`trade_manager.py:50`). Las dos piernas de un grupo comparten dirección,
así que basta inspeccionar cualquiera de ellas.

### Notificación de grupos excluidos

Si el filtro excluye al menos un grupo, se emite una vez por request:

```python
await self._notify(
    "mgmt_direction_filtered",
    channel="audit",
    chat_id=chat_id,
    direction_hint=direction_hint,
    excluded_group_ids=excluded,
    action=action,
    message=(f"Acción '{action}' limitada a grupos {direction_hint}. "
             f"Grupos excluidos por dirección opuesta: {excluded}."),
)
```

Canal `audit`, no `both`: es información de por qué el sistema **no** actuó
sobre algo. Útil al reconstruir un incidente, ruido innecesario en el canal
de Telegram del operador.

### Si el filtro deja cero grupos

Se retorna `{"status": "no_active_trade"}` — el shape que ya existe para
"no hay nada sobre lo que actuar" — y se emite `mgmt_no_active_trade` como
hoy. El caso es real: un `SELL TRADE INVALID` cuando solo hay un BUY activo
significa que el mensaje no aplica a nada abierto.

## 7. Comportamiento sobre el incidente del 2026-09-17

Con este diseño, la misma secuencia de mensajes produce:

| Hora | Evento | Resultado |
|---|---|---|
| 05:36:21.861 | Llega `XAUUSD SELL TRADE INVALID / Close now` | `CLOSE_NOW_RE` matchea, `direction_hint="SELL"` |
| ~05:36:22 | POST directo a `/mgmt/action` | Grupo 149 (SELL) cerrado en ~1s |
| — | n8n | **Nunca recibe el mensaje** |
| 05:37:20 | Se abre el grupo 150 (BUY) | Intacto |
| 05:37:58 | *(no ocurre nada: no hay callback)* | Grupo 150 sigue abierto |

Las dos protecciones son independientes y cada una habría bastado por sí
sola: el atajo cierra el 149 antes de que el 150 exista, y el
`direction_hint="SELL"` habría excluido al 150 (BUY) aunque el cierre
hubiera llegado tarde.

## 8. Testing

**`services/router_parser/test_app.py`:**
- `CLOSE_NOW_RE` matchea el mensaje real completo
  (`XAUUSD SELL TRADE INVALID ❌\n\nClose now`).
- No matchea: solo `Close now`; solo `TRADE INVALID`; `Close now` antes de
  `TRADE INVALID` (orden invertido).
- `direction_hint` se extrae como `SELL` del mensaje real, como `BUY` de su
  variante, y como `None` cuando el texto no nombra dirección.
- Un texto que ya parsea como señal de apertura no se evalúa como cierre.
- El atajo hace POST con el payload y el header esperados.
- El atajo **no** llama a `forward_to_n8n` cuando la acción se ejecuta bien.
- Reintentos: falla 5xx dos veces y acierta a la tercera → una sola acción
  ejecutada, sin notificación de fallo.
- Tres fallos → notificación a `N8N_EVENT_WEBHOOK_URL`, y `forward_to_n8n`
  nunca se llama.
- Un 4xx no se reintenta; notifica de inmediato.
- Sin `TRADE_ORCHESTRATOR_MGMT_URL` configurada → se reenvía a n8n y se
  registra el error.

**`services/trade_orchestrator/test_mgmt_action_endpoint.py`:**
- `direction_hint` ausente → comportamiento actual, sin cambios.
- `direction_hint="SELL"` / `"BUY"` → aceptados y propagados.
- `direction_hint="sell"` (minúsculas) y `"LONG"` → 422.

**`services/trade_orchestrator/test_trade_manager_dual_tp.py`:**
- **Reproducción del incidente:** grupo 149 SELL y grupo 150 BUY activos en
  el mismo `chat_id`; `close_now` con `direction_hint="SELL"` cierra solo el
  149 y deja el 150 intacto.
- El mismo caso sin `direction_hint` cierra ambos (el comportamiento actual
  sigue siendo el default).
- El filtro emite `mgmt_direction_filtered` con los `group_ids` excluidos.
- Filtro que deja cero grupos → `no_active_trade`, sin tocar MT5.
- El filtro aplica también a `move_sl_be_now` (al vivir antes del switch).

## 9. Retrocompatibilidad

- `direction_hint` es opcional en ambas capas. Un callback de n8n que no lo
  mande se comporta exactamente como hoy.
- El workflow de n8n **no requiere cambios**. Deja de recibir los mensajes
  que matchean el patrón; los demás siguen igual.
- Ningún cambio en `ManagedTrade`, en la persistencia, ni en
  `find_active_groups_for_chat`.

### Consecuencia consciente

Los mensajes que matchean el patrón dejan de aparecer en cualquier registro
que n8n lleve de sus clasificaciones. La trazabilidad se conserva en el
audit log de `trade_orchestrator` (evento `mgmt_close_now` con su
`raw_text`) y en las notificaciones de Telegram. Se evaluó mandar a n8n una
copia marcada `already_handled: true` para preservar ese historial y se
descartó: dependería de que el workflow respetara el flag, y un cambio
futuro que lo ignorara reintroduciría el callback tardío en silencio.
