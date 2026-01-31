#!/bin/bash
set -e

# Esperar a que postgres esté listo
echo "Esperando a que PostgreSQL esté disponible..."
while ! pg_isready -h postgres -p 5432 -U trading_user; do
  sleep 2
done

echo "Creando tablas y registros iniciales..."
psql postgresql://trading_user:trading_pass@postgres:5432/trading_config -f services/common/config_db_schema_full.sql

echo "Migrando configuración desde .env..."
export $(grep -v '^#' .env | xargs)
python3 services/common/config_db_migration.py

echo "Listo. Configuración migrada a PostgreSQL."
