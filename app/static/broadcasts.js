/* All credential inputs are write-only; no local/session storage or HTML interpolation. */
"use strict";
(() => {
  const root = document.querySelector("#broadcast-sessions");
  if (!root) return;
  let snapshot = null, loading = false, busy = false, timer = null;
  const pending = new Map();
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;
  const message = (value, error = false) => {
    const target = document.querySelector("#broadcast-message");
    target.textContent = value; target.dataset.error = String(error);
  };
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  async function api(path, body, requestKey = path) {
    const options = {credentials: "same-origin", signal: AbortSignal.timeout(20000)};
    if (body !== undefined) {
      if (!pending.has(requestKey)) pending.set(requestKey, crypto.randomUUID());
      Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json",
        "X-CSRF-Token": csrf(), "Idempotency-Key": pending.get(requestKey)}, body: JSON.stringify(body)});
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) {
      if (response.status < 500) pending.delete(requestKey);
      throw new Error(result.error?.code || "Запрос не выполнен");
    }
    if (body !== undefined) pending.delete(requestKey);
    return result;
  }
  async function action(button, task) {
    if (busy) return;
    busy = true; button.disabled = true;
    try { await task(); message("Запрос принят. Статус обновляется по данным сервера."); await refresh(); }
    catch (error) { message(error.message || "Нет связи с панелью", true); }
    finally { busy = false; button.disabled = false; }
  }
  const button = (text, task) => {
    const b = el("button", text, "button button--quiet button--small"); b.type = "button";
    b.addEventListener("click", () => action(b, task)); return b;
  };
  const nodeName = id => snapshot.nodes.find(n => n.id === id)?.display_name || id;
  function choices(select, items) {
    const value = select.value;
    select.replaceChildren(...items.map(([id, name]) => {
      const option = el("option", name); option.value = id; return option;
    }));
    if (items.some(([id]) => id === value)) select.value = value;
  }
  function render() {
    document.querySelectorAll(".node-select").forEach(s => choices(s, snapshot.nodes.map(n => [n.id, n.display_name])));
    choices(document.querySelector("#session-select"), snapshot.sessions.map(s => [s.id, s.name]));
    choices(document.querySelector("#channel-select"), snapshot.accounts.filter(a => a.status === "connected").map(a => [a.channel_id, a.display_name]));
    const channels = document.querySelector("#channels"); channels.replaceChildren();
    for (const account of snapshot.accounts) {
      const row = el("p", `${account.display_name} · ${account.status} `);
      row.append(button("Отозвать доступ", () => api(`/api/broadcasts/youtube/${account.channel_id}/revoke`, {})));
      channels.append(row);
    }
    root.replaceChildren();
    for (const session of snapshot.sessions) {
      const card = el("article", undefined, "broadcast-card"); card.append(el("h2", session.name));
      card.dataset.sessionId = session.id;
      card.append(el("p", `Moblin → ${nodeName(session.ingress_node_id)} · ${session.policy} · 1080 × 1920 / 30`));
      const batch = el("div", undefined, "broadcast-actions");
      for (const enabled of [true, false]) batch.append(button(enabled ? "Запустить все выходы" : "Остановить все выходы", async () => {
        const result = await api("/api/broadcasts/outputs/intent", {output_ids: session.outputs.map(o => o.id), enabled}, `batch:${session.id}:${enabled}`);
        const failed = result.results.filter(r => !r.accepted); if (failed.length) throw new Error(failed.map(r => r.code).join(", "));
      }));
      card.append(batch);
      for (const output of session.outputs) {
        const block = el("section", undefined, "broadcast-output");
        block.dataset.outputId = output.id;
        block.append(el("h3", output.name), el("p", output.state, "broadcast-state"));
        block.append(el("p", `YouTube: ${output.youtube.lifecycle_status} · Приём: ${output.youtube.stream_status} · Зритель: UNKNOWN`));
        if (output.safe_error_code) block.append(el("p", output.safe_error_code));
        const controls = el("div", undefined, "broadcast-actions");
        for (const enabled of [true, false]) controls.append(button(enabled ? "Начать передачу" : "Остановить передачу", () => api(`/api/broadcasts/outputs/${output.id}/intent`, {enabled}, `intent:${output.id}:${enabled}`)));
        if (output.mode === "youtube_api") {
          controls.append(button("Создать событие", () => api(`/api/broadcasts/outputs/${output.id}/provision`, {})));
          controls.append(button("Обновить YouTube", () => api(`/api/broadcasts/outputs/${output.id}/youtube-status`, {})));
          controls.append(button("Начать эфир YouTube", () => api(`/api/broadcasts/outputs/${output.id}/transition`, {state: "live"})));
          controls.append(button("Завершить событие YouTube", async () => {
            if (window.confirm("Завершить событие YouTube? Вернуть его в эфир будет нельзя.")) await api(`/api/broadcasts/outputs/${output.id}/transition`, {state: "complete"});
          }));
        }
        block.append(controls);
        const routes = el("div", undefined, "broadcast-routes");
        for (const route of output.routes) {
          const routeCard = el("div", undefined, "broadcast-route");
          routeCard.dataset.routeId = route.id;
          routeCard.append(el("strong", nodeName(route.node_id)), el("p", `${route.role} · ${route.youtube_slot || "Свободный слот"}`));
          routeCard.append(el("p", `Источник: ${route.source_kind} · Телефон → резерв: UNKNOWN`));
          routes.append(routeCard);
        }
        block.append(routes);
        const add = el("select"); add.setAttribute("aria-label", `Резерв для ${output.name}`);
        choices(add, snapshot.nodes.filter(n => !output.routes.some(r => r.node_id === n.id)).map(n => [n.id, n.display_name]));
        block.append(add, button("Добавить резервный сервер", async () => {
          if (add.value) await api(`/api/broadcasts/outputs/${output.id}/routes`, {node_id: add.value}, `route:${output.id}:${add.value}`);
        }));
        card.append(block);
      }
      const details = el("details"); details.append(el("summary", "История · UTC"));
      const events = el("ul", undefined, "broadcast-events");
      session.events.forEach(event => events.append(el("li", `${event.created_at} · ${event.event_type}`)));
      details.append(events); card.append(details); root.append(card);
    }
    if (!snapshot.sessions.length) root.append(el("p", "Сессий пока нет. Добавьте источник и первый выход."));
    document.dispatchEvent(new CustomEvent("broadcast-render", {detail: {snapshot, root, api, button, el, action}}));
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try { snapshot = await api("/api/broadcasts"); render(); }
    catch (error) { message(`Мониторинг недоступен: ${error.message}. Это не подтверждает остановку эфира.`, true); }
    finally { loading = false; }
  }
  document.querySelector("#session-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, data = Object.fromEntries(new FormData(form));
    action(form.querySelector("button"), async () => { await api("/api/broadcasts/sessions", data); form.reset(); });
  });
  document.querySelector("#output-mode").addEventListener("change", event => {
    const manual = event.target.value === "manual";
    document.querySelector("#manual-fields").hidden = !manual; document.querySelector("#api-fields").hidden = manual;
    document.querySelector('[name="stream_key"]').value = "";
  });
  document.querySelector("#output-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget, values = Object.fromEntries(new FormData(form));
    const data = {name: values.name, node_id: values.node_id, mode: values.mode};
    if (values.mode === "manual") Object.assign(data, {primary_url: values.primary_url, backup_url: values.backup_url || null, stream_key: values.stream_key});
    else Object.assign(data, {channel_id: values.channel_id, visibility: values.visibility, scheduled_start: values.scheduled_start ? `${values.scheduled_start}:00Z` : null});
    action(form.querySelector('button[type="submit"]'), async () => {
      try { await api(`/api/broadcasts/sessions/${values.session_id}/outputs`, data, "create-output"); }
      finally { form.querySelector('[name="stream_key"]').value = ""; data.stream_key = undefined; }
    });
  });
  document.querySelector("#youtube-connect").addEventListener("click", event => action(event.currentTarget, async () => {
    const result = await api("/api/broadcasts/youtube/connect", {}); window.location.assign(result.url);
  }));
  function schedule() { clearTimeout(timer); timer = setTimeout(async () => { if (!document.hidden && !busy) await refresh(); schedule(); }, 5000); }
  window.addEventListener("pagehide", () => clearTimeout(timer));
  window.addEventListener("pageshow", schedule);
  refresh(); schedule();
})();
