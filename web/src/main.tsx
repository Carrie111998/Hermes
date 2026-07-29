import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import App from "./App";
import { SystemActionsProvider } from "./contexts/SystemActions";
import { I18nProvider } from "./i18n";
import { exposePluginSDK } from "./plugins";
import { ThemeProvider } from "./themes";
import { HERMES_BASE_PATH } from "./lib/api";

// `npm audit` flags react-router GHSA-qwww-vcr4-c8h2 (RSC Mode CSRF Bypass)
// against every react-router-dom >=7.12.0, and no fixed release of that package
// exists — 7.18.1 is the latest and there is no 8.x line. The advisory only
// applies to RSC mode; this app is a client-only SPA rendered through
// <BrowserRouter> below, with no RSC entry point, server router, or server
// actions, so the vulnerable code path is never loaded. The finding is
// therefore accepted rather than remediated. Revisit if this app ever adopts
// RSC mode or a server-side data router.
//
// Expose the plugin SDK before rendering so plugins loaded via <script>
// can access React, components, etc. immediately.
exposePluginSDK();

createRoot(document.getElementById("root")!).render(
  <BrowserRouter basename={HERMES_BASE_PATH || undefined}>
    <I18nProvider>
      <ThemeProvider>
        <SystemActionsProvider>
          <App />
        </SystemActionsProvider>
      </ThemeProvider>
    </I18nProvider>
  </BrowserRouter>,
);
