import React from "react";
import PermissionsTable from "../components/PermissionsTable";
import { Container, Typography } from "@mui/material";

function Permissions() {
  return (
    <Container>
      <Typography variant="h5" gutterBottom>
        Permisos de Copiado
      </Typography>
      <PermissionsTable />
    </Container>
  );
}

export default Permissions;
