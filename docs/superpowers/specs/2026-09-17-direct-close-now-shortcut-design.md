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
  `MgmtActionRequest`, que filtra los grupos por dirección **en la rama
  `close_now` únicamente** (ver §6 para por qué no en las demás).
- Notificación de los grupos excluidos por ese filtro.
- `N8N_ACTION_API_KEY` pasa a ser requerida por `validate_router_parser()`.

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

`direction_hint` se normaliza con `.upper()` antes de enviarse.
`DIRECTION_RE` es case-insensitive y puede capturar `buy` en minúsculas
(p. ej. `TRADE INVALID ... close now the buy position`), que el validador
del endpoint rechazaría con 422.

Se reutiliza `N8N_ACTION_API_KEY` — es la clave que ya protege ese endpoint;
introducir una segunda credencial para el mismo endpoint no agrega
seguridad y sí una variable más que mantener sincronizada.

### Configuración nueva

`TRADE_ORCHESTRATOR_MGMT_URL`, con valor
`http://trade_orchestrator:8200/mgmt/action` — el puerto es el que
`app.py:205` levanta (`MGMT_API_PORT`, default 8200) y que
`docker-compose.yml` publica como `8200:8200`; `trade_orchestrator` es el
nombre de servicio en compose, resoluble por DNS en la red default.

La variable se agrega a `.env` / `.env.example` y se lee con
`config.get("TRADE_ORCHESTRATOR_MGMT_URL", "")`. No hay nada que declarar en
`services/common/config.py`: `ConfigProvider.get` lee `os.environ`
directamente.

`router_parser` ya recibe `N8N_ACTION_API_KEY` sin cambios de compose —
ambos servicios cargan el mismo `env_file: .env`. Pero
`validate_router_parser()` (`services/common/env_validator.py`) hoy solo
exige `REDIS_URL` y `DEDUP_TTL_SECONDS`: se le agrega `N8N_ACTION_API_KEY`
como requerida, para que una clave ausente falle al arrancar y no en el
primer cierre real con un 401.

Sin `TRADE_ORCHESTRATOR_MGMT_URL` configurada, el atajo no puede operar: se
registra un error y el mensaje se reenvía a n8n como antes (degradación
explícita, no silenciosa — sin esta variable el sistema se comporta como
hoy, con su ventana de carrera, pero al menos el mensaje no se pierde).

Esta es la única situación en que un mensaje que matchea el patrón llega a
n8n, y ocurre por configuración ausente, no por un fallo en tiempo de
ejecución.

### Reintentos y fallo

Tres intentos con backoff 1s / 2s / 4s. Se reintenta ante error de red,
timeout, y respuestas 5xx. **No** se reintenta ante 4xx (401 por clave mal
configurada, 422 por payload inválido): son errores de configuración que un
reintento no resuelve.

Si los tres intentos fallan, o ante un 4xx, `router_parser` encola una
notificación para el operador llamando a
`services.trade_orchestrator.n8n_retry_worker.enqueue(redis_client, envelope)`
con un envelope de la misma forma que arma `EventBus.emit`:

```python
{
    "event_id": str(uuid.uuid4()),
    "event_type": "mgmt_direct_close_failed",
    "channel": "both",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "message": (f"🚨 CIERRE AUTOMÁTICO FALLIDO — Canal: {chat_id}\n"
                f"Motivo: \"{raw_text}\"\n"
                f"No se pudo ejecutar el cierre tras 3 intentos: {last_error}\n"
                f"REVISAR LA CUENTA MANUALMENTE — las posiciones pueden seguir abiertas."),
    "payload": {"chat_id": chat_id, "raw_text": raw_text,
                "direction_hint": direction_hint, "error": last_error},
}
```

`enqueue` solo necesita el cliente Redis que `router_parser` ya tiene
abierto, y el worker que drena la cola (`run_retry_worker`, lanzado en
`trade_orchestrator/app.py:222`) aporta backoff y dead-letter sin código
nuevo.

**Limitación consciente:** el worker vive en `trade_orchestrator`. Si ese
servicio está caído —el caso más probable de fallo del atajo— la
notificación espera encolada hasta que vuelva. Se aceptó a cambio de no
duplicar la lógica de entrega ni montar `./data` en `router_parser` (que
hoy no lo monta, y sin ese volumen no puede escribir el audit log local que
`EventBus` escribe antes de encolar). El evento queda en Redis, no se
pierde; llega tarde.

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

`apply_mgmt_action` agrega el kwarg `direction_hint: Optional[str] = None`.
El filtro se aplica **dentro de la rama `close_now` únicamente**, sobre su
propia copia de `group_ids`:

```python
if action == "close_now":
    if direction_hint:
        group_ids, excluded = self._filter_groups_by_direction(group_ids, direction_hint)
        ...
```

`_filter_groups_by_direction` compara contra `ManagedTrade.direction`
(`trade_manager.py:50`). Las dos piernas de un grupo comparten dirección,
así que basta inspeccionar cualquiera de ellas.

### Por qué solo `close_now`, y no antes del switch

La versión inicial de este diseño aplicaba el filtro una sola vez antes del
switch por acción, para que toda acción se beneficiara sin duplicar lógica.
Revisar el código descartó esa opción por dos motivos concretos:

1. **`signal_correction` usa `group_ids[-1]`** — el grupo más reciente — por
   decisión explícita del spec de chat_id-scoping (§5): una corrección de un
   campo se refiere a la señal recién mandada. Filtrar antes del switch
   cambiaría silenciosamente a qué grupo aplica una corrección.

