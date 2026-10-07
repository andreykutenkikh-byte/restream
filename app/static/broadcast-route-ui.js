"use strict";
(() => {
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, action) => { const b = el("button", text, "button button--quiet button--small"); b.type = "button"; b.addEventListener("click", async event => { try { await action(event); } catch (error) { b.parentElement?.append(el("p", error.message || "Запрос не выполнен")); } }); return b; };
  const name = (snapshot, id) => snapshot.nodes.find(n => n.id === id)?.display_name || "Сервер";
  const bitrate = value => Number.isFinite(value) ? `${(value / 1000000).toFixed(1)} Mbit/s` : "UNKNOWN";
  function links(route, snapshot, session) {
    const box = el("div", undefined, "broadcast-link-status");
    box.dataset.severity = route.media_state === "LOST" || route.egress_link.state === "ERROR" ? "alert" : route.media_state === "LIVE" ? "live" : "unknown";
    box.append(el("p", `Сервер: ${route.server_ready ? "READY" : "UNKNOWN"} · Медиапоток: ${route.media_state}`),
      el("p", `Телефон → сервер: ${route.phone_link.state} · ${bitrate(route.phone_link.bitrate_bps)} · RTT/потери: UNKNOWN`),
      el("p", `Relay → relay: ${route.interrelay_link.state} · ${bitrate(route.interrelay_link.bitrate_bps)}`),
      el("p", `Relay → YouTube: ${route.egress_link.state} · Зритель: UNKNOWN`));
    if (snapshot && session && route.role === "current") {
      const path = route.phone_link.state === "MEASURED" ? name(snapshot, route.node_id) :
        route.interrelay_link.state === "MEDIA_READY" ? `${name(snapshot, session.ingress_node_id)} → ${name(snapshot, route.node_id)}` : "UNKNOWN";
      box.append(el("p", `Наблюдаемый маршрут: OBS/Moblin → ${path} → YouTube · воспроизведение зрителем UNKNOWN`, "actual-topology"));
    }
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
    const handoff = el("select"); handoff.setAttribute("aria-label", "Режим переключения");
    for (const [value, text] of [["egress", "Сменить сервер отправки"], ["full", `Полностью перейти на ${name(snapshot, target.node_id)}`]]) {
      const option = el("option", text); option.value = value; handoff.append(option);
    }
    handoff.value = "full";
    const explanation = el("p"), sourceStep = el("p");
    const explain = () => {
      explanation.textContent = handoff.value === "full" ? "FULL_ROUTE: сначала система подготовит сервер отправки. Затем потребуется подключить источник к целевому серверу. Вход меняется для всех outputs этой сессии." :
        "EGRESS_ONLY: система меняет выход к YouTube. OBS/Moblin продолжает передавать на прежний вход; действий в источнике нет. Старый ingress остаётся в маршруте.";
      sourceStep.textContent = handoff.value === "full" ? "Moblin: выберите заранее сохранённый профиль целевого сервера и переподключите отправку. OBS: остановите только отправку OBS, выберите подготовленный SRT URL целевого сервера и запустите отправку. Не завершайте событие YouTube. Система дождётся пригодного прямого видео и аудио." : "YouTube key повторно вводить не нужно. Событие YouTube не создаётся заново и не завершается.";
    };
    handoff.addEventListener("change", explain); explain(); body.append(handoff, explanation, sourceStep);
    const status = el("p"); status.setAttribute("role", "status"); body.append(status);
    let pending = false;
    const confirm = button("Подготовить и переключить", async () => {
      if (pending) return; pending = true; confirm.disabled = true;
      try { await send(output.id, target.id, handoff.value === "full"); dialog.close(); }
      catch (error) { status.textContent = error.message || "Нет связи. Повторите запрос."; }
      finally { pending = false; confirm.disabled = false; }
    });
    body.append(confirm, button("Закрыть", () => dialog.close()));
  }
  function operation(output, cancel) {
    const box = el("div", undefined, "broadcast-operation"), op = output.switch;
    if (!op) return box;
    box.append(el("p", `Переключение: ${op.state}`, "switch-state"), el("p", `Режим: ${op.durations?.handoff_ingress ? "FULL_ROUTE" : "EGRESS_ONLY"}`));
    if (op.safe_error_code) box.append(el("p", `Причина: ${op.safe_error_code}`));
    if (op.active && !op.cutover_at && op.state !== "ROLLING_BACK" && cancel) box.append(button("Отменить переключение", () => cancel(op.id)));
    if (op.active && op.cutover_at) {
      box.append(el("p", "Сервер передачи переключён. Ожидается прямое подключение Moblin."));
      const link = el("a", "Открыть Moblin", "button"); link.href = "moblin://";
      box.append(link, el("p", "В Moblin выберите заранее сохранённый профиль целевого сервера и переподключитесь. В OBS остановите отправку, выберите подготовленный SRT URL цели и запустите отправку. Прямой путь ещё не подтверждён. Во время reconnect могут отсутствовать исходные кадры; событие YouTube не завершайте."));
    }
    const gap = op.durations?.source_switch_gap_ms;
    if (Number.isFinite(gap)) box.append(el("p", `Интервал подачи видео в publisher на стыке: ${Math.round(gap)} мс. Это не измерение плеера YouTube.`));
    return box;
  }
  window.BroadcastRouteUI = {el, button, name, links, modal, switchDialog, operation};
})();
