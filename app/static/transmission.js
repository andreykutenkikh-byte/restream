/* The main admin screen. Media intent and switching remain server transactions. */
(() => {
  "use strict";
  if (!document.querySelector("#transmission")) return;
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const pending = new Map();
  let state = null, selected = new URL(location.href).searchParams.get("output") || "";
  let busy = false, stale = true, loading = false, activeDialog = null;
  const explanations = {
    node_install_failed: "Установка сервера не завершена. Откройте управление сервером для повторной настройки.",
    node_install_in_progress: "Установка сервера ещё выполняется.",
    media_node_unavailable: "Сервер недоступен. Проверьте его в управлении серверами.",
    media_node_disabled: "Требует настройки в управлении серверами.",
    media_capability_missing: "Требует настройки: версия агента не поддерживает переключение.",
    media_heartbeat_stale: "Нет свежего подтверждения от сервера.",
    public_media_address_required: "Требует настройки: нужен внешний адрес приёма.",
    connection_not_prepared: "Агент ещё не подготовил подключение. Повторите получение после его синхронизации.",
    rtmp_ingress_not_configured: "RTMP на этом сервере ещё не настроен. Используйте SRT или откройте управление сервером.",
    publisher_limit: "На сервере нет свободного места для отправки.",
    egress_limit: "Недостаточно пропускной способности сервера.",
    forward_limit: "Достигнут лимит маршрутов между серверами.",
    media_profile_incompatible: "Параметры видео несовместимы с сервером.",
    output_limit: "Достигнут лимит эфиров для этого источника.",
    output_not_running: "Сначала начните отправку.",
    stop_before_selecting_server: "Отправка уже запущена. Используйте переключение во время эфира.",
    youtube_dual_ingest_required: "Для переключения добавьте резервный адрес из YouTube Studio.",
    youtube_slot_not_free: "Предыдущий маршрут ещё освобождается.",
    current_server_unavailable: "Нет подтверждения доступности текущего сервера.",
    switch_in_progress: "Дождитесь завершения текущего переключения.",
    source_handoff_in_progress: "Перенос подключения этого источника уже выполняется.",
    stop_before_changing_youtube: "Сначала остановите отправку, чтобы изменить ключ или адрес.",
    waiting_for_publisher_stop: "Ожидаем подтверждения остановки отправки. Повторите действие позже.",
    independent_output_requires_unique_stream: "Этот ключ уже используется другим эфиром. Нужен отдельный ключ.",
    distinct_backup_endpoint_required: "Основной и резервный адреса должны различаться.",
    manual_credentials_required: "Укажите ключ трансляции и основной адрес YouTube.",
    use_youtube_api_settings: "Этот эфир настроен через OAuth. Откройте дополнительные действия.",
  };
  const explain = (code) => explanations[code] || "Действие недоступно. Проверьте настройку сервера и эфира в дополнительных действиях.";
  const serverAddress = (node) => node?.ip_address || "IP-адрес не определён";
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function button(text, action, disabled = false) {
    const node = el("button", text, "button button--secondary button--small");
    node.type = "button"; node.disabled = disabled;
    node.addEventListener("click", action);
    return node;
  }
  function link(text, href) { const node = el("a", text); node.href = href; return node; }
  function message(text = "", error = false) { $("tx-message").textContent = text; $("tx-message").dataset.error = String(error); }
  function pick() {
    for (const session of state?.sessions || []) {
      const output = session.outputs.find((o) => o.id === selected);
      if (output) return { session, output };
    }
    return {};
  }
  function select(id) {
    selected = id;
    const url = new URL(location.href);
    if (id) url.searchParams.set("output", id); else url.searchParams.delete("output");
    history.replaceState(null, "", url);
    render();
  }
  async function api(path, body, logicalKey) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = csrf;
      if (logicalKey) {
        if (!pending.has(logicalKey)) pending.set(logicalKey, crypto.randomUUID());
        headers["Idempotency-Key"] = pending.get(logicalKey);
      }
    }
    let response;
    try { response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal: AbortSignal.timeout(12000) }); }
    catch { throw new Error("Связь с панелью прервана. Результат действия неизвестен; обновите состояние перед повтором."); }
    if (response.status === 401) { location.assign("/login"); throw new Error("Войдите заново."); }
    const data = await response.json().catch(() => ({}));
    if (logicalKey && (response.ok || response.status < 500)) pending.delete(logicalKey);
    if (!response.ok) {
      const code = typeof data.detail === "string" ? data.detail : data.error?.code;
      throw new Error(response.status === 422 ? "Проверьте формат ключа и штатных RTMPS-адресов YouTube." : explain(code));
    }
    return data;
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      state = await api("/api/broadcasts/ui-state"); stale = false;
      const outputs = state.sessions.flatMap((s) => s.outputs);
      if (selected && !outputs.some((o) => o.id === selected)) selected = "";
      if (!selected && outputs.length === 1) selected = outputs[0].id;
    } catch { stale = true; message("Нет связи с панелью. Текущее состояние неизвестно. Попробуйте обновить.", true); }
    finally { loading = false; render(); }
  }
  async function action(work, status) {
    if (busy || stale) return;
    busy = true; render();
    try { await work(); message(status || "Изменение принято. Ожидаем подтверждения состояния."); }
    catch (error) { message(error.message, true); }
    finally { await refresh(); busy = false; render(); }
  }
  function dialog(title) {
    const returnFocus = document.activeElement;
    activeDialog?.close();
    const d = el("dialog", undefined, "tx-dialog");
    const heading = el("h2", title); heading.id = "tx-dialog-title";
    d.setAttribute("aria-labelledby", heading.id); d.append(heading);
    const body = el("div"), status = el("p", "", "tx-dialog-status"), actions = el("div", undefined, "tx-dialog-actions");
    status.setAttribute("role", "status");
    actions.append(button("Закрыть", () => d.close()));
    d.append(body, status, actions);
    d.addEventListener("close", () => {
      d.querySelectorAll("input").forEach((input) => { input.value = ""; });
      d.remove(); if (activeDialog === d) activeDialog = null;
      const focus = returnFocus?.isConnected && returnFocus !== document.body ? returnFocus : $("obs-connect");
      focus.focus({ preventScroll: true });
    });
    document.body.append(d); activeDialog = d; d.showModal();
    return { d, body, status, actions };
  }
  function field(parent, title, type = "text", value = "") {
    const label = el("label", title), input = el("input"); input.type = type; input.value = value;
    input.autocomplete = "off"; label.append(input); parent.append(label); return input;
  }
  function submitDialog(ui, label, work) {
    let submitting = false;
    const submit = button(label, async () => {
      if (submitting || busy || stale) return;
      submitting = true; submit.disabled = true; ui.status.textContent = "Сохраняем…";
      try { await work(); }
      catch (error) { ui.status.textContent = error.message; ui.status.dataset.error = "true"; }
      finally { submitting = false; submit.disabled = false; }
    });
    submit.classList.add("button--primary"); ui.actions.append(submit); return submit;
  }
  function prepareDialog() {
    const ui = dialog("Подготовить подключение");
    ui.body.append(el("p", "Будет создан эфир с отдельным ключом YouTube. Отправка начнётся только по вашей команде."));
    const name = field(ui.body, "Название эфира", "text", "Моя трансляция"); name.maxLength = 120;
    const sourceLabel = el("label", "Источник видео"), source = el("select");
    source.append(new Option("Новое подключение OBS / Moblin", ""));
    for (const s of state.sessions) source.append(new Option(`Использовать источник: ${s.name}`, s.id));
    const existing = pick().session;
    if (existing) source.value = existing.id;
    sourceLabel.append(source); ui.body.append(sourceLabel);
    const nodeLabel = el("label", "Сервер приёма"), node = el("select");
    nodeLabel.append(node); ui.body.append(nodeLabel);
    const updateNodes = () => {
      const session = state.sessions.find((s) => s.id === source.value);
      node.replaceChildren(new Option("Выберите сервер", ""));
      for (const n of state.nodes.filter((n) => !n.setup_error && (!session || n.id === session.ingress_node_id))) node.append(new Option(serverAddress(n), n.id));
      if (session && node.options.length === 2) node.selectedIndex = 1;
    };
    source.addEventListener("change", updateNodes); updateNodes();
    ui.body.append(el("p", "Настройки будут переданы агенту. Фактический приём подтвердится после появления видео."), link("Нет доступного сервера? Открыть управление серверами", "/servers"));
    submitDialog(ui, "Подготовить подключение", async () => {
      if (!node.value || !name.value.trim()) throw new Error("Укажите название и выберите доступный сервер приёма.");
      const payload = { name: name.value.trim(), ingress_node_id: node.value, session_id: source.value || null };
      // Only non-secret preparation intent persists for a retry after a lost response/reload.
      const signature = JSON.stringify(payload), stored = sessionStorage.getItem("tx-prepare");
      let retry = stored ? JSON.parse(stored) : null;
      if (!retry || retry.signature !== signature) retry = { signature, key: crypto.randomUUID() };
      sessionStorage.setItem("tx-prepare", JSON.stringify(retry));
      pending.set("prepare", retry.key);
      const result = await api("/api/broadcasts/prepare", payload, "prepare");
      sessionStorage.removeItem("tx-prepare");
      selected = result.output_id; await refresh(); select(result.output_id); ui.d.close();
      await reveal(result.session_id);
    });
  }
  async function reveal(sid, routeId) {
    const ui = dialog(routeId ? "Целевое подключение OBS / Moblin" : "Подключение OBS / Moblin");
    const label = el("label", "Протокол подключения"), protocol = el("select");
    protocol.append(new Option("SRT", "srt"), new Option("RTMP", "rtmp")); label.append(protocol);
    const fields = el("div"), copies = el("div", undefined, "tx-dialog-actions");
    ui.body.append(label, fields); ui.actions.append(copies);
    let request = 0;
    const load = async () => {
      const current = ++request;
      fields.querySelectorAll("input").forEach((input) => { input.value = ""; });
      fields.replaceChildren(); copies.replaceChildren();
      ui.status.textContent = "Получаем действующее подключение…"; ui.status.dataset.error = "false";
      try {
        const result = await api(`/api/broadcasts/sessions/${sid}/connection`, { target_route_id: routeId || null, protocol: protocol.value });
        if (!ui.d.isConnected || current !== request) return;
        if (routeId) fields.append(el("p", "Это отдельное целевое подключение. Текущий адрес источника сохраняется до подтверждения переноса."));
        fields.append(el("p", result.protocol === "rtmp" ? "OBS → Настройки → Трансляция → Пользовательский. Скопируйте сервер и ключ в соответствующие поля. Это ключ входа OBS, а не ключ YouTube." : "OBS → Настройки → Трансляция → Пользовательский. Вставьте адрес в «Сервер», поле ключа оставьте пустым. В Moblin выберите SRT."));
        const copyField = (name, value, copyLabel, type = "text") => {
          const input = field(fields, name, type, value); input.readOnly = true; input.dataset.sensitive = "true";
          copies.append(button(copyLabel, async () => {
            try { await navigator.clipboard.writeText(input.value); ui.status.textContent = "Скопировано."; }
            catch { input.select(); ui.status.textContent = "Выделите значение и скопируйте его вручную."; }
          }));
        };
        copyField("Сервер", result.protocol === "rtmp" ? result.server : result.url, result.protocol === "rtmp" ? "Скопировать сервер" : "Скопировать адрес");
        if (result.protocol === "rtmp") copyField("Ключ трансляции OBS", result.stream_key, "Скопировать ключ", "password");
        fields.append(el("p", "Видео: H.264 + AAC. Вертикальный или горизонтальный формат и частота кадров определяются автоматически и сохраняются при передаче. Рекомендуемый интервал ключевых кадров — 2 с."));
        fields.append(el("p", result.protocol === "rtmp" ? "RTMP передаёт видео и ключ без шифрования. Для защищённого подключения выберите SRT." : "Адрес содержит доступ к источнику. Не публикуйте его."));
        ui.status.textContent = "Параметры получены. Отправка на YouTube не запущена этим действием.";
      } catch (error) { if (ui.d.isConnected && current === request) { ui.status.textContent = error.message; ui.status.dataset.error = "true"; } }
    };
    protocol.addEventListener("change", load); await load();
  }
  function youtubeDialog(output, needBackup = false) {
    if (output.mode !== "manual") { location.assign("/broadcasts"); return; }
    const ui = dialog("Подключение YouTube");
    ui.body.append(el("p", output.credential_stored ? "Ключ сохранён. Оставьте поле пустым, чтобы сохранить его. Замена ключа или основного адреса требует остановки отправки." : "Скопируйте ключ из YouTube Studio. После сохранения он больше не отображается."));
    const key = field(ui.body, "Ключ трансляции YouTube", "password"); key.dataset.sensitive = "true";
    const details = el("details"); details.open = needBackup;
    details.append(el("summary", "Дополнительные настройки подключения"));
    const primary = field(details, "Основной адрес отправки", "url", output.connection.primary_url || "rtmps://a.rtmps.youtube.com/live2");
    const backup = field(details, "Резервный адрес из YouTube Studio", "url", output.connection.backup_url || "");
    details.append(el("p", "Для смены сервера во время отправки нужен резервный RTMPS-адрес этого же потока из YouTube Studio. До запуска можно выбрать сервер без резервного адреса."));
    ui.body.append(details);
    submitDialog(ui, "Сохранить", async () => {
      const value = key.value; key.value = "";
      if (!value && !output.credential_stored) throw new Error("Укажите ключ из YouTube Studio.");
      await api(`/api/broadcasts/outputs/${output.id}/connection`, { primary_url: primary.value.trim(), backup_url: backup.value.trim() || null, stream_key: value || null }, `youtube:${output.id}`);
      ui.d.close(); message("Ключ сохранён. Это ещё не подтверждение отправки на YouTube."); await refresh();
    });
  }
  function switchDialog(session, output, route, full) {
    const target = state.nodes.find((n) => n.id === route.node_id);
    const ui = dialog(full ? "Переключить подключение OBS/Moblin" : "Переключить сервер передачи");
    ui.body.append(el("p", `Сервер назначения: ${serverAddress(target)}. Ключ YouTube этого эфира сохраняется.`));
    ui.body.append(el("p", full ? "Потребуется изменить адрес источника и переподключиться. Перенос подключения влияет на все эфиры с этим источником. Сначала подготовим новый маршрут, затем предложим целевой адрес. Непрерывность просмотра на YouTube не гарантируется." : "OBS продолжит передавать на прежний сервер приёма. Изменится путь отправки в YouTube. Текущий сервер изменится после подтверждения переключения."));
    if (full) ui.body.append(button("Показать целевой адрес отдельно", () => reveal(session.id, route.id)));
    submitDialog(ui, "Подтвердить переключение", async () => {
      await api(`/api/broadcasts/outputs/${output.id}/switch`, { target_route_id: route.id, handoff_ingress: full }, `switch:${output.id}:${route.id}:${full}`);
      ui.d.close(); message("Подготавливаем новый маршрут…"); await refresh();
    });
  }
  function operation(session, output) {
    const box = $("tx-operation"), sw = output?.switch;
    box.replaceChildren(); box.hidden = !sw;
    if (!sw) return;
    box.dataset.state = sw.active ? "pending" : ["FAILED", "CANCELLED"].includes(sw.state) ? "failed" : "complete";
    const current = output.routes.find((r) => r.role === "current");
    let text;
    if (sw.active) {
      text = ["AWAITING_DIRECT_SOURCE", "EGRESS_SWITCH_COMPLETED"].includes(sw.state) ? "Сервер передачи переключён. Измените адрес в OBS / Moblin и переподключитесь; ждём прямое видео и звук." : sw.state === "ROLLING_BACK" ? "Переключение не удалось. Проверяем возврат к прежнему маршруту…" : ["CUTOVER_ARMED", "OLD_EGRESS_DRAINING", "OLD_CREDENTIAL_REVOKED"].includes(sw.state) ? "Переключаем… Проверяем состояние обоих серверов." : "Подготавливаем… Ожидаем подтверждения нового маршрута.";
    } else if (["FAILED", "CANCELLED"].includes(sw.state)) {
      text = "Не удалось переключиться.";
      if (!stale && current.id === sw.old_route_id && current.egress_link.state === "CONNECTED" && current.media_state === "LIVE") text += ` Передача через ${serverAddress(state.nodes.find((n) => n.id === current.node_id))} продолжается.`;
      else text += " Текущее состояние передачи показано ниже.";
    } else text = "Переключение подтверждено. Текущий сервер отмечен «Используется».";
    box.append(el("p", text));
    if (sw.active && sw.durations.handoff_ingress) box.append(button("Получить целевой адрес", () => reveal(session.id, sw.target_route_id), busy || stale));
  }
  function renderServers(session, output) {
    const box = $("tx-servers"), items = [];
    const nodes = [...(state?.nodes || [])];
    const rank = (node) => output?.routes.some((r) => r.node_id === node.id && r.role === "current") ? 0 : node.setup_error ? 2 : 1;
    nodes.sort((a, b) => rank(a) - rank(b));
    for (const node of nodes) {
      const route = output?.routes.find((r) => r.node_id === node.id);
      const card = el("div", undefined, "tx-server"), title = el("div", undefined, "tx-server-title");
      card.dataset.nodeId = node.id; card.dataset.outputId = output?.id || ""; card.dataset.current = String(route?.role === "current");
      title.append(el("strong", serverAddress(node)));
      if (route?.role === "current") title.append(el("span", output.desired_enabled ? "Используется" : "Выбран", "tx-current"));
      card.append(title);
      if (node.setup_error) {
        const installing = ["node_install_failed", "node_install_in_progress"].includes(node.setup_error);
        let reason = installing ? explain(node.setup_error) : node.mode === "legacy_static" ? "Сервер не настроен для передачи видео и управляемого переключения." : explain(node.setup_error);
        if (node.setup_error === "node_install_failed") {
          const failures = {
            docker_install_failed: "Не удалось установить Docker.",
            docker_repository_incomplete: "В репозитории Docker отсутствуют необходимые пакеты.",
            remote_command_timeout: "Превышено время ожидания шага установки.",
            remote_output_limit_exceeded: "Вывод команды установки превысил допустимый размер.",
            ssh_authentication_failed: "Сервер отклонил SSH-аутентификацию.",
          };
          if (failures[node.installation_error]) reason += ` ${failures[node.installation_error]}`;
        }
        card.append(el("p", reason), link("Управление сервером", "/servers"));
      } else if (!output) card.append(el("p", "Выберите эфир или подготовьте подключение."));
      else if (!route) {
        card.append(el("p", "Можно добавить к этому эфиру. Пригодность маршрута будет проверена отдельно."));
        card.append(button("Добавить сервер к эфиру", () => action(() => api(`/api/broadcasts/outputs/${output.id}/routes`, { node_id: node.id }, `route:${output.id}:${node.id}`)), busy || stale || output.switch?.active));
      } else if (route.role === "current") card.append(el("p", stale ? "Нет данных" : route.admission_error ? explain(route.admission_error) : route.egress_link.state === "CONNECTED" ? "Отправка работает" : output.desired_enabled ? "Ожидаем подтверждения отправки" : output.stop_confirmed ? "Отправка остановлена" : "Останавливаем…"));
      else if (!output.desired_enabled) {
        card.append(el("p", stale ? "Нет данных" : route.selection_error ? explain(route.selection_error) : "Можно выбрать до начала отправки. Адрес OBS останется прежним."));
        card.append(button("Выбрать для отправки", () => action(() => api(`/api/broadcasts/outputs/${output.id}/server`, { target_route_id: route.id }, `select:${output.id}:${route.id}`), `Выбран сервер ${serverAddress(node)}. Нажмите «Начать отправку», когда источник готов.`), busy || stale || Boolean(route.selection_error)));
      } else {
        card.append(el("p", stale ? "Нет данных" : route.switch_error ? explain(route.switch_error) : "Доступен для подготовки переключения. Качество нового пути ещё не подтверждено."));
        if (route.switch_error === "youtube_dual_ingest_required") card.append(button("Добавить резервный адрес YouTube", () => youtubeDialog(output, true), busy || stale));
        card.append(button("Переключить передачу на этот сервер", () => switchDialog(session, output, route, false), busy || stale || Boolean(route.switch_error)));
        const details = el("details"), summary = el("summary", "Сменить также подключение источника");
        details.append(summary, button("Переключить подключение OBS/Moblin", () => switchDialog(session, output, route, true), busy || stale || Boolean(route.handoff_error)));
        if (route.handoff_error && route.handoff_error !== route.switch_error) details.append(el("p", explain(route.handoff_error)));
        card.append(details);
      }
      items.push(card);
    }
    if (!items.length) items.push(el("p", "Нет подготовленного сервера приёма. Подключите сервер в управлении серверами."));
    // Keep focus and expanded secondary actions stable during unchanged polls.
    const signature = items.map((i) => i.outerHTML).join("");
    if (box.dataset.signature !== signature) { box.replaceChildren(...items); box.dataset.signature = signature; }
  }
  function render() {
    const { session, output } = pick(), outputs = state?.sessions.flatMap((s) => s.outputs) || [];
    const choice = $("broadcast-choice"), options = outputs.map((o) => [o.id, o.name]);
    const signature = JSON.stringify(options);
    if (choice.dataset.signature !== signature) {
      choice.replaceChildren(new Option(outputs.length ? "Выберите эфир" : "Подключение ещё не подготовлено", ""));
      for (const [id, name] of options) choice.append(new Option(name, id));
      choice.dataset.signature = signature;
    }
    choice.value = selected; choice.disabled = busy || stale || !outputs.length;
    $("add-broadcast").disabled = busy || stale;
    $("obs-connect").disabled = busy || stale || (!output && outputs.length > 0);
    $("youtube-settings").disabled = busy || stale || !output || output.switch?.active;
    $("youtube-settings").textContent = output?.mode === "youtube_api" ? "Настройки OAuth" : output?.credential_stored ? "Изменить" : "Сохранить ключ YouTube";
    $("youtube-state").textContent = output?.credential_stored ? "Ключ сохранён" : "Скопируйте ключ из YouTube Studio и сохраните здесь.";
    $("tx-diagnostics").hidden = !output;
    if (output) $("tx-diagnostics").href = `/broadcasts/outputs/${encodeURIComponent(output.id)}/diagnostics`;
    const ingress = state?.nodes.find((n) => n.id === session?.ingress_node_id);
    $("obs-node").textContent = ingress ? `Сервер приёма: ${serverAddress(ingress)}` : "Адрес выдаётся выбранным сервером приёма.";
    $("obs-state").textContent = output ? "Подключение сохранено. Получите действующий адрес по кнопке ниже." : outputs.length ? "Выберите эфир, чтобы получить его подключение." : "Адрес для OBS и Moblin. Ключ YouTube сюда не нужен.";
    const current = output?.routes.find((r) => r.role === "current");
    const startError = !output ? "Подготовьте подключение или выберите эфир." : !output.credential_stored ? "Сохраните ключ YouTube." : output.switch?.active ? explain("switch_in_progress") : !output.desired_enabled && !output.stop_confirmed ? "Ожидаем подтверждения остановки отправки." : !output.desired_enabled && current.admission_error ? explain(current.admission_error) : "";
    $("send-action").disabled = busy || stale || Boolean(startError);
    $("send-action").textContent = output?.desired_enabled ? "Остановить отправку" : "Начать отправку";
    $("send-hint").textContent = stale ? "Нет свежего состояния панели." : startError || (output.desired_enabled ? "Остановка не завершает событие на YouTube." : "Запустите источник в OBS, затем начните отправку.");
    const source = output?.routes.find((r) => r.node_id === session.ingress_node_id && r.phone_link.state === "MEASURED") || current;
    const known = output && !stale, live = known && source?.media_state === "LIVE";
    $("input-status").textContent = live ? "Видео поступает" : known ? "Ожидаем видео из OBS" : "Нет данных";
    $("input-status").dataset.tone = live ? "live" : "";
    const bps = known ? (source?.phone_link.bitrate_bps ?? source?.interrelay_link.bitrate_bps) : null;
    $("input-bitrate").textContent = bps == null ? "Нет данных" : `${(bps / 1000000).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} Мбит/с`;
    const connected = known && current?.egress_link.state === "CONNECTED", failed = known && current?.egress_link.state === "ERROR";
    $("output-status").textContent = connected ? "Отправка работает" : failed ? "Ошибка отправки" : !known ? "Нет данных" : output.desired_enabled ? (live ? "Подключаем…" : "Ожидаем видео из OBS") : output.stop_confirmed ? "Отправка остановлена" : "Останавливаем…";
    $("output-status").dataset.tone = connected ? "live" : failed ? "error" : "";
    const yt = output?.youtube;
    const platformLabels = { live: "В эфире", active: "Поток активен", inactive: "Поток неактивен", good: "Качество хорошее", bad: "Есть проблемы", ok: "Без ошибок", testing: "Проверка", ready: "Подготовлен", complete: "Завершён", created: "Создан", revoked: "Доступ отозван" };
    const platform = known && yt ? [yt.lifecycle_status, yt.stream_status, yt.health_status].filter((value) => value && value !== "unknown").map((value) => platformLabels[value] || value) : [];
    $("platform-status").textContent = platform.length ? `Последний ответ: ${platform.join(" · ")}` : "Нет данных";
    const egress = state?.nodes.find((n) => n.id === current?.node_id);
    $("tx-topology").textContent = output ? `OBS / Moblin → приём: ${serverAddress(ingress)} → передача: ${serverAddress(egress)} → YouTube` : "Маршрут появится после подготовки подключения.";
    renderServers(session, output); operation(session, output);
  }
  $("broadcast-choice").addEventListener("change", (event) => { message(); select(event.target.value); });
  $("add-broadcast").addEventListener("click", prepareDialog);
  $("obs-connect").addEventListener("click", () => { const { session } = pick(); if (session) reveal(session.id); else prepareDialog(); });
  $("youtube-settings").addEventListener("click", () => youtubeDialog(pick().output));
  $("tx-refresh").addEventListener("click", refresh);
  $("send-action").addEventListener("click", () => {
    const { output } = pick(); if (!output) return;
    if (!output.desired_enabled) action(() => api(`/api/broadcasts/outputs/${output.id}/intent`, { enabled: true }, `start:${output.id}`));
    else {
      const ui = dialog("Остановить отправку");
      ui.body.append(el("p", "Отправка этого эфира с сервера на YouTube остановится. Источник OBS может продолжать работать. Событие на YouTube автоматически не завершается."));
      submitDialog(ui, "Остановить отправку", async () => {
        await api(`/api/broadcasts/outputs/${output.id}/intent`, { enabled: false }, `stop:${output.id}`);
        ui.d.close(); await refresh();
      });
    }
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) activeDialog?.close(); else refresh(); });
  window.addEventListener("pagehide", () => activeDialog?.close());
  refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 3000);
})();
