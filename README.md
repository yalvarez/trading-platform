## Inicialización de Redis Streams

Si ves el error:

```
redis.exceptions.ResponseError: NOGROUP No such key 'raw_messages' or consumer group 'router_group' in XREADGROUP with GROUP option
```

Debes crear el stream y el grupo de consumidores en Redis antes de iniciar los servicios dependientes. Ejecuta:

```
docker exec -it atp-redis redis-cli XGROUP CREATE raw_messages router_group $ MKSTREAM
```

Esto crea el stream `raw_messages` y el grupo `router_group` si no existen.
# auto-trading-platform

Arquitectura en contenedores (Linux) para:
- leer mensajes de Telegram
- parsear señales vs. gestión
- distribuir ejecución/gestión a 6 contenedores MT5
- operar concurrentemente

## Requisitos
- Docker + Docker Compose (Linux amd64)
- Credenciales Telegram (api_id, api_hash, phone)

## Setup
1) Copia `.env.example` a `.env` y llena credenciales.
2) Arranca:
```bash
docker compose up -d --build
```

## Configuración de cuentas y canales permitidos

Cada cuenta de trading se configura en la variable de entorno `ACCOUNTS_JSON` (en el archivo `.env`). Ahora puedes restringir qué canales de Telegram puede copiar cada cuenta usando el campo `allowed_channels`.

- Si `allowed_channels` está presente, la cuenta solo copiará señales provenientes de esos canales (por su ID numérico).
- Si no está presente, la cuenta copiará señales de todos los canales (comportamiento retrocompatible).

### Ejemplo de configuración en `.env`:

```
ACCOUNTS_JSON=[
  {"name":"Ysaias Vantage","host":"mt5_acct1","port":8001,"active":false,"fixed_lot":0.03,"chat_id":8592452414, "allowed_channels": [-5250557024, -1003209803455]},
  {"name":"Ysaias TickMill","host":"mt5_acct2","port":8001,"active":false,"fixed_lot":0.02,"chat_id":8592452414, "allowed_channels": [-5250557024, -1003209803455]},
  ...
]
```

En este ejemplo, todas las cuentas solo copiarán señales de los canales de Hannah (`-5250557024`) y Limitless (`-1003209803455`).

> **Nota:** Los IDs de canal deben ser numéricos y pueden obtenerse usando bots de Telegram o inspeccionando los mensajes.

### ¿Cómo funciona?
- Cuando llega una señal, el sistema verifica el canal de origen (`source_chat_id` o `chat_id`).
- Solo las cuentas que tengan ese canal en su lista `allowed_channels` procesarán la señal.
- Si una cuenta no tiene el campo, procesará señales de cualquier canal.

## Modalidad de trading por cuenta

Cada cuenta puede elegir su modalidad de gestión de trades usando el campo `trading_mode` en `ACCOUNTS_JSON`. Las modalidades disponibles son:

- `general`: Gestión clásica (parcial en TP1, BE, trailing, etc.).
- `be_pips`: Al alcanzar X pips (configurable con `be_pips`), mueve el SL a BE, luego sigue la gestión normal.
- `be_pnl`: Al alcanzar X pips y tras cierre parcial, pone el SL en el precio que permita perder solo lo ganado en la parcial (requiere `be_pips`).

### Ejemplo de configuración:

```
ACCOUNTS_JSON=[
  {"name":"Ysaias Vantage","host":"mt5_acct1","port":8001,"active":true,"fixed_lot":0.03,"chat_id":8592452414, "allowed_channels": [-5250557024, -1003209803455], "trading_mode": "be_pnl", "be_pips": 35},
  {"name":"Ysaias TickMill","host":"mt5_acct2","port":8001,"active":false,"fixed_lot":0.02,"chat_id":8592452414, "allowed_channels": [-5250557024, -1003209803455], "trading_mode": "general"},
  ...
]
```

- Si no se especifica `trading_mode`, se usará la modalidad `general` por defecto.
- El campo `be_pips` es obligatorio para las modalidades `be_pips` y `be_pnl`.

### ¿Cómo funciona?
- El sistema detecta la modalidad de cada cuenta y aplica la lógica de gestión correspondiente de forma automática.
- Cada modalidad tiene su propia función de gestión, aislada y fácil de mantener.

---
