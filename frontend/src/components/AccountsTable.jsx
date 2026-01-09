import React, { useEffect, useState } from "react";
import { Table, TableHead, TableRow, TableCell, TableBody, Paper, Button } from "@mui/material";
import { getAccounts, createAccount, updateAccount } from "../api/accounts";
import AccountForm from "./AccountForm";

  const [accounts, setAccounts] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [editData, setEditData] = useState(null);

  useEffect(() => {
    getAccounts().then(setAccounts);
  }, []);

  const handleAdd = async (data) => {
    if (editData) {
      await updateAccount(editData.id, data);
      setEditData(null);
    } else {
      await createAccount(data);
    }
    getAccounts().then(setAccounts);
    setShowForm(false);
  };

  return (
    <Paper>
      <Button variant="contained" onClick={() => setShowForm(true)} sx={{ m: 2 }}>
        Agregar cuenta
      </Button>
      {showForm && <AccountForm onSubmit={handleAdd} />}
      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Nombre</TableCell>
            <TableCell>Host</TableCell>
            <TableCell>Puerto</TableCell>
            <TableCell>Activo</TableCell>
            <TableCell>Fixed Lot</TableCell>
            <TableCell>Chat ID</TableCell>
          </TableRow>
        const handleDelete = async (id) => {
          if (window.confirm("¿Seguro que deseas eliminar esta cuenta?")) {
            await require("../api/accounts").deleteAccount(id);
            getAccounts().then(setAccounts);
          }
        };

        const handleDelete = async (id) => {
          if (window.confirm("¿Seguro que deseas eliminar esta cuenta?")) {
            await require("../api/accounts").deleteAccount(id);
            getAccounts().then(setAccounts);
          }
        };

        return (
          <Paper>
            <Button variant="contained" onClick={() => { setEditData(null); setShowForm(true); }} sx={{ m: 2 }}>
              Agregar cuenta
            </Button>
            {showForm && <AccountForm onSubmit={handleAdd} initialData={editData} />}
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Nombre</TableCell>
                  <TableCell>Host</TableCell>
                  <TableCell>Puerto</TableCell>
                  <TableCell>Activo</TableCell>
                  <TableCell>Fixed Lot</TableCell>
                  <TableCell>Chat ID</TableCell>
                  <TableCell></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {accounts.map((row, idx) => (
                  <TableRow key={idx}>
                    <TableCell>{row.name}</TableCell>
                    <TableCell>{row.host}</TableCell>
                    <TableCell>{row.port}</TableCell>
                    <TableCell>{row.active ? "Sí" : "No"}</TableCell>
                    <TableCell>{row.fixed_lot}</TableCell>
                    <TableCell>{row.chat_id}</TableCell>
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
