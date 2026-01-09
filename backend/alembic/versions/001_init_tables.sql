-- Alembic migration: create initial tables
CREATE TABLE cuentas (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    host VARCHAR(100) NOT NULL,
    port INTEGER NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    fixed_lot NUMERIC(10,4) NOT NULL,
    chat_id BIGINT
);

CREATE TABLE configuraciones (
    id SERIAL PRIMARY KEY,
    clave VARCHAR(100) UNIQUE NOT NULL,
    valor TEXT NOT NULL
);
