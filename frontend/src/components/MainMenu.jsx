import React from "react";
import { AppBar, Toolbar, Button } from "@mui/material";
import { Link } from "react-router-dom";

function MainMenu() {
  return (
    <AppBar position="static">
      <Toolbar>
        <Button color="inherit" component={Link} to="/">Dashboard</Button>
        <Button color="inherit" component={Link} to="/accounts">Cuentas</Button>
        <Button color="inherit" component={Link} to="/providers">Proveedores</Button>
        <Button color="inherit" component={Link} to="/configurations">Configuraciones</Button>
        <Button color="inherit" component={Link} to="/permissions">Permisos</Button>
      </Toolbar>
    </AppBar>
  );
}

export default MainMenu;