2. **`group_ids[-1]` sobre una lista vacía lanza `IndexError`.** El guard de
   lista vacía está en `trade_manager.py:1306`, *antes* del punto donde iría
   el filtro; un filtro que vaciara la lista después de ese guard llegaría a
   `signal_correction` sin protección. El `IndexError` lo capturaría el
   `except Exception` genérico de `mgmt_api.py:65`, devolviendo
   `internal_error` sin notificar al operador.

`move_sl_be_now` queda **deliberadamente sin filtrar** en este cambio: sigue
aplicando BE a todos los grupos activos del chat, sin importar la dirección
que el mensaje nombre. Es el mismo defecto latente que causó el incidente
del 149/150, pero su consecuencia es acotada —mover un SL a breakeven
protege capital; no cierra una posición ni realiza una pérdida— y el atajo
directo no genera esta acción. Si un mensaje de BE mal dirigido llegara a
causar un problema real, extender el filtro a esta rama es un cambio de dos
líneas sobre el helper que este spec ya introduce.

`note_sl_hit` (solo notifica) e `ignore` (no toca grupos) tampoco se filtran.

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

Dentro de `close_now`, se retorna `{"status": "no_active_trade"}` — el shape
que ya existe para "no hay nada sobre lo que actuar" — sin entrar al loop de
cierre. El caso es real: un `SELL TRADE INVALID` cuando solo hay un BUY
activo significa que el mensaje no aplica a nada abierto.

Como el filtro vive dentro de la rama, el guard de `trade_manager.py:1306`
sigue cubriendo su caso original (cero grupos para el chat) y ninguna otra
acción puede recibir una lista vaciada por el filtro.

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
sola para este incidente: el atajo cierra el 149 antes de que el 150 exista,
y el `direction_hint="SELL"` habría excluido al 150 (BUY) aunque el cierre
hubiera llegado tarde.

No son redundantes, porque cubren fallos distintos. El atajo protege contra
la latencia pero solo actúa sobre el patrón que su regex reconoce: un
mensaje de cierre con otra redacción sigue yendo por n8n, con su ventana de
carrera intacta. El `direction_hint` protege contra el alcance equivocado
pero solo cuando el texto nombra una dirección y llega por una ruta que lo
propague — hoy, únicamente el atajo. Un `close_now` clasificado por n8n
sobre un mensaje sin dirección explícita sigue cerrando todos los grupos
activos del chat, como hoy.

## 8. Testing

**`services/router_parser/test_app.py`:**
- `CLOSE_NOW_RE` matchea el mensaje real completo
  (`XAUUSD SELL TRADE INVALID ❌\n\nClose now`).
- No matchea: solo `Close now`; solo `TRADE INVALID`; `Close now` antes de
  `TRADE INVALID` (orden invertido).
- `direction_hint` se extrae como `SELL` del mensaje real, como `BUY` de su
  variante, y como `None` cuando el texto no nombra dirección.
- Una dirección en minúsculas en el texto se envía normalizada a mayúsculas.
- Un texto que ya parsea como señal de apertura no se evalúa como cierre.
- El atajo hace POST con el payload y el header esperados.
- El atajo **no** llama a `forward_to_n8n` cuando la acción se ejecuta bien.
- Reintentos: falla 5xx dos veces y acierta a la tercera → una sola acción
  ejecutada, sin notificación de fallo.
- Tres fallos → se encola un envelope `mgmt_direct_close_failed` en
  `n8n_event_retry_queue`, y `forward_to_n8n` nunca se llama.
- Un 4xx no se reintenta; encola la notificación de inmediato.
- Sin `TRADE_ORCHESTRATOR_MGMT_URL` configurada → se reenvía a n8n y se
  registra el error.

**`services/common/test_env_validator.py`** (o el archivo que cubra el
validador):
- `validate_router_parser()` falla si falta `N8N_ACTION_API_KEY`.

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
- **`signal_correction` con `direction_hint` presente sigue aplicando a
  `group_ids[-1]` sin filtrar** — dos grupos de direcciones opuestas, una
  corrección con `direction_hint="SELL"` aplica al más reciente aunque sea
  BUY. Protege contra que un refactor futuro mueva el filtro antes del
  switch y cambie esta semántica en silencio.
- `move_sl_be_now` con `direction_hint` presente aplica BE a todos los
  grupos del chat, sin filtrar (comportamiento declarado en §6).

## 9. Retrocompatibilidad

- `direction_hint` es opcional en ambas capas. Un callback de n8n que no lo
  mande se comporta exactamente como hoy.
- El workflow de n8n **no requiere cambios**. Deja de recibir los mensajes
  que matchean el patrón; los demás siguen igual.
- Ningún cambio en `ManagedTrade`, en la persistencia, ni en
  `find_active_groups_for_chat`.
- **`router_parser` no arrancará sin `N8N_ACTION_API_KEY`** tras el cambio a
  `validate_router_parser()`. La variable ya existe en `.env` (la exige
  `validate_trade_orchestrator()` desde antes) y ambos servicios cargan el
  mismo `env_file`, así que en el despliegue actual no hace falta agregarla
  — pero un entorno que corra `router_parser` aislado sí deberá definirla.

### Consecuencia consciente

Los mensajes que matchean el patrón dejan de aparecer en cualquier registro
que n8n lleve de sus clasificaciones. La trazabilidad se conserva en el
audit log de `trade_orchestrator` (evento `mgmt_close_now` con su
`raw_text`) y en las notificaciones de Telegram. Se evaluó mandar a n8n una
copia marcada `already_handled: true` para preservar ese historial y se
descartó: dependería de que el workflow respetara el flag, y un cambio
futuro que lo ignorara reintroduciría el callback tardío en silencio.
