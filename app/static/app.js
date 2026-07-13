(() => {
  "use strict";

  const API_BASE = (document.querySelector('meta[name="api-base"]')?.content || "/api").replace(/\/$/, "");
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const toastRegion = document.querySelector("[data-toast-region]");
  const destinationCache = new Map();
  let csrfToken = csrfMeta?.content || "";
  let statusRequestPending = false;
  let destinationRequestPending = false;
  let destinationsLoaded = false;
  let sessionReady = Promise.resolve();

  class ApiError extends Error {
    constructor(message, status = 0, payload = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  function apiUrl(path) {
    if (/^https?:\/\//i.test(path)) return path;
    if (path.startsWith(`${API_BASE}/`) || path === API_BASE) return path;
    return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  }

  function setCsrfToken(token) {
    if (!token || typeof token !== "string") return;
    csrfToken = token;
    if (csrfMeta) csrfMeta.content = token;
    document.querySelectorAll('input[name="csrf_token"]').forEach((input) => {
      input.value = token;
    });
  }

  function safeServerMessage(payload) {
    const candidate =
      (typeof payload?.message === "string" && payload.message) ||
      (typeof payload?.detail === "string" && payload.detail) ||
      (typeof payload?.error?.message === "string" && payload.error.message) ||
      (typeof payload?.error === "string" && payload.error) ||
      "";

    if (
      !candidate ||
      candidate.length > 240 ||
      /traceback|stack trace|exception|\/app\/|\\app\\|\.py:\d+/i.test(candidate) ||
      candidate.split(/\r?\n/).length > 2
    ) {
      return "";
    }
    return candidate.trim();
  }

  function friendlyError(error, fallback = "Не удалось выполнить действие. Попробуйте ещё раз.") {
    if (!(error instanceof ApiError)) return fallback;
    if (error.status === 400) return safeServerMessage(error.payload) || "Проверьте введённые данные.";
    if (error.status === 401) return "Неверный логин или пароль.";
    if (error.status === 403) return "Сессия устарела. Обновите страницу и повторите действие.";
    if (error.status === 404) return "Запрошенные данные не найдены.";
    if (error.status === 409) return safeServerMessage(error.payload) || "Действие сейчас недоступно.";
    if (error.status === 422) return "Проверьте правильность заполнения полей.";
    if (error.status === 429) return "Слишком много попыток. Подождите немного и повторите.";
    if (error.status >= 500) return "Сервис временно недоступен. Попробуйте чуть позже.";
    return safeServerMessage(error.payload) || fallback;
  }

  async function apiRequest(path, options = {}) {
    const { allowUnauthorized = false, ...fetchOptions } = options;
    const headers = new Headers(fetchOptions.headers || {});
    headers.set("Accept", "application/json");
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    if (fetchOptions.body && !(fetchOptions.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    let response;
    try {
      response = await fetch(apiUrl(path), {
        credentials: "same-origin",
        ...fetchOptions,
        headers,
      });
    } catch (_error) {
      throw new ApiError("Нет соединения с сервисом.", 0);
    }

    let payload = null;
    if (response.status !== 204) {
      const text = await response.text();
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (_error) {
          payload = null;
        }
      }
    }

    if (payload?.csrf_token) setCsrfToken(payload.csrf_token);

    if (!response.ok) {
      if (response.status === 401 && !allowUnauthorized && !document.querySelector("[data-login-form]")) {
        window.location.assign("/login");
      }
      throw new ApiError(safeServerMessage(payload) || response.statusText, response.status, payload);
    }

    return payload;
  }

  function unwrap(payload, preferredKey) {
    if (!payload || typeof payload !== "object") return payload;
    if (preferredKey && payload[preferredKey] !== undefined) return payload[preferredKey];
    if (payload.data !== undefined) return payload.data;
    return payload;
  }

  function setBusy(target, busy) {
    if (!target) return;
    const button = target.matches?.("button") ? target : target.querySelector?.('button[type="submit"]');
    target.setAttribute("aria-busy", String(busy));
    if (button) {
      button.setAttribute("aria-busy", String(busy));
      button.disabled = busy;
    }
  }

  function showToast(title, detail = "", type = "success", timeout = 4500) {
    if (!toastRegion) return;
    const toast = document.createElement("div");
    toast.className = `toast toast--${type}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");

    const icon = document.createElement("span");
    icon.className = "toast__icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = type === "error" ? "!" : type === "warning" ? "!" : "✓";

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
    window.setTimeout(() => toast.remove(), timeout);
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function safeEndpointLabel(value) {
    if (!value) return "RTMP-адрес сохранён";
    try {
      const url = new URL(value);
      url.username = "";
      url.password = "";
      const sensitiveNames = /key|token|secret|signature|auth|password/i;
      [...url.searchParams.keys()].forEach((key) => {
        if (sensitiveNames.test(key)) url.searchParams.set(key, "••••••••");
      });
      return url.toString();
    } catch (_error) {
      return "RTMP-адрес сохранён";
    }
  }

  function formatDuration(value) {
    if (value === null || value === undefined || value === "") return "";
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return typeof value === "string" ? value : "";
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    return [hours, minutes, remaining].map((part) => String(part).padStart(2, "0")).join(":");
  }

  function formatBitrate(data) {
    let mbps = null;
    const present = value => value !== null && value !== undefined && value !== "";
    if (present(data?.bitrate_mbps) && Number.isFinite(Number(data.bitrate_mbps))) {
      mbps = Number(data.bitrate_mbps);
    } else if (present(data?.bitrate_bps) && Number.isFinite(Number(data.bitrate_bps))) {
      mbps = Number(data.bitrate_bps) / 1_000_000;
    } else if (present(data?.bitrate_kbps) && Number.isFinite(Number(data.bitrate_kbps))) {
      mbps = Number(data.bitrate_kbps) / 1_000;
    } else if (present(data?.bitrate) && Number.isFinite(Number(data.bitrate))) {
      const value = Number(data.bitrate);
      mbps = value >= 100_000 ? value / 1_000_000 : value >= 1_000 ? value / 1_000 : value;
    }
    if (mbps === null || mbps < 0) return "";
    return `${mbps.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} Мбит/с`;
  }

  function formatUpdatedAt(date = new Date()) {
    return `Обновлено в ${new Intl.DateTimeFormat("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date)}`;
  }

  function normalizeIngestState(value) {
    const state = String(value || "offline").toLowerCase();
    if (["live", "online", "streaming", "publishing", "ready"].includes(state)) return "live";
    if (["connecting", "starting", "probing"].includes(state)) return "connecting";
    if (["unstable", "degraded", "reconnecting"].includes(state)) return "unstable";
    if (["error", "failed", "unhealthy"].includes(state)) return "error";
    return "offline";
  }

  const ingestStateCopy = {
    offline: {
      label: "Сигнал не поступает",
      short: "Нет сигнала",
      description: "Запустите трансляцию в OBS — сигнал появится здесь.",
      tone: "neutral",
    },
    connecting: {
      label: "Подключение",
      short: "Подключение",
      description: "Получаем параметры входящего потока…",
      tone: "warning",
    },
    live: {
      label: "Трансляция поступает",
      short: "Сигнал поступает",
      description: "Входящий поток принимается без ошибок.",
      tone: "success",
    },
    unstable: {
      label: "Нестабильный поток",
      short: "Поток нестабилен",
      description: "Сигнал прерывается. Проверьте соединение с интернетом.",
      tone: "warning",
    },
    error: {
      label: "Ошибка потока",
      short: "Ошибка потока",
      description: "Не удалось принять сигнал. Проверьте настройки OBS.",
      tone: "danger",
    },
  };

  const destinationStateCopy = {
    live: { label: "В эфире", tone: "success" },
    starting: { label: "Подключение", tone: "warning" },
    connecting: { label: "Подключение", tone: "warning" },
    waiting_for_input: { label: "Ожидает сигнал", tone: "warning" },
    reconnecting: { label: "Переподключение", tone: "warning" },
    stopping: { label: "Останавливается", tone: "warning" },
    error: { label: "Ошибка", tone: "danger" },
    failed: { label: "Ошибка", tone: "danger" },
    stopped: { label: "Остановлено", tone: "neutral" },
    idle: { label: "Остановлено", tone: "neutral" },
    disabled: { label: "Выключено", tone: "neutral" },
  };

  function normalizeDestinationState(value) {
    const state = String(value || "stopped").toLowerCase();
    return destinationStateCopy[state] ? state : "stopped";
  }

  function updateIngestConnection(payload) {
    const data = unwrap(payload, "ingest") || {};
    const url = data.rtmp_server_url || data.rtmp_url || data.server_url || "";
    const key = data.stream_key || data.key || "";
    const masked = data.stream_key_masked || "";
    const urlInput = document.querySelector("[data-ingest-url]");
    const keyInput = document.querySelector("[data-ingest-key]");
    if (urlInput && url) {
      urlInput.value = url;
      urlInput.placeholder = "RTMP-сервер недоступен";
    }
    if (keyInput && (key || masked)) {
      keyInput.value = key || masked;
      keyInput.dataset.maskedOnly = key ? "false" : "true";
      keyInput.placeholder = "Ключ недоступен";
    }
  }

  function setMetadataValue(name, value) {
    const row = document.querySelector(`[data-metadata-row="${name}"]`);
    const output = document.querySelector(`[data-metadata="${name}"]`);
    if (!row || !output) return false;
    const present = value !== null && value !== undefined && value !== "";
    row.hidden = !present;
    if (present) output.textContent = value;
    return present;
  }

  function updateIngestStatus(payload) {
    const unwrapped = unwrap(payload);
    const raw =
      unwrapped && typeof unwrapped.status === "object" ? unwrapped.status : unwrapped || {};
    const data = typeof raw === "string" ? { status: raw } : raw;
    const metadata = { ...data, ...(data.metadata || {}) };
    const state = normalizeIngestState(data.status || data.state || data.ingest_status);
    const copy = ingestStateCopy[state];
    const stateBox = document.querySelector("[data-ingest-state]");
    const label = document.querySelector("[data-ingest-label]");
    const description = document.querySelector("[data-ingest-description]");
    const dot = document.querySelector("[data-ingest-dot]");
    const headerLabel = document.querySelector("[data-header-signal-label]");
    const headerDot = document.querySelector("[data-header-signal-dot]");

    if (stateBox) {
      stateBox.dataset.state = state;
      stateBox.dataset.tone = copy.tone;
    }
    if (label) label.textContent = copy.label;
    if (description) description.textContent = copy.description;
    if (headerLabel) headerLabel.textContent = copy.short;

    [dot, headerDot].forEach((item) => {
      if (!item) return;
      item.classList.remove("status-dot--neutral", "status-dot--success", "status-dot--warning", "status-dot--danger");
      item.classList.add(`status-dot--${copy.tone}`);
    });

    const resolution =
      metadata.resolution ||
      (metadata.width && metadata.height ? `${metadata.width} × ${metadata.height}` : "");
    const fpsNumber = Number(metadata.fps ?? metadata.frame_rate);
    const fps = Number.isFinite(fpsNumber) && fpsNumber > 0
      ? `${fpsNumber.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} кадр/с`
      : "";
    const codecs =
      metadata.codecs ||
      [metadata.video_codec, metadata.audio_codec]
        .filter(Boolean)
        .map((codec) => String(codec).toUpperCase())
        .join(" + ");
    const bitrate = formatBitrate(metadata);
    const uptime = formatDuration(metadata.uptime_seconds ?? metadata.uptime ?? metadata.duration_seconds);

    const visibleRows = [
      setMetadataValue("resolution", resolution),
      setMetadataValue("fps", fps),
      setMetadataValue("codecs", codecs),
      setMetadataValue("bitrate", bitrate),
      setMetadataValue("uptime", uptime),
    ].filter(Boolean).length;

    const metadataList = document.querySelector("[data-signal-metadata]");
    const placeholder = document.querySelector("[data-signal-placeholder]");
    if (metadataList) metadataList.hidden = visibleRows === 0;
    if (placeholder) placeholder.hidden = visibleRows > 0;

    const updated = document.querySelector("[data-ingest-updated]");
    if (updated) updated.textContent = formatUpdatedAt();
  }

  function destinationArray(payload) {
    const data = unwrap(payload, "destinations");
    if (Array.isArray(data)) return data;
    if (Array.isArray(data?.items)) return data.items;
    return [];
  }

  function destinationCardMarkup(destination) {
    const id = String(destination.id);
    const encodedId = encodeURIComponent(id);
    const name = String(destination.name || "Без названия");
    const serverUrl = String(destination.server_url || "");
    const enabled = destination.enabled !== false;
    const state = normalizeDestinationState(destination.state);
    const stateCopy = destinationStateCopy[state];
    const isActive = [
      "running",
      "live",
      "starting",
      "connecting",
      "reconnecting",
      "waiting_for_input",
    ].includes(state);
    const uptime = formatDuration(destination.uptime_seconds);
    const bitrate = formatBitrate({
      bitrate_bps: destination.bitrate_bps ?? destination.output_bitrate_bps,
      bitrate_kbps: destination.bitrate_kbps,
      bitrate_mbps: destination.bitrate_mbps,
    });
    const stat = bitrate || (uptime ? `Работает ${uptime}` : "");
    const csrf = escapeHtml(csrfToken);

    return `
      <article
        class="destination-card"
        data-destination-id="${escapeHtml(id)}"
        data-destination-name="${escapeHtml(name)}"
        data-destination-enabled="${enabled}"
        data-destination-has-key="${destination.has_stream_key !== false}"
      >
        <div class="destination-card__identity">
          <span class="destination-avatar" aria-hidden="true">${escapeHtml(name.slice(0, 1).toUpperCase())}</span>
          <div>
            <h3>${escapeHtml(name)}</h3>
            <p title="${escapeHtml(safeEndpointLabel(serverUrl))}">${escapeHtml(safeEndpointLabel(serverUrl))}</p>
          </div>
        </div>

        <div class="destination-card__status">
          <span class="status-pill" data-state="${escapeHtml(state)}">
            <span class="status-dot status-dot--${stateCopy.tone}"></span>
            ${escapeHtml(stateCopy.label)}
          </span>
          ${stat ? `<span class="destination-stat">${escapeHtml(stat)}</span>` : ""}
          ${destination.last_error ? '<span class="destination-hint">Требуется внимание</span>' : ""}
        </div>

        <div class="destination-card__toggle">
          <label class="switch">
            <input type="checkbox" data-destination-toggle aria-label="Включить площадку ${escapeHtml(name)}" ${enabled ? "checked" : ""}>
            <span class="switch__track" aria-hidden="true"><span></span></span>
            <span class="switch__label">Активна</span>
          </label>
        </div>

        <div class="destination-card__actions">
          <form method="post" action="${API_BASE}/destinations/${encodedId}/${isActive ? "stop" : "start"}" data-destination-action="${isActive ? "stop" : "start"}">
            <input type="hidden" name="csrf_token" value="${csrf}">
            <button class="button ${isActive ? "button--danger-soft" : "button--primary"} button--small" type="submit">
              ${isActive ? "Остановить" : "Запустить"}
            </button>
          </form>
          <button class="button button--quiet button--small" type="button" data-edit-destination>Изменить</button>
          <button class="icon-button icon-button--plain icon-button--danger" type="button" data-open-delete aria-label="Удалить площадку ${escapeHtml(name)}" title="Удалить">
            <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
              <path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7m4 4v6m4-6v6"/>
            </svg>
          </button>
        </div>
      </article>`;
  }

  function renderDestinations(destinations) {
    const list = document.querySelector("[data-destination-list]");
    const empty = document.querySelector("[data-destinations-empty]");
    const count = document.querySelector("[data-destination-count]");
    if (!list) return;

    destinationCache.clear();
    destinations.forEach((destination) => destinationCache.set(String(destination.id), destination));
    list.innerHTML = destinations.map(destinationCardMarkup).join("");
    list.hidden = destinations.length === 0;
    if (empty) empty.hidden = destinations.length > 0;
    if (count) count.textContent = String(destinations.length);
  }

  async function loadSession() {
    try {
      const payload = await apiRequest("/auth/session");
      const data = unwrap(payload, "session") || payload || {};
      if (data.csrf_token) setCsrfToken(data.csrf_token);
      if (data.authenticated === false) window.location.assign("/login");
      const userLabel = document.querySelector(".header-user");
      const login = data.login || data.user?.login || data.username;
      if (userLabel && login) userLabel.textContent = login;
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        showToast("Не удалось проверить сессию", friendlyError(error), "error");
      }
    }
  }

  async function loadIngestConnection() {
    try {
      updateIngestConnection(await apiRequest("/ingest"));
    } catch (error) {
      showToast("Не удалось получить настройки OBS", friendlyError(error), "error");
      const urlInput = document.querySelector("[data-ingest-url]");
      if (urlInput && !urlInput.value) urlInput.placeholder = "Не удалось загрузить";
    }
  }

  async function loadIngestStatus({ silent = false } = {}) {
    if (statusRequestPending) return;
    statusRequestPending = true;
    try {
      updateIngestStatus(await apiRequest("/ingest/status"));
    } catch (error) {
      if (!silent) showToast("Не удалось обновить состояние", friendlyError(error), "error");
      const updated = document.querySelector("[data-ingest-updated]");
      if (updated) updated.textContent = "Не удалось обновить состояние";
    } finally {
      statusRequestPending = false;
    }
  }

  async function loadDestinations({ silent = false } = {}) {
    if (destinationRequestPending) return;
    destinationRequestPending = true;
    const list = document.querySelector("[data-destination-list]");
    const loading = document.querySelector("[data-destinations-loading]");
    const errorBox = document.querySelector("[data-destinations-error]");
    const shouldShowLoading = !destinationsLoaded && (!list || list.children.length === 0);
    if (loading) loading.hidden = !shouldShowLoading;
    if (shouldShowLoading && list) list.hidden = true;
    if (errorBox) errorBox.hidden = true;

    try {
      const destinations = destinationArray(await apiRequest("/destinations"));
      renderDestinations(destinations);
      destinationsLoaded = true;
    } catch (error) {
      if (errorBox) errorBox.hidden = false;
      if (!silent) showToast("Не удалось загрузить площадки", friendlyError(error), "error");
    } finally {
      if (loading) loading.hidden = true;
      destinationRequestPending = false;
    }
  }

  function initializeLogin() {
    const form = document.querySelector("[data-login-form]");
    if (!form) return;
    const errorBox = document.querySelector("[data-login-error]");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      if (errorBox) {
        errorBox.hidden = true;
        errorBox.textContent = "";
      }
      form.querySelectorAll("input").forEach((input) => input.removeAttribute("aria-invalid"));
      setBusy(form, true);

      try {
        const payload = await apiRequest("/auth/login", {
          method: "POST",
          body: JSON.stringify({
            login: form.elements.login.value.trim(),
            password: form.elements.password.value,
          }),
          allowUnauthorized: true,
        });
        if (payload?.csrf_token) setCsrfToken(payload.csrf_token);
        const nextUrl = payload?.next_url || payload?.redirect_to || payload?.redirect || "/";
        window.location.assign(nextUrl);
      } catch (error) {
        const message = friendlyError(error, "Не удалось войти. Попробуйте ещё раз.");
        if (errorBox) {
          errorBox.textContent = message;
          errorBox.hidden = false;
        }
        form.elements.login.setAttribute("aria-invalid", "true");
        form.elements.password.setAttribute("aria-invalid", "true");
        form.elements.password.select();
      } finally {
        setBusy(form, false);
      }
    });
  }

  function resetDestinationForm(mode = "create", destination = null) {
    const form = document.querySelector("[data-destination-form]");
    if (!form) return;
    form.reset();
    const isEdit = mode === "edit" && destination;
    form.elements.destination_id.value = isEdit ? destination.id : "";
    form.elements.name.value = isEdit ? destination.name || "" : "";
    form.elements.server_url.value = isEdit ? destination.server_url || "" : "";
    form.elements.stream_key.value = "";
    form.elements.stream_key.required = !isEdit;
    form.elements.stream_key.placeholder = isEdit ? "Оставьте пустым, чтобы не менять" : "Вставьте ключ площадки";
    form.elements.enabled.checked = isEdit ? destination.enabled !== false : true;

    const title = form.querySelector("[data-destination-form-title]");
    const submitLabel = form.querySelector("[data-destination-submit-label]");
    const keyHelp = form.querySelector("[data-key-help]");
    const enabledTitle = form.querySelector("[data-enabled-title]");
    const enabledHelp = form.querySelector("[data-enabled-help]");
    const errorBox = form.querySelector("[data-destination-form-error]");
    if (title) title.textContent = isEdit ? "Изменить площадку" : "Новая площадка";
    if (submitLabel) submitLabel.textContent = isEdit ? "Сохранить" : "Добавить";
    if (keyHelp) {
      keyHelp.textContent = isEdit
        ? "Оставьте поле пустым, если ключ менять не нужно."
        : "Ключ будет зашифрован и больше не отобразится целиком.";
    }
    if (enabledTitle) enabledTitle.textContent = isEdit ? "Площадка включена" : "Запустить после сохранения";
    if (enabledHelp) {
      enabledHelp.textContent = isEdit
        ? "Выключение остановит передачу на эту площадку."
        : "Площадка начнёт ожидать входящий сигнал.";
    }
    if (errorBox) {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }
  }

  function openDestinationEditor(destination = null) {
    resetDestinationForm(destination ? "edit" : "create", destination);
    const dialog = document.querySelector("[data-destination-dialog]");
    openDialog(dialog);
    window.setTimeout(() => dialog?.querySelector('input[name="name"]')?.focus(), 0);
  }

  async function submitDestinationForm(form) {
    if (!form.reportValidity()) return;
    await sessionReady;
    const id = form.elements.destination_id.value;
    const streamKey = form.elements.stream_key.value.trim();
    const payload = {
      name: form.elements.name.value.trim(),
      server_url: form.elements.server_url.value.trim(),
      enabled: form.elements.enabled.checked,
    };
    if (streamKey) payload.stream_key = streamKey;

    const errorBox = form.querySelector("[data-destination-form-error]");
    if (errorBox) {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }
    setBusy(form, true);
    try {
      await apiRequest(id ? `/destinations/${encodeURIComponent(id)}` : "/destinations", {
        method: id ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
      closeDialog(form.closest("dialog"));
      showToast(id ? "Изменения сохранены" : "Площадка добавлена");
      await loadDestinations({ silent: true });
    } catch (error) {
      const message = friendlyError(error, "Не удалось сохранить площадку.");
      if (errorBox) {
        errorBox.textContent = message;
        errorBox.hidden = false;
      }
    } finally {
      setBusy(form, false);
    }
  }

  async function runDestinationAction(card, action, form) {
    const id = card?.dataset.destinationId;
    if (!id) return;
    await sessionReady;
    card.dataset.busy = "true";
    setBusy(form, true);
    try {
      await apiRequest(`/destinations/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      showToast(action === "start" ? "Площадка запускается" : "Площадка остановлена");
      await loadDestinations({ silent: true });
      await loadIngestStatus({ silent: true });
    } catch (error) {
      showToast(
        action === "start" ? "Не удалось запустить площадку" : "Не удалось остановить площадку",
        friendlyError(error),
        "error",
      );
    } finally {
      delete card.dataset.busy;
      setBusy(form, false);
    }
  }

  async function toggleDestination(card, checkbox) {
    const id = card?.dataset.destinationId;
    const destination = destinationCache.get(String(id));
    if (!id || !destination) {
      checkbox.checked = !checkbox.checked;
      showToast("Не удалось изменить площадку", "Обновите страницу и повторите действие.", "error");
      return;
    }
    await sessionReady;
    checkbox.disabled = true;
    try {
      await apiRequest(`/destinations/${encodeURIComponent(id)}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: checkbox.checked }),
      });
      showToast(checkbox.checked ? "Площадка включена" : "Площадка выключена");
      await loadDestinations({ silent: true });
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      showToast("Не удалось изменить площадку", friendlyError(error), "error");
    } finally {
      checkbox.disabled = false;
    }
  }

  function initializeDashboard() {
    const dashboard = document.querySelector("[data-dashboard]");
    if (!dashboard) return;

    sessionReady = loadSession();
    void loadIngestConnection();
    void loadIngestStatus();
    void loadDestinations();

    document.querySelectorAll("[data-open-destination]").forEach((button) => {
      button.addEventListener("click", () => openDestinationEditor());
    });

    document.querySelector("[data-refresh-dashboard]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      await Promise.all([loadIngestConnection(), loadIngestStatus(), loadDestinations({ silent: true })]);
      button.disabled = false;
    });

    document.querySelector("[data-retry-destinations]")?.addEventListener("click", () => {
      void loadDestinations();
    });

    document.querySelector("[data-open-rotate]")?.addEventListener("click", () => {
      openDialog(document.querySelector("[data-rotate-dialog]"));
    });

    document.querySelector("[data-rotate-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      await sessionReady;
      setBusy(form, true);
      try {
        const payload = await apiRequest("/ingest/rotate", { method: "POST" });
        updateIngestConnection(payload);
        closeDialog(form.closest("dialog"));
        showToast("Ключ трансляции сменён", "Скопируйте новый ключ в OBS.", "warning", 7000);
      } catch (error) {
        showToast("Не удалось сменить ключ", friendlyError(error), "error");
      } finally {
        setBusy(form, false);
      }
    });

    const destinationForm = document.querySelector("[data-destination-form]");
    destinationForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      void submitDestinationForm(event.currentTarget);
    });

    const list = document.querySelector("[data-destination-list]");
    list?.addEventListener("submit", (event) => {
      const form = event.target.closest("[data-destination-action]");
      if (!form) return;
      event.preventDefault();
      const card = form.closest("[data-destination-id]");
      void runDestinationAction(card, form.dataset.destinationAction, form);
    });

    list?.addEventListener("click", (event) => {
      const card = event.target.closest("[data-destination-id]");
      if (!card) return;
      const id = String(card.dataset.destinationId);
      if (event.target.closest("[data-edit-destination]")) {
        const cached = destinationCache.get(id) || {
          id,
          name: card.dataset.destinationName,
          server_url: card.dataset.destinationUrl || "",
          enabled: card.dataset.destinationEnabled !== "false",
          has_stream_key: card.dataset.destinationHasKey !== "false",
        };
        openDestinationEditor(cached);
      }
      if (event.target.closest("[data-open-delete]")) {
        const dialog = document.querySelector("[data-delete-dialog]");
        const form = dialog?.querySelector("[data-delete-form]");
        if (form) form.elements.destination_id.value = id;
        const name = dialog?.querySelector("[data-delete-name]");
        if (name) name.textContent = card.dataset.destinationName || "эту площадку";
        openDialog(dialog);
      }
    });

    list?.addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-destination-toggle]");
      if (!checkbox) return;
      void toggleDestination(checkbox.closest("[data-destination-id]"), checkbox);
    });

    document.querySelector("[data-delete-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const id = form.elements.destination_id.value;
      if (!id) return;
      await sessionReady;
      setBusy(form, true);
      try {
        await apiRequest(`/destinations/${encodeURIComponent(id)}`, { method: "DELETE" });
        closeDialog(form.closest("dialog"));
        showToast("Площадка удалена");
        await loadDestinations({ silent: true });
      } catch (error) {
        showToast("Не удалось удалить площадку", friendlyError(error), "error");
      } finally {
        setBusy(form, false);
      }
    });

    window.setInterval(() => {
      if (document.visibilityState === "visible") void loadIngestStatus({ silent: true });
    }, 5000);

    window.setInterval(() => {
      const listHasFocus = document.querySelector("[data-destination-list]:focus-within");
      const modalOpen = document.querySelector("dialog[open]");
      if (document.visibilityState === "visible" && !listHasFocus && !modalOpen) {
        void loadDestinations({ silent: true });
      }
    }, 8000);
  }

  function initializeGlobalControls() {
    document.addEventListener("click", async (event) => {
      const toggle = event.target.closest("[data-password-toggle]");
      if (toggle) {
        const input = document.querySelector(toggle.dataset.passwordToggle);
        if (input) {
          const reveal = input.type === "password";
          input.type = reveal ? "text" : "password";
          toggle.setAttribute("aria-pressed", String(reveal));
          toggle.setAttribute("aria-label", reveal ? "Скрыть значение" : "Показать значение");
          const label = toggle.querySelector("[data-toggle-label]");
          if (label) label.textContent = reveal ? "Скрыть" : "Показать";
        }
        return;
      }

      const copyButton = event.target.closest("[data-copy-from]");
      if (copyButton) {
        const source = document.querySelector(copyButton.dataset.copyFrom);
        const value = source?.value || source?.textContent || "";
        if (!value || source?.dataset.maskedOnly === "true") {
          showToast("Нечего копировать", "Обновите страницу и попробуйте снова.", "error");
          return;
        }
        try {
          if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
          } else {
            const helper = document.createElement("textarea");
            helper.value = value;
            helper.style.position = "fixed";
            helper.style.opacity = "0";
            document.body.append(helper);
            helper.select();
            const copied = document.execCommand("copy");
            helper.remove();
            if (!copied) throw new Error("copy failed");
          }
          showToast("Скопировано");
        } catch (_error) {
          showToast("Не удалось скопировать", "Выделите значение и скопируйте вручную.", "error");
        }
        return;
      }

      const closeButton = event.target.closest("[data-close-dialog]");
      if (closeButton) closeDialog(closeButton.closest("dialog"));
    });

    document.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) closeDialog(dialog);
      });
    });

    document.querySelector("[data-logout-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      await sessionReady;
      setBusy(form, true);
      try {
        await apiRequest("/auth/logout", { method: "POST" });
        window.location.assign("/login");
      } catch (error) {
        showToast("Не удалось выйти", friendlyError(error), "error");
        setBusy(form, false);
      }
    });
  }

  initializeGlobalControls();
  initializeLogin();
  initializeDashboard();
})();
