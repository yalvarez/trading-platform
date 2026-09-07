# Diseño: Ámbito de `/mgmt/action` por `chat_id`, no por `symbol`

Fecha: 2026-09-08
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

## 1. Contexto y problema

`POST /mgmt/action` (el endpoint que n8n/Ollama llama tras clasificar un
mensaje de gestión de Telegram) exige hoy un campo `symbol` en el body
(`MgmtActionRequest.symbol: str`, `services/trade_orchestrator/mgmt_api.py`).
`apply_mgmt_action` usa ese `symbol` para resolver, vía
`find_active_group_for_symbol`, **el grupo más reciente** de ese símbolo, y
le aplica la acción solo a ese grupo.

Esto se probó contra el flujo real de n8n/Ollama (2026-09-08) y reveló dos
problemas:

1. **`symbol` no aporta nada real hoy.** El sistema opera un solo símbolo
   (XAUUSD); pedirle a Ollama que lo infiera y lo incluya es trabajo sin
   beneficio — y en la práctica Ollama lo está mandando como `null`,
   rompiendo la validación de Pydantic antes de que la solicitud llegue a
   ejecutarse.
2. **La regla "solo el grupo más reciente" es demasiado estrecha.** Cuando
   un canal de Telegram tiene más de un grupo activo a la vez (p. ej. una
   reapertura tras `REOPEN_COOLDOWN_SECONDS`), un mensaje de gestión de ese
   mismo canal debería, en general, aplicar a **todo lo que ese canal tenga
   abierto** — no solo a la apertura más nueva.

Lo que sí importa para decidir a qué posiciones aplicar una acción es **de
qué canal de Telegram vino el mensaje de gestión** — y ese dato (`chat_id`)
ya viaja hasta `trade_orchestrator` sin usarse: `router_parser` ya lo
agrega a cada señal publicada en `Streams.SIGNALS`
(`services/router_parser/app.py:168`, `sig["chat_id"] = chat_id`), pero
`trade_orchestrator`'s `handle_signal_fields`
(`services/trade_orchestrator/app.py:20-103`) nunca lo lee ni lo propaga a
`open_group`/`ManagedTrade`.

## 2. Alcance

**Dentro de alcance:**
- Agregar `chat_id` a `ManagedTrade`, `open_group`, la persistencia
  (Redis + archivo), y `reconcile_from_mt5`.
- Reemplazar `find_active_group_for_symbol(symbol) -> Optional[int]` por
  `find_active_groups_for_chat(chat_id) -> list[int]` (todos los grupos
  activos de ese `chat_id`, ordenados de más antiguo a más reciente).
- Cambiar el contrato de `/mgmt/action`: `MgmtActionRequest.symbol: str` →
  `MgmtActionRequest.chat_id: str`.
- Redefinir el comportamiento de cada acción de `apply_mgmt_action` sobre
  múltiples grupos (ver §5).
- Trato explícito de grupos "huérfanos de `chat_id`" (abiertos antes de
  este cambio, o reconciliados en modo degradado sin ese dato).

**Fuera de alcance (explícitamente):**
- Multi-símbolo real — el sistema sigue operando XAUUSD únicamente; este
  cambio no introduce soporte para varios símbolos simultáneos, solo deja
  de depender de `symbol` para resolver el ámbito de gestión.
- Cambios al flujo de n8n/Ollama en sí (el prompt que clasifica el mensaje
  y arma el JSON) — eso lo ajusta el usuario directamente en su instancia
  de n8n; este spec solo define el contrato que `trade_orchestrator`
  expone y exige.
- Migración retroactiva de documentos ya persistidos sin `chat_id` — se
  tratan como huérfanos, no se intenta inferir su `chat_id` de ninguna
  fuente.

## 3. Modelo de datos

`ManagedTrade` (`services/trade_orchestrator/trade_manager.py:20-33`)
agrega un campo:

```python
chat_id: Optional[str] = None
```

`open_group` agrega un parámetro `chat_id: Optional[str] = None` (default
`None` para no romper llamadas existentes — ver §7), y lo asigna a cada
`ManagedTrade` que crea (ambas piernas del grupo comparten el mismo
`chat_id`, igual que ya comparten `group_id`).

`handle_signal_fields` (`services/trade_orchestrator/app.py`) lee
`fields.get("chat_id")` (ya presente en el dict, actualmente ignorado) y lo
pasa a cada llamada de `open_group`.

## 4. Persistencia

