import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { installLlmTransport } from "./services/llmTransport";
import "./styles/global.css";

// Route LLM HTTP through the Rust backend (CORS-free) before the app mounts.
installLlmTransport();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
