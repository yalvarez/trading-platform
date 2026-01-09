class Proveedor(Base):
    __tablename__ = "proveedores"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    tipo = Column(String(50), nullable=False)
    estado = Column(Boolean, default=True, nullable=False)
from sqlalchemy import Column, Integer, String, Boolean, Numeric, BigInteger, Text
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Cuenta(Base):
    __tablename__ = "cuentas"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    host = Column(String(100), nullable=False)
    port = Column(Integer, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    fixed_lot = Column(Numeric(10,4), nullable=False)
    chat_id = Column(BigInteger)

class Configuracion(Base):
    __tablename__ = "configuraciones"
    id = Column(Integer, primary_key=True, index=True)
    clave = Column(String(100), unique=True, nullable=False)
    valor = Column(Text, nullable=False)

    class PermisoCopiado(Base):
        __tablename__ = "permisos_copiado"
        id = Column(Integer, primary_key=True, index=True)
        cuenta = Column(String(100), nullable=False)
        proveedor = Column(String(100), nullable=False)
        activo = Column(Boolean, default=True, nullable=False)
