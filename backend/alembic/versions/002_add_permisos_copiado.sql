-- Alembic migration: add permisos_copiado table
CREATE TABLE permisos_copiado (
    id SERIAL PRIMARY KEY,
    cuenta VARCHAR(100) NOT NULL,
    proveedor VARCHAR(100) NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT true
);
