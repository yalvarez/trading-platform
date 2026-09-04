# Persistencia y Reconciliación de Estado de TradeManager — Diseño

## Contexto y problema

`TradeManager` gestiona posiciones dual-TP (`tp1_leg` + `runner_leg`) usando
únicamente estado en memoria: `self.trades: dict[int, ManagedTrade]`, indexado
por ticket de MT5. Este estado incluye tanto datos derivables de MT5 en
cualquier momento (`symbol`, `direction`, `planned_sl`, `entry_price`) como
"memoria de gestión" que **no existe en MT5 en absoluto** — `tp1_price`,
`tp2_price`, `be_applied`, `peak_multiple`.

Cuando el contenedor `trade_orchestrator` se reinicia por cualquier motivo
(despliegue, crash, timeout de MT5 congelado — ver el incidente que motivó
`MT5_CALL_TIMEOUT_SECONDS`), `self.trades` empieza vacío. Las posiciones
siguen abiertas en MT5, pero el sistema ya no las reconoce como parte de un
grupo gestionado: no reciben BE automático al cerrar `tp1_leg`, no continúan
el trailing, y `apply_mgmt_action`/`update_group_signal` no las encuentran
(`find_active_group_for_symbol` solo mira `self.trades`).

Esto se observó en producción: un reinicio de `trade_orchestrator` (para
desplegar un fix no relacionado) dejó un grupo con ambas piernas abiertas en
MT5 sin ningún tracking — el runner quedó con su SL original,
permanentemente, sin protección mecánica adicional.

**Objetivo de este diseño:** un reinicio del contenedor nunca debe volver a
dejar un grupo sin gestión mecánica. El sistema debe persistir su memoria de
gestión y, al arrancar, reconciliarla contra el estado real de MT5 antes de
que el loop mecánico (`run_forever`) empiece a tickear.

## Alcance

Cubre únicamente `trade_orchestrator` (el único servicio con estado de
gestión en memoria). No afecta `router_parser`, `telegram_ingestor`, ni
`trade_api` (que ya opera sin estado propio, directo contra MT5 en cada
llamada).

## Arquitectura

Un componente nuevo, `TradeStateStore` (`services/trade_orchestrator/trade_state_store.py`),
encapsula toda la persistencia. `TradeManager` lo usa en dos momentos:

- **Escritura**: cada vez que `self.trades` cambia de forma relevante para la
  gestión mecánica (ver "Puntos de escritura" abajo), en el mismo punto
  síncrono del código que ya modifica el diccionario — no hay temporizador
  separado.
- **Reconciliación al arranque**: una vez, en `main()` (`app.py`), antes de
  lanzar `run_forever()`.

`TradeStateStore` no conoce `ManagedTrade` como clase — opera sobre el `dict`
ya serializado que `TradeManager` le pasa, y devuelve `dict`s al leer.
`TradeManager` es responsable de la conversión hacia/desde `ManagedTrade`.

## Tres capas de recuperación, en orden de preferencia

1. **Redis** (fuente primaria — rápida, ya en el stack, usada por
   `Streams.SIGNALS`/`Streams.RAW`). Si el documento de un `group_id` existe
   ahí, se usa tal cual.
2. **Archivo de respaldo** (`data/trade_state.jsonl`, bind-mount fuera del
   contenedor — mismo patrón que `telegram_ingestor.session`). Espejo de
   Redis, en formato JSON Lines (un documento JSON por línea, append-only).
   Sobrevive aunque el volumen/instancia de Redis se pierda por completo
   (`docker compose down -v`, reinstalación, etc.) — algo que Redis por sí
   solo no garantiza.
3. **Reconstrucción mínima desde MT5** (último recurso). Si ni Redis ni el
   archivo tienen el `group_id` (se perdió, o nunca existió ahí — ambos casos
   se tratan igual, ver más abajo), se reconstruye lo mínimo posible
   directamente desde la posición real en MT5: `group_id`/`leg` (del
   comment), `symbol`/`direction`/`planned_sl`/`entry_price` (todos
   disponibles en el objeto `TradePosition` en vivo). `tp1_price`,
   `tp2_price`, `peak_multiple`, `be_applied` quedan en sus defaults seguros
   (`None`, `None`, `0.0`, `False`). El grupo vuelve a quedar bajo gestión
   mecánica — detección de cierre y BE al cerrar `tp1_leg` funcionan de
   inmediato — pero el trailing arranca desde cero (`peak_multiple=0.0`) en
   vez de continuar desde su progreso anterior, y `signal_correction`/una
   señal completa posterior tendrán que repoblar `tp1_price`/`tp2_price`
   antes de que el trailing pueda activarse (mismo guard que ya existe hoy).
   **Este camino se anuncia por n8n** como recuperación degradada.

