"use strict";
(() => {
  const root = document.querySelector("#broadcast-monitor"), ui = window.BroadcastRouteUI;
  let timer = null, busy = false;
  async function refresh() {
    if (busy || document.hidden) return;
    // The legacy HUD owns pairing and logout. Wait for its first authenticated
    // render and stop with it; a second poller must never race its cookie exchange.
    const hudState = document.body.dataset.hudState;
    if (["monitoring", "revoked", "logout-pending", "logout-error"].includes(hudState)) {
      root.replaceChildren(); return;
    }
    busy = true;
    try {
      const response = await fetch("/moblin-hud/api/broadcasts", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(8000)});
      if (!response.ok) { root.replaceChildren(); return; }
      const data = await response.json(); root.replaceChildren();
      for (const session of data.sessions) {
        const card = ui.el("section", undefined, "broadcast-card"); card.append(ui.el("h2", session.name));
        for (const output of session.outputs) {
          card.append(ui.el("h3", output.name));
          for (const route of output.routes) card.append(ui.el("strong", `${ui.name(data, route.node_id)} · ${route.role} / ${route.youtube_slot || "NONE"}`), ui.links(route, data, session));
          card.append(ui.operation(output, null));
        } root.append(card);
      }
    } catch (_) { root.replaceChildren(ui.el("p", "Мониторинг трансляций недоступен · UNKNOWN")); }
    finally { busy = false; }
  }
  function poll() { clearTimeout(timer); timer = setTimeout(async () => { await refresh(); poll(); }, 5000); }
  window.addEventListener("pagehide", () => clearTimeout(timer)); window.addEventListener("pageshow", poll);
  refresh(); poll();
})();
