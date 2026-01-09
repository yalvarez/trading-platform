import React from "react";
import ConfigTable from "../components/ConfigTable";
import { Container, Typography } from "@mui/material";

function Configurations() {
  return (
    <Container>
      <Typography variant="h5" gutterBottom>
        Configuraciones Globales
      </Typography>
      <ConfigTable />
    </Container>
  );
}

export default Configurations;
