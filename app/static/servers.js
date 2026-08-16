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

  const exported = {
    MAX_POLL_ATTEMPTS,
    MAX_COMMAND_POLL_ATTEMPTS,
    MAX_TRANSIENT_POLL_FAILURES,
    POLL_INTERVAL_MS,
    TERMINAL_JOB_STATES,
    buildBootstrapPayload,
    createPollBudget,
    normalizeNodeStatus,
    safeDisplayString,
    transientPollDelay,
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
      if (response.status === 401) window.location.assign("/login");
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
    const availability = appendText(heading, "span", "server-card__scope", "Подготовлен, но ещё не участвует в эфире");
    availability.setAttribute("title", "Передача видеопотока будет добавлена на следующем этапе");
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

    const actions = document.createElement("div");
    actions.className = "server-card__actions";
    const selfTest = appendText(actions, "button", "button button--secondary button--small", "Проверить сервер");
    selfTest.type = "button";
    selfTest.disabled = !["ready", "degraded", "offline"].includes(status);
    selfTest.addEventListener("click", () => void requestSelfTest(node.id, selfTest));
    const rename = appendText(actions, "button", "button button--quiet button--small", "Переименовать");
    rename.type = "button";
    rename.addEventListener("click", () => void renameNode(node));
    const revoke = appendText(actions, "button", "button button--danger-soft button--small", "Отозвать доступ");
    revoke.type = "button";
    revoke.disabled = !["ready", "degraded", "offline"].includes(status);
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
      serverList?.replaceChildren(...items.map(renderNodeCard));
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
      dialog?.querySelectorAll('input[type="password"]').forEach((input) => {
        input.value = "";
      });
      closeDialog(dialog);
    }
  });

  document.querySelector("[data-retry-servers]")?.addEventListener("click", () => void loadServers());
  window.addEventListener("pagehide", () => {
    stopPolling();
    document.querySelectorAll('input[type="password"]').forEach((input) => {
      input.value = "";
    });
  });

  if (page.dataset.bootstrapAvailable !== "true") bootstrapUnavailable.hidden = false;
  void loadServers();
  void resumeActiveBootstrap();
})(typeof globalThis !== "undefined" ? globalThis : this);
