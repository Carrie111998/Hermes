// agent-src/artifact_surface/static/hermes-live.js
// Minimal client polling helper for Hermes Canvas artifacts.
// Usage inside an artifact:
//   <script src="/static/hermes-live.js"></script>
//   <script>hermesLive.poll('/api/cron', render, {everySecs: 15});</script>
(function () {
  function poll(url, render, opts) {
    opts = opts || {};
    var everySecs = opts.everySecs || 15;
    function tick() {
      fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) { try { render(data); } catch (e) { console.error("hermesLive render", e); } })
        .catch(function (e) { console.error("hermesLive fetch", url, e); });
    }
    tick();
    return setInterval(tick, everySecs * 1000);
  }
  window.hermesLive = { poll: poll };
})();
