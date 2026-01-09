import React from "react";
import { BrowserRouter as Router, Routes, Route } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Accounts from "./pages/Accounts";
import Providers from "./pages/Providers";
import Configurations from "./pages/Configurations";
import Permissions from "./pages/Permissions";
import MainMenu from "./components/MainMenu";

function App() {
  return (
    <Router>
      <MainMenu />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/providers" element={<Providers />} />
        <Route path="/configurations" element={<Configurations />} />
        <Route path="/permissions" element={<Permissions />} />
      </Routes>
    </Router>
  );
}

export default App;
