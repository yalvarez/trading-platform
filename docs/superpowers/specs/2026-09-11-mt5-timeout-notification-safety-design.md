# Diseño: proteger notificaciones de negocio contra timeouts de `order_send`

Fecha: 2026-09-11
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

## 1. Contexto y problema

Investigando por qué no llegó a Telegram la notificación de `tp1_hit` del
grupo real 122 (desplegado el mismo día), se confirmó con el historial real
de deals de MT5 (`history_deals_get`) que **el TP1 sí ocurrió realmente**:
el ticket `1994769244` (pierna `tp1`) cerró con `reason=5`
(`DEAL_REASON_TP`), `price=4371.22` (exactamente el `tp1_price` planeado),
`profit=+8.22`. Pero el evento `tp1_hit` nunca se generó — no aparece en
`data/audit_log.jsonl`, no llegó a n8n, no llegó a Telegram.

Los logs de producción muestran la causa exacta:

```
14:33:10,706 ERROR [TM] MT5 call order_send colgada tras 10s (timeout) — abortando esta operacion, el hilo puede seguir vivo en 2do plano
14:33:10,708 ERROR [TM] error gestionando cuenta Ysaias Vantage:
```

Mecanismo confirmado leyendo el código actual
(`services/trade_orchestrator/trade_manager.py`):

1. `_tick_once_account` (líneas 555-640) detecta que el ticket `tp1`
   desapareció de `positions_get`, lo saca de `self.trades` (línea 568,
   **antes** de cualquier otra cosa), y llama a `_classify_leg_closure`
   (línea 580), que correctamente clasifica la causa como `"tp1"` usando
   `deal.reason` real.
2. `_on_tp1_leg_closed` (línea 682) se invoca. Llama a `_force_runner_sl`
   (línea 700) para mover el runner a breakeven — este `order_send` es el
   que se cuelga.
3. `_force_runner_sl` (línea 789) llama a `self._call(client.order_send,
   req)` dentro de su loop de reintentos (línea 810), sin ningún
   try/except propio. `_call` (línea 151) envuelve la llamada en
   `asyncio.wait_for(..., timeout=MT5_CALL_TIMEOUT_SECONDS)`; al vencer el
   timeout, loguea el error y **relanza** `asyncio.TimeoutError` sin
   capturarla (línea 189: `raise`).
4. La excepción se propaga sin interceptar por `_force_runner_sl` →
   `_on_tp1_leg_closed` → `_tick_once_account`, donde el único
   try/except (líneas 559-640, catch genérico en la 639) la atrapa,
   loguea `"error gestionando cuenta %s: %s"` (con el mensaje vacío,
   porque `TimeoutError` no lleva texto propio) y **aborta el resto del
   tick para esa cuenta**.
5. **Consecuencia**: el bloque de código que construye el mensaje de
   Telegram y llama a `_notify("tp1_hit", ...)` (dentro del `if ok:` de
   `_on_tp1_leg_closed`, líneas 701-728) nunca se alcanza. El TP1 ya había
   pasado de verdad en MT5, con ganancia real, pero el evento de negocio
   correspondiente se pierde en silencio — no queda ni en el JSONL local
   de auditoría (`EventBus`'s "fuente de verdad primaria" nunca llega a
   escribirse para este evento), ni en n8n, ni en Telegram.

Este es el mismo bug de fondo que ya documenta la memoria
`pooledmt5client-lock-never-released-after-timeout` (un `order_send`
colgado deja el lock de `PooledMT5Client` tomado indefinidamente), pero
con una consecuencia adicional no documentada antes: **también se pierde
la notificación del evento de negocio que se estaba procesando en el mismo
tick**, aunque ese evento ya fuera un hecho confirmado antes de intentar
la acción de MT5 que se colgó.

El mismo patrón de riesgo existe en dos lugares más del loop de gestión:

