import React from "react";
import AccountsTable from "../components/AccountsTable";
import { Container, Typography } from "@mui/material";

function Accounts() {
  return (
    <Container>
      <Typography variant="h5" gutterBottom>
        Cuentas
      </Typography>
      <AccountsTable />
    </Container>
  );
}

export default Accounts;
