
from fastapi import FastAPI, Depends, HTTPException
from typing import List
from .schemas import Cuenta, CuentaCreate, Configuracion, ConfiguracionCreate, PermisoCopiado, PermisoCopiadoCreate, Proveedor, ProveedorCreate
from .models import Cuenta as CuentaModel, Configuracion as ConfiguracionModel, PermisoCopiado as PermisoCopiadoModel, Proveedor as ProveedorModel
from .crud import get_cuentas, create_cuenta, get_configuraciones, create_configuracion, get_permisos, create_permiso, update_permiso, delete_permiso, get_proveedores, create_proveedor, update_proveedor, delete_proveedor
@app.get("/proveedores", response_model=List[Proveedor])
def listar_proveedores(db: Session = Depends(get_db)):
    return get_proveedores(db)

@app.post("/proveedores", response_model=Proveedor)
def crear_proveedor(proveedor: ProveedorCreate, db: Session = Depends(get_db)):
    db_proveedor = ProveedorModel(**proveedor.dict())
    return create_proveedor(db, db_proveedor)

@app.put("/proveedores/{proveedor_id}", response_model=Proveedor)
def editar_proveedor(proveedor_id: int = Path(...), proveedor: ProveedorCreate = None, db: Session = Depends(get_db)):
    updated = update_proveedor(db, proveedor_id, proveedor.dict())
    if not updated:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return updated

@app.delete("/proveedores/{proveedor_id}", response_model=Proveedor)
def eliminar_proveedor(proveedor_id: int = Path(...), db: Session = Depends(get_db)):
    deleted = delete_proveedor(db, proveedor_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return deleted
from fastapi import Path
app = FastAPI()

from .dependencies import get_db
from sqlalchemy.orm import Session

app = FastAPI()

@app.get("/cuentas", response_model=List[Cuenta])
def listar_cuentas(db: Session = Depends(get_db)):
    return get_cuentas(db)

@app.post("/cuentas", response_model=Cuenta)
def crear_cuenta(cuenta: CuentaCreate, db: Session = Depends(get_db)):
    db_cuenta = CuentaModel(**cuenta.dict())
    return create_cuenta(db, db_cuenta)

@app.get("/configuraciones", response_model=List[Configuracion])
def listar_configuraciones(db: Session = Depends(get_db)):
    return get_configuraciones(db)


@app.get("/permisos", response_model=List[PermisoCopiado])
def listar_permisos(db: Session = Depends(get_db)):
    return get_permisos(db)

@app.post("/permisos", response_model=PermisoCopiado)
def crear_permiso(permiso: PermisoCopiadoCreate, db: Session = Depends(get_db)):
    db_permiso = PermisoCopiadoModel(**permiso.dict())
    return create_permiso(db, db_permiso)

@app.put("/permisos/{permiso_id}", response_model=PermisoCopiado)
def editar_permiso(permiso_id: int = Path(...), permiso: PermisoCopiadoCreate = None, db: Session = Depends(get_db)):
    updated = update_permiso(db, permiso_id, permiso.dict())
    if not updated:
        raise HTTPException(status_code=404, detail="Permiso no encontrado")
    return updated

@app.delete("/permisos/{permiso_id}", response_model=PermisoCopiado)
def eliminar_permiso(permiso_id: int = Path(...), db: Session = Depends(get_db)):
    deleted = delete_permiso(db, permiso_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Permiso no encontrado")
    return deleted
