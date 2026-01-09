import React, { useEffect, useState } from "react";
import { Table, TableHead, TableRow, TableCell, TableBody, Paper, Button } from "@mui/material";
import { getPermissions, createPermission, updatePermission, deletePermission } from "../api/permissions";
import PermissionForm from "./PermissionForm";

function PermissionsTable() {
  const [permissions, setPermissions] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [editData, setEditData] = useState(null);

  useEffect(() => {
    getPermissions().then(setPermissions);
  }, []);

  const handleAdd = async (data) => {
    if (editData) {
      await updatePermission(editData.id, data);
      setEditData(null);
    } else {
      await createPermission(data);
    }
    getPermissions().then(setPermissions);
    setShowForm(false);
  };

  const handleDelete = async (id) => {
    if (window.confirm("¿Seguro que deseas eliminar este permiso?")) {
      await deletePermission(id);
      getPermissions().then(setPermissions);
    }
  };

  return (
    <Paper>
      <Button variant="contained" onClick={() => { setEditData(null); setShowForm(true); }} sx={{ m: 2 }}>
        Agregar permiso
      </Button>
      {showForm && <PermissionForm onSubmit={handleAdd} initialData={editData} />}
      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Cuenta</TableCell>
            <TableCell>Proveedor</TableCell>
            <TableCell>Activo</TableCell>
            <TableCell></TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {permissions.map((row, idx) => (
            <TableRow key={idx}>
              <TableCell>{row.cuenta}</TableCell>
              <TableCell>{row.proveedor}</TableCell>
              <TableCell>{row.activo ? "Sí" : "No"}</TableCell>
              <TableCell>
                <Button variant="outlined" color="primary" size="small" sx={{mr:1}} onClick={() => { setEditData(row); setShowForm(true); }}>Editar</Button>
                <Button variant="outlined" color="error" size="small" onClick={() => handleDelete(row.id)}>Eliminar</Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Paper>
  );
}

export default PermissionsTable;
