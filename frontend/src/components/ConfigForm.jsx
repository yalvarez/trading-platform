import React, { useState } from "react";
import { TextField, Button, Box } from "@mui/material";

function ConfigForm({ onSubmit }) {
  const [form, setForm] = useState({ clave: "", valor: "" });

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit(form);
  };

  return (
    <Box component="form" onSubmit={handleSubmit} sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <TextField label="Clave" name="clave" value={form.clave} onChange={handleChange} required />
      <TextField label="Valor" name="valor" value={form.valor} onChange={handleChange} required />
      <Button type="submit" variant="contained">Guardar</Button>
    </Box>
  );
}

export default ConfigForm;
