"use strict";
(() => {
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, action) => { const b = el("button", text, "button button--quiet button--small"); b.type = "button"; b.addEventListener("click", async event => { try { await action(event); } catch (error) { b.parentElement?.append(el("p", error.message || "Запрос не выполнен")); } }); return b; };
  const name = (snapshot, id) => snapshot.nodes.find(n => n.id === id)?.display_name || "Сервер";
  const bitrate = value => Number.isFinite(value) ? `${(value / 1000000).toFixed(1)} Mbit/s` : "UNKNOWN";
  function links(route) {
    const box = el("div", undefined, "broadcast-link-status");
    box.dataset.severity = route.media_state === "LOST" || route.egress_link.state === "ERROR" ? "alert" : route.media_state === "LIVE" ? "live" : "unknown";
    box.append(el("p", `Сервер: ${route.server_ready ? "READY" : "UNKNOWN"} · Медиапоток: ${route.media_state}`),
      el("p", `Телефон → сервер: ${route.phone_link.state} · ${bitrate(route.phone_link.bitrate_bps)} · RTT/потери: UNKNOWN`),
      el("p", `Relay → relay: ${route.interrelay_link.state} · ${bitrate(route.interrelay_link.bitrate_bps)}`),
      el("p", `Relay → YouTube: ${route.egress_link.state} · Зритель: UNKNOWN`));
    return box;
  }
  function modal(title) {
    const dialog = el("dialog", undefined, "broadcast-dialog"), body = el("div");
    dialog.append(el("h2", title), body);
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog); dialog.showModal();
    return {dialog, body};
  }
  function switchDialog(snapshot, output, target, send) {
    const old = output.routes.find(r => r.role === "current");
    const {dialog, body} = modal(`Переключиться на ${name(snapshot, target.node_id)}`);
    body.append(el("p", `Текущий выход: ${name(snapshot, old.node_id)} / ${old.youtube_slot}. Он продолжит передачу, пока целевой publisher не готов.`),
      el("p", "Ключ уже сохранён у трансляции. Повторный ввод не нужен. Готовность сервера не подтверждает прямой маршрут телефона."));
    const label = el("label"), handoff = el("input"); handoff.type = "checkbox"; handoff.checked = true;
    label.append(handoff, document.createTextNode(" Затем сменить вход Moblin для всей сессии")); body.append(label);
    const status = el("p"); status.setAttribute("role", "status"); body.append(status);
    let pending = false;
    const confirm = button("Подготовить и переключить", async () => {
      if (pending) return; pending = true; confirm.disabled = true;
      try { await send(output.id, target.id, handoff.checked); dialog.close(); }
      catch (error) { status.textContent = error.message || "Нет связи. Повторите запрос."; }
      finally { pending = false; confirm.disabled = false; }
    });
    body.append(confirm, button("Закрыть", () => dialog.close()));
  }
  function operation(output, cancel) {
    const box = el("div", undefined, "broadcast-operation"), op = output.switch;
    if (!op) return box;
    box.append(el("p", `Переключение: ${op.state}`, "switch-state"));
    if (op.safe_error_code) box.append(el("p", `Причина: ${op.safe_error_code}`));
    if (op.active && !op.cutover_at && op.state !== "ROLLING_BACK" && cancel) box.append(button("Отменить переключение", () => cancel(op.id)));
    if (op.active && op.cutover_at) {
      box.append(el("p", "Сервер передачи переключён. Ожидается прямое подключение Moblin."));
      const link = el("a", "Открыть Moblin", "button"); link.href = "moblin://";
      box.append(link, el("p", "В Moblin выберите заранее сохранённый профиль целевого сервера. Если приложение требует остановки, остановите отправку телефона и снова начните её. Возможен краткий разрыв; событие YouTube не завершается."));
    }
    const gap = op.durations?.source_switch_gap_ms;
    if (Number.isFinite(gap)) box.append(el("p", `Разрыв publisher при смене источника: ${Math.round(gap)} мс`));
    return box;
  }
  window.BroadcastRouteUI = {el, button, name, links, modal, switchDialog, operation};
})();
