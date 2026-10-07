"use strict";
document.addEventListener("broadcast-render", event => {
  const {snapshot, root, api, el, button} = event.detail, ui = window.BroadcastRouteUI;
  for (const session of snapshot.sessions) {
    const card = root.querySelector(`[data-session-id="${session.id}"]`);
    card.append(button("Профили Moblin", async () => {
      const result = await api(`/api/broadcasts/sessions/${session.id}/moblin-profiles`, {});
      const {dialog, body} = ui.modal("Сохранить профили Moblin");
      body.append(el("p", "Импортируйте профили до эфира. В Moblin задайте H.264/AAC, портрет 1080×1920, 30 fps и интервал ключевых кадров 2 с. Автоматическая смена активного LIVE не проверена."));
      const link = el("a", "Импортировать в Moblin", "button"); link.href = result.moblin_url;
      body.append(link, ui.button("Закрыть", () => { link.removeAttribute("href"); dialog.close(); }));
      dialog.addEventListener("close", () => { link.removeAttribute("href"); result.moblin_url = ""; });
    }));
    card.append(button("Разрешить оператору переключение", async () => {
      const {dialog, body} = ui.modal("Доступ оператора на 1 час"), label = el("label"), checkbox = el("input");
      checkbox.type = "checkbox"; label.append(checkbox, document.createTextNode(" Разрешить переключение маршрута с этого устройства"));
      const device = el("input"); device.placeholder = "Название устройства"; device.setAttribute("aria-label", "Название устройства"); device.maxLength = 80;
      const status = el("p"); status.setAttribute("role", "status"); body.append(device, label, status);
      let pending = false;
      const create = ui.button("Создать ссылку", async () => {
        if (!checkbox.checked || !device.value.trim() || pending) return;
        pending = true; create.disabled = true;
        try {
          const result = await api(`/api/broadcasts/sessions/${session.id}/operators`, {label: device.value.trim(), allow_switching: true, ttl_minutes: 60});
          const link = el("a", "Открыть операторский HUD", "button"); link.href = `${location.origin}/stream-operator#pair=${result.pairing_token}`;
          body.append(link, ui.button("Скопировать ссылку", () => navigator.clipboard.writeText(link.href)));
          status.textContent = "Ссылка одноразовая, действует 10 минут. Доступ ограничен этой сессией.";
          dialog.addEventListener("close", () => { link.removeAttribute("href"); result.pairing_token = ""; });
        } catch (error) { status.textContent = error.message; pending = false; create.disabled = false; }
      }); body.append(create, ui.button("Закрыть", () => dialog.close()));
    }));
    card.append(button("Устройства оператора", async () => {
      const result = await api("/api/broadcasts/operators"), {dialog, body} = ui.modal("Доступ оператора");
      for (const device of result.operators.filter(d => d.session_id === session.id)) {
        const row = el("p", `${device.label} · ${device.revoked_at ? "Отозван" : device.expires_at} `);
        if (!device.revoked_at) row.append(ui.button("Отозвать", async event => {
          const revokeButton = event.currentTarget;
          await api(`/api/broadcasts/operators/${device.id}/revoke`, {}); revokeButton.disabled = true; row.append(el("span", " Отозван"));
        })); body.append(row);
      } body.append(ui.button("Закрыть", () => dialog.close()));
    }));
    for (const output of session.outputs) {
      const block = card.querySelector(`[data-output-id="${output.id}"]`);
      block.append(el("p", `YouTube credential: ${output.credential_stored ? "Сохранён" : "Не создан"} · Активных leases: ${output.active_egress_leases}`),
        ui.operation(output, id => api(`/api/broadcasts/switches/${id}/cancel`, {})));
      for (const route of output.routes) {
        const routeCard = block.querySelector(`[data-route-id="${route.id}"]`); routeCard.append(ui.links(route, snapshot, session));
        if (route.role === "standby") {
          const switchButton = ui.button(`Переключиться на ${ui.name(snapshot, route.node_id)}`, () => ui.switchDialog(snapshot, output, route,
            (oid, rid, handoff) => api(`/api/broadcasts/outputs/${oid}/switch`, {target_route_id: rid, handoff_ingress: handoff}, `switch:${oid}:${rid}`)));
          switchButton.disabled = !route.server_ready || !output.desired_enabled || !output.youtube.has_backup || Boolean(output.switch?.active);
          routeCard.append(switchButton);
        }
      }
      if (output.recommendation.target_route_ids.length) block.append(el("p", "Текущий маршрут нестабилен. Доступен готовый сервер; прямой путь телефона к нему пока UNKNOWN."));
    }
  }
});
