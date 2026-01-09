import React, { useEffect, useState } from "react";
import { Table, TableHead, TableRow, TableCell, TableBody, Paper, Button } from "@mui/material";
import { getProviders, createProvider, updateProvider, deleteProvider } from "../api/providers";
import ProviderForm from "./ProviderForm";

function ProvidersTable() {
  const [providers, setProviders] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [editData, setEditData] = useState(null);

  useEffect(() => {
    getProviders().then(setProviders);
  }, []);

  const handleAdd = async (data) => {
    if (editData) {
      await updateProvider(editData.id, data);
      setEditData(null);
    } else {
      await createProvider(data);
    }
    getProviders().then(setProviders);
    setShowForm(false);
  };

  const handleDelete = async (id) => {
    if (window.confirm("¿Seguro que deseas eliminar este proveedor?")) {
      await deleteProvider(id);
      getProviders().then(setProviders);
    }
  };

  return (
    <Paper>
      <Button variant="contained" onClick={() => { setEditData(null); setShowForm(true); }} sx={{ m: 2 }}>
        Agregar proveedor
      </Button>
      {showForm && <ProviderForm onSubmit={handleAdd} initialData={editData} />}
      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Nombre</TableCell>
            <TableCell>Tipo</TableCell>
            <TableCell>Estado</TableCell>
            <TableCell></TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {providers.map((row, idx) => (
            <TableRow key={idx}>
              <TableCell>{row.nombre}</TableCell>
              <TableCell>{row.tipo}</TableCell>
              <TableCell>{row.estado ? "Activo" : "Inactivo"}</TableCell>
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

export default ProvidersTable;