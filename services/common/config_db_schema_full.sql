-- Tabla de configuración general
CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(64) PRIMARY KEY,
    value VARCHAR(256) NOT NULL
);

-- Tabla de cuentas de trading
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    host VARCHAR(64) NOT NULL,
    port INTEGER NOT NULL,
    active BOOLEAN NOT NULL,
    fixed_lot FLOAT NOT NULL,
    chat_id BIGINT NOT NULL,
    trading_mode VARCHAR(32) NOT NULL
);

-- Tabla de canales permitidos por cuenta
CREATE TABLE IF NOT EXISTS account_channels (
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (account_id, channel_id)
);

-- Tabla de proveedores de señales
CREATE TABLE IF NOT EXISTS signal_providers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    parser VARCHAR(64) NOT NULL
);

-- Tabla de canales y sus proveedores
CREATE TABLE IF NOT EXISTS channel_providers (
    channel_id BIGINT NOT NULL,
    provider_id INTEGER REFERENCES signal_providers(id) ON DELETE CASCADE,
    PRIMARY KEY (channel_id, provider_id)
);

-- Registros iniciales para settings
-- INSERT INTO settings (key, value) VALUES
--     ('DEFAULT_SL_XAUUSD_PIPS', '60'),
--     ('REDIS_URL', 'redis://redis:6379/0'),
--     ('TG_API_ID', '21104104'),
--     ('TG_API_HASH', '7afb33549783f0315ae6538370c78ab9'),
--     ('TG_PHONE', '+18295201448'),
--     ('TELEGRAM_INGESTOR_URL', 'http://telegram_ingestor:8000'),
--     ('TG_TEST_CHAT_ID', '8592452414'),
--     ('LOG_LEVEL', 'DEBUG'),
--     ('TRADING_WINDOWS', '00:00-23:59'),
--     ('SCALING_TRAMO_PIPS', '40'),
--     ('SCALING_PERCENT_PER_TRAMO', '25'),
--     ('ENTRY_WAIT_SECONDS', '90'),
--     ('ENTRY_POLL_MS', '200'),
--     ('ENTRY_BUFFER_POINTS', '1.5'),
--     ('MT5_WEB_USER', 'admin'),
--     ('MT5_WEB_PASS', 'admin123'),
--     ('DEDUP_TTL_SECONDS', '120'),
--     ('ENABLE_NOTIFICATIONS', 'true'),
--     ('ENABLE_ADVANCED_TRADE_MGMT', 'true'),
--     ('SCALP_TP1_PERCENT', '50'),
--     ('SCALP_TP2_PERCENT', '80'),
--     ('LONG_TP1_PERCENT', '50'),
--     ('LONG_TP2_PERCENT', '80'),
--     ('ENABLE_BREAKEVEN', 'true'),
--     ('BREAKEVEN_OFFSET_PIPS', '1'),
--     ('ENABLE_TRAILING', 'true'),
--     ('TRAILING_ACTIVATION_PIPS', '20'),
--     ('TRAILING_STOP_PIPS', '15'),
--     ('ENABLE_ADDON', 'true'),
--     ('ADDON_MAX_COUNT', '1'),
--     ('ADDON_LOT_FACTOR', '0.5'),
--     ('FAST_UPDATE_WINDOW_SECONDS', '30')
-- ON CONFLICT (key) DO NOTHING;
