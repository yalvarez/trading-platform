-- Script para crear la tabla de configuración en PostgreSQL
CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(64) PRIMARY KEY,
    value VARCHAR(256) NOT NULL
);

-- Ejemplo de registros iniciales
INSERT INTO settings (key, value) VALUES
    ('SCALING_OUT_PIPS', '20'),
    ('SCALING_OUT_PERCENT', '50'),
    ('SCALING_OUT_MIN_LOT', '0.01'),
    ('SCALING_OUT_ENABLED', 'true')
ON CONFLICT (key) DO NOTHING;
