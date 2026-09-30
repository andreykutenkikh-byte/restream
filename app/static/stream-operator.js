"use strict";
(() => {
  const ui = window.BroadcastRouteUI, root = document.querySelector("#operator-sessions"), message = document.querySelector("#operator-message");
  const pending = new Map(); let csrf = null, fetching = false, stopped = false, timer = null, failures = 0;
  async function request(path, body) {
    const options = {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(8000)};
    if (body !== undefined) {
      if (!pending.has(path)) pending.set(path, crypto.randomUUID());
      Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf || "", "Idempotency-Key": pending.get(path)}, body: JSON.stringify(body)});
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      if (response.status < 500) pending.delete(path);
      const error = new Error(response.status === 401 ? "Доступ истёк или отозван. Получите новую ссылку у администратора." : (await response.json()).error?.code || "Нет связи");
      error.unauthorized = response.status === 401; throw error;
    }
    if (body !== undefined) pending.delete(path);
    return response.status === 204 ? null : response.json();
  }
  const switchRequest = async (output, route, handoff) => {
    await request(`/stream-operator/api/outputs/${output}/switch`, {target_route_id: route, handoff_ingress: handoff}); await refresh();
  };
  async function cancel(id) { await request(`/stream-operator/api/switches/${id}/cancel`, {}); await refresh(); }
  function render(data) {
    root.replaceChildren();
    for (const session of data.sessions) {
      const card = ui.el("article", undefined, "broadcast-card"); card.append(ui.el("h2", session.name));
      for (const output of session.outputs) {
        const block = ui.el("section", undefined, "broadcast-output"); block.dataset.outputId = output.id;
        block.append(ui.el("h3", output.name), ui.el("p", `YouTube overall: ${output.youtube.stream_status} / ${output.youtube.health_status} · Зритель: UNKNOWN`), ui.operation(output, cancel));
        for (const route of output.routes) {
          const row = ui.el("div", undefined, "broadcast-route"); row.dataset.routeId = route.id;
          row.append(ui.el("strong", `${ui.name(data, route.node_id)} · ${route.role} / ${route.youtube_slot || "NONE"}`), ui.links(route));
          if (route.role === "standby") {
            const b = ui.button(`Переключиться на ${ui.name(data, route.node_id)}`, () => ui.switchDialog(data, output, route, switchRequest));
            b.disabled = !route.server_ready || !output.desired_enabled || !output.youtube.has_backup || Boolean(output.switch?.active);
            row.append(b);
          } block.append(row);
        }
        if (output.recommendation.target_route_ids.length) block.append(ui.el("p", "Есть готовый сервер для ручного переключения. Прямой маршрут телефона к нему не измерен."));
        card.append(block);
      } root.append(card);
    }
  }
  async function refresh() {
    if (fetching || stopped) return; fetching = true;
    try {
      const data = await request("/stream-operator/api/state"); csrf = data.csrf_token; failures = 0; render(data);
      message.textContent = `Только разрешённая сессия · доступ до ${data.expires_at}`; message.dataset.error = "false";
      document.querySelector("#operator-logout").hidden = false;
    } catch (error) {
      failures++; message.textContent = error.unauthorized ? error.message : "Мониторинг недоступен. Состояние эфира UNKNOWN; это не подтверждает остановку передачи.";
      message.dataset.error = "true";
      root.querySelectorAll("button").forEach(b => { b.disabled = true; });
      if (error.unauthorized) { stopped = true; root.replaceChildren(); document.querySelector("#operator-logout").hidden = true; }
    } finally { fetching = false; }
  }
  function schedule() {
    clearTimeout(timer); if (stopped) return;
    timer = setTimeout(async () => { if (!document.hidden) await refresh(); schedule(); }, Math.min(15000, 2000 * 2 ** failures));
  }
  document.querySelector("#operator-logout").addEventListener("click", async () => {
    try { await request("/stream-operator/api/logout", {}); stopped = true; clearTimeout(timer); root.replaceChildren(); message.textContent = "Доступ отключён."; }
    catch (error) { message.textContent = error.message; }
  });
  async function start() {
    let fragment = location.hash;
    history.replaceState(null, "", location.pathname); // Erase before any network request.
    if (fragment) {
      const match = /^#pair=([A-Za-z0-9_-]{43})$/.exec(fragment); fragment = "";
      if (!match) { message.textContent = "Некорректная ссылка доступа."; return; }
      try { await request("/stream-operator/api/pair", {token: match[1]}); }
      catch (error) { message.textContent = error.message; return; }
      match[1] = "";
    }
    await refresh(); schedule();
  }
  window.addEventListener("pagehide", () => clearTimeout(timer));
  window.addEventListener("pageshow", () => { if (csrf && !stopped) schedule(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !stopped) { refresh(); schedule(); } });
  start();
})();
