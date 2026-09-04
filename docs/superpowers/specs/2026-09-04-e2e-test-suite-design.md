# Diseño: Suite de pruebas end-to-end contra el VPS real (cuenta demo)

Fecha: 2026-09-04
Branch: `n8n-integration`
Autor: Ysaias Alvarez (con Claude Code)

## 1. Contexto y objetivo

El sistema (`telegram_ingestor` → Redis → `router_parser` → `trade_orchestrator`
→ MT5, con excepciones vía n8n/Ollama) solo se ha validado con unit/integration
tests locales (`tests/`, `services/trade_orchestrator/test_*.py`). No existe
ninguna prueba que ejercite el flujo completo contra el VPS real: mandar un
mensaje de Telegram de verdad, y confirmar que se propaga correctamente por
cada capa (Redis → parser → orchestrator → MT5) hasta convertirse en una
posición gestionada correctamente.

Objetivo: una suite de pruebas end-to-end, repetible y automatizable, que
corre en el propio VPS contra una cuenta MT5 **demo**, manda mensajes reales
a un chat de Telegram de prueba, y verifica el comportamiento observando las
tres capas (logs de Docker, streams de Redis, posiciones en MT5).

## 2. Alcance

**Dentro de alcance:**
- Paquete `tests/e2e/` con helpers reutilizables (lectura de precio vía MT5,
  envío de mensajes vía Telethon, observación de logs/Redis/MT5) y 15
  escenarios organizados en 3 familias (ver §5).
- Un servicio nuevo `e2e_runner` en `docker-compose.yml`, bajo `profile: e2e`
  (no arranca con `docker compose up` normal), que comparte la red interna
  de Docker con Redis y `mt5_acct1` sin publicar ningún puerto nuevo.
- Cleanup automático de posiciones abiertas por la suite en la cuenta demo.
- Reporte por escenario con evidencia de cada capa (línea de log, entrada de
  stream, estado de posición) para diagnóstico rápido de fallos.

**Fuera de alcance (explícitamente):**
- Mock del flujo n8n/Ollama — la suite corre contra el n8n/Ollama real de
  pruebas (decisión explícita: probar el pipeline completo, no solo el
  bot). Un escenario de management puede fallar por causa ajena al bot
  (n8n/Ollama caído); el runner debe distinguir esa causa (ver §7).
- CI/ejecución automática programada — esta suite se corre manualmente
  (`docker compose --profile e2e run --rm e2e_runner --scenario X`) por
  ahora; integrarla a un pipeline de CI queda para una iteración futura.
- Pruebas de carga/concurrencia (múltiples señales simultáneas) — cada
  escenario corre en serie, aislado.
- Multi-cuenta — igual que el resto del sistema hoy, la suite asume una sola
  cuenta activa en `ACCOUNTS_JSON`.

## 3. Arquitectura

### 3.1 Módulos (`tests/e2e/`)

- **`price_reader.py`** — lee el precio actual de XAUUSD directo de
  `mt5_acct1` reutilizando el mismo patrón RPyC que
  `trade_manager._get_price_with_retry` (`services/trade_orchestrator/mt5_pool.py`,
  `tick_price`/`symbol_info_tick`). Es la fuente de verdad para construir
  mensajes de señal con precios realistas (ej. un ENTRY PRICE que sí caiga
  dentro del entry-range gate) y para verificar el precio de apertura
  registrado.
- **`telegram_sender.py`** — cliente Telethon con una sesión de prueba
  separada de la del bot, que manda mensajes a `TG_TEST_CHAT_ID`. **Precondición
  de entorno** (no de código): `telegram_ingestor` filtra por
  `allowed_channels` de `ACCOUNTS_JSON`
  (`services/telegram_ingestor/app.py::build_channel_filter`), no por
  `TG_TEST_CHAT_ID` — ese env var no está conectado a ningún filtro por sí
  mismo. Para que los mensajes de prueba lleguen al pipeline, `TG_TEST_CHAT_ID`
  debe ser el `chat_id` de un **grupo/canal de Telegram dedicado a
  pruebas** (no el canal real de TradePulse — evita mezclar mensajes de
  prueba, incluyendo cierres forzados y spam simulado, con el tráfico real
  visible a otros miembros), agregado explícitamente a `allowed_channels`
  en el `ACCOUNTS_JSON` del VPS de pruebas. Este canal de pruebas y su alta
  en `ACCOUNTS_JSON` son un prerrequisito de setup, no parte del código de
  la suite (ver §4).