## Identificación de posiciones propias en MT5

Toda posición con `magic == MAGIC` (987654) es candidata. Su `comment` tiene
el formato `TM-GRP{group_id}-{leg}` (`leg` es `tp1` o `runner`), truncado a
31 caracteres por `safe_comment` — con `group_id` como contador incremental
pequeño, esto no se trunca en la práctica.

**Si el comment no parsea** (corrupto, de una versión anterior del sistema,
truncado por un `group_id` inusualmente grande, o cualquier formato
inesperado): la posición se trata como **huérfana**. No se gestiona
mecánicamente (sin BE/trailing automático) y tampoco se toca ni se cierra —
se notifica por n8n con el ticket y el comment crudo, para que el usuario
decida manualmente. El sistema nunca adivina un `group_id`/`leg` que no puede
leer con certeza.

**Si el comment sí parsea** pero el `group_id` no aparece en Redis ni en el
archivo (ambos casos — estado perdido, o nunca existió ahí — son
indistinguibles y se tratan igual): reconstrucción mínima desde MT5 (capa 3
arriba). El comment por sí solo (magic correcto + formato válido) es
evidencia suficiente de que la posición es nuestra.

## Formato de persistencia

Un documento por `group_id` (no por ticket) — las dos piernas comparten casi
todo el estado relevante, y `apply_mgmt_action`/`update_group_signal` ya
operan a nivel de grupo.

```json
{
  "group_id": 1,
  "account_name": "Ysaias Vantage",
  "symbol": "XAUUSD",
  "direction": "BUY",
  "tp1_price": 4439.8,
  "tp2_price": 4439.9,
  "legs": {
    "tp1": {"ticket": 1940695523, "planned_sl": 4429.8, "entry_price": 4435.79},
    "runner": {"ticket": 1940695553, "planned_sl": 4429.8, "entry_price": 4435.73, "be_applied": true, "peak_multiple": 0.42}
  },
  "updated_ts": 1788552900.1
}
```

`tp1_price`/`tp2_price` viven a nivel de grupo (son iguales para ambas
piernas conceptualmente, aunque `ManagedTrade` los duplica hoy por pierna).
`be_applied`/`peak_multiple` solo tienen sentido para la pierna `runner`
(`tp1_leg` no los usa) pero se guardan bajo su leg por si el modelo cambia.

### Redis

Key: `trade_groups:{group_id}` → `SET` con el JSON completo como string (no
`HSET` por campo — siempre se lee/escribe el documento entero, un hash no
aporta nada aquí). Sin TTL: el estado vive mientras el grupo esté activo, y
se borra explícitamente (`DEL`) cuando ambas piernas del grupo salen de
`self.trades` (grupo completamente cerrado — ver "Puntos de escritura").

### Archivo de respaldo

`data/trade_state.jsonl`, bind-mounted en el contenedor en la misma ruta
(igual patrón que `telegram_ingestor.session` en `docker-compose.yml`). Cada
escritura hace un **append** de una línea JSON (el documento completo del
grupo, igual que en Redis) — nunca reescribe el archivo completo en caliente,
evitando el riesgo de corrupción a medio escribir que tendría un array JSON
único. Al leer (solo ocurre en la reconciliación de arranque), se recorre el
archivo línea por línea y se conserva solo la entrada más reciente por
`group_id` (última línea gana) — así un grupo actualizado 50 veces solo
cuenta como su estado final, no como 50 documentos a fusionar.

Una entrada especial `{"group_id": N, "closed": true}` marca el cierre
explícito de un grupo (ver "Puntos de escritura") — al leer, si la última
línea para un `group_id` es un cierre, ese grupo se considera cerrado y no se
reconstruye aunque MT5 tuviera (por error) una posición residual con ese
comment.

**Compactación**: al final de una reconciliación exitosa de arranque, el
archivo se reescribe (operación atómica: escribir a `.tmp`, luego `rename`)
conservando solo la última entrada de cada `group_id` que sigue activo tras
la reconciliación — evita que el archivo crezca sin límite con el histórico
completo de actualizaciones de trailing.

## Puntos de escritura

Todos ya identificados en el código actual de `trade_manager.py`:

- `open_group`: al crear ambos `ManagedTrade` — escribe el documento completo
  del grupo nuevo.
- `update_group_signal`: al actualizar `tp1_price`/`tp2_price`/`planned_sl`
  (señal completa llega, o `signal_correction` vía `/mgmt/action`) —
  reescribe el documento.
- `_on_tp1_leg_closed`: al aplicar BE exitosamente sobre el runner —
  reescribe el documento (con `be_applied=true`).
