import React from "react";
import { Typography, Container } from "@mui/material";

function Dashboard() {
  return (
    <Container>
      <Typography variant="h4" gutterBottom>
        Panel de administración
      </Typography>
      <Typography variant="body1">
        Bienvenido al dashboard de la plataforma de trading.
      </Typography>
    </Container>
  );
}

export default Dashboard;