`_group_doc` (`trade_manager.py`) agrega `"chat_id": first.chat_id` al
documento que ya arma para Redis/archivo — cambio aditivo, no rompe la
lectura de documentos previos (que simplemente no tendrán esa clave).

`reconcile_from_mt5`: un grupo reconstruido desde un documento del store
(Redis o archivo) hereda su `chat_id` si el documento lo tiene. Un grupo
reconstruido en **modo degradado** (sin documento en ningún lado, solo
desde los datos vivos de la posición en MT5) queda con `chat_id=None` — MT5
no tiene forma de saber de qué canal vino la señal originalmente.

## 5. Resolución y comportamiento por acción

Nuevo método:

```python
def find_active_groups_for_chat(self, chat_id: str) -> list[int]:
    """
    Todos los group_id con al menos una pierna activa cuyo chat_id
    coincide exactamente con `chat_id`. Un grupo con chat_id=None
    (huérfano — legacy o reconciliado en modo degradado) NUNCA aparece
    aquí, sin importar qué chat_id se consulte: no hay gestión
    automática para un grupo cuyo canal de origen no se conoce con
    certeza. Ordenado de más antiguo a más reciente (por opened_ts,
    luego group_id como desempate — mismo criterio que
    find_active_group_for_symbol ya usaba).
    """
```

`apply_mgmt_action(*, action: str, chat_id: str, raw_text: str, correction: Optional[dict]) -> dict`
(firma actualizada: `symbol` → `chat_id`):

**Aislamiento por grupo (obligatorio para `close_now` y `move_sl_be_now`):**
cada iteración del loop sobre `group_id` — incluida la resolución de la
cuenta de ese grupo específico (ver nota de cuentas más abajo) — va
envuelta en su propio `try/except Exception`. Una excepción real (no solo
un resultado con `retcode` fallido — p. ej. un timeout de red, una
excepción de `_call`) en el procesamiento de un `group_id` se captura ahí
mismo, se registra en el resultado de ESE grupo como
`{"group_id": N, "status": "failed", "reason": "exception"}`, y el loop
sigue con el siguiente `group_id`. Sin esto, una excepción no capturada en
el primer grupo abortaría todo el método antes de reportar los grupos ya
procesados — perdiendo exactamente la granularidad por-grupo que este
cambio busca preservar.

**Resolución de cuenta por grupo:** el código actual resuelve la cuenta una
sola vez antes del switch de acciones
(`account = self._ensure_account_dict(legs[0].account_name)`, asumiendo un
único grupo). Con múltiples `group_id` bajo el mismo `chat_id`, la cuenta
se resuelve **dentro de cada iteración**, a partir de las `legs` de ESE
`group_id` — no de un `legs[0]` global. Hoy esto es un no-op observable
(una sola cuenta activa en `ACCOUNTS_JSON`), pero deja el código correcto
si en el futuro se activa una segunda cuenta y dos grupos del mismo
`chat_id` terminan en cuentas distintas.

- **`close_now`**: itera `find_active_groups_for_chat(chat_id)` en orden.
  Por cada `group_id`, resuelve su cuenta y sus `legs` propias, e intenta
  cerrar ambas piernas (mismo mecanismo de hoy), con el aislamiento
  por-grupo descrito arriba. Retorna:
  ```json
  {"status": "completed", "results": [
    {"group_id": 5, "status": "closed"},
    {"group_id": 7, "status": "failed"}
  ]}
  ```
- **`move_sl_be_now`**: mismo patrón de iteración con el mismo aislamiento
  por-grupo. Por cada grupo, aplica BE al runner (mismo mecanismo de hoy:
  no-op si ya está en o mejor que BE). Retorna:
  ```json
  {"status": "completed", "results": [
    {"group_id": 5, "status": "applied"},
    {"group_id": 7, "status": "already_satisfied"}
  ]}
  ```
  (`status` por grupo: `applied` | `already_satisfied` | `failed` |
  `no_active_trade` si el grupo no tiene runner; un `failed` puede llevar
  `"reason": "exception"` si el aislamiento por-grupo capturó una
  excepción real, distinto de un `retcode` fallido de MT5).
- **`note_sl_hit`**: se notifica una vez por cada `group_id` activo del
  `chat_id` (no toca MT5, aplicar a todos es seguro). Retorna
  `{"status": "noted", "group_ids": [5, 7]}`.
