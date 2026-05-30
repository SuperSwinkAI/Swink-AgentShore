import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { SessionProvider } from "./services/sessionContext";
import "./styles.css";
// The agentshore-dashboard lib build emits its stylesheet separately
// (dist/index.css). Side-effect import so the desktop bundle picks
// it up and the Dashboard React tree renders styled.
import "agentshore-dashboard/dist/index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <SessionProvider>
        <App />
      </SessionProvider>
    </BrowserRouter>
  </React.StrictMode>
);
