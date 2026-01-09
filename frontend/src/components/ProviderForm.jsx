import React, { useState } from "react";
import { TextField, Button, Box } from "@mui/material";

function ProviderForm({ onSubmit, initialData }) {
  const [form, setForm] = useState(initialData || { nombre: "", tipo: "", estado: true });

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setForm({
      ...form,
      [name]: type === "checkbox" ? checked : value
    });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit({ ...form, estado: Boolean(form.estado) });
  };

  return (
    <Box component="form" onSubmit={handleSubmit} sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <TextField label="Nombre" name="nombre" value={form.nombre} onChange={handleChange} required />
      <TextField label="Tipo" name="tipo" value={form.tipo} onChange={handleChange} required />
      <label>
        <input
          type="checkbox"
          name="estado"
          checked={!!form.estado}
          onChange={handleChange}
        />
        Activo
      </label>
      <Button type="submit" variant="contained">Guardar</Button>
    </Box>
  );
}

export default ProviderForm;