- **`_apply_tp2_partial_close`** (línea 865): llama a
  `self._call(client.partial_close, account, runner.ticket, 50)` (línea
  944) — si esta llamada se cuelga, no solo se pierde la notificación de
  `tp2_partial_closed`, sino que además queda la incertidumbre de si el
  cierre parcial realmente se ejecutó en MT5 o no (el hilo colgado "puede
  seguir vivo en 2do plano").
- **`_apply_trailing`** (línea 976): también hace `order_send` vía
  `_force_runner_sl` en su implementación (no mostrado arriba, mismo
  patrón). No emite ninguna notificación de negocio hoy (`trailing_updated`
  es solo log, decisión ya tomada por el usuario), así que aquí el único
  riesgo es que un timeout aborte el resto del tick para esa cuenta
  (afectando a otros grupos que aún no se procesaron en el mismo ciclo),
  no que se pierda una notificación específica.

## 2. Alcance

**Dentro de alcance:**

- Reordenar `_on_tp1_leg_closed` para notificar `tp1_hit` **antes** de
  intentar `_force_runner_sl` — el TP1 es un hecho ya confirmado por
  `deal.reason`, independiente del resultado del BE.
- Distinguir, en `_force_runner_sl`, un timeout de `order_send` (`asyncio.
  TimeoutError`) de un rechazo limpio de MT5 (`retcode` distinto de
  `10009` tras agotar los 3 intentos) — son riesgos distintos y ya
  fueron confirmados como tal con el usuario.
- Nuevo evento `tp1_hit_be_timeout` (`channel: "both"`), distinto del ya
  existente `tp1_hit_be_failed`, para el caso de timeout — mensaje
  explícito de que el estado del BE es desconocido y puede requerir
  revisión manual en MT5 directamente (el hilo colgado puede haber
  aplicado la orden igual, en segundo plano, sin que el sistema se entere).
- Mismo tratamiento en `_apply_tp2_partial_close`: si `partial_close` se
  cuelga, nuevo evento `tp2_partial_timeout` (`channel: "both"`) en vez de
  perder la notificación en silencio.
- `_apply_trailing` no gana ninguna notificación nueva (sigue sin
  notificar nada, decisión ya tomada) — solo se beneficia indirectamente
  de que un timeout ahí ya no vuelva a tumbar el procesamiento de *otros*
  grupos en el mismo tick (ver §3, aislamiento por grupo).
- Aislar el manejo de excepciones **por grupo/ticket dentro del ciclo**,
  no solo por cuenta — hoy un solo timeout en cualquier grupo aborta el
  procesamiento de *todos* los demás grupos de esa cuenta en ese tick.

**Fuera de alcance (explícitamente):**

- Arreglar el bug de fondo del lock de `PooledMT5Client` que nunca se
  libera tras un timeout (ya documentado en memoria, requiere su propio
  diseño — posiblemente un lock con timeout propio o reemplazar el pool).
  Este spec solo evita que ese problema *además* se trague notificaciones.
- Cambiar `MT5_CALL_TIMEOUT_SECONDS` ni la lógica de reintentos de
  `_call`/`_force_runner_sl` en sí — se mantienen tal cual.
- Generalizar el aislamiento a `/mgmt/action` (que tiene su propio manejo
  de excepciones por grupo, ya con `try/except` individual por
  `group_id`, confirmado leyendo `apply_mgmt_action` — no comparte el
  problema de este spec).
- Cualquier cambio a la detección de causa de cierre (`_classify_leg_closure`)
  — ya funciona correctamente, confirmado con el caso real del grupo 122.

## 3. Diseño

### 3.1 Reordenar `_on_tp1_leg_closed`: notificar TP1 antes de intentar BE

Hoy: `_force_runner_sl(...)` → si `ok`: notificar `tp1_hit` → si no:
notificar `tp1_hit_be_failed`.

Nuevo orden:
1. Notificar `tp1_hit` inmediatamente (ya se tiene toda la info necesaria:
   `deal_info` de `_get_close_deal_info`, `channel_name`, etc. — no
   depende de `_force_runner_sl` en absoluto).
2. Intentar `_force_runner_sl(...)`.
3. Según el resultado (ver 3.2): notificar `tp1_hit_be_failed` (rechazo
   limpio) o `tp1_hit_be_timeout` (timeout) o no notificar nada
   adicional (éxito — el BE ya quedó implícito en que no hubo problema).

Esto es un cambio de orden puro, no de lógica de negocio: `TP1_HITS.inc()`
y el resto de side-effects de la función se mantienen donde están.

### 3.2 `_force_runner_sl` distingue timeout de rechazo limpio

Nueva excepción interna `MT5CallTimeoutError(Exception)` (definida en
`trade_manager.py`, junto a las demás utilidades del módulo). El loop de
reintentos de `_force_runner_sl` envuelve cada `self._call(client.order_send,
req)` en un try/except: si captura `asyncio.TimeoutError`, no reintenta
más (un intento ya colgado 10s es indicio de que MT5/la conexión están en
mal estado, reintentar de inmediato no ayuda) y relanza como
`MT5CallTimeoutError` con el `reason` original como contexto. Si el error
es cualquier otro (incluye el camino ya existente de `retcode` != `10009`),
el comportamiento de reintento no cambia.

`_on_tp1_leg_closed` y `_apply_tp2_partial_close` (los dos callers que
notifican) capturan `MT5CallTimeoutError` específicamente alrededor de su
llamada a `_force_runner_sl`/`partial_close`, y en ese caso emiten el
evento de timeout correspondiente en vez de dejar que la excepción siga
subiendo. `move_sl_be_now` (en `apply_mgmt_action`) también debe capturar
esta excepción del mismo modo — hoy ese código ya tiene su propio
try/except por grupo, así que solo necesita reconocer el nuevo tipo de
excepción explícitamente para dar un mensaje de resultado (`{"status":
"timeout"}`) distinto de `{"status": "failed"}`, en vez de que caiga en
la rama genérica `except Exception`.

### 3.3 Aislamiento por grupo, no solo por cuenta

`_tick_once_account`'s loop de detección de cierres (líneas 565-624) pasa
a envolver el cuerpo de **cada iteración** (cada `ticket`/`closed_trade`)
en su propio try/except, en vez de depender únicamente del try/except
exterior que cubre la función entera. Un error no manejado en el
procesamiento de un grupo (cualquier excepción que no sea ya capturada
localmente por 3.2) se loguea con el `group_id` afectado y el loop
continúa con el siguiente ticket — no aborta el resto de la cuenta.

Mismo tratamiento para el segundo loop de la función (BE/TP2/trailing,
líneas 628-637): cada `ticket`/`t` se procesa dentro de su propio
try/except, para que un timeout en el trailing de un grupo no impida que
el trailing de otro grupo en la misma cuenta se aplique en ese tick.

El try/except exterior de `_tick_once_account` (línea 639) se mantiene
como red de seguridad de última instancia (p. ej. un fallo de
`positions_get` que impide construir `pos_by_ticket` en absoluto, antes de
poder aislar nada por grupo).

Los tres niveles de manejo de errores no se solapan por diseño: 3.2 captura
`MT5CallTimeoutError` específicamente en el punto exacto donde se genera
(dentro de `_on_tp1_leg_closed`/`_apply_tp2_partial_close`), para poder
emitir el evento de negocio correcto. El try/except por grupo de 3.3 es la
red que atrapa cualquier OTRA excepción que 3.2 no haya manejado ya
(p. ej. un error inesperado no relacionado con timeouts de MT5) — nunca
vuelve a capturar una `MT5CallTimeoutError` que 3.2 ya resolvió, porque
esa excepción ya no se propaga más allá del punto donde se maneja.

### 3.4 Catálogo de eventos nuevos

| `event_type` | `channel` | Cuándo se dispara | Mensaje |
|---|---|---|---|
| `tp1_hit_be_timeout` (nuevo) | both | `_force_runner_sl` timeoutea al mover el runner a BE tras TP1 | ⚠️ TP1 alcanzado, pero no se pudo CONFIRMAR si el runner quedó en breakeven (MT5 no respondió a tiempo). El runner puede seguir con su SL original, o el BE puede haberse aplicado igual en segundo plano sin que el sistema se entere — revisar manualmente en MT5. |
| `tp2_partial_timeout` (nuevo) | both | `partial_close` timeoutea al intentar el cierre parcial de TP2 | ⚠️ TP2 alcanzado, pero no se pudo confirmar si el cierre parcial del 50% se ejecutó (MT5 no respondió a tiempo) — revisar manualmente el volumen real de la posición en MT5. |
| `mgmt_move_sl_be_timeout` (nuevo, dentro de `apply_mgmt_action`) | both | timeout al mover SL a BE vía `/mgmt/action` | Mismo mensaje que `tp1_hit_be_timeout`, adaptado al contexto de una orden manual del usuario. |

`tp1_hit_be_failed` (ya existente, rechazo limpio tras 3 intentos) no
cambia de significado — sigue siendo "MT5 respondió que no se pudo", un
caso de certeza distinto del de timeout ("no sabemos qué pasó").

## 4. Testing

- Extender el simulador (`SimuladorMT5`) con un modo de fallo que simule
  un `order_send`/`partial_close` colgado — la forma más simple es que
  el simulador exponga un método que retrase su propia respuesta más
  allá de un timeout configurable en el test (o, más simple aún, que el
  test monkee-parchee `client.order_send`/`client.partial_close` para
  lanzar `asyncio.TimeoutError` directamente, ya que lo único que
  `_force_runner_sl`/`_apply_tp2_partial_close` necesitan ver es esa
  excepción específica — no hace falta simular un cuelgue real de 10s en
  los tests).
- Test: TP1 real (vía `close_position_by_tp`) seguido de un
  `order_send` que lanza `asyncio.TimeoutError` en el intento de BE →
  confirmar que `tp1_hit` SÍ se notificó (antes del intento de BE) y que
  `tp1_hit_be_timeout` se notificó también (no `tp1_hit_be_failed`).
- Test: mismo escenario pero con rechazo limpio (`retcode` inválido tras
  3 intentos, sin timeout) → confirmar que sigue notificando
  `tp1_hit_be_failed` como hoy (regresión, no debe cambiar).
- Test: TP2 alcanzado con `partial_close` lanzando timeout → confirmar
  `tp2_partial_timeout` en vez de que la excepción aborte el tick.
- Test: dos grupos distintos en la misma cuenta, uno con timeout en su
  procesamiento y otro sin problemas → confirmar que el segundo grupo sí
  se procesa normalmente en el mismo tick (aislamiento por grupo).
- Test de regresión: `move_sl_be_now` vía `/mgmt/action` con timeout →
  confirmar `{"status": "timeout"}` en el resultado, distinto de
  `{"status": "failed"}`.