- **`signal_correction`**: aplica **solo al grupo más reciente** de
  `find_active_groups_for_chat(chat_id)` (último elemento de la lista
  ordenada) — una corrección de un campo de la señal (p. ej. "TP2 es
  4687") se refiere a la señal que se acaba de mandar, no a una reapertura
  anterior del mismo canal. Retorna `{"status": "applied", "group_id": N}`
  (sin cambio de forma respecto a hoy).
- **`ignore`**: sin cambios (`{"status": "ignored"}`), no depende de
  grupos.
- **Sin ningún grupo activo para ese `chat_id`** (cualquier acción excepto
  `ignore`): `{"status": "no_active_trade"}` — mismo shape que hoy.

### 5.1. Notificación de rutas de salida silenciosas

Auditoría separada (2026-09-08) de cada `await self._notify(...)` en
`trade_manager.py` encontró que los 16 llamados existentes ya incluyen
`message=` — no requieren cambios. Pero `apply_mgmt_action` tiene **5 rutas
de retorno que hoy nunca llaman a `_notify`**, por lo que un caso de falla o
ambigüedad queda invisible para n8n/el operador (solo aparece en
`docker logs`), aun cuando `raw_text` ya está disponible como parámetro en
la mayoría de estos puntos y se descarta en silencio. Este cambio agrega
`_notify` a cada una de esas 5 rutas, como parte del mismo trabajo que
redefine `apply_mgmt_action` (ambos tocan el mismo método):

1. **Ningún grupo activo para el `chat_id`** (`find_active_groups_for_chat`
   devuelve lista vacía, sección 5 arriba) — hoy solo `log.info`. Agregar:
   ```python
   await self._notify(
       event="mgmt_no_active_trade",
       message=f"Acción '{action}' recibida pero no hay trades activos para este chat. Texto: {raw_text!r}",
       chat_id=chat_id,
       action=action,
   )
   ```
2. **Cuenta no resoluble** (`account_unresolved`, dentro del loop por
   grupo — ver aislamiento por-grupo arriba) — hoy solo `log.error`. Se
   notifica por grupo, no una sola vez:
   ```python
   await self._notify(
       event="mgmt_account_unresolved",
       message=f"No se pudo resolver la cuenta del grupo {group_id} al aplicar '{action}'.",
       chat_id=chat_id,
       group_id=group_id,
       action=action,
   )
   ```
3. **`move_sl_be_now` sin runner leg** en un grupo (`no_active_trade` a
   nivel de grupo, dentro del resultado por-grupo de la sección 5) — hoy
   sin ningún aviso. Se notifica por grupo:
   ```python
   await self._notify(
       event="mgmt_no_runner_leg",
       message=f"Grupo {group_id} no tiene runner leg activo; no se pudo mover SL a BE.",
       chat_id=chat_id,
       group_id=group_id,
   )
   ```
4. **`signal_correction` con campo inválido** (`correction.get("field")`
   fuera de `("sl", "tp1", "tp2")`) — hoy sin aviso. `raw_text` está
   disponible:
   ```python
   await self._notify(
       event="mgmt_invalid_correction",
       message=f"Corrección con campo inválido ('{correction.get('field')}') para el chat. Texto: {raw_text!r}",
       chat_id=chat_id,
       correction=correction,
   )
   ```
5. **`action` desconocida** (fallthrough final) — hoy sin aviso.
   `raw_text` está disponible:
   ```python
   await self._notify(
       event="mgmt_unknown_action",
       message=f"Acción desconocida '{action}' recibida. Texto: {raw_text!r}",
       chat_id=chat_id,
       action=action,
   )
   ```

Ninguno de estos 5 casos cambia el `dict` que `apply_mgmt_action` retorna
(los shapes ya definidos en la sección 5 no se alteran) — el cambio es
exclusivamente que cada ruta, antes de retornar, ahora también llama a
`_notify` con el mismo criterio (nombre de evento + `message` legible +
contexto estructurado) que ya usan las 16 llamadas existentes.

## 6. Contrato de `/mgmt/action`

`MgmtActionRequest` (`services/trade_orchestrator/mgmt_api.py`):

```python
class MgmtActionRequest(BaseModel):
    action: str
    chat_id: str
    raw_text: str
    correction: Optional[Correction] = None
```

(`symbol: str` eliminado). El `chat_id` que n8n/Ollama debe mandar es el
mismo que Telethon expone para el mensaje entrante — dato directo, sin
inferencia, a diferencia de `symbol`.

## 7. Retrocompatibilidad

- `open_group(..., chat_id: Optional[str] = None)`: default `None`
  preserva las llamadas existentes en tests
  (`services/trade_orchestrator/test_trade_manager_dual_tp.py` y otros)
  sin tocarlas — un grupo abierto sin `chat_id` explícito queda huérfano
  de gestión automática, consistente con el trato general de huérfanos.
- Documentos persistidos antes de este cambio (sin la clave `chat_id`) se
  leen igual; al reconstruir el `ManagedTrade`, el campo ausente resuelve
  a `None` (huérfano).
- La suite e2e (`tests/e2e/scenarios/b*.py`) no requiere cambios de código
  — ya usa `TG_TEST_CHAT_ID` como el canal que envía sus mensajes de
  prueba; ese es exactamente el valor que el n8n/Ollama real debe extraer
  y mandar como `chat_id` en el callback. El ajuste pendiente es del lado
  del flujo de n8n (fuera de este repo), ya en curso por el usuario.
- `mgmt_api.py`'s docstring de módulo (líneas 3-8 actuales) describe el
  endpoint como resolviendo "el grupo activo por símbolo" — se actualiza
  para reflejar la resolución por `chat_id`, evitando que quede
  describiendo el mecanismo viejo.

## 8. Testing

- Tests unitarios existentes de `apply_mgmt_action` (`test_mgmt_action_endpoint.py`,
  y los de `find_active_group_for_symbol` en `test_trade_manager_dual_tp.py`)
  deben actualizarse a la nueva firma (`chat_id` en vez de `symbol`).
- Casos nuevos a cubrir:
  - Un `chat_id` con 2 grupos activos: `close_now` cierra ambos, reporta
    ambos resultados.
  - Un `chat_id` con 2 grupos, uno falla al cerrar (resultado con
    `retcode` fallido, no excepción): el otro se cierra igual, el
    resultado combinado refleja ambos estados.
  - Un `chat_id` con 2 grupos, uno lanza una excepción real durante su
    procesamiento (no solo un `retcode` fallido — p. ej. `_call` propaga
    una excepción): el aislamiento por-grupo la captura, ese grupo queda
    `{"status": "failed", "reason": "exception"}`, y el otro grupo se
    procesa y reporta con normalidad.
  - Un `chat_id` con 2 grupos: `move_sl_be_now` aplica a ambos con estados
    mixtos (uno `applied`, otro `already_satisfied`).
  - `signal_correction` con 2 grupos del mismo `chat_id`: solo el más
    reciente recibe la corrección; el otro queda intacto.
  - Un grupo con `chat_id=None` (legacy/degradado): ninguna acción de
    `/mgmt/action` lo afecta, para ningún `chat_id` que se consulte.
  - `find_active_groups_for_chat` con cero grupos para ese `chat_id`:
    lista vacía, `apply_mgmt_action` retorna `no_active_trade`.
  - `reconcile_from_mt5`: un documento del store con `chat_id` lo hereda
    correctamente al `ManagedTrade` reconstruido; uno sin esa clave (o
    reconstruido en modo degradado) resuelve a `None`.
  - Ningún grupo activo para el `chat_id`: `apply_mgmt_action` llama a
    `_notify` con `event="mgmt_no_active_trade"` y un `message` que incluye
    el `raw_text` recibido, además de retornar `{"status": "no_active_trade"}`.
  - Cuenta no resoluble en un grupo (`account_unresolved`): `_notify` se
    llama con `event="mgmt_account_unresolved"` y el `group_id` afectado;
    si hay más de un grupo bajo el mismo `chat_id`, se llama una vez por
    cada grupo cuya cuenta falla, no una sola vez para todo el request.
  - `move_sl_be_now` sobre un grupo sin runner leg: `_notify` se llama con
    `event="mgmt_no_runner_leg"` y ese `group_id`, además de que el
    resultado de ESE grupo en la lista `results` sea `no_active_trade`
    (los demás grupos del mismo request no se ven afectados).
  - `signal_correction` con `correction.field` fuera de
    `("sl", "tp1", "tp2")`: `_notify` se llama con
    `event="mgmt_invalid_correction"` y un `message` que incluye el campo
    inválido recibido, además de retornar `{"status": "invalid_correction"}`.
  - `action` no reconocida: `_notify` se llama con
    `event="mgmt_unknown_action"` y un `message` que incluye el `raw_text`
    recibido, además de retornar `{"status": "unknown_action"}`.
