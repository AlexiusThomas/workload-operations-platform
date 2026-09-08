import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { TutorialProvider } from "./tutorial/TutorialProvider";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <TutorialProvider>
          <App />
        </TutorialProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
