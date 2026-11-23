import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import "./components/controls/controls.css";
import { TooltipProvider } from "./components/controls";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <TooltipProvider delay={400}>
      <App />
    </TooltipProvider>
  </React.StrictMode>,
);
