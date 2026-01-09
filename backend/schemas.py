class ProveedorBase(BaseModel):
    nombre: str
    tipo: str
    estado: bool

class ProveedorCreate(ProveedorBase):
    pass

class Proveedor(ProveedorBase):
    id: int
    class Config:
        orm_mode = True
from pydantic import BaseModel
from typing import Optional

class CuentaBase(BaseModel):
    name: str
    host: str
    port: int
    active: bool
    fixed_lot: float
    chat_id: Optional[int]

class CuentaCreate(CuentaBase):
    pass

class Cuenta(CuentaBase):
    id: int
    class Config:
        orm_mode = True

    class PermisoCopiadoBase(BaseModel):
        cuenta: str
        proveedor: str
        activo: bool

    class PermisoCopiadoCreate(PermisoCopiadoBase):
        pass

    class PermisoCopiado(PermisoCopiadoBase):
        id: int
        class Config:
            orm_mode = True

class ConfiguracionBase(BaseModel):
    clave: str
    valor: str

class ConfiguracionCreate(ConfiguracionBase):
    pass

class Configuracion(ConfiguracionBase):
    id: int
    class Config:
        orm_mode = True