- **`vps_observer.py`** — agrega tres fuentes de verificación:
  - logs de `docker compose logs <servicio>` (subprocess local al VPS),
  - lectura de streams de Redis: `XRANGE raw_messages` y
    `XRANGE parsed_signals` (nombres reales, `services/common/redis_streams.py::Streams`)
    para confirmar que una señal avanzó por el pipeline de apertura. **No
    existe un stream de Redis para mensajes de gestión** — la ruta de
    familia B es Telegram → `router_parser` (no reconocido como señal) →
    POST a `N8N_INBOUND_WEBHOOK_URL` → n8n/Ollama clasifica → n8n hace
    `POST /mgmt/action` (HTTP, con header `X-N8N-Action-Key`) sobre
    `trade_orchestrator:8200`. Para familia B, `vps_observer` verifica por
    logs de `trade_orchestrator` (líneas `[TM][MGMT]`, ver
    `trade_manager.py::apply_mgmt_action`) y por el estado resultante de
    la posición en MT5, no por un stream de Redis,
  - estado de posiciones en MT5 vía el mismo pool RPyC que `price_reader`.
- **`scenarios/`** — un archivo por escenario, cada uno una función
  `async def run(ctx) -> ScenarioResult` con arrange/act/assert explícitos
  y su propio cleanup.
- **`runner.py`** — CLI (`python -m tests.e2e.runner --scenario <nombre>` o
  `--all`) que orquesta: lee precio → arma mensaje → lo manda → espera y
  verifica en las 3 capas → imprime resumen pass/fail con evidencia →
  ejecuta cleanup.

### 3.2 Despliegue

Servicio nuevo en `docker-compose.yml`:

```yaml
e2e_runner:
  build: ./tests/e2e
  profiles: ["e2e"]
  env_file: .env
  depends_on: [redis, mt5_acct1, telegram_ingestor, router_parser, trade_orchestrator]
  networks: [default]   # misma red interna que el resto de servicios
```

No se publica ningún puerto nuevo al host. `redis` y `mt5_acct1:8001` siguen
siendo accesibles solo dentro de la red interna de Docker, tal como hoy.
Ejecución: `docker compose --profile e2e run --rm e2e_runner --scenario fast_signal`.

**Excepción explícita — socket de Docker:** para que `vps_observer` pueda
leer logs de otros contenedores (`docker logs <servicio>`, spec §3.1), el
servicio `e2e_runner` monta `/var/run/docker.sock` de solo lectura. Esto es
más acceso del que "solo red interna, sin puertos nuevos" sugiere por sí
solo — el contenedor puede leer logs de *cualquier* contenedor del host, no
solo los de este `docker-compose.yml`. Aceptado como decisión explícita del
operador (no una omisión): el riesgo es acotado (solo lectura, VPS ya de
confianza) y es más simple que la alternativa (correr el runner fuera de
compose, en el host, lo que a su vez requeriría exponer Redis/RPyC al host
igual que la opción de "correr localmente" ya descartada en la sección 4).

## 4. Fuentes de verdad / entorno

- **Precio:** MT5 vía RPyC (`mt5_acct1`), no una API externa — es la misma
  fuente que usa el sistema real, evitando falsos fallos por desviación de
  feed entre proveedores.
- **Cuenta:** MT5 **demo** en el VPS de producción — mismo entorno real,
  sin riesgo de dinero real.
- **Canal de entrada:** `TG_TEST_CHAT_ID`, enviado por script (Telethon), no
  a mano — permite repetir la suite sin intervención manual.
- **n8n/Ollama:** instancia real de pruebas (no mock) — ver §7 para cómo se
  reporta una falla causada por esa dependencia externa. **Precondición de
  setup:** ese n8n debe tener su flujo de `N8N_INBOUND_WEBHOOK_URL`
  apuntando al `router_parser` de este mismo VPS de pruebas, y su callback
  de decisión apuntando a `POST http://<este-vps>:8200/mgmt/action` (con
  el `N8N_ACTION_API_KEY` de este `.env`) — no al entorno de producción.
  Si n8n está apuntando al `trade_orchestrator` equivocado, familia B
  parece fallar por timeout (dependencia externa) cuando en realidad es un
  error de configuración cruzada; confirmar esta configuración es parte
  del checklist de pre-vuelo del runner (ver §7).
- **Canal de Telegram de pruebas:** un grupo/canal dedicado, agregado a
  `allowed_channels` en el `ACCOUNTS_JSON` de este VPS — ver §3.1
  (`telegram_sender.py`) para el porqué.

## 5. Escenarios

### Familia A — Ciclo de vida de la señal (apertura + gestión completa)

