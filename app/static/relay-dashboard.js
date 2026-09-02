(function relayDashboardModule(globalScope) {
  "use strict";

  const POLL_ACTIVE_MS = 3000;
  const POLL_IDLE_MS = 8000;
  const COMMAND_POLL_MS = 1500;
  const COMMAND_POLL_LIMIT = 40;
  const SAFE_STOPPED_STATES = new Set(["offline", "ok", "healthy"]);

  function token(value, allowed, fallback = "unknown") {
    return typeof value === "string" && allowed.includes(value) ? value : fallback;
  }

  function normalizeRelayStatus(payload) {
    const status = payload?.status && typeof payload.status === "object" ? payload.status : {};
    const rawBitrate = status.input_bitrate_bps;
    const bitrate = typeof rawBitrate === "number" ? rawBitrate : Number.NaN;
    return {
      available: payload?.available === true,
      lastSeenAt: typeof payload?.last_seen_at === "string" ? payload.last_seen_at : null,
      service: token(status.service, ["active", "inactive", "failed", "unknown"]),
      enabled: status.enabled === true,
      mainProcess: token(status.main_process, ["running", "stopped", "failed", "unknown"]),
      srtListener: token(status.srt_listener, ["listening", "closed", "failed", "unknown"]),
      source: token(status.source, ["SLATE", "LIVE", "NONE", "UNKNOWN"], "UNKNOWN"),
      youtubeForward: token(status.youtube_forward, ["active", "inactive", "connecting", "failed", "unknown"]),
      overall: token(status.overall, ["ok", "healthy", "degraded", "failed", "offline", "unknown"]),
      youtubeUrlConfigured: status.youtube_url_configured === true,
      youtubeKeyConfigured: status.youtube_key_configured === true,
      portraitProfile: status.portrait_profile === true,
      errorCode: typeof status.error_code === "string" ? status.error_code : null,
      inputBitrateBps: Number.isFinite(bitrate) && bitrate >= 0 && bitrate <= 1_000_000_000
        ? Math.round(bitrate)
        : null,
    };
  }

  function relayViewModel(relay) {
    const fullyConfigured = relay.youtubeUrlConfigured && relay.youtubeKeyConfigured;
    const active = relay.service === "active" || relay.mainProcess === "running";
    const blockingError = relay.errorCode !== null && relay.errorCode !== "youtube_not_configured";
    const safelyStopped = relay.service === "inactive"
      && relay.mainProcess === "stopped"
      && relay.srtListener === "closed"
      && relay.source === "NONE"
      && relay.youtubeForward === "inactive"
      && SAFE_STOPPED_STATES.has(relay.overall)
      && !blockingError;
    const failed = [relay.service, relay.mainProcess, relay.srtListener, relay.youtubeForward, relay.overall]
      .includes("failed") || blockingError;
    const coherentActive = relay.service === "active"
      && relay.mainProcess === "running"
      && relay.srtListener === "listening"
      && ["LIVE", "SLATE"].includes(relay.source)
      && ["active", "connecting"].includes(relay.youtubeForward)
      && !failed;
    const operable = relay.available && !failed;

    let badgeLabel = "Нет связи";
    let badgeTone = "neutral";
    if (relay.available && failed) {
      badgeLabel = "Нужна проверка";
      badgeTone = "danger";
    } else if (relay.source === "LIVE" && coherentActive) {
      badgeLabel = "Видеопоток поступает";
      badgeTone = "success";
    } else if (coherentActive && relay.source === "SLATE") {
      badgeLabel = "Ожидаем Moblin";
      badgeTone = "warning";
    } else if (safelyStopped) {
      badgeLabel = fullyConfigured ? "Готов к запуску" : "Нужна настройка";
      badgeTone = fullyConfigured ? "success" : "warning";
    } else if (relay.available) {
      badgeLabel = "Проверяем состояние";
      badgeTone = "warning";
    }

    let actionReason = "Состояние HK-сервера недоступно.";
    if (failed) actionReason = "Команды заблокированы до устранения ошибки.";
    else if (!fullyConfigured) actionReason = "Сначала настройте YouTube.";
    else if (safelyStopped) actionReason = "Relay готов к безопасному запуску.";
    else if (active) actionReason = "Relay работает. Перед остановкой завершите broadcast в YouTube.";
    else if (relay.available) actionReason = "Обновите состояние перед выполнением команды.";

    return {
      active,
      fullyConfigured,
      safelyStopped,
      coherentActive,
      operable,
      badgeLabel,
      badgeTone,
      actionReason,
      startDisabled: !(operable && safelyStopped && fullyConfigured && relay.portraitProfile),
      stopDisabled: !(operable && active),
      configureDisabled: !(operable && safelyStopped),
      clearDisabled: !(operable && safelyStopped && (relay.youtubeUrlConfigured || relay.youtubeKeyConfigured)),
    };
  }

  function formatBitrate(value) {
    if (value === null || value === undefined || value === "") return "—";
    const bps = Number(value);
    if (!Number.isFinite(bps) || bps < 0) return "—";
    if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1).replace(".0", "")} Мбит/с`;
    if (bps >= 1000) return `${Math.round(bps / 1000)} Кбит/с`;
    return `${Math.round(bps)} бит/с`;
  }

  function formatAge(value, now = Date.now()) {
    const timestamp = typeof value === "string" ? Date.parse(value) : NaN;
    if (!Number.isFinite(timestamp)) return "—";
    const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
    if (seconds < 5) return "только что";
    if (seconds < 60) return `${seconds} сек. назад`;
    return `${Math.round(seconds / 60)} мин. назад`;
  }

  function sanitizeSrtUrl(value) {
    if (typeof value !== "string" || value.length > 4096 || /[\s\u0000-\u001f\u007f]/.test(value)) return "";
    try {
      const parsed = new URL(value);
      return parsed.protocol === "srt:" && parsed.hostname ? value : "";
    } catch (_error) {
      return "";
    }
  }

  function buildYouTubePayload(values) {
    const url = String(values.url || "").trim();
    const streamKey = String(values.stream_key || "").trim();
    if (!url.startsWith("rtmps://") || url.includes("#") || /\s/.test(url)) {
      throw new TypeError("Используйте точный rtmps:// адрес из YouTube Studio без пробелов и символа #.");
    }
    if (!streamKey || /\s/.test(streamKey)) throw new TypeError("Введите stream key без пробелов.");
    return { url, stream_key: streamKey };
  }

  function buildYouTubeKeyPayload(values) {
    const streamKey = String(values.stream_key || "").trim();
    if (!streamKey || /\s/.test(streamKey)) throw new TypeError("Введите stream key без пробелов.");
    return { stream_key: streamKey };
  }

  function buildAdminPasswordPayload(values) {
    const adminPassword = String(values.admin_password || "");
    if (!adminPassword) throw new TypeError("Введите пароль администратора панели.");
    return { admin_password: adminPassword };
  }

  function wipeSecretObject(value) {
    if (!value || typeof value !== "object") return;
    for (const key of Object.keys(value)) {
      if (value[key] && typeof value[key] === "object") wipeSecretObject(value[key]);
      else value[key] = "";
    }
  }

  function clearSensitiveFields(root) {
    root?.querySelectorAll?.('input[type="password"], [data-sensitive-output]').forEach((input) => {
      input.value = "";
    });
  }

  function relayCommandOutcome(command) {
    if (!command || typeof command !== "object") return "failed";
    if (["queued", "leased", "acknowledged"].includes(command.state)) return "pending";
    if (command.state !== "completed") return "failed";
    if (command.completion_status === "ok") return "success";
    if (command.completion_status === "conflict") return "conflict";
    return "failed";
  }

  function createIdempotencyKey(action) {
    const random = globalScope.crypto?.randomUUID?.();
    return typeof random === "string" ? `ui:${action}:${random}` : "";
  }

  function isCurrentDialogRequest(
    expectedNodeId,
    currentNodeId,
    expectedGeneration,
    currentGeneration,
    dialogOpen,
  ) {
    return Boolean(
      expectedNodeId
      && String(currentNodeId || "") === String(expectedNodeId)
      && Number.isInteger(expectedGeneration)
      && expectedGeneration === currentGeneration
      && dialogOpen === true,
    );
  }

  function previewUpdateIsCurrent(expectedGeneration, currentGeneration, expectedNodeId, currentNodeId, relay) {
    return Number.isInteger(expectedGeneration)
      && expectedGeneration === currentGeneration
      && typeof expectedNodeId === "string"
      && expectedNodeId.length > 0
      && expectedNodeId === currentNodeId
      && relay?.available === true
      && relay?.source === "LIVE";
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      buildAdminPasswordPayload,
      buildYouTubeKeyPayload,
      buildYouTubePayload,
      formatAge,
      formatBitrate,
      isCurrentDialogRequest,
      normalizeRelayStatus,
      previewUpdateIsCurrent,
      relayCommandOutcome,
      relayViewModel,
      sanitizeSrtUrl,
      wipeSecretObject,
    };
  }

  if (typeof document === "undefined") return;
  const page = document.querySelector("[data-relay-dashboard]");
  if (!page) return;

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const toastRegion = document.querySelector("[data-toast-region]");
  const badge = page.querySelector("[data-relay-dashboard-badge]");
  const primaryAction = page.querySelector("[data-relay-primary-action]");
  const retryButton = page.querySelector("[data-relay-dashboard-retry]");
  const pageError = page.querySelector("[data-relay-dashboard-error]");
  const youtubeDialog = document.querySelector("[data-relay-dashboard-youtube-dialog]");
  const youtubeForm = document.querySelector("[data-relay-dashboard-youtube-form]");
  const youtubeError = document.querySelector("[data-relay-dashboard-youtube-error]");
  const moblinDialog = document.querySelector("[data-relay-dashboard-moblin-dialog]");
  const moblinForm = document.querySelector("[data-relay-dashboard-moblin-form]");
  const moblinError = document.querySelector("[data-relay-dashboard-moblin-error]");
  const clearDialog = document.querySelector("[data-relay-dashboard-clear-dialog]");
  const clearForm = document.querySelector("[data-relay-dashboard-clear-form]");
  const clearError = document.querySelector("[data-relay-dashboard-clear-error]");
  const previewContainer = page.querySelector("[data-relay-preview]");
  const previewVideo = page.querySelector("[data-relay-video]");
  const youtubeUrlDetails = youtubeDialog?.querySelector("[data-youtube-url-details]");
  const youtubeUrlInput = youtubeDialog?.querySelector("[data-dashboard-youtube-url]");
  let csrfToken = csrfMeta?.content || "";
  let relayNodeId = "";
  let currentRelay = normalizeRelayStatus(null);
  let currentView = relayViewModel(currentRelay);
  let pollTimer = null;
  let loadGeneration = 0;
  let previewController = null;
  let previewNodeId = "";
  let previewLeaseRenewedAt = 0;
  let previewUpdateGeneration = 0;
  let commandBusy = false;
  let youtubeRequestGeneration = 0;
  let moblinRequestGeneration = 0;
  let clearRequestGeneration = 0;

  class ApiError extends Error {
    constructor(message, status = 0, payload = null) {
      super(message);
      this.status = status;
      this.payload = payload;
    }
  }

  function serverCode(payload) {
    return typeof payload?.error?.code === "string" ? payload.error.code : "";
  }

  function friendlyError(error, fallback) {
    if (error instanceof TypeError) return error.message;
    if (!(error instanceof ApiError)) return fallback;
    const code = serverCode(error.payload);
    if (error.status === 401 && code === "step_up_failed") return "Пароль администратора неверен.";
    if (error.status === 401) return "Сессия завершена. Войдите снова.";
    if (error.status === 403) return "Сессия устарела. Обновите страницу и повторите действие.";
    if (error.status === 409 && code === "relay_active") return "Сначала завершите broadcast в YouTube и остановите relay.";
    if (error.status === 409 && code === "relay_unavailable") return "HK relay сейчас не на связи.";
    if (error.status === 409 && code === "relay_command_pending") return "Предыдущая команда ещё выполняется.";
    if (error.status === 429) return "Слишком много попыток. Подождите и повторите.";
    return fallback;
  }

  async function apiRequest(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    let response;
    try {
      response = await fetch(path, { credentials: "same-origin", ...options, headers });
    } catch (_error) {
      throw new ApiError("Нет соединения");
    }
    const text = response.status === 204 ? "" : await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (_error) { payload = null; }
    }
    if (!response.ok) {
      if (response.status === 401 && serverCode(payload) !== "step_up_failed") location.assign("/login");
      throw new ApiError(response.statusText, response.status, payload);
    }
    if (typeof payload?.csrf_token === "string") {
      csrfToken = payload.csrf_token;
      if (csrfMeta) csrfMeta.content = csrfToken;
    }
    return payload;
  }

  function showToast(title, detail = "", type = "success") {
    if (!toastRegion) return;
    const toast = document.createElement("div");
    toast.className = `toast toast--${type}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    const content = document.createElement("span");
    content.className = "toast__content";
    const strong = document.createElement("strong");
    strong.textContent = title;
    content.append(strong);
    if (detail) {
      const span = document.createElement("span");
      span.textContent = detail;
      content.append(span);
    }
    toast.append(content);
    toastRegion.append(toast);
    setTimeout(() => toast.remove(), 5000);
  }

  function openDialog(dialog) {
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (typeof dialog?.close === "function") dialog.close();
    else dialog?.removeAttribute("open");
  }

  function setBusy(target, busy) {
    if (!target) return;
    target.setAttribute("aria-busy", String(busy));
    const submit = target.matches?.("button") ? target : target.querySelector('button[type="submit"]');
    if (submit) submit.disabled = busy;
  }

  function setText(selector, value) {
    const element = page.querySelector(selector);
    if (element) element.textContent = value;
  }

  function setTechnicalField(key, value) {
    const output = page.querySelector(`[data-relay-field="${key}"]`);
    if (output) output.textContent = value;
  }

  function relayLabel(value) {
    return ({
      active: "Работает", inactive: "Остановлен", failed: "Ошибка", unknown: "Нет данных",
      listening: "Принимает подключения", closed: "Закрыт", LIVE: "Moblin LIVE",
      SLATE: "Серверная заставка", NONE: "Нет потока", UNKNOWN: "Нет данных",
    })[value] || "Нет данных";
  }

  function updateSignal(relay) {
    const container = page.querySelector("[data-relay-signal-state]");
    const dot = page.querySelector("[data-relay-signal-dot]");
    let state = "offline";
    let label = "Ретрансляция выключена";
    let description = "Запустите relay, чтобы сервер начал ожидать видеопоток.";
    let hint = "После запуска приложения здесь появятся статус, битрейт и превью.";
    if (!relay.available) {
      label = "Нет связи с HK-сервером";
      description = "Последнее состояние входящего потока неизвестно.";
      hint = "Обновите состояние или проверьте сервер в разделе «Дополнительно».";
    } else if (relay.source === "LIVE") {
      state = "live";
      label = "Видеопоток из Moblin поступает";
      description = "Сервер принимает SRT и передаёт видео дальше.";
      hint = "Контролируйте битрейт и изображение перед началом эфира.";
    } else if (relay.source === "SLATE") {
      state = "waiting";
      label = "Ожидаем видеопоток из приложения";
      description = "Moblin не подключён — в YouTube уходит серверная заставка.";
      hint = "Запустите SRT-трансляцию в Moblin или OBS.";
    } else if (relay.service === "active") {
      state = "waiting";
      label = "Relay запущен, сигнала пока нет";
      description = "Сервер ожидает подключение программы для стриминга.";
    }
    if (container) container.dataset.state = state;
    if (dot) dot.className = `status-dot status-dot--large status-dot--${state === "live" ? "success" : state === "waiting" ? "warning" : "neutral"}`;
    setText("[data-relay-signal-label]", label);
    setText("[data-relay-signal-description]", description);
    setText("[data-relay-monitor-hint]", hint);
  }

  async function renewPreviewLease() {
    if (!relayNodeId || currentRelay.source !== "LIVE") return false;
    const now = Date.now();
    if (now - previewLeaseRenewedAt < 8000) return true;
    try {
      await apiRequest(`/api/nodes/${encodeURIComponent(relayNodeId)}/relay/preview/lease`, { method: "POST" });
      previewLeaseRenewedAt = now;
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (previewContainer) previewContainer.dataset.previewState = "error";
        return false;
      }
      if (previewContainer) previewContainer.dataset.previewState = "error";
      return false;
    }
  }

  async function updatePreview(relay) {
    if (!previewContainer || !previewVideo) return;
    const updateGeneration = ++previewUpdateGeneration;
    const requestedNodeId = relayNodeId;
    if (relay.source !== "LIVE" || !relay.available) {
      previewLeaseRenewedAt = 0;
      previewController?.suspend("offline");
      previewContainer.dataset.previewState = "offline";
      return;
    }
    const leaseReady = await renewPreviewLease();
    if (
      !leaseReady
      || !previewUpdateIsCurrent(
        updateGeneration,
        previewUpdateGeneration,
        requestedNodeId,
        relayNodeId,
        currentRelay,
      )
    ) return;
    if (!previewController || previewNodeId !== relayNodeId) {
      previewController?.suspend("offline");
      previewNodeId = relayNodeId;
      previewController = new globalScope.IngestPreviewController({
        container: previewContainer,
        video: previewVideo,
        hlsClass: globalScope.Hls || null,
        sourceUrl: `/api/nodes/${encodeURIComponent(relayNodeId)}/relay/preview/index.m3u8`,
      });
    }
    previewController.resume();
    previewController.setStreamState("live");
  }

  function updateControls(relay, view) {
    for (const button of page.querySelectorAll("[data-open-youtube-config]")) {
      button.disabled = commandBusy || view.configureDisabled;
      button.textContent = relay.youtubeUrlConfigured
        ? (relay.youtubeKeyConfigured ? "Заменить ключ YouTube" : "Ввести ключ YouTube")
        : "Настроить YouTube";
    }
    for (const button of page.querySelectorAll("[data-open-moblin-url]")) button.disabled = commandBusy || !view.operable;
    for (const button of page.querySelectorAll("[data-relay-start]")) button.disabled = commandBusy || view.startDisabled;
    for (const button of page.querySelectorAll("[data-relay-stop]")) button.disabled = commandBusy || view.stopDisabled;
    for (const button of page.querySelectorAll("[data-open-youtube-clear]")) button.disabled = commandBusy || view.clearDisabled;
    const refresh = page.querySelector("[data-relay-refresh]");
    if (refresh) refresh.disabled = commandBusy || !relay.available;
    if (primaryAction) {
      primaryAction.disabled = commandBusy || (view.active ? view.stopDisabled : view.startDisabled);
      primaryAction.textContent = view.active ? "Остановить ретрансляцию" : "Включить ретрансляцию";
      primaryAction.dataset.action = view.active ? "stop" : "start";
      primaryAction.className = view.active ? "button button--danger-soft" : "button button--primary";
    }
    setText("[data-relay-action-reason]", view.actionReason);
  }

  function updatePage(payload) {
    currentRelay = normalizeRelayStatus(payload);
    currentView = relayViewModel(currentRelay);
    if (badge) {
      badge.className = `relay-badge relay-badge--${currentView.badgeTone}`;
      badge.textContent = currentView.badgeLabel;
    }
    const youtubeState = currentView.fullyConfigured ? "configured" : "missing";
    const youtubeStatus = page.querySelector("[data-youtube-setup-status]");
    if (youtubeStatus) {
      youtubeStatus.dataset.state = youtubeState;
      youtubeStatus.textContent = currentView.fullyConfigured ? "Настроен" : "Не настроен";
    }
    setText("[data-youtube-setup-copy]", currentView.fullyConfigured
      ? "Ключ сохранён на HK relay. При выдаче нового ключа замените только его."
      : currentRelay.youtubeUrlConfigured
        ? "RTMPS-адрес уже сохранён. Введите только новый stream key из YouTube Studio."
        : "Для первой настройки нужны stream key и точный RTMPS-адрес из YouTube Studio.");
    const moblinStatus = page.querySelector("[data-moblin-setup-status]");
    if (moblinStatus) {
      moblinStatus.dataset.state = currentRelay.available ? "ready" : "offline";
      moblinStatus.textContent = currentRelay.available ? "Доступна" : "Нет связи";
    }
    updateSignal(currentRelay);
    setText("[data-relay-bitrate]", currentRelay.source === "LIVE" ? formatBitrate(currentRelay.inputBitrateBps) : "—");
    setText("[data-relay-youtube-forward]", ({ active: "Отправляется", connecting: "Подключение…", inactive: "Остановлена", failed: "Ошибка" })[currentRelay.youtubeForward] || "Нет данных");
    setText("[data-relay-profile]", currentRelay.portraitProfile ? "720×1280 · 30 FPS" : "Профиль не подтверждён");
    setText("[data-relay-updated]", formatAge(currentRelay.lastSeenAt));
    setTechnicalField("service", relayLabel(currentRelay.service));
    setTechnicalField("srt-listener", relayLabel(currentRelay.srtListener));
    setTechnicalField("source", relayLabel(currentRelay.source));
    setTechnicalField("youtube-url", currentRelay.youtubeUrlConfigured ? "Настроен" : "Не настроен");
    setTechnicalField("youtube-key", currentRelay.youtubeKeyConfigured ? "Настроен" : "Не настроен");
    setTechnicalField("last-seen", formatAge(currentRelay.lastSeenAt));
    updateControls(currentRelay, currentView);
    void updatePreview(currentRelay);
  }

  function schedulePoll() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    const delay = currentView.active || currentRelay.source === "LIVE" ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    pollTimer = setTimeout(() => void loadStatus({ quiet: true }), delay);
  }

  async function loadStatus({ quiet = false } = {}) {
    const generation = ++loadGeneration;
    try {
      if (!relayNodeId) {
        const nodes = await apiRequest("/api/relay-nodes");
        const item = Array.isArray(nodes?.items) ? nodes.items[0] : null;
        if (!item?.node_id) throw new ApiError("Relay not found", 404);
        relayNodeId = String(item.node_id);
      }
      const payload = await apiRequest(`/api/nodes/${encodeURIComponent(relayNodeId)}/relay`);
      if (generation !== loadGeneration) return;
      if (pageError) pageError.hidden = true;
      updatePage(payload);
    } catch (error) {
      if (generation !== loadGeneration) return;
      updatePage({ available: false });
      if (pageError) pageError.hidden = false;
      if (!quiet) showToast("Не удалось обновить состояние", friendlyError(error, "Попробуйте ещё раз."), "error");
    } finally {
      if (generation === loadGeneration) schedulePoll();
    }
  }

  async function waitForRelayCommand(nodeId, commandId, { isCurrent = null } = {}) {
    for (let attempt = 0; attempt < COMMAND_POLL_LIMIT; attempt += 1) {
      if (isCurrent && !isCurrent()) return "stale";
      const command = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay/commands/${encodeURIComponent(commandId)}`);
      if (isCurrent && !isCurrent()) return "stale";
      const outcome = relayCommandOutcome(command);
      if (outcome !== "pending") return outcome;
      await new Promise((resolve) => setTimeout(resolve, COMMAND_POLL_MS));
      if (isCurrent && !isCurrent()) return "stale";
    }
    return "pending";
  }

  async function runRelayAction(action) {
    if (!relayNodeId || !["start", "stop", "refresh"].includes(action)) return;
    if (action === "start" && !confirm("Включить relay и начать отправку заставки в настроенный YouTube broadcast?")) return;
    if (action === "stop" && !confirm("Остановить relay? Сначала завершите broadcast в YouTube.")) return;
    commandBusy = true;
    updateControls(currentRelay, currentView);
    try {
      const nodeId = String(relayNodeId);
      const headers = {};
      const key = createIdempotencyKey(action);
      if (key) headers["Idempotency-Key"] = key;
      const queued = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay/${action}`, { method: "POST", headers });
      const outcome = await waitForRelayCommand(nodeId, queued?.command_id || "");
      if (outcome !== "success") throw new ApiError("Relay command failed", outcome === "conflict" ? 409 : 422);
      showToast(action === "start" ? "Relay запущен" : action === "stop" ? "Relay остановлен" : "Состояние обновлено");
    } catch (error) {
      showToast("Команда не выполнена", friendlyError(error, "Попробуйте ещё раз."), "error");
    } finally {
      commandBusy = false;
      await loadStatus({ quiet: true });
    }
  }

  function resetDialog(dialog, errorOutput) {
    clearSensitiveFields(dialog);
    const form = dialog?.querySelector("form");
    form?.reset();
    setBusy(form, false);
    if (errorOutput) {
      errorOutput.textContent = "";
      errorOutput.hidden = true;
    }
  }

  function dialogRequestIsCurrent(nodeId, generation, currentGeneration, dialog) {
    const dialogOpen = dialog?.open === true || dialog?.hasAttribute?.("open") === true;
    return isCurrentDialogRequest(
      nodeId,
      relayNodeId,
      generation,
      currentGeneration,
      dialogOpen,
    );
  }

  function prepareYouTubeDialog() {
    const urlAlreadyConfigured = currentRelay.youtubeUrlConfigured;
    if (youtubeUrlDetails) youtubeUrlDetails.open = !urlAlreadyConfigured;
    if (youtubeUrlInput) {
      youtubeUrlInput.required = !urlAlreadyConfigured;
      youtubeUrlInput.value = "";
    }
    const title = youtubeDialog?.querySelector("[data-youtube-dialog-title]");
    const help = youtubeDialog?.querySelector("[data-youtube-dialog-help]");
    const summary = youtubeDialog?.querySelector("[data-youtube-url-summary]");
    const submit = youtubeDialog?.querySelector("[data-youtube-submit-label]");
    if (title) title.textContent = urlAlreadyConfigured ? "Ключ потока YouTube" : "Первичная настройка YouTube";
    if (help) help.textContent = urlAlreadyConfigured
      ? "Введите только новый stream key. Сохранённый на HK relay RTMPS-адрес останется без изменений."
      : "Для первого запуска скопируйте stream key и точный RTMPS-адрес из YouTube Live Control Room.";
    if (summary) summary.textContent = urlAlreadyConfigured
      ? "Дополнительно: заменить RTMPS-адрес"
      : "RTMPS-адрес YouTube для первого запуска";
    if (submit) submit.textContent = urlAlreadyConfigured ? "Сохранить ключ" : "Сохранить настройку";
  }

  async function submitYouTube(event) {
    event.preventDefault();
    if (!relayNodeId) return;
    const nodeId = String(relayNodeId);
    const requestGeneration = ++youtubeRequestGeneration;
    const requestIsCurrent = () => dialogRequestIsCurrent(
      nodeId,
      requestGeneration,
      youtubeRequestGeneration,
      youtubeDialog,
    );
    let payload = null;
    let body = "";
    setBusy(youtubeForm, true);
    if (youtubeError) youtubeError.hidden = true;
    try {
      const values = Object.fromEntries(new FormData(youtubeForm).entries());
      const replaceUrl = String(values.url || "").trim().length > 0;
      const keyOnly = currentRelay.youtubeUrlConfigured && !replaceUrl;
      payload = keyOnly ? buildYouTubeKeyPayload(values) : buildYouTubePayload(values);
      body = JSON.stringify(payload);
      const headers = {};
      const action = keyOnly ? "configure-youtube-key" : "configure-youtube";
      const key = createIdempotencyKey(action);
      if (key) headers["Idempotency-Key"] = key;
      const queued = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay/${action}`, { method: "PUT", body, headers });
      if (!requestIsCurrent()) return;
      const outcome = await waitForRelayCommand(nodeId, queued?.command_id || "", { isCurrent: requestIsCurrent });
      if (!requestIsCurrent()) return;
      if (outcome !== "success") throw new ApiError("Relay command failed", outcome === "conflict" ? 409 : 422);
      await loadStatus({ quiet: true });
      if (!requestIsCurrent()) return;
      resetDialog(youtubeDialog, youtubeError);
      closeDialog(youtubeDialog);
      showToast("Данные YouTube сохранены");
    } catch (error) {
      if (!requestIsCurrent()) return;
      if (youtubeError) {
        youtubeError.textContent = friendlyError(error, "Не удалось сохранить данные YouTube.");
        youtubeError.hidden = false;
      }
    } finally {
      wipeSecretObject(payload);
      payload = null;
      body = "";
      if (requestIsCurrent()) {
        clearSensitiveFields(youtubeForm);
        setBusy(youtubeForm, false);
      }
    }
  }

  async function submitMoblin(event) {
    event.preventDefault();
    if (!relayNodeId) return;
    const nodeId = String(relayNodeId);
    const requestGeneration = ++moblinRequestGeneration;
    const requestIsCurrent = () => dialogRequestIsCurrent(
      nodeId,
      requestGeneration,
      moblinRequestGeneration,
      moblinDialog,
    );
    let response = null;
    let body = "{}";
    setBusy(moblinForm, true);
    if (moblinError) moblinError.hidden = true;
    try {
      const headers = {};
      const key = createIdempotencyKey("reveal-moblin");
      if (key) headers["Idempotency-Key"] = key;
      const endpoint = `/api/nodes/${encodeURIComponent(nodeId)}/relay/reveal-moblin-url`;
      response = await apiRequest(endpoint, { method: "POST", body, headers });
      if (!requestIsCurrent()) return;
      let publicUrl = sanitizeSrtUrl(response?.public_url);
      let vpnUrl = sanitizeSrtUrl(response?.vpn_url);
      if (!publicUrl && !vpnUrl && typeof response?.command_id === "string") {
        const outcome = await waitForRelayCommand(nodeId, response.command_id, { isCurrent: requestIsCurrent });
        if (!requestIsCurrent()) return;
        if (outcome !== "success") {
          throw new ApiError("Relay command failed", outcome === "conflict" ? 409 : 422);
        }
        wipeSecretObject(response);
        response = await apiRequest(
          `${endpoint}?wait=0`,
          { method: "POST", body, headers },
        );
        if (!requestIsCurrent()) return;
        publicUrl = sanitizeSrtUrl(response?.public_url);
        vpnUrl = sanitizeSrtUrl(response?.vpn_url);
      }
      if (!publicUrl && !vpnUrl) throw new ApiError("SRT URL not ready", 409);
      const publicInput = moblinDialog.querySelector("[data-dashboard-moblin-public-url]");
      const vpnInput = moblinDialog.querySelector("[data-dashboard-moblin-vpn-url]");
      const publicRow = moblinDialog.querySelector("[data-dashboard-public-url-row]");
      const vpnRow = moblinDialog.querySelector("[data-dashboard-vpn-url-row]");
      if (publicInput) publicInput.value = publicUrl;
      if (vpnInput) vpnInput.value = vpnUrl;
      if (publicRow) publicRow.hidden = !publicUrl;
      if (vpnRow) vpnRow.hidden = !vpnUrl;
      const results = moblinDialog.querySelector("[data-dashboard-moblin-results]");
      const reveal = moblinDialog.querySelector("[data-dashboard-reveal-moblin]");
      if (results) results.hidden = false;
      if (reveal) reveal.hidden = true;
    } catch (error) {
      if (!requestIsCurrent()) return;
      if (moblinError) {
        moblinError.textContent = friendlyError(error, "Не удалось получить SRT URL.");
        moblinError.hidden = false;
      }
    } finally {
      wipeSecretObject(response);
      response = null;
      body = "";
      if (requestIsCurrent()) setBusy(moblinForm, false);
    }
  }

  async function submitClear(event) {
    event.preventDefault();
    if (!relayNodeId) return;
    const nodeId = String(relayNodeId);
    const requestGeneration = ++clearRequestGeneration;
    const requestIsCurrent = () => dialogRequestIsCurrent(
      nodeId,
      requestGeneration,
      clearRequestGeneration,
      clearDialog,
    );
    let payload = null;
    let body = "";
    setBusy(clearForm, true);
    if (clearError) clearError.hidden = true;
    try {
      payload = buildAdminPasswordPayload(Object.fromEntries(new FormData(clearForm).entries()));
      body = JSON.stringify(payload);
      const headers = {};
      const key = createIdempotencyKey("clear-youtube");
      if (key) headers["Idempotency-Key"] = key;
      const queued = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay/youtube`, { method: "DELETE", body, headers });
      if (!requestIsCurrent()) return;
      const outcome = await waitForRelayCommand(nodeId, queued?.command_id || "", { isCurrent: requestIsCurrent });
      if (!requestIsCurrent()) return;
      if (outcome !== "success") throw new ApiError("Relay command failed", outcome === "conflict" ? 409 : 422);
      await loadStatus({ quiet: true });
      if (!requestIsCurrent()) return;
      resetDialog(clearDialog, clearError);
      closeDialog(clearDialog);
      showToast("Данные YouTube удалены");
    } catch (error) {
      if (!requestIsCurrent()) return;
      if (clearError) {
        clearError.textContent = friendlyError(error, "Не удалось удалить данные YouTube.");
        clearError.hidden = false;
      }
    } finally {
      wipeSecretObject(payload);
      payload = null;
      body = "";
      if (requestIsCurrent()) {
        clearSensitiveFields(clearForm);
        setBusy(clearForm, false);
      }
    }
  }

  page.addEventListener("click", (event) => {
    if (event.target.closest("[data-open-youtube-config]")) {
      youtubeRequestGeneration += 1;
      resetDialog(youtubeDialog, youtubeError);
      prepareYouTubeDialog();
      openDialog(youtubeDialog);
      youtubeDialog?.querySelector("[data-dashboard-youtube-stream-key]")?.focus();
      return;
    }
    if (event.target.closest("[data-open-moblin-url]")) {
      moblinRequestGeneration += 1;
      resetDialog(moblinDialog, moblinError);
      moblinDialog?.querySelector("[data-dashboard-moblin-results]")?.setAttribute("hidden", "");
      const reveal = moblinDialog?.querySelector("[data-dashboard-reveal-moblin]");
      if (reveal) reveal.hidden = false;
      openDialog(moblinDialog);
      reveal?.focus();
      return;
    }
    if (event.target.closest("[data-open-youtube-clear]")) {
      clearRequestGeneration += 1;
      resetDialog(clearDialog, clearError);
      openDialog(clearDialog);
      clearDialog?.querySelector("[data-dashboard-clear-admin-password]")?.focus();
      return;
    }
    const action = event.target.closest("[data-relay-primary-action]")?.dataset.action;
    if (action) void runRelayAction(action);
    if (event.target.closest("[data-relay-start]")) void runRelayAction("start");
    if (event.target.closest("[data-relay-stop]")) void runRelayAction("stop");
    if (event.target.closest("[data-relay-refresh]")) void runRelayAction("refresh");
  });

  youtubeDialog?.addEventListener("close", () => {
    youtubeRequestGeneration += 1;
    resetDialog(youtubeDialog, youtubeError);
  });
  moblinDialog?.addEventListener("close", () => {
    moblinRequestGeneration += 1;
    resetDialog(moblinDialog, moblinError);
  });
  clearDialog?.addEventListener("close", () => {
    clearRequestGeneration += 1;
    resetDialog(clearDialog, clearError);
  });
  youtubeForm?.addEventListener("submit", submitYouTube);
  moblinForm?.addEventListener("submit", submitMoblin);
  clearForm?.addEventListener("submit", submitClear);
  retryButton?.addEventListener("click", () => void loadStatus());
  page.querySelector("[data-relay-preview-retry]")?.addEventListener("click", () => {
    previewLeaseRenewedAt = 0;
    previewController?.retry();
    void updatePreview(currentRelay);
  });
  moblinDialog?.querySelectorAll("[data-dashboard-copy-moblin]").forEach((button) => {
    button.addEventListener("click", async () => {
      const selector = button.dataset.dashboardCopyMoblin === "vpn"
        ? "[data-dashboard-moblin-vpn-url]"
        : "[data-dashboard-moblin-public-url]";
      const value = sanitizeSrtUrl(moblinDialog.querySelector(selector)?.value);
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
        showToast("SRT URL скопирован", "Вставьте его в поле URL профиля приложения.");
      } catch (_error) {
        showToast("Не удалось скопировать", "Выделите адрес и скопируйте вручную.", "error");
      }
    });
  });
  addEventListener("pagehide", () => {
    if (pollTimer !== null) clearTimeout(pollTimer);
    loadGeneration += 1;
    previewUpdateGeneration += 1;
    youtubeRequestGeneration += 1;
    moblinRequestGeneration += 1;
    clearRequestGeneration += 1;
    setBusy(youtubeForm, false);
    setBusy(moblinForm, false);
    setBusy(clearForm, false);
    previewController?.suspend("offline");
    clearSensitiveFields(document);
  });

  void loadStatus();
})(typeof globalThis !== "undefined" ? globalThis : this);