- `_apply_trailing`: cada vez que el SL avanza — reescribe el documento (con
  el `peak_multiple`/SL nuevos). Este es el punto de escritura más frecuente;
  el append-only del archivo lo hace barato.
- `apply_mgmt_action` (`move_sl_be_now`): al aplicar BE exitosamente vía
  gestión manual — igual que `_on_tp1_leg_closed`.
- **Borrado**: en `_tick_once_account`, cuando se detecta que la segunda
  pierna de un grupo (la última que quedaba en `self.trades`) ya no está en
  MT5 — se hace `DEL` en Redis y se escribe la entrada de cierre
  `{"group_id": N, "closed": true}` en el archivo.

Todas estas escrituras son "fire and forget" respecto al flujo principal: un
fallo al escribir en Redis o el archivo se loguea como warning pero **nunca**
bloquea ni aborta la operación de trading en curso — la persistencia es una
capa de seguridad adicional, no una dependencia dura del camino crítico.

## Reconciliación al arranque

Nuevo método `TradeManager.reconcile_from_mt5()`, llamado una vez en
`main()` (`app.py`) después de construir `TradeManager` y antes de lanzar
`asyncio.create_task(tradeManager.run_forever())`:

1. Para cada cuenta activa, `positions_get()` filtrado por `magic=MAGIC`.
2. Agrupar las posiciones encontradas por el `group_id` parseado de su
   comment (posiciones sin comment parseable van directo a la lista de
   huérfanas).
3. Para cada `group_id` con al menos una posición:
   - Buscar el documento en Redis; si no está, buscar en el archivo de
     respaldo (última entrada no-cierre para ese `group_id`).
   - Si se encuentra en cualquiera de los dos: reconstruir `ManagedTrade`
     para cada pierna presente en MT5, usando los datos del documento
     encontrado. Si el documento menciona una pierna que ya no está abierta
     en MT5 (cerró mientras el sistema estaba caído), esa pierna
     simplemente no se reconstruye — se procesará como cierre normal en el
     primer tick de `_tick_once_account` si el grupo sigue teniendo la otra
     pierna activa y el estado dice que la que falta era `tp1` (dispara BE)
     o se ignora si era `runner` (ya no hay nada que hacer con ella).
   - Si no se encuentra en ninguno: reconstrucción mínima directamente desde
     los campos de la posición MT5 — modo degradado.
4. Reescribir en Redis cualquier grupo reconstruido en modo degradado o
   recuperado del archivo (para que la próxima vez, si solo se reinicia el
   contenedor sin perder Redis, la recuperación sea completa desde la capa
   1).
5. Notificar por n8n un resumen agregado: cuántos grupos se recuperaron
   completos (desde Redis), cuántos desde el archivo, cuántos en modo
   degradado, cuántos tickets quedaron huérfanos (con sus tickets/comments
   crudos listados).

Este método es puramente de lectura sobre MT5 (nunca envía `order_send`) —
la única escritura que hace es repoblar `self.trades` en memoria y
Redis/archivo si estaban desincronizados.

## Testing

- `TradeStateStore` se prueba de forma aislada (sin `TradeManager`): escribir
  un documento, leerlo de vuelta, cerrar un grupo y confirmar que no
  reaparece, y el comportamiento de "última entrada gana" del archivo con
  múltiples escrituras al mismo `group_id`.
- `TradeManager.reconcile_from_mt5()` se prueba con `SimuladorMT5` poblado
  con posiciones (algunas con comment parseable + estado en el store
  simulado, algunas con comment parseable sin estado en ningún lado, algunas
  con comment no parseable) y se verifica que `self.trades` termina en el
  estado esperado para cada caso, y que las notificaciones correctas se
  disparan.
- Un test de integración de extremo a extremo: abrir un grupo con
  `open_group` (persistiéndolo), simular la pérdida completa de
  `self.trades` (nuevo `TradeManager`, mismo store), reconciliar, y
  verificar que el trailing puede continuar desde su `peak_multiple` previo
  sin perder progreso.

## Fuera de alcance (explícitamente, para no sobre-construir)

- No se persiste nada de `trade_api` ni `router_parser` — no tienen estado de
  gestión propio.
- No se implementa un mecanismo de reconciliación periódica en caliente
  (mientras el proceso corre) — la reconciliación es solo al arranque. Una
  desincronización mientras el proceso está vivo y corriendo normalmente no
  debería ocurrir salvo intervención manual externa en MT5, que queda fuera
  de este diseño.
- No se resuelve la limitación ya documentada de `MT5_CALL_TIMEOUT_SECONDS`
  (un hilo colgado puede seguir reteniendo el lock de `PooledMT5Client`) —
  ese es un problema distinto, ya mitigado por separado.
