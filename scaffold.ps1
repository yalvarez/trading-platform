# Crear carpeta raíz
New-Item -ItemType Directory -Path "auto-trading-platform" -Force | Out-Null

# Crear archivos en la raíz
New-Item -ItemType File -Path "auto-trading-platform/docker-compose.yml" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/.env.example" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/README.md" -Force | Out-Null

# Crear estructura services/common
New-Item -ItemType Directory -Path "auto-trading-platform/services/common" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/common/__init__.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/common/config.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/common/redis_streams.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/common/timewindow.py" -Force | Out-Null

# Crear estructura services/telegram_ingestor
New-Item -ItemType Directory -Path "auto-trading-platform/services/telegram_ingestor" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/telegram_ingestor/Dockerfile" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/telegram_ingestor/requirements.txt" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/telegram_ingestor/app.py" -Force | Out-Null

# Crear estructura services/router_parser
New-Item -ItemType Directory -Path "auto-trading-platform/services/router_parser" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/router_parser/Dockerfile" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/router_parser/requirements.txt" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/router_parser/app.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/router_parser/gb_filters.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/router_parser/torofx_filters.py" -Force | Out-Null

# Crear estructura services/market_data
New-Item -ItemType Directory -Path "auto-trading-platform/services/market_data" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/market_data/Dockerfile" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/market_data/requirements.txt" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/market_data/app.py" -Force | Out-Null

# Crear estructura services/trade_orchestrator
New-Item -ItemType Directory -Path "auto-trading-platform/services/trade_orchestrator" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/trade_orchestrator/Dockerfile" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/trade_orchestrator/requirements.txt" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/trade_orchestrator/app.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/trade_orchestrator/mt5_client.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/trade_orchestrator/trade_manager.py" -Force | Out-Null
New-Item -ItemType File -Path "auto-trading-platform/services/trade_orchestrator/mt5_executor.py" -Force | Out-Null

Write-Host "Estructura creada exitosamente."