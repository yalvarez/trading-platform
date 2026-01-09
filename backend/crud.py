from .models import Proveedor
# Proveedores
def get_proveedores(db: Session):
    return db.query(Proveedor).all()

def create_proveedor(db: Session, proveedor: Proveedor):
    db.add(proveedor)
    db.commit()
    db.refresh(proveedor)
    return proveedor

def update_proveedor(db: Session, proveedor_id: int, proveedor_data: dict):
    proveedor = db.query(Proveedor).filter(Proveedor.id == proveedor_id).first()
    if not proveedor:
        return None
    for key, value in proveedor_data.items():
        setattr(proveedor, key, value)
    db.commit()
    db.refresh(proveedor)
    return proveedor

def delete_proveedor(db: Session, proveedor_id: int):
    proveedor = db.query(Proveedor).filter(Proveedor.id == proveedor_id).first()
    if not proveedor:
        return None
    db.delete(proveedor)
    db.commit()
    return proveedor
from sqlalchemy.orm import Session
from .models import Cuenta, Configuracion, PermisoCopiado

# Cuentas

def get_cuentas(db: Session):
    return db.query(Cuenta).all()

def create_cuenta(db: Session, cuenta: Cuenta):
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return cuenta

# Configuraciones

def get_configuraciones(db: Session):
    return db.query(Configuracion).all()

def create_configuracion(db: Session, config: Configuracion):
    db.add(config)
    db.commit()
    db.refresh(config)
    return config

# Permisos de copiado
# Permisos de copiado
def get_permisos(db: Session):
    return db.query(PermisoCopiado).all()


def create_permiso(db: Session, permiso: PermisoCopiado):
    db.add(permiso)
    db.commit()
    db.refresh(permiso)
    return permiso

def update_permiso(db: Session, permiso_id: int, permiso_data: dict):
    permiso = db.query(PermisoCopiado).filter(PermisoCopiado.id == permiso_id).first()
    if not permiso:
        return None
    for key, value in permiso_data.items():
        setattr(permiso, key, value)
    db.commit()
    db.refresh(permiso)
    return permiso

def delete_permiso(db: Session, permiso_id: int):
    permiso = db.query(PermisoCopiado).filter(PermisoCopiado.id == permiso_id).first()
    if not permiso:
        return None
    db.delete(permiso)
    db.commit()
    return permiso
