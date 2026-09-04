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
  separada de la del bot, que manda mensajes a `TG_TEST_CHAT_ID` (ya
  definido en `.env.example`).
- **`vps_observer.py`** — agrega tres fuentes de verificación:
  - logs de `docker compose logs <servicio>` (subprocess local al VPS),
  - lectura de streams de Redis (`XRANGE raw_messages`, `signals`,
    management) para confirmar que un mensaje avanzó por el pipeline,
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

## 4. Fuentes de verdad / entorno

- **Precio:** MT5 vía RPyC (`mt5_acct1`), no una API externa — es la misma
  fuente que usa el sistema real, evitando falsos fallos por desviación de
  feed entre proveedores.
- **Cuenta:** MT5 **demo** en el VPS de producción — mismo entorno real,
  sin riesgo de dinero real.
- **Canal de entrada:** `TG_TEST_CHAT_ID`, enviado por script (Telethon), no
  a mano — permite repetir la suite sin intervención manual.
- **n8n/Ollama:** instancia real de pruebas (no mock) — ver §7 para cómo se
  reporta una falla causada por esa dependencia externa.

## 5. Escenarios

### Familia A — Ciclo de vida de la señal (apertura + gestión completa)

- **A1. Fast solo** — `"XAUUSD BUY NOW"`, sin full después. Abre con SL/TP
  por defecto (`DEFAULT_SL_XAUUSD_PIPS`/`DEFAULT_TP_XAUUSD_PIPS`). Gestiona
  ciclo completo (BE al cerrar tp1_leg, trailing en runner, cierre) solo con
  esos valores.
- **A2. Fast → Full temprano** — full llega antes de que tp1_leg cierre.
  `update_group_signal` reemplaza SL/TP1/TP2 sobre el grupo ya abierto (no
  abre un segundo grupo). Assert: SL/TP en MT5 reflejan los del full, no los
  default. Ciclo completo con los valores actualizados.
- **A3. Fast → Full tardío (borde)** — full llega después de que tp1_leg ya
  cerró (BE aplicado) o con trailing ya activo en el runner. Assert basado
  en comportamiento explícito ya presente en `trade_manager.py`
  (`update_group_signal`, líneas ~312-352): el SL nunca retrocede respecto
  al ya mejorado por BE/trailing, y `peak_multiple` del runner se re-escala
  al nuevo `tp1`/`tp2` sin perder el progreso acumulado.
- **A4. Full solo, sin fast previo** — abre directo con los valores del
  full (no default). Ciclo completo normal.

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
- **B7. SL hit / recovery — no debe generar acción** — *"HIT SL ❌. GET
  READY FOR RECOVERY 🤝"*. Sin acción implícita.
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
`signals`, management), y estado final de la posición en MT5. Un fallo se
clasifica en una de dos categorías:

- **Fallo del bot** — el pipeline propio (ingestor/parser/orchestrator) no
  se comportó como se esperaba.
- **Fallo de dependencia externa** — el escenario depende de n8n/Ollama
  (familia B) y esa instancia no respondió o respondió fuera de lo
  esperado. El runner detecta esto por timeout en la acción de mgmt
  correspondiente sin señal de error propia, y lo reporta explícitamente
  como tal para no confundirlo con un bug del bot.

## 8. Testing de la propia suite

Los helpers (`price_reader`, `telegram_sender`, `vps_observer`) son código
nuevo y se prueban con unit tests locales (mockeando RPyC/Telethon/Redis),
siguiendo el mismo patrón que el resto del repo (`tests/`,
`services/trade_orchestrator/test_*.py`). Los escenarios en sí no se
"testean" — son la prueba; su corrección se valida corriéndolos contra el
VPS real como parte de esta misma suite.