**Nota sobre determinismo:** el precio real de XAUUSD no se puede forzar a
moverse; con el SL/TP por defecto (30 pips de TP) o el de un `SIGNAL ALERT`
típico del canal (decenas de pips), alcanzar TP1 puede tardar minutos, horas,
o no ocurrir en la ventana de la prueba. Para que A1/A2/A4 sean deterministas
y rápidos, el mensaje de prueba se construye con un **TP1 muy cercano al
precio leído por `price_reader`** (pocos pips — suficiente para no chocar
con el spread/slippage típico, pero alcanzable en segundos a minutos dado el
movimiento normal de XAUUSD) en vez de replicar los pips típicos de una señal
real. Cada escenario define un timeout explícito (p. ej. 10 minutos) tras el
cual, si TP1 no se alcanzó, el escenario se reporta como **inconcluso**
(ni pass ni fail) en vez de fallar — distinción explícita en el reporte del
runner (§7) para no confundir "el mercado no se movió" con "el bot falló".
La fase de apertura + valores registrados (SL/TP/entry) se verifica siempre,
incluso si la fase de cierre por TP1 queda inconclusa.

**Nota sobre la ventana de entry-range de oro (5s):** para XAUUSD,
`open_group` usa una ventana de entrada reducida a **5 segundos** (poll de
100ms), no los `ENTRY_WAIT_SECONDS` (90s por defecto) que aplican a otros
símbolos (`trade_manager.py`, ~línea 148:
`entry_wait_max = 5.0 if is_gold else entry_wait_seconds`). Cualquier
escenario de familia A o C3 que use un `SIGNAL ALERT` completo con
`ENTRY PRICE` depende de que el precio esté dentro de ese rango dentro de
esos 5s desde que `open_group` evalúa la señal — margen angosto frente a la
latencia real del pipeline (Telethon → ingestor → Redis → router_parser →
orchestrator). Mitigación: `price_reader` lee el precio **inmediatamente
antes** de construir y enviar el mensaje (mínimo tiempo entre lectura y
envío), y el rango de entry del mensaje se construye holgado alrededor de
ese precio (considerando `TOLERANCE_PIPS`) en vez de un rango ajustado. Si,
pese a eso, el escenario aborta por `open_aborted` con causa de timeout de
rango (no por otra razón), el runner lo reporta como **inconcluso por
timing de entry-range** (ver §7), distinto de un fallo del bot, y permite
reintentar el escenario.

- **A1. Fast solo** — `"XAUUSD BUY NOW"`, sin full después. Abre con SL/TP
  por defecto (`DEFAULT_SL_XAUUSD_PIPS`/`DEFAULT_TP_XAUUSD_PIPS`). Gestiona
  ciclo completo (BE al cerrar tp1_leg, trailing en runner, cierre) solo con
  esos valores; ver nota de determinismo arriba para el timeout de TP1.
- **A2. Fast → Full temprano** — full llega antes de que tp1_leg cierre, con
  TP1 cercano al precio leído (ver nota de determinismo). `update_group_signal`
  reemplaza SL/TP1/TP2 sobre el grupo ya abierto (no abre un segundo grupo).
  Assert: SL/TP en MT5 reflejan los del full, no los default. Ciclo completo
  con los valores actualizados.
- **A3. Fast → Full tardío (borde)** — full llega después de que tp1_leg ya
  cerró (BE aplicado) o con trailing ya activo en el runner — esto requiere
  que A3 fuerce TP1 muy cerca del precio de apertura para llegar a ese estado
  rápido y confiablemente antes de mandar el full. Assert basado en
  comportamiento explícito ya presente en `trade_manager.py`
  (`update_group_signal`, líneas ~312-352): el SL nunca retrocede respecto
  al ya mejorado por BE/trailing, y `peak_multiple` del runner se re-escala
  al nuevo `tp1`/`tp2` sin perder el progreso acumulado.
- **A4. Full solo, sin fast previo** — abre directo con los valores del
  full (no default), TP1 cercano al precio leído. Ciclo completo normal.

### Familia B — Gestión vía mensajes de management (lenguaje libre → n8n/Ollama)

Mensajes tomados del corpus real analizado en la memoria de proyecto
`tradepulse-channel-message-patterns` (290 mensajes, canal TradePulse):

- **B1. BE explícito, variante 1** — *"Set BE for zero risk"* → acción
  `move_sl_be_now`.
- **B2. BE explícito, variante 2** — *"Make sure you adjust your sl to
  Entry for zero risk"* → mismo resultado que B1; valida que Ollama
  generaliza la intención más allá de una keyword fija.
- **B3. BE explícito, variante 3** — *"lock all your trades in Break
  Even"* → ídem.
- **B4. Cierre forzado** — *"MARKET STRUCTURE SHIFTED! DON'T HOLD SELL.
  Close now"* → posición se cierra.
