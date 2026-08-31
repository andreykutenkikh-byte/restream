(function serverPageModule(globalScope) {
  "use strict";

  const TERMINAL_JOB_STATES = new Set(["completed", "cancelled", "failed"]);
  const POLL_INTERVAL_MS = 1500;
  const MAX_POLL_ATTEMPTS = 600;
  const MAX_COMMAND_POLL_ATTEMPTS = 40;
  const MAX_TRANSIENT_POLL_FAILURES = 5;
  const MAX_TRANSIENT_POLL_DELAY_MS = 12000;
  const KNOWN_STEP_LABELS = Object.freeze({
    ssh_connect: "Проверяем SSH",
    resolving: "Проверяем адрес сервера",
    connecting: "Проверяем SSH",
    verifying_host_key: "Закрепляем SSH-ключ",
    authenticating: "Проверяем доступ",
    checking_privileges: "Проверяем права root или sudo",
    checking_system: "Проверяем операционную систему",
    checking_resources: "Проверяем ресурсы",
    checking_docker: "Проверяем Docker",
    installing_docker: "Устанавливаем Docker",
    needs_enrollment_token: "Подготавливаем Node Agent",
    preparing_agent: "Подготавливаем Node Agent",
    installing_agent: "Устанавливаем Node Agent",
    waiting_for_enrollment: "Подключаем агент к панели",
    running_self_test: "Выполняем финальную проверку",
  });
  const KNOWN_STEP_STATES = new Set(["pending", "running", "completed", "failed", "skipped"]);
  const NODE_STATUS = Object.freeze({
    installing: ["Установка", "warning"],
    connecting: ["Подключается", "warning"],
    ready: ["Готов к назначению", "success"],
    degraded: ["Требуется внимание", "warning"],
    offline: ["Нет связи", "neutral"],
    revoked: ["Доступ отозван", "danger"],
    failed: ["Установка не завершена", "danger"],
  });

  function normalizeNodeStatus(value) {
    const normalized = typeof value === "string" ? value.toLowerCase() : "offline";
    return Object.hasOwn(NODE_STATUS, normalized) ? normalized : "offline";
  }

  function safeDisplayString(value, fallback = "—", maximumLength = 160) {
    if (typeof value !== "string" && typeof value !== "number") return fallback;
    const text = String(value).replace(/[\u0000-\u001f\u007f]/g, " ").trim();
    if (!text) return fallback;
    return text.slice(0, maximumLength);
  }

  function buildBootstrapPayload(formValues) {
    const port = Number(formValues.port ?? 22);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new TypeError("SSH port is invalid");
    }
    const fingerprint = String(formValues.expected_host_fingerprint || "").trim();
    return {
      address: String(formValues.address || "").trim(),
      port,
      username: String(formValues.username || "").trim(),
      password: String(formValues.password || ""),
      expected_host_fingerprint: fingerprint || null,
    };
  }

  function createPollBudget(maximum = MAX_POLL_ATTEMPTS) {
    let remaining = maximum;
    return {
      consume() {
        if (remaining <= 0) return false;
        remaining -= 1;
        return true;
      },
      get remaining() {
        return remaining;
      },
    };
  }

  function transientPollDelay(failureCount) {
    if (!Number.isInteger(failureCount) || failureCount < 1 || failureCount > MAX_TRANSIENT_POLL_FAILURES) {
      return null;
    }
    return Math.min(POLL_INTERVAL_MS * 2 ** (failureCount - 1), MAX_TRANSIENT_POLL_DELAY_MS);
  }

  function hasMoblinRelayCapability(node) {
    return Array.isArray(node?.capabilities) && node.capabilities.includes("moblin_relay");
  }

  function canRevokeNode(status, relayCapable = false) {
    return ["ready", "degraded", "offline"].includes(status)
      || (relayCapable === true && status === "connecting");
  }

  function compactRelayValue(value) {
    return String(value || "").replace(/\s+/g, "");
  }

  function buildYouTubeConfigPayload(formValues) {
    const url = compactRelayValue(formValues?.url);
    const streamKey = compactRelayValue(formValues?.stream_key);
    const adminPassword = String(formValues?.admin_password || "");
    if (!url.startsWith("rtmps://") || url.includes("#")) {
      throw new TypeError("Укажите RTMPS URL без символа #.");
    }
    if (!streamKey) throw new TypeError("Введите YouTube stream key.");
    if (!adminPassword.trim()) throw new TypeError("Введите пароль администратора панели.");
    return { url, stream_key: streamKey, admin_password: adminPassword };
  }

  function buildAdminPasswordPayload(formValues) {
    const adminPassword = String(formValues?.admin_password || "");
    if (!adminPassword.trim()) throw new TypeError("Введите пароль администратора панели.");
    return { admin_password: adminPassword };
  }

  function sanitizeSrtUrl(value) {
    if (typeof value !== "string" || value.length > 4096 || /[\u0000-\u001f\u007f]/.test(value)) return "";
    const candidate = value.trim();
    return candidate.startsWith("srt://") ? candidate : "";
  }

  function clearSensitiveFields(container) {
    if (!container || typeof container.querySelectorAll !== "function") return;
    container.querySelectorAll('input[type="password"], [data-sensitive-output]').forEach((field) => {
      if ("value" in field) field.value = "";
      else field.textContent = "";
    });
  }

  function wipeSecretObject(payload) {
    if (!payload || typeof payload !== "object") return;
    for (const key of ["stream_key", "admin_password", "public_url", "vpn_url"]) {
      if (Object.hasOwn(payload, key)) payload[key] = "";
    }
  }

  function createRelayIdempotencyKey(action, nonce = globalScope.crypto?.randomUUID?.()) {
    const safeAction = typeof action === "string" && /^[a-z0-9_-]{1,32}$/.test(action) ? action : "";
    const safeNonce = typeof nonce === "string" && /^[A-Za-z0-9-]{8,64}$/.test(nonce) ? nonce : "";
    return safeAction && safeNonce ? `ui:${safeAction}:${safeNonce}` : null;
  }

  function isCurrentSecretRequest(
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

  function normalizeRelayStatus(payload) {
    const status = payload?.status && typeof payload.status === "object" ? payload.status : {};
    const token = (value, allowed, fallback = "unknown") => {
      const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
      return allowed.includes(normalized) ? normalized : fallback;
    };
    const source = typeof status.source === "string" ? status.source.trim().toUpperCase() : "";
    return {
      available: payload?.available === true,
      service: token(status.service, ["active", "inactive", "activating", "deactivating", "failed"]),
      enabled: status.enabled === true,
      mainProcess: token(status.main_process, ["running", "stopped", "failed", "unknown"]),
      srtListener: token(status.srt_listener, ["listening", "closed", "failed", "unknown"]),
      source: ["SLATE", "LIVE"].includes(source) ? source : "unknown",
      youtubeForward: token(status.youtube_forward, ["active", "inactive", "connecting", "failed", "unknown"]),
      overall: token(status.overall, ["ok", "healthy", "degraded", "failed", "offline", "unknown"]),
      youtubeUrlConfigured: status.youtube_url_configured === true,
      youtubeKeyConfigured: status.youtube_key_configured === true,
      portraitProfile: status.portrait_profile === true,
      lastSeenAt: typeof payload?.last_seen_at === "string" ? payload.last_seen_at : "",
    };
  }

  function relayIsOperable(relay) {
    return relay?.available === true;
  }

  function relayIsSafelyStopped(relay) {
    return relay?.service === "inactive" && relay?.mainProcess === "stopped";
  }

  function relayCommandOutcome(command) {
    const state = typeof command?.state === "string" ? command.state.toLowerCase() : "";
    const completion = typeof command?.completion_status === "string"
      ? command.completion_status.toLowerCase()
      : typeof command?.status === "string" ? command.status.toLowerCase() : "";
    if (state === "completed" && completion === "ok") return "success";
    if (completion === "conflict") return "conflict";
    if (["completed", "failed", "cancelled"].includes(state)) return "failed";
    return "pending";
  }

  const exported = {
    MAX_POLL_ATTEMPTS,
    MAX_COMMAND_POLL_ATTEMPTS,
    MAX_TRANSIENT_POLL_FAILURES,
    POLL_INTERVAL_MS,
    TERMINAL_JOB_STATES,
    buildAdminPasswordPayload,
    buildBootstrapPayload,
    buildYouTubeConfigPayload,
    canRevokeNode,
    clearSensitiveFields,
    createRelayIdempotencyKey,
    createPollBudget,
    hasMoblinRelayCapability,
    isCurrentSecretRequest,
    normalizeNodeStatus,
    normalizeRelayStatus,
    relayCommandOutcome,
    relayIsOperable,
    relayIsSafelyStopped,
    safeDisplayString,
    sanitizeSrtUrl,
    transientPollDelay,
    wipeSecretObject,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (!globalScope || !globalScope.document) return;

  const { document, window } = globalScope;
  const page = document.querySelector("[data-servers-page]");
  if (!page) return;

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const toastRegion = document.querySelector("[data-toast-region]");
  const serverDialog = document.querySelector("[data-server-dialog]");
  const progressDialog = document.querySelector("[data-server-progress-dialog]");
  const revokeDialog = document.querySelector("[data-revoke-dialog]");
  const youtubeConfigDialog = document.querySelector("[data-youtube-config-dialog]");
  const youtubeConfigForm = document.querySelector("[data-youtube-config-form]");
  const youtubeConfigError = document.querySelector("[data-youtube-config-error]");
  const youtubeClearDialog = document.querySelector("[data-youtube-clear-dialog]");
  const youtubeClearForm = document.querySelector("[data-youtube-clear-form]");
  const youtubeClearError = document.querySelector("[data-youtube-clear-error]");
  const moblinUrlDialog = document.querySelector("[data-moblin-url-dialog]");
  const moblinUrlForm = document.querySelector("[data-moblin-url-form]");
  const moblinUrlError = document.querySelector("[data-moblin-url-error]");
  const moblinUrlResults = document.querySelector("[data-moblin-url-results]");
  const moblinReauth = document.querySelector("[data-moblin-reauth]");
  const revealMoblinButton = document.querySelector("[data-reveal-moblin-url]");
  const serverForm = document.querySelector("[data-server-form]");
  const sudoForm = document.querySelector("[data-sudo-form]");
  const serverList = document.querySelector("[data-server-list]");
  const emptyState = document.querySelector("[data-servers-empty]");
  const loadingState = document.querySelector("[data-servers-loading]");
  const listError = document.querySelector("[data-servers-error]");
  const bootstrapUnavailable = document.querySelector("[data-bootstrap-unavailable]");
  const installSteps = document.querySelector("[data-install-steps]");
  const jobStateLabel = document.querySelector("[data-job-state-label]");
  const jobProgressLabel = document.querySelector("[data-job-progress-label]");
  const jobProgress = document.querySelector("[data-job-progress]");
  const jobError = document.querySelector("[data-job-error]");
  const cancelJobButton = document.querySelector("[data-cancel-job]");
  const closeProgressButton = document.querySelector("[data-close-progress]");
  const formError = document.querySelector("[data-server-form-error]");
  let csrfToken = csrfMeta?.content || "";
  let activeJobId = null;
  let pollGeneration = 0;
  let pollTimer = null;
  let pollBudget = createPollBudget();
  let transientPollFailures = 0;
  let pendingRevokeNodeId = null;
  let pendingYouTubeNodeId = null;
  let pendingYouTubeClearNodeId = null;
  let pendingMoblinNodeId = null;
  let youtubeConfigRequestGeneration = 0;
  let youtubeClearRequestGeneration = 0;
  let moblinRequestGeneration = 0;

  class ApiError extends Error {
    constructor(message, status = 0, payload = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  function serverMessage(payload) {
    const candidate = payload?.error?.message || payload?.safe_error?.message || payload?.message;
    if (typeof candidate !== "string") return "";
    if (
      candidate.length > 240 ||
      candidate.split(/\r?\n/).length > 2 ||
      /traceback|stack trace|exception|\/app\/|\\app\\|\.py:\d+|authorization|bearer/i.test(candidate)
    ) {
      return "";
    }
    return candidate.trim();
  }

  function serverErrorCode(payload) {
    const candidate = payload?.error?.code || payload?.safe_error?.code || payload?.detail?.code;
    return typeof candidate === "string" && /^[a-z0-9_]{1,64}$/.test(candidate) ? candidate : "";
  }

  function friendlyError(error, fallback = "Не удалось выполнить действие. Попробуйте ещё раз.") {
    if (!(error instanceof ApiError)) return fallback;
    if (error.status === 401) return "Сессия завершена. Войдите снова.";
    if (error.status === 403) return "Сессия устарела. Обновите страницу и повторите действие.";
    if (error.status === 409) return serverMessage(error.payload) || "Действие сейчас недоступно.";
    if (error.status === 422) return serverMessage(error.payload) || "Проверьте введённые данные.";
    if (error.status === 429) return "Слишком много попыток. Подождите и повторите.";
    if (error.status >= 500) return "Сервис установки временно недоступен.";
    return serverMessage(error.payload) || fallback;
  }

  function friendlyRelayError(error, action = "выполнить действие") {
    if (error instanceof TypeError && typeof error.message === "string") return error.message;
    if (!(error instanceof ApiError)) return `Не удалось ${action}. Попробуйте ещё раз.`;
    const code = serverErrorCode(error.payload);
    if (error.status === 401 && code === "step_up_failed") return "Пароль администратора неверен.";
    if (error.status === 401) return "Сессия завершена. Войдите снова.";
    if (error.status === 403) return "Сессия устарела. Обновите страницу и повторите действие.";
    if (error.status === 409) {
      if (code === "relay_unavailable") return "Relay-сервер сейчас не на связи.";
      if (code === "youtube_not_configured") return "Сначала настройте YouTube RTMPS URL и stream key.";
      if (code === "relay_command_pending") return "Предыдущая команда relay ещё выполняется.";
      if (code !== "relay_active") return `Сейчас нельзя ${action}. Обновите статус и повторите.`;
      return "Ретранслятор активен. Завершите broadcast в YouTube и остановите relay, затем повторите.";
    }
    if (error.status === 422) return "Проверьте RTMPS URL, stream key и пароль администратора.";
    if (error.status === 429) return "Слишком много попыток. Подождите и повторите.";
    return `Не удалось ${action}. Попробуйте ещё раз.`;
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
      throw new ApiError("Нет соединения с сервисом.");
    }
    let payload = null;
    if (response.status !== 204) {
      const body = await response.text();
      if (body) {
        try {
          payload = JSON.parse(body);
        } catch (_error) {
          payload = null;
        }
      }
    }
    if (!response.ok) {
      if (response.status === 401 && serverErrorCode(payload) !== "step_up_failed") {
        window.location.assign("/login");
      }
      throw new ApiError(serverMessage(payload) || response.statusText, response.status, payload);
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
    const icon = document.createElement("span");
    icon.className = "toast__icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = type === "success" ? "✓" : "!";
    const content = document.createElement("span");
    content.className = "toast__content";
    const heading = document.createElement("strong");
    heading.textContent = title;
    content.append(heading);
    if (detail) {
      const description = document.createElement("span");
      description.textContent = detail;
      content.append(description);
    }
    const close = document.createElement("button");
    close.className = "toast__close";
    close.type = "button";
    close.setAttribute("aria-label", "Закрыть уведомление");
    close.textContent = "×";
    close.addEventListener("click", () => toast.remove());
    toast.append(icon, content, close);
    toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }

  function setBusy(target, busy) {
    if (!target) return;
    target.setAttribute("aria-busy", String(busy));
    const button = target.matches?.("button") ? target : target.querySelector('button[type="submit"]');
    if (button) button.disabled = busy;
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function appendText(parent, tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    parent.append(element);
    return element;
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} ГБ`;
    return `${Math.round(bytes / 1024 ** 2)} МБ`;
  }

  function formatAge(value) {
    if (typeof value !== "string") return "—";
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "—";
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 60) return `${seconds} сек. назад`;
    const minutes = Math.round(seconds / 60);
    return `${minutes} мин. назад`;
  }

  function metricRow(list, label, value) {
    const row = document.createElement("div");
    appendText(row, "dt", "", label);
    appendText(row, "dd", "", value);
    list.append(row);
  }

  const RELAY_LABELS = Object.freeze({
    active: "Активен",
    inactive: "Остановлен",
    activating: "Запускается",
    deactivating: "Останавливается",
    running: "Работает",
    stopped: "Остановлен",
    listening: "Слушает SRT",
    closed: "Не слушает",
    connecting: "Подключается",
    ok: "Норма",
    healthy: "Норма",
    degraded: "Требуется внимание",
    failed: "Ошибка",
    offline: "Нет связи",
    unknown: "Нет данных",
    LIVE: "LIVE — поток Moblin",
    SLATE: "SLATE — серверная заставка",
  });

  function relayLabel(value) {
    return RELAY_LABELS[value] || RELAY_LABELS.unknown;
  }

  function relayMetricRow(list, label, key, initial = "Проверяем…") {
    const row = document.createElement("div");
    appendText(row, "dt", "", label);
    const value = appendText(row, "dd", "", initial);
    value.dataset.relayField = key;
    list.append(row);
  }

  function setRelayMetric(panel, key, value) {
    const output = panel?.querySelector(`[data-relay-field="${key}"]`);
    if (output) output.textContent = value;
  }

  function renderRelayPanel(node) {
    const panel = document.createElement("section");
    panel.className = "relay-panel";
    panel.dataset.relayPanel = "";
    panel.dataset.nodeId = String(node.id || "");
    panel.setAttribute("aria-label", "Управление Moblin Relay");

    const heading = document.createElement("div");
    heading.className = "relay-panel__heading";
    const titleGroup = document.createElement("div");
    appendText(titleGroup, "h3", "", "Moblin Relay");
    appendText(titleGroup, "p", "", "Вертикальный поток 720×1280 через SRT в YouTube");
    const badge = appendText(heading, "span", "relay-badge relay-badge--neutral", "Проверяем…");
    badge.dataset.relayBadge = "";
    badge.setAttribute("role", "status");
    heading.prepend(titleGroup);
    panel.append(heading);

    const alert = appendText(panel, "p", "relay-panel__notice", "Получаем безопасное состояние relay…");
    alert.dataset.relayNotice = "";
    alert.setAttribute("aria-live", "polite");

    const metrics = document.createElement("dl");
    metrics.className = "relay-metrics";
    relayMetricRow(metrics, "Общее состояние", "overall");
    relayMetricRow(metrics, "Сервис", "service");
    relayMetricRow(metrics, "Источник", "source");
    relayMetricRow(metrics, "Отправка в YouTube", "youtube-forward");
    relayMetricRow(metrics, "SRT listener", "srt-listener");
    relayMetricRow(metrics, "YouTube RTMPS URL", "youtube-url");
    relayMetricRow(metrics, "YouTube stream key", "youtube-key");
    relayMetricRow(metrics, "Профиль видео", "portrait-profile");
    panel.append(metrics);

    const actions = document.createElement("div");
    actions.className = "relay-panel__actions";
    const start = appendText(actions, "button", "button button--primary button--small", "Запустить relay");
    start.type = "button";
    start.dataset.relayAction = "start";
    start.addEventListener("click", () => void requestRelayAction(node.id, "start", start, panel));
    const stop = appendText(actions, "button", "button button--danger-soft button--small", "Остановить relay");
    stop.type = "button";
    stop.dataset.relayAction = "stop";
    stop.addEventListener("click", () => void requestRelayAction(node.id, "stop", stop, panel));
    const refresh = appendText(actions, "button", "button button--secondary button--small", "Обновить статус");
    refresh.type = "button";
    refresh.dataset.relayAction = "refresh";
    refresh.addEventListener("click", () => void requestRelayAction(node.id, "refresh", refresh, panel));
    const configure = appendText(actions, "button", "button button--secondary button--small", "Настроить YouTube");
    configure.type = "button";
    configure.dataset.relayAction = "configure";
    configure.addEventListener("click", () => openYouTubeConfig(node.id));
    const clearYouTube = appendText(actions, "button", "button button--danger-soft button--small", "Очистить YouTube");
    clearYouTube.type = "button";
    clearYouTube.dataset.relayAction = "clear-youtube";
    clearYouTube.addEventListener("click", () => openYouTubeClear(node.id));
    const reveal = appendText(actions, "button", "button button--quiet button--small", "Показать SRT для Moblin");
    reveal.type = "button";
    reveal.dataset.relayAction = "reveal";
    reveal.addEventListener("click", () => openMoblinUrl(node.id));
    panel.append(actions);
    return panel;
  }

  function updateRelayPanel(panel, payload) {
    if (!panel) return;
    const relay = normalizeRelayStatus(payload);
    const badge = panel.querySelector("[data-relay-badge]");
    const notice = panel.querySelector("[data-relay-notice]");
    const actionButtons = panel.querySelectorAll("[data-relay-action]");
    const operable = relayIsOperable(relay);
    const active = relay.service === "active" || relay.mainProcess === "running";
    const safelyStopped = relayIsSafelyStopped(relay);
    const fullyConfigured = relay.youtubeUrlConfigured && relay.youtubeKeyConfigured;
    panel.dataset.relayActive = String(active);
    panel.dataset.relayAvailable = String(operable);

    if (badge) {
      badge.className = "relay-badge relay-badge--neutral";
      if (!operable) {
        badge.textContent = relay.available ? "Нет связи" : "Недоступен";
      } else if (["failed", "degraded"].includes(relay.service) || ["failed", "degraded"].includes(relay.overall)) {
        badge.textContent = "Требуется внимание";
        badge.className = "relay-badge relay-badge--danger";
      } else if (active) {
        badge.textContent = "Активен";
        badge.className = "relay-badge relay-badge--success";
      } else {
        badge.textContent = "Остановлен";
      }
    }
    if (notice) {
      notice.textContent = !operable
        ? relay.available
          ? "Связь с relay-агентом потеряна. Управление временно заблокировано."
          : "Узел не подтвердил доступность Moblin Relay. Проверьте связь с агентом."
        : !safelyStopped
          ? "Relay не подтвердил полную остановку. Завершите broadcast и остановите relay перед изменением YouTube."
          : fullyConfigured
            ? "Relay остановлен. Можно безопасно обновить настройки YouTube."
            : "Relay остановлен. Перед запуском настройте YouTube RTMPS URL и stream key.";
    }

    setRelayMetric(panel, "overall", relayLabel(relay.overall));
    setRelayMetric(panel, "service", relayLabel(relay.service));
    setRelayMetric(panel, "source", relayLabel(relay.source));
    setRelayMetric(panel, "youtube-forward", relayLabel(relay.youtubeForward));
    setRelayMetric(panel, "srt-listener", relayLabel(relay.srtListener));
    setRelayMetric(panel, "youtube-url", relay.youtubeUrlConfigured ? "Настроен" : "Не настроен");
    setRelayMetric(panel, "youtube-key", relay.youtubeKeyConfigured ? "Настроен" : "Не настроен");
    setRelayMetric(panel, "portrait-profile", relay.portraitProfile ? "720×1280 · 30 FPS" : "Не подтверждён");

    for (const button of actionButtons) button.disabled = !operable;
    const start = panel.querySelector('[data-relay-action="start"]');
    const stop = panel.querySelector('[data-relay-action="stop"]');
    const configure = panel.querySelector('[data-relay-action="configure"]');
    const clearYouTube = panel.querySelector('[data-relay-action="clear-youtube"]');
    if (start) {
      start.disabled = !operable || !safelyStopped || !fullyConfigured;
      start.title = !fullyConfigured ? "Сначала настройте YouTube RTMPS URL и stream key" : "";
    }
    if (stop) stop.disabled = !operable || safelyStopped;
    if (configure) {
      configure.disabled = !operable || !safelyStopped;
      configure.title = !safelyStopped ? "Сначала завершите broadcast и остановите relay" : "";
    }
    if (clearYouTube) {
      const configured = relay.youtubeUrlConfigured || relay.youtubeKeyConfigured;
      clearYouTube.disabled = !operable || !safelyStopped || !configured;
      clearYouTube.title = !safelyStopped ? "Сначала завершите broadcast и остановите relay" : "";
    }
  }

  async function loadRelayStatus(nodeId, panel, { quiet = false } = {}) {
    try {
      const payload = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay`);
      updateRelayPanel(panel, payload);
      return payload;
    } catch (error) {
      updateRelayPanel(panel, { available: false });
      if (!quiet) showToast("Не удалось получить состояние relay", friendlyRelayError(error), "error");
      return null;
    }
  }

  function renderNodeCard(node) {
    const status = normalizeNodeStatus(node?.status);
    const [statusLabel, statusTone] = NODE_STATUS[status];
    const card = document.createElement("article");
    card.className = "server-card";
    const heading = document.createElement("div");
    heading.className = "server-card__heading";
    const identity = document.createElement("div");
    appendText(identity, "h2", "", safeDisplayString(node?.display_name, "Сервер"));
    const statusPill = appendText(identity, "span", `server-status server-status--${statusTone}`, statusLabel);
    statusPill.setAttribute("data-node-status", status);
    heading.append(identity);
    const relayCapable = hasMoblinRelayCapability(node);
    const availability = appendText(
      heading,
      "span",
      "server-card__scope",
      relayCapable ? "Управление видеоретранслятором" : "Подготовлен, но ещё не участвует в эфире",
    );
    availability.setAttribute(
      "title",
      relayCapable ? "Состояние Moblin Relay обновляется с сервера" : "Передача видеопотока пока не назначена",
    );
    card.append(heading);

    const metrics = document.createElement("dl");
    metrics.className = "server-metrics";
    metricRow(metrics, "IP", safeDisplayString(node?.resolved_ip || node?.address));
    const os = [safeDisplayString(node?.os_name, "", 60), safeDisplayString(node?.os_version, "", 30)]
      .filter(Boolean)
      .join(" ") || "—";
    metricRow(metrics, "Операционная система", os);
    metricRow(metrics, "CPU", Number.isInteger(node?.cpu_count) ? `${node.cpu_count} ядер` : "—");
    metricRow(metrics, "RAM", formatBytes(node?.memory_total_bytes));
    metricRow(metrics, "Диск свободен", formatBytes(node?.disk_free_bytes));
    const controlLatency = node?.control_latency_ms;
    const controlLatencyLabel = Number.isFinite(controlLatency)
      && controlLatency >= 0
      && controlLatency <= 60000
      ? `${Math.round(controlLatency)} мс`
      : "—";
    metricRow(metrics, "Связь с панелью", controlLatencyLabel);
    metricRow(metrics, "Последний heartbeat", formatAge(node?.last_seen_at));
    metricRow(metrics, "Версия агента", safeDisplayString(node?.agent_version));
    if (node?.host_key_fingerprint) {
      metricRow(metrics, "SSH fingerprint", safeDisplayString(node.host_key_fingerprint, "—", 128));
    }
    card.append(metrics);

    if (relayCapable) card.append(renderRelayPanel(node));

    const actions = document.createElement("div");
    actions.className = "server-card__actions";
    if (!relayCapable) {
      const selfTest = appendText(actions, "button", "button button--secondary button--small", "Проверить сервер");
      selfTest.type = "button";
      selfTest.disabled = !["ready", "degraded", "offline"].includes(status);
      selfTest.addEventListener("click", () => void requestSelfTest(node.id, selfTest));
    }
    const rename = appendText(actions, "button", "button button--quiet button--small", "Переименовать");
    rename.type = "button";
    rename.addEventListener("click", () => void renameNode(node));
    const revoke = appendText(actions, "button", "button button--danger-soft button--small", "Отозвать доступ");
    revoke.type = "button";
    revoke.disabled = !canRevokeNode(status, relayCapable);
    revoke.addEventListener("click", () => {
      pendingRevokeNodeId = node.id;
      openDialog(revokeDialog);
    });
    card.append(actions);
    return card;
  }

  async function loadServers() {
    if (loadingState) loadingState.hidden = false;
    if (listError) listError.hidden = true;
    try {
      const payload = await apiRequest("/api/nodes");
      const items = Array.isArray(payload?.items) ? payload.items : [];
      const cards = items.map(renderNodeCard);
      serverList?.replaceChildren(...cards);
      items.forEach((node, index) => {
        const panel = cards[index]?.querySelector("[data-relay-panel]");
        if (panel) void loadRelayStatus(node.id, panel, { quiet: true });
      });
      if (emptyState) emptyState.hidden = items.length > 0;
    } catch (error) {
      if (listError) listError.hidden = false;
      showToast("Не удалось загрузить серверы", friendlyError(error), "error");
    } finally {
      if (loadingState) loadingState.hidden = true;
    }
  }

  function renderJob(job) {
    const progress = Math.min(100, Math.max(0, Number(job?.progress_percent) || 0));
    const state = typeof job?.state === "string" ? job.state : "running";
    if (jobProgress) {
      jobProgress.value = progress;
      jobProgress.textContent = `${progress}%`;
    }
    if (jobProgressLabel) jobProgressLabel.textContent = `${progress}%`;
    if (jobStateLabel) {
      jobStateLabel.textContent = state === "completed" ? "Сервер готов к назначению" : "Выполняем безопасную установку";
    }
    const steps = Array.isArray(job?.steps) ? job.steps : [];
    const rows = [];
    for (const step of steps) {
      const label = KNOWN_STEP_LABELS[step?.name];
      if (!label) continue;
      const stateName = KNOWN_STEP_STATES.has(step?.state) ? step.state : "pending";
      const row = document.createElement("li");
      row.className = `install-step install-step--${stateName}`;
      const marker = appendText(row, "span", "install-step__marker", stateName === "completed" ? "✓" : stateName === "running" ? "●" : stateName === "failed" ? "!" : "○");
      marker.setAttribute("aria-hidden", "true");
      appendText(row, "span", "", label);
      rows.push(row);
    }
    installSteps?.replaceChildren(...rows);
    if (jobError) {
      let message = serverMessage(job?.safe_error || job);
      if (TERMINAL_JOB_STATES.has(state) && job?.docker_install_started === true) {
        const dockerNotice = "Установка Docker была начата; Docker может остаться установленным на сервере.";
        message = message ? `${message} ${dockerNotice}` : dockerNotice;
      }
      jobError.textContent = message;
      jobError.hidden = !message;
    }
    if (sudoForm) sudoForm.hidden = state !== "needs_sudo_password";
    if (cancelJobButton) cancelJobButton.hidden = TERMINAL_JOB_STATES.has(state);
    if (closeProgressButton) closeProgressButton.hidden = !TERMINAL_JOB_STATES.has(state);
    return state;
  }

  function clearPollTimer() {
    if (pollTimer !== null) window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function stopPolling() {
    pollGeneration += 1;
    clearPollTimer();
  }

  async function pollJob(generation) {
    if (generation !== pollGeneration || !activeJobId) return;
    if (!pollBudget.consume()) {
      stopPolling();
      if (jobError) {
        jobError.textContent = "Проверка заняла слишком много времени. Обновите страницу и проверьте состояние сервера.";
        jobError.hidden = false;
      }
      if (cancelJobButton) cancelJobButton.hidden = true;
      if (closeProgressButton) closeProgressButton.hidden = false;
      return;
    }
    try {
      const job = await apiRequest(`/api/nodes/bootstrap/${encodeURIComponent(activeJobId)}`);
      transientPollFailures = 0;
      const state = renderJob(job);
      if (state === "needs_sudo_password") return;
      if (TERMINAL_JOB_STATES.has(state)) {
        if (state === "completed") {
          showToast("Сервер подключён", "Узел готов к назначению.");
          await loadServers();
        }
        return;
      }
      pollTimer = window.setTimeout(() => void pollJob(generation), POLL_INTERVAL_MS);
    } catch (error) {
      const transient = error instanceof ApiError && (error.status === 0 || error.status >= 500);
      const retryDelay = transient ? transientPollDelay((transientPollFailures += 1)) : null;
      if (retryDelay !== null && generation === pollGeneration && activeJobId) {
        if (jobError) {
          jobError.textContent = "Связь с сервисом установки прервана. Повторяем проверку…";
          jobError.hidden = false;
        }
        pollTimer = window.setTimeout(() => void pollJob(generation), retryDelay);
        return;
      }
      stopPolling();
      if (jobError) {
        jobError.textContent = transient
          ? "Не удалось обновить состояние. Обновите страницу — активная задача будет восстановлена."
          : friendlyError(error, "Не удалось получить состояние установки.");
        jobError.hidden = false;
      }
      if (cancelJobButton) cancelJobButton.hidden = false;
      if (closeProgressButton) closeProgressButton.hidden = true;
    }
  }

  function startPolling(jobId) {
    stopPolling();
    activeJobId = jobId;
    pollBudget = createPollBudget();
    transientPollFailures = 0;
    pollGeneration += 1;
    void pollJob(pollGeneration);
  }

  async function resumeActiveBootstrap() {
    try {
      const job = await apiRequest("/api/nodes/bootstrap/active");
      const jobId = typeof job?.job_id === "string" ? job.job_id : "";
      if (!jobId) return;
      activeJobId = jobId;
      const state = renderJob(job);
      openDialog(progressDialog);
      if (!TERMINAL_JOB_STATES.has(state) && state !== "needs_sudo_password") {
        startPolling(jobId);
      }
    } catch (error) {
      showToast("Не удалось восстановить установку", friendlyError(error), "error");
    }
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function waitForCommand(nodeId, commandId) {
    const budget = createPollBudget(MAX_COMMAND_POLL_ATTEMPTS);
    while (budget.consume()) {
      const command = await apiRequest(
        `/api/nodes/${encodeURIComponent(nodeId)}/commands/${encodeURIComponent(commandId)}`,
      );
      if (command?.state === "completed" || command?.state === "failed") return command;
      await delay(POLL_INTERVAL_MS);
    }
    return null;
  }

  async function waitForRelayCommand(nodeId, commandId, { isCurrent = () => true } = {}) {
    const budget = createPollBudget(MAX_COMMAND_POLL_ATTEMPTS);
    while (budget.consume()) {
      if (!isCurrent()) return null;
      const command = await apiRequest(
        `/api/nodes/${encodeURIComponent(nodeId)}/relay/commands/${encodeURIComponent(commandId)}`,
      );
      if (!isCurrent()) return null;
      if (relayCommandOutcome(command) !== "pending") return command;
      await delay(POLL_INTERVAL_MS);
    }
    return null;
  }

  async function awaitRelayCompletion(nodeId, queued, { isCurrent = () => true } = {}) {
    const commandId = typeof queued?.command_id === "string"
      ? queued.command_id
      : typeof queued?.id === "string" ? queued.id : "";
    if (!commandId) throw new ApiError("Missing relay command identifier", 502);
    const result = await waitForRelayCommand(nodeId, commandId, { isCurrent });
    if (!result) return false;
    const outcome = relayCommandOutcome(result);
    if (outcome === "conflict") throw new ApiError("Relay command conflict", 409);
    if (outcome !== "success") throw new ApiError("Relay command failed", 422);
    return true;
  }

  function relayPanelForNode(nodeId) {
    return [...document.querySelectorAll("[data-relay-panel]")]
      .find((panel) => panel.dataset.nodeId === String(nodeId)) || null;
  }

  async function requestRelayAction(nodeId, action, button, panel = relayPanelForNode(nodeId)) {
    const confirmations = {
      start: "Запустить relay и начать отправку серверной заставки в настроенный YouTube broadcast?",
      stop: "Остановить relay? Это немедленно прервёт отправку потока в YouTube broadcast.",
    };
    if (confirmations[action] && !window.confirm(confirmations[action])) return;
    if (!new Set(["start", "stop", "refresh"]).has(action)) return;
    setBusy(button, true);
    try {
      const queued = await apiRequest(
        `/api/nodes/${encodeURIComponent(nodeId)}/relay/${encodeURIComponent(action)}`,
        { method: "POST" },
      );
      const completed = await awaitRelayCompletion(nodeId, queued);
      if (!completed) {
        showToast("Команда ещё выполняется", "Обновите статус через несколько секунд.", "warning");
        return;
      }
      if (action === "start") showToast("Relay запущен", "Проверьте Stream health в YouTube Studio.");
      if (action === "stop") showToast("Relay остановлен", "Отправка в YouTube прекращена.");
      if (action === "refresh") showToast("Состояние relay обновлено");
    } catch (error) {
      showToast("Команда relay не выполнена", friendlyRelayError(error), "error");
    } finally {
      setBusy(button, false);
      await loadRelayStatus(nodeId, panel, { quiet: true });
    }
  }

  function clearYouTubeConfig() {
    youtubeConfigForm?.reset();
    clearSensitiveFields(youtubeConfigDialog);
    setBusy(youtubeConfigForm, false);
    if (youtubeConfigError) {
      youtubeConfigError.textContent = "";
      youtubeConfigError.hidden = true;
    }
  }

  function dialogRequestIsCurrent(nodeId, currentNodeId, generation, currentGeneration, dialog) {
    const dialogOpen = dialog?.open === true || dialog?.hasAttribute?.("open") === true;
    return isCurrentSecretRequest(
      nodeId,
      currentNodeId,
      generation,
      currentGeneration,
      dialogOpen,
    );
  }

  function openYouTubeConfig(nodeId) {
    youtubeConfigRequestGeneration += 1;
    clearYouTubeConfig();
    pendingYouTubeNodeId = String(nodeId);
    openDialog(youtubeConfigDialog);
    youtubeConfigForm?.querySelector('[name="url"]')?.focus();
  }

  function clearMoblinUrlDialog() {
    moblinUrlForm?.reset();
    clearSensitiveFields(moblinUrlDialog);
    setBusy(moblinUrlForm, false);
    if (moblinUrlResults) moblinUrlResults.hidden = true;
    if (moblinReauth) moblinReauth.hidden = false;
    if (revealMoblinButton) revealMoblinButton.hidden = false;
    moblinUrlDialog?.querySelectorAll("[data-public-url-row], [data-vpn-url-row]").forEach((row) => {
      row.hidden = true;
    });
    if (moblinUrlError) {
      moblinUrlError.textContent = "";
      moblinUrlError.hidden = true;
    }
  }

  function moblinRequestIsCurrent(nodeId, generation) {
    return dialogRequestIsCurrent(
      nodeId,
      pendingMoblinNodeId,
      generation,
      moblinRequestGeneration,
      moblinUrlDialog,
    );
  }

  function openMoblinUrl(nodeId) {
    moblinRequestGeneration += 1;
    clearMoblinUrlDialog();
    pendingMoblinNodeId = String(nodeId);
    openDialog(moblinUrlDialog);
    moblinUrlForm?.querySelector("[data-moblin-admin-password]")?.focus();
  }

  youtubeConfigForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pendingYouTubeNodeId) return;
    const nodeId = String(pendingYouTubeNodeId);
    const requestGeneration = ++youtubeConfigRequestGeneration;
    const requestIsCurrent = () => dialogRequestIsCurrent(
      nodeId,
      pendingYouTubeNodeId,
      requestGeneration,
      youtubeConfigRequestGeneration,
      youtubeConfigDialog,
    );
    let payload = null;
    let requestBody = "";
    if (youtubeConfigError) youtubeConfigError.hidden = true;
    setBusy(youtubeConfigForm, true);
    try {
      payload = buildYouTubeConfigPayload(Object.fromEntries(new FormData(youtubeConfigForm).entries()));
      requestBody = JSON.stringify(payload);
      const queued = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay/configure-youtube`, {
        method: "PUT",
        body: requestBody,
      });
      const completed = await awaitRelayCompletion(nodeId, queued, { isCurrent: requestIsCurrent });
      if (!requestIsCurrent()) return;
      pendingYouTubeNodeId = null;
      clearYouTubeConfig();
      closeDialog(youtubeConfigDialog);
      await loadRelayStatus(nodeId, relayPanelForNode(nodeId), { quiet: true });
      if (completed) {
        showToast("YouTube настроен", "URL и stream key безопасно сохранены на relay-сервере.");
      } else {
        showToast("Настройка ещё выполняется", "Обновите статус через несколько секунд.", "warning");
      }
    } catch (error) {
      if (!requestIsCurrent()) return;
      if (youtubeConfigError) {
        youtubeConfigError.textContent = friendlyRelayError(error, "сохранить настройки YouTube");
        youtubeConfigError.hidden = false;
      }
    } finally {
      wipeSecretObject(payload);
      payload = null;
      requestBody = "";
      if (requestIsCurrent()) {
        clearSensitiveFields(youtubeConfigForm);
        const urlInput = youtubeConfigForm?.querySelector('[name="url"]');
        if (urlInput) urlInput.value = "";
        setBusy(youtubeConfigForm, false);
      }
    }
  });

  function clearYouTubeClearDialog() {
    youtubeClearForm?.reset();
    clearSensitiveFields(youtubeClearDialog);
    setBusy(youtubeClearForm, false);
    if (youtubeClearError) {
      youtubeClearError.textContent = "";
      youtubeClearError.hidden = true;
    }
  }

  function openYouTubeClear(nodeId) {
    youtubeClearRequestGeneration += 1;
    clearYouTubeClearDialog();
    pendingYouTubeClearNodeId = String(nodeId);
    openDialog(youtubeClearDialog);
    youtubeClearForm?.querySelector("[data-youtube-clear-admin-password]")?.focus();
  }

  youtubeClearForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pendingYouTubeClearNodeId) return;
    const nodeId = String(pendingYouTubeClearNodeId);
    const requestGeneration = ++youtubeClearRequestGeneration;
    const requestIsCurrent = () => dialogRequestIsCurrent(
      nodeId,
      pendingYouTubeClearNodeId,
      requestGeneration,
      youtubeClearRequestGeneration,
      youtubeClearDialog,
    );
    let payload = null;
    let requestBody = "";
    if (youtubeClearError) youtubeClearError.hidden = true;
    setBusy(youtubeClearForm, true);
    try {
      payload = buildAdminPasswordPayload(Object.fromEntries(new FormData(youtubeClearForm).entries()));
      requestBody = JSON.stringify(payload);
      const queued = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/relay/youtube`, {
        method: "DELETE",
        body: requestBody,
      });
      const completed = await awaitRelayCompletion(nodeId, queued, { isCurrent: requestIsCurrent });
      if (!requestIsCurrent()) return;
      pendingYouTubeClearNodeId = null;
      clearYouTubeClearDialog();
      closeDialog(youtubeClearDialog);
      await loadRelayStatus(nodeId, relayPanelForNode(nodeId), { quiet: true });
      if (completed) {
        showToast("YouTube очищен", "RTMPS URL и stream key удалены с relay-сервера.");
      } else {
        showToast("Очистка ещё выполняется", "Обновите статус через несколько секунд.", "warning");
      }
    } catch (error) {
      if (!requestIsCurrent()) return;
      if (youtubeClearError) {
        youtubeClearError.textContent = friendlyRelayError(error, "очистить настройки YouTube");
        youtubeClearError.hidden = false;
      }
    } finally {
      wipeSecretObject(payload);
      payload = null;
      requestBody = "";
      if (requestIsCurrent()) {
        clearSensitiveFields(youtubeClearForm);
        setBusy(youtubeClearForm, false);
      }
    }
  });

  moblinUrlForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pendingMoblinNodeId) return;
    const nodeId = String(pendingMoblinNodeId);
    const requestGeneration = ++moblinRequestGeneration;
    const requestIsCurrent = () => moblinRequestIsCurrent(nodeId, requestGeneration);
    const endpoint = `/api/nodes/${encodeURIComponent(nodeId)}/relay/reveal-moblin-url`;
    const idempotencyKey = createRelayIdempotencyKey("reveal-moblin");
    const requestHeaders = idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {};
    let payload = null;
    let response = null;
    let requestBody = "";
    if (moblinUrlError) moblinUrlError.hidden = true;
    setBusy(moblinUrlForm, true);
    try {
      payload = buildAdminPasswordPayload(Object.fromEntries(new FormData(moblinUrlForm).entries()));
      requestBody = JSON.stringify(payload);
      response = await apiRequest(endpoint, { method: "POST", body: requestBody, headers: requestHeaders });
      if (!requestIsCurrent()) return;
      let publicUrl = sanitizeSrtUrl(response?.public_url);
      let vpnUrl = sanitizeSrtUrl(response?.vpn_url);
      if (!publicUrl && !vpnUrl && typeof response?.command_id === "string") {
        const command = await waitForRelayCommand(nodeId, response.command_id, { isCurrent: requestIsCurrent });
        if (!requestIsCurrent()) return;
        if (!command) {
          showToast("SRT URL готовится", "Повторите запрос через несколько секунд.", "warning");
          return;
        }
        const outcome = relayCommandOutcome(command);
        if (outcome === "conflict") throw new ApiError("Relay command conflict", 409);
        if (outcome !== "success") throw new ApiError("Relay command failed", 422);
        wipeSecretObject(response);
        response = await apiRequest(`${endpoint}?wait=0`, {
          method: "POST",
          body: requestBody,
          headers: requestHeaders,
        });
        if (!requestIsCurrent()) return;
        publicUrl = sanitizeSrtUrl(response?.public_url);
        vpnUrl = sanitizeSrtUrl(response?.vpn_url);
        if (!publicUrl && !vpnUrl && typeof response?.command_id === "string") {
          showToast("SRT URL готовится", "Повторите запрос через несколько секунд.", "warning");
          return;
        }
      }
      if (!publicUrl && !vpnUrl) throw new ApiError("Invalid relay response", 502);
      const publicInput = moblinUrlDialog?.querySelector("[data-moblin-public-url]");
      const vpnInput = moblinUrlDialog?.querySelector("[data-moblin-vpn-url]");
      const publicRow = moblinUrlDialog?.querySelector("[data-public-url-row]");
      const vpnRow = moblinUrlDialog?.querySelector("[data-vpn-url-row]");
      if (publicInput) publicInput.value = publicUrl;
      if (vpnInput) vpnInput.value = vpnUrl;
      if (publicRow) publicRow.hidden = !publicUrl;
      if (vpnRow) vpnRow.hidden = !vpnUrl;
      if (moblinUrlResults) moblinUrlResults.hidden = false;
      if (moblinReauth) moblinReauth.hidden = true;
      if (revealMoblinButton) revealMoblinButton.hidden = true;
    } catch (error) {
      if (!requestIsCurrent()) return;
      clearSensitiveFields(moblinUrlDialog);
      if (moblinUrlError) {
        moblinUrlError.textContent = friendlyRelayError(error, "получить SRT URL");
        moblinUrlError.hidden = false;
      }
    } finally {
      wipeSecretObject(payload);
      wipeSecretObject(response);
      payload = null;
      response = null;
      requestBody = "";
      if (requestIsCurrent()) {
        const passwordInput = moblinUrlForm.querySelector("[data-moblin-admin-password]");
        if (passwordInput) passwordInput.value = "";
        setBusy(moblinUrlForm, false);
      }
    }
  });

  moblinUrlDialog?.querySelectorAll("[data-copy-moblin-url]").forEach((button) => {
    button.addEventListener("click", async () => {
      const selector = button.dataset.copyMoblinUrl === "vpn" ? "[data-moblin-vpn-url]" : "[data-moblin-public-url]";
      const value = sanitizeSrtUrl(moblinUrlDialog.querySelector(selector)?.value);
      if (!value) return;
      try {
        await window.navigator.clipboard.writeText(value);
        showToast("SRT URL скопирован", "Вставьте его в поле URL профиля Moblin.");
      } catch (_error) {
        showToast("Не удалось скопировать", "Выделите адрес и скопируйте вручную.", "error");
      }
    });
  });

  youtubeConfigDialog?.addEventListener("close", () => {
    youtubeConfigRequestGeneration += 1;
    clearYouTubeConfig();
    pendingYouTubeNodeId = null;
  });

  youtubeClearDialog?.addEventListener("close", () => {
    youtubeClearRequestGeneration += 1;
    clearYouTubeClearDialog();
    pendingYouTubeClearNodeId = null;
  });

  moblinUrlDialog?.addEventListener("close", () => {
    moblinRequestGeneration += 1;
    clearMoblinUrlDialog();
    pendingMoblinNodeId = null;
  });

  async function requestSelfTest(nodeId, button) {
    setBusy(button, true);
    try {
      const queued = await apiRequest(`/api/nodes/${encodeURIComponent(nodeId)}/self-test`, { method: "POST" });
      showToast("Проверка запущена", "Результат появится после ответа агента.");
      const commandId = typeof queued?.id === "string" ? queued.id : "";
      if (!commandId) throw new ApiError("Сервис вернул некорректный идентификатор команды.");
      const result = await waitForCommand(nodeId, commandId);
      if (!result) {
        showToast("Проверка ещё выполняется", "Обновите страницу через минуту.", "error");
      } else if (result.state === "completed" && result.safe_result?.status === "ok") {
        showToast("Проверка пройдена", "Агент и системные зависимости доступны.");
        await loadServers();
      } else {
        showToast("Проверка не пройдена", "Откройте карточку позже и повторите попытку.", "error");
      }
    } catch (error) {
      showToast("Не удалось запустить проверку", friendlyError(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function renameNode(node) {
    const currentName = safeDisplayString(node?.display_name, "Сервер");
    const requested = window.prompt("Новое отображаемое название", currentName);
    if (requested === null || requested.trim() === currentName) return;
    try {
      await apiRequest(`/api/nodes/${encodeURIComponent(node.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ display_name: requested.trim() }),
      });
      await loadServers();
      showToast("Название обновлено");
    } catch (error) {
      showToast("Не удалось переименовать сервер", friendlyError(error), "error");
    }
  }

  serverForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const passwordInput = serverForm.querySelector("[data-ssh-password]");
    if (formError) formError.hidden = true;
    setBusy(serverForm, true);
    let payload = null;
    try {
      payload = buildBootstrapPayload(Object.fromEntries(new FormData(serverForm).entries()));
      const response = await apiRequest("/api/nodes/bootstrap", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      payload.password = "";
      if (passwordInput) passwordInput.value = "";
      closeDialog(serverDialog);
      renderJob({ state: "queued", progress_percent: 0, steps: [] });
      openDialog(progressDialog);
      startPolling(response.job_id);
    } catch (error) {
      if (payload) payload.password = "";
      if (formError) {
        formError.textContent = friendlyError(error, "Не удалось создать задачу подключения.");
        formError.hidden = false;
      }
    } finally {
      setBusy(serverForm, false);
    }
  });

  sudoForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!activeJobId) return;
    const passwordInput = sudoForm.querySelector("[data-sudo-password]");
    let sudoPassword = passwordInput?.value || "";
    const sudoPayload = { sudo_password: sudoPassword };
    setBusy(sudoForm, true);
    try {
      await apiRequest(`/api/nodes/bootstrap/${encodeURIComponent(activeJobId)}/sudo-password`, {
        method: "POST",
        body: JSON.stringify(sudoPayload),
      });
      sudoForm.hidden = true;
      startPolling(activeJobId);
    } catch (error) {
      showToast("Не удалось продолжить установку", friendlyError(error), "error");
    } finally {
      sudoPayload.sudo_password = "";
      sudoPassword = "";
      if (passwordInput) passwordInput.value = "";
      setBusy(sudoForm, false);
    }
  });

  cancelJobButton?.addEventListener("click", async () => {
    if (!activeJobId) return;
    setBusy(cancelJobButton, true);
    try {
      await apiRequest(`/api/nodes/bootstrap/${encodeURIComponent(activeJobId)}/cancel`, { method: "POST" });
      startPolling(activeJobId);
    } catch (error) {
      showToast("Не удалось отменить установку", friendlyError(error), "error");
    } finally {
      setBusy(cancelJobButton, false);
    }
  });

  closeProgressButton?.addEventListener("click", () => {
    stopPolling();
    activeJobId = null;
    closeDialog(progressDialog);
  });

  document.querySelector("[data-confirm-revoke]")?.addEventListener("click", async (event) => {
    if (!pendingRevokeNodeId) return;
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      await apiRequest(`/api/nodes/${encodeURIComponent(pendingRevokeNodeId)}/revoke`, { method: "POST" });
      closeDialog(revokeDialog);
      pendingRevokeNodeId = null;
      await loadServers();
      showToast("Доступ отозван");
    } catch (error) {
      showToast("Не удалось отозвать доступ", friendlyError(error), "error");
    } finally {
      setBusy(button, false);
    }
  });

  document.addEventListener("click", (event) => {
    const openButton = event.target.closest("[data-open-server-dialog]");
    if (openButton) {
      if (page.dataset.bootstrapAvailable !== "true") {
        bootstrapUnavailable.hidden = false;
        bootstrapUnavailable.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (formError) formError.hidden = true;
      openDialog(serverDialog);
      serverForm?.querySelector('[name="address"]')?.focus();
      return;
    }
    const closeButton = event.target.closest("[data-close-dialog]");
    if (closeButton) {
      const dialog = closeButton.closest("dialog");
      clearSensitiveFields(dialog);
      if (dialog === youtubeConfigDialog) clearYouTubeConfig();
      if (dialog === youtubeClearDialog) clearYouTubeClearDialog();
      if (dialog === moblinUrlDialog) clearMoblinUrlDialog();
      closeDialog(dialog);
    }
  });

  document.querySelector("[data-retry-servers]")?.addEventListener("click", () => void loadServers());
  window.addEventListener("pagehide", () => {
    stopPolling();
    youtubeConfigRequestGeneration += 1;
    youtubeClearRequestGeneration += 1;
    moblinRequestGeneration += 1;
    clearSensitiveFields(document);
    clearYouTubeConfig();
    clearYouTubeClearDialog();
    clearMoblinUrlDialog();
    pendingYouTubeNodeId = null;
    pendingYouTubeClearNodeId = null;
    pendingMoblinNodeId = null;
  });

  if (page.dataset.bootstrapAvailable !== "true") bootstrapUnavailable.hidden = false;
  void loadServers();
  void resumeActiveBootstrap();
})(typeof globalThis !== "undefined" ? globalThis : this);
