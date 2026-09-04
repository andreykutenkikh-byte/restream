(function moblinHudAdminModule(globalScope) {
  "use strict";

  function formatLastSeen(value, now = Date.now()) {
    if (!value) return "Ещё не открывался";
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "Нет данных";
    const age = Math.max(0, Math.floor((now - timestamp) / 1000));
    if (age < 10) return "Только что";
    if (age < 60) return `${age} сек. назад`;
    if (age < 3600) return `${Math.floor(age / 60)} мин. назад`;
    return `${Math.floor(age / 3600)} ч. назад`;
  }

  function pairingPayloadIsSafe(payload) {
    if (!payload || typeof payload !== "object") return false;
    if (!String(payload.pairing_url || "").includes("/moblin-hud#pair=")) return false;
    if (String(payload.pairing_url).includes("?pair=")) return false;
    return String(payload.moblin_url || "").startsWith("moblin://?");
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { formatLastSeen, pairingPayloadIsSafe };
  }

  if (!globalScope?.document) return;
  const document = globalScope.document;
  const root = document.querySelector("[data-moblin-hud-admin]");
  if (!root) return;

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const createButton = root.querySelector("[data-hud-create-pairing]");
  const pairingBox = root.querySelector("[data-hud-pairing]");
  const pairingInput = root.querySelector("[data-hud-pairing-url]");
  const moblinLink = root.querySelector("[data-hud-moblin-link]");
  const copyButton = root.querySelector("[data-hud-copy-pairing]");
  const expiry = root.querySelector("[data-hud-pairing-expiry]");
  const error = root.querySelector("[data-hud-admin-error]");
  const list = root.querySelector("[data-hud-device-list]");
  const empty = root.querySelector("[data-hud-devices-empty]");
  let clearPairingTimer = null;

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
  }

  function clearError() {
    error.textContent = "";
    error.hidden = true;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (csrf && options.method === "POST") headers.set("X-CSRF-Token", csrf);
    const response = await fetch(path, { credentials: "same-origin", ...options, headers });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(payload?.error?.message || "Запрос не выполнен");
    }
    return payload;
  }

  function clearPairing() {
    if (clearPairingTimer !== null) globalScope.clearTimeout(clearPairingTimer);
    clearPairingTimer = null;
    pairingInput.value = "";
    moblinLink.removeAttribute("href");
    expiry.textContent = "";
    pairingBox.hidden = true;
  }

  function renderDevice(item) {
    const row = document.createElement("li");
    row.className = "moblin-hud-device";

    const copy = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = String(item.display_name || "Moblin HUD");
    const state = document.createElement("span");
    state.className = "moblin-hud-device__state";
    state.dataset.state = String(item.status || "expired");
    state.textContent = item.status === "active" ? "● Подключён" : item.status === "revoked" ? "Доступ отозван" : "Срок истёк";
    const seen = document.createElement("small");
    seen.textContent = `Последний раз: ${formatLastSeen(item.last_seen_at)}`;
    copy.append(name, state, seen);
    row.append(copy);

    if (item.status === "active") {
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "button button--danger-soft button--small";
      revoke.textContent = "Отозвать доступ";
      revoke.dataset.hudRevoke = String(item.id || "");
      row.append(revoke);
    }
    return row;
  }

  async function loadDevices() {
    const payload = await api("/api/moblin-hud/devices");
    const items = Array.isArray(payload?.items) ? payload.items : [];
    list.replaceChildren(...items.map(renderDevice));
    empty.hidden = items.length > 0;
  }

  createButton?.addEventListener("click", async () => {
    clearError();
    createButton.disabled = true;
    try {
      const payload = await api("/api/moblin-hud/pairings", {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (!pairingPayloadIsSafe(payload)) throw new Error("Сервер вернул некорректную ссылку");
      clearPairing();
      pairingInput.value = String(payload.pairing_url);
      moblinLink.href = String(payload.moblin_url);
      expiry.textContent = `Действует до ${new Date(payload.expires_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
      pairingBox.hidden = false;
      clearPairingTimer = globalScope.setTimeout(clearPairing, 10 * 60 * 1000);
    } catch (requestError) {
      showError(requestError instanceof Error ? requestError.message : "Не удалось создать ссылку");
    } finally {
      createButton.disabled = false;
    }
  });

  copyButton?.addEventListener("click", async () => {
    const value = pairingInput.value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      copyButton.textContent = "Скопировано";
      globalScope.setTimeout(() => { copyButton.textContent = "Скопировать ссылку"; }, 1500);
    } catch (_error) {
      pairingInput.focus();
      pairingInput.select();
      showError("Не удалось скопировать автоматически. Скопируйте выделенную ссылку.");
    }
  });

  list?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-hud-revoke]");
    const deviceId = button?.dataset.hudRevoke;
    if (!deviceId) return;
    clearError();
    button.disabled = true;
    try {
      await api(`/api/moblin-hud/devices/${encodeURIComponent(deviceId)}/revoke`, {
        method: "POST",
        body: "{}",
      });
      await loadDevices();
    } catch (requestError) {
      showError(requestError instanceof Error ? requestError.message : "Не удалось отозвать доступ");
      button.disabled = false;
    }
  });

  loadDevices().catch(() => showError("Не удалось загрузить HUD-устройства"));
})(typeof window !== "undefined" ? window : globalThis);
