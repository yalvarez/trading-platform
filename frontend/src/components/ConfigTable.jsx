import React, { useEffect, useState } from "react";
import { Table, TableHead, TableRow, TableCell, TableBody, Paper, Button } from "@mui/material";
import { getConfigs, createConfig } from "../api/config";
import ConfigForm from "./ConfigForm";

function ConfigTable() {
  const [configs, setConfigs] = useState([]);
  const [showForm, setShowForm] = useState(false);

  useEffect(() => {
    getConfigs().then(setConfigs);
  }, []);

  const handleAdd = async (data) => {
    await createConfig(data);
    getConfigs().then(setConfigs);
    setShowForm(false);
  };

  return (
    <Paper>
      <Button variant="contained" onClick={() => setShowForm(true)} sx={{ m: 2 }}>
        Agregar configuración
      </Button>
      {showForm && <ConfigForm onSubmit={handleAdd} />}
      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Clave</TableCell>
            <TableCell>Valor</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {configs.map((row, idx) => (
            <TableRow key={idx}>
              <TableCell>{row.clave}</TableCell>
              <TableCell>{row.valor}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Paper>
  );
}

export default ConfigTable;