- **B5. Corrección de señal** — *"SIGNAL UPDATED" / "TP 2 IS 4687
  Correction"* → acción `signal_correction`; solo actualiza la referencia
  de TP2 usada por el trailing, no toca MT5 directamente en el runner.
- **B6. Progreso/milestone — no debe generar acción** — *"+240 PIPS
  SKYROCKETING"*, *"TP 1 DONE"*, *"Road to TP ONE"*. Assert: ninguna acción
  de mgmt disparada (falso positivo = fallo).
- **B7. SL hit / recovery** — *"HIT SL ❌. GET READY FOR RECOVERY 🤝"* →
  acción `note_sl_hit` (`trade_manager.py::apply_mgmt_action`, líneas
  ~546-548): registra el evento y notifica, pero no modifica SL/TP ni
  cierra la posición. Assert: la posición queda sin cambios en MT5 (no
  "ninguna acción de mgmt", sino "ninguna acción que mute la posición").
- **B8. Spam promocional — no debe generar acción ni trade** — mensaje tipo
  VIP/Pool Trading upsell. Cero efectos observables en Redis/MT5.

### Familia C — Robustez del pipeline

- **C1. Deduplicación** — mismo fast signal mandado 2 veces seguidas.
  Assert: la segunda es descartada, no se abre un segundo grupo.
- **C2. Texto no reconocido → n8n inbound** — texto que no es señal ni
  gestión reconocible. Assert: POST a `N8N_INBOUND_WEBHOOK_URL`, sin trade
  ni acción de mgmt.
- **C3. Variantes de formato del entry range** — dash irregular en el rango
  de entry (`"4600- 4590"`, `"4325 - 4335"`), tal como aparece en mensajes
  reales del canal. Assert: el parser lo acepta igual (regresión sobre la
  variance ya documentada en la memoria de proyecto).

## 6. Limpieza

Cada escenario que abre una posición en la cuenta demo debe cerrarla al
finalizar — ya sea porque el propio flujo de gestión la cierra como parte
de la prueba (ej. B4), o explícitamente en su cleanup. Si un escenario falla
a mitad de camino, el runner ejecuta un cleanup de emergencia (cierra
cualquier posición abierta por ese escenario, identificada por `group_id`)
antes de reportar el fallo, para no dejar posiciones huérfanas entre
corridas.

## 7. Reporte y manejo de dependencias externas

El runner imprime, por escenario, un resumen con lo observado en cada capa:
línea de log relevante, entrada de stream de Redis (`raw_messages`,
`parsed_signals`), y estado final de la posición en MT5. Un resultado se
clasifica en una de tres categorías:

- **Fallo del bot** — el pipeline propio (ingestor/parser/orchestrator) no
  se comportó como se esperaba.
- **Fallo de dependencia externa** — el escenario depende de n8n/Ollama
  (familia B) y esa instancia no respondió o respondió fuera de lo
  esperado. El runner detecta esto por timeout en la acción de mgmt
  correspondiente sin señal de error propia, y lo reporta explícitamente
  como tal para no confundirlo con un bug del bot.
- **Inconcluso** — exclusivo de familia A (y C3, por la ventana de
  entry-range): dos motivos posibles, distinguidos en el reporte —
  (a) la fase de apertura se verificó correctamente, pero el precio real
  no alcanzó TP1 dentro del timeout del escenario (ver nota de
  determinismo en §5); (b) el escenario abortó por `open_aborted` con
  causa de timeout de la ventana de entry-range de 5s propia de XAUUSD
  (ver nota en §5), no por otra razón — en ese caso el runner permite
  reintentar el escenario. Ninguno de los dos cuenta como fallo del bot.

**Checklist de pre-vuelo** (antes de correr `--all`, el runner valida y
reporta si falta):
- `TG_TEST_CHAT_ID` está en `allowed_channels` de `ACCOUNTS_JSON`.
- El flujo n8n de pruebas responde en `N8N_INBOUND_WEBHOOK_URL` y su
  callback apunta a este `trade_orchestrator:8200/mgmt/action` (ver §4) —
  un chequeo básico (p. ej. un ping/health check si n8n lo expone, o
  advertencia explícita si no se puede verificar automáticamente).

## 8. Testing de la propia suite

Los helpers (`price_reader`, `telegram_sender`, `vps_observer`) son código
nuevo y se prueban con unit tests locales (mockeando RPyC/Telethon/Redis),
siguiendo el mismo patrón que el resto del repo (`tests/`,
`services/trade_orchestrator/test_*.py`). Los escenarios en sí no se
"testean" — son la prueba; su corrección se valida corriéndolos contra el
VPS real como parte de esta misma suite.
