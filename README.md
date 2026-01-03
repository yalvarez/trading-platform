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
