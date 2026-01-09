import React from "react";
import ProvidersTable from "../components/ProvidersTable";
import { Container, Typography } from "@mui/material";

function Providers() {
  return (
    <Container>
      <Typography variant="h5" gutterBottom>
        Proveedores
      </Typography>
      <ProvidersTable />
    </Container>
  );
}

export default Providers;
