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
