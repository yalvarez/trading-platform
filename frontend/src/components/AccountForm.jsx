import React, { useState } from "react";
import { TextField, Button, Box } from "@mui/material";

function AccountForm({ onSubmit, initialData }) {
  const [form, setForm] = useState(initialData || {
    name: "",
    host: "",
    port: "",
    active: true,
    fixed_lot: "",
    chat_id: ""
  });

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit(form);
  };

  return (
    <Box component="form" onSubmit={handleSubmit} sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <TextField label="Nombre" name="name" value={form.name} onChange={handleChange} required />
      <TextField label="Host" name="host" value={form.host} onChange={handleChange} required />
      <TextField label="Puerto" name="port" value={form.port} onChange={handleChange} required type="number" />
      <TextField label="Fixed Lot" name="fixed_lot" value={form.fixed_lot} onChange={handleChange} required type="number" />
      <TextField label="Chat ID" name="chat_id" value={form.chat_id} onChange={handleChange} />
      <Button type="submit" variant="contained">Guardar</Button>
    </Box>
  );
}

export default AccountForm;
