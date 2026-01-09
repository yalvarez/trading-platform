import React, { useState, useEffect } from "react";
import { TextField, Button, Box, Checkbox, FormControlLabel } from "@mui/material";

function PermissionForm({ onSubmit, initialData }) {
  const [form, setForm] = useState({ cuenta: "", proveedor: "", activo: true });

  useEffect(() => {
    if (initialData) setForm(initialData);
  }, [initialData]);

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setForm({ ...form, [name]: type === "checkbox" ? checked : value });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit(form);
  };

  return (
    <Box component="form" onSubmit={handleSubmit} sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <TextField label="Cuenta" name="cuenta" value={form.cuenta} onChange={handleChange} required />
      <TextField label="Proveedor" name="proveedor" value={form.proveedor} onChange={handleChange} required />
      <FormControlLabel control={<Checkbox checked={form.activo} onChange={handleChange} name="activo" />} label="Activo" />
      <Button type="submit" variant="contained">Guardar</Button>
    </Box>
  );
}

export default PermissionForm;
