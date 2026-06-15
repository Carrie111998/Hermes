// agent-workspace dashboard plugin — frontend bundle (placeholder).
//
// Phase: backend-proxy-first. The backend (plugin_api.py) is the live half —
// it proxies the FRIDAI kernel over MCP behind the dashboard's auth. The tab is
// `hidden` in manifest.json, so this no-op registration is all the shell needs
// until the lanes + learnings panels land in the follow-up frontend phase.
(function () {
  try {
    var hub = window.__HERMES_PLUGINS__;
    if (hub && typeof hub.register === 'function') {
      // Register nothing visible yet; presence keeps the loader happy.
    }
  } catch (e) {
    /* never break the dashboard shell over a placeholder */
  }
})();
