((root, factory) => {
  "use strict";

  const exports = factory(root || globalThis);
  if (typeof module === "object" && module.exports) module.exports = exports;
  if (root?.document) exports.initializeHud(root);
})(typeof window === "undefined" ? globalThis : window, (globalScope) => {
  "use strict";

  const PAIR_TOKEN_PATTERN = /^[A-Za-z0-9_-]{40,128}$/;
  const REASON_CODE_PATTERN = /^[a-zA-Z0-9_.-]{1,80}$/;
  const ROUTE_ID_PATTERN = /^[a-zA-Z0-9_-]{1,128}$/;
  const IPV4_PATTERN = /(?:^|[^\d.])(?:\d{1,3}\.){3}\d{1,3}(?:$|[^\d.])/;
  const IPV6_PATTERN = /(?:^|[^a-f\d:])(?:[a-f\d]{0,4}:){2,}[a-f\d]{0,4}(?:$|[^a-f\d:])/i;
  const LEVELS = Object.freeze(["unknown", "green", "yellow", "red", "black"]);
  const STREAM_STATES = Object.freeze(["idle", "active", "ambiguous", "unknown"]);
  const CONFIDENCE = Object.freeze(["active_path_measured", "standby_server_readiness"]);
  const ACTIONS = Object.freeze(["stay", "watch", "switch_recommended", "reconnect", "unavailable"]);
  const TRENDS = Object.freeze(["unknown", "rising", "stable", "falling"]);
  const ALERT_COOLDOWN_MS = 45_000;
  const MUTE_DURATION_MS = 60_000;
  const REQUEST_TIMEOUT_MS = 7_000;
  const HUD_INSTANCE_KEY = Symbol.for("adojapan.moblinHud.instance");

  function safeText(value, fallback = "—", maxLength = 240) {
    if (typeof value !== "string") return fallback;
    const normalized = value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
    return normalized ? normalized.slice(0, maxLength) : fallback;
  }

  function safeDisplayName(value, fallback = "Сервер не определён") {
    const name = safeText(value, fallback, 128);
    if (IPV4_PATTERN.test(name) || IPV6_PATTERN.test(name) || name.includes("://")) return fallback;
    return name;
  }

  function finiteNumber(value, maximum = Number.MAX_SAFE_INTEGER) {
    return typeof value === "number"
      && Number.isFinite(value)
      && value >= 0
      && value <= maximum
      ? value
      : null;
  }

  function allowedToken(value, values, fallback) {
    return typeof value === "string" && values.includes(value) ? value : fallback;
  }

  function parsePairToken(hash) {
    if (typeof hash !== "string" || !hash.startsWith("#pair=")) return null;
    const token = hash.slice(6);
    return PAIR_TOKEN_PATTERN.test(token) ? token : null;
  }

  function formatBitrate(value) {
    const bitrate = finiteNumber(value, 1_000_000_000);
    if (bitrate === null) return "—";
    if (bitrate >= 1_000_000) return `${(bitrate / 1_000_000).toFixed(1).replace(".0", "")} Мбит/с`;
    if (bitrate >= 1000) return `${Math.round(bitrate / 1000)} Кбит/с`;
    return `${Math.round(bitrate)} бит/с`;
  }

  function formatTrend(value) {
    return {
      rising: "Битрейт растёт",
      stable: "Битрейт стабилен",
      falling: "Битрейт снижается",
      unknown: "Динамика уточняется",
    }[allowedToken(value, TRENDS, "unknown")];
  }

  function formatSource(value) {
    const source = safeText(value, "UNKNOWN", 16).toUpperCase();
    return source === "LIVE" ? "Moblin LIVE" : source === "SLATE" ? "Заставка" : source === "NONE" ? "Нет потока" : "Неизвестно";
  }

  function formatYouTube(value) {
    const state = safeText(value, "unknown", 32).toLowerCase();
    return {
      active: "Передаётся",
      forwarding: "Передаётся",
      live: "Передаётся",
      running: "Передаётся",
      connecting: "Подключение",
      starting: "Подключение",
      pending: "Подключение",
      inactive: "Остановлен",
      failed: "Ошибка",
      error: "Ошибка",
    }[state] || "Неизвестно";
  }

  function formatHeartbeat(value) {
    const seconds = finiteNumber(value, 86_400);
    if (seconds === null) return "Нет данных";
    if (seconds < 2) return "Сейчас";
    if (seconds < 60) return `${Math.round(seconds)} с назад`;
    return `${Math.round(seconds / 60)} мин назад`;
  }

  function formatPercent(value) {
    const percent = finiteNumber(value, 100);
    return percent === null ? "—" : `${Math.round(percent)}%`;
  }

  function formatMemory(available, total) {
    const safeAvailable = finiteNumber(available, Number.MAX_SAFE_INTEGER);
    const safeTotal = finiteNumber(total, Number.MAX_SAFE_INTEGER);
    if (safeAvailable === null || safeTotal === null || safeTotal <= 0 || safeAvailable > safeTotal) return "—";
    const gib = safeAvailable / (1024 ** 3);
    return `${gib.toFixed(1)} ГБ (${Math.round((safeAvailable / safeTotal) * 100)}%)`;
  }

  function normalizeRoute(value) {
    if (!value || typeof value !== "object") return null;
    const routeId = typeof value.route_id === "string" && ROUTE_ID_PATTERN.test(value.route_id)
      ? value.route_id
      : "unknown";
    return {
      routeId,
      displayName: safeDisplayName(value.display_name),
      kind: value.kind === "main" || value.kind === "relay" ? value.kind : "relay",
      source: safeText(value.source, "UNKNOWN", 16),
      inputBitrateBps: finiteNumber(value.input_bitrate_bps, 1_000_000_000),
      emaInputBitrateBps: finiteNumber(value.ema_input_bitrate_bps, 1_000_000_000),
      stableBaselineBps: finiteNumber(value.stable_baseline_bps, 1_000_000_000),
      bitrateTrend: allowedToken(value.bitrate_trend, TRENDS, "unknown"),
      youtubeForwardState: safeText(value.youtube_forward_state, "unknown", 32).toLowerCase(),
      overallState: safeText(value.overall_state, "unknown", 32).toLowerCase(),
      heartbeatAgeSeconds: finiteNumber(value.heartbeat_age_seconds, 86_400),
      hostCpuPercent: finiteNumber(value.host_cpu_percent, 100),
      hostMemoryAvailableBytes: finiteNumber(value.host_memory_available_bytes),
      hostMemoryTotalBytes: finiteNumber(value.host_memory_total_bytes),
    };
  }

  function normalizeStatus(payload) {
    if (!payload || typeof payload !== "object" || payload.scope !== "stream_monitor") {
      throw new TypeError("Invalid HUD status scope");
    }
    const health = payload.health && typeof payload.health === "object" ? payload.health : {};
    const recommendation = payload.recommendation && typeof payload.recommendation === "object"
      ? payload.recommendation
      : {};
    const reasonCodes = Array.isArray(health.reason_codes)
      ? health.reason_codes.filter((value) => typeof value === "string" && REASON_CODE_PATTERN.test(value)).slice(0, 12)
      : [];
    const recommendationCode = typeof recommendation.reason_code === "string"
      && REASON_CODE_PATTERN.test(recommendation.reason_code)
      ? recommendation.reason_code
      : "unknown";
    const generatedAt = typeof payload.generated_at === "string" && Number.isFinite(Date.parse(payload.generated_at))
      ? payload.generated_at
      : null;
    return {
      streamState: allowedToken(payload.stream_state, STREAM_STATES, "idle"),
      generatedAt,
      health: {
        level: allowedToken(health.level, LEVELS, "unknown"),
        title: safeText(health.title, "Состояние уточняется", 120),
        message: safeText(health.message, "Недостаточно данных для оценки.", 280),
        reasonCodes,
        confidence: allowedToken(health.confidence, CONFIDENCE, "active_path_measured"),
      },
      currentRoute: normalizeRoute(payload.current_route),
      standbyRoute: normalizeRoute(payload.standby_route),
      recommendation: {
        action: allowedToken(recommendation.action, ACTIONS, "watch"),
        targetDisplayName: recommendation.target_display_name === null
          ? null
          : safeDisplayName(recommendation.target_display_name, "Резервный сервер"),
        confidence: allowedToken(recommendation.confidence, CONFIDENCE, "active_path_measured"),
        reason: safeText(recommendation.reason, "Продолжается наблюдение.", 320),
        reasonCode: recommendationCode,
        routeToTargetMeasured: false,
      },
    };
  }

  function recommendationLabel(recommendation) {
    if (recommendation.action === "switch_recommended") {
      const target = recommendation.targetDisplayName || "резервный сервер";
      return `Переключите Moblin вручную: ${target}`;
    }
    if (recommendation.action === "reconnect") return "Переподключите поток в Moblin";
    if (recommendation.action === "stay") return "Оставайтесь на текущем сервере";
    if (recommendation.action === "unavailable") return "Нет активного маршрута";
    return "Наблюдайте за текущим потоком";
  }

  function confidenceLabel(value) {
    return value === "standby_server_readiness"
      ? "Измерена только готовность резервного сервера"
      : "Измерен активный входящий путь";
  }

  function formatGeneratedAt(value, now = Date.now()) {
    if (typeof value !== "string") return "Время неизвестно";
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "Время неизвестно";
    const age = Math.max(0, Math.round((now - timestamp) / 1000));
    return age < 2 ? "Обновлено сейчас" : `Обновлено ${age} с назад`;
  }

  function nextPollDelay({ hidden = false, status = null, errorCount = 0 } = {}) {
    if (hidden) return 10_000;
    if (errorCount > 0) return Math.min(15_000, 1000 * (2 ** Math.min(errorCount, 4)));
    const level = status?.health?.level;
    if (status?.streamState === "active" || ["yellow", "red", "black"].includes(level)) return 2000;
    return 5000;
  }

  function shouldSoundTransition(previousLevel, nextLevel, now, lastAlertAt = null, cooldownMs = ALERT_COOLDOWN_MS, lastAlertLevel = null) {
    const severity = { green: 0, yellow: 1, red: 2, black: 3 };
    if (!(previousLevel in severity) || !(nextLevel in severity)) return false;
    if (severity[nextLevel] <= severity[previousLevel]) return false;
    return lastAlertAt === null
      || now - lastAlertAt >= cooldownMs
      || (lastAlertLevel in severity && severity[nextLevel] > severity[lastAlertLevel]);
  }

  class HudPoller {
    constructor({
      fetchFn,
      isHidden = () => false,
      onStatus = () => {},
      onMonitoringOffline = () => {},
      onRevoked = () => {},
      setTimeoutFn = (callback, delay) => globalScope.setTimeout(callback, delay),
      clearTimeoutFn = (timer) => globalScope.clearTimeout(timer),
      abortControllerClass = globalScope.AbortController,
      requestTimeoutMs = REQUEST_TIMEOUT_MS,
    }) {
      if (typeof fetchFn !== "function" || typeof abortControllerClass !== "function") {
        throw new TypeError("fetchFn and AbortController are required");
      }
      this.fetchFn = fetchFn;
      this.isHidden = isHidden;
      this.onStatus = onStatus;
      this.onMonitoringOffline = onMonitoringOffline;
      this.onRevoked = onRevoked;
      this.setTimeoutFn = setTimeoutFn;
      this.clearTimeoutFn = clearTimeoutFn;
      this.AbortControllerClass = abortControllerClass;
      this.requestTimeoutMs = requestTimeoutMs;
      this.running = false;
      this.generation = 0;
      this.timer = null;
      this.controller = null;
      this.pendingPoll = null;
      this.errorCount = 0;
      this.lastStatus = null;
    }

    start() {
      if (this.running) return;
      this.running = true;
      this.errorCount = 0;
      this.generation += 1;
      this.schedule(0, this.generation);
    }

    restart(delay = 0) {
      if (!this.running) {
        this.start();
        return;
      }
      this.generation += 1;
      this.cancelPending();
      this.schedule(Math.max(0, delay), this.generation);
    }

    stop() {
      this.running = false;
      this.generation += 1;
      this.cancelPending();
    }

    cancelPending() {
      if (this.timer !== null) {
        this.clearTimeoutFn(this.timer);
        this.timer = null;
      }
      if (this.controller !== null) {
        this.controller.abort();
      }
      this.pendingPoll = null;
    }

    schedule(delay, generation) {
      if (!this.running || generation !== this.generation) return;
      // An aborted fetch still owns its slot until its promise/body settles.
      // Visibility/page lifecycle events may replace only this pending schedule.
      if (this.controller !== null) {
        this.pendingPoll = { delay, generation };
        return;
      }
      if (this.timer !== null) this.clearTimeoutFn(this.timer);
      this.timer = this.setTimeoutFn(() => {
        this.timer = null;
        void this.poll(generation);
      }, delay);
    }

    async poll(generation = this.generation) {
      if (!this.running || generation !== this.generation || this.controller !== null) return;
      const controller = new this.AbortControllerClass();
      this.controller = controller;
      const timeout = this.setTimeoutFn(() => controller.abort(), this.requestTimeoutMs);
      try {
        const response = await this.fetchFn("/moblin-hud/api/status", {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!this.running || generation !== this.generation || this.controller !== controller) return;
        if (response.status === 401) {
          this.onRevoked();
          this.stop();
          return;
        }
        if (!response.ok) throw new Error("status_request_failed");
        const status = normalizeStatus(await response.json());
        if (!this.running || generation !== this.generation || this.controller !== controller) return;
        this.errorCount = 0;
        this.lastStatus = status;
        this.onStatus(status);
        this.schedule(nextPollDelay({ hidden: this.isHidden(), status }), generation);
      } catch (error) {
        if (!this.running || generation !== this.generation || this.controller !== controller) return;
        this.errorCount += 1;
        this.onMonitoringOffline(this.errorCount, error);
        this.schedule(nextPollDelay({ hidden: this.isHidden(), errorCount: this.errorCount }), generation);
      } finally {
        this.clearTimeoutFn(timeout);
        if (this.controller === controller) this.controller = null;
        const pending = this.pendingPoll;
        this.pendingPoll = null;
        if (pending) this.schedule(pending.delay, pending.generation);
      }
    }
  }

  class AlertAudio {
    constructor({
      audioContextClass = globalScope.AudioContext || globalScope.webkitAudioContext,
      now = () => Date.now(),
      onStateChange = () => {},
    } = {}) {
      this.AudioContextClass = audioContextClass;
      this.now = now;
      this.onStateChange = onStateChange;
      this.context = null;
      this.enabled = false;
      this.mutedUntil = 0;
      this.lastAlertAt = null;
      this.lastAlertLevel = null;
    }

    async toggle() {
      if (this.enabled) {
        this.enabled = false;
        this.onStateChange(this);
        return false;
      }
      if (typeof this.AudioContextClass !== "function") return false;
      if (!this.context) this.context = new this.AudioContextClass();
      await this.context.resume?.();
      this.enabled = true;
      this.play("test");
      this.onStateChange(this);
      return true;
    }

    mute() {
      this.mutedUntil = this.now() + MUTE_DURATION_MS;
      this.onStateChange(this);
    }

    notify(previousLevel, nextLevel) {
      const now = this.now();
      if (!this.enabled || now < this.mutedUntil) return false;
      if (!shouldSoundTransition(previousLevel, nextLevel, now, this.lastAlertAt, ALERT_COOLDOWN_MS, this.lastAlertLevel)) return false;
      this.lastAlertAt = now;
      this.lastAlertLevel = nextLevel;
      this.play(nextLevel);
      return true;
    }

    play(kind) {
      if (!this.context || !this.enabled) return false;
      const frequency = kind === "black" ? 220 : kind === "red" ? 330 : kind === "yellow" ? 520 : 660;
      const count = kind === "black" ? 3 : kind === "red" ? 2 : 1;
      for (let index = 0; index < count; index += 1) {
        const oscillator = this.context.createOscillator();
        const gain = this.context.createGain();
        const start = this.context.currentTime + (index * 0.29);
        oscillator.type = "sine";
        oscillator.frequency.setValueAtTime(frequency, start);
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.12, start + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.22);
        oscillator.connect(gain);
        gain.connect(this.context.destination);
        oscillator.start(start);
        oscillator.stop(start + 0.24);
      }
      return true;
    }

    destroy() {
      this.enabled = false;
      void this.context?.close?.();
      this.context = null;
    }
  }

  async function pairFromFragment({ location, history, fetchFn, alreadyPaired = false }) {
    const hash = typeof location?.hash === "string" ? location.hash : "";
    let token = parsePairToken(hash);
    if (!hash) return { attempted: false, paired: false };
    const cleanUrl = `${location.pathname || "/moblin-hud"}${location.search || ""}`;
    history.replaceState(null, "", cleanUrl);
    if (alreadyPaired) return { attempted: false, paired: true };
    if (token === null) return { attempted: true, paired: false };
    const body = JSON.stringify({ token });
    token = null;
    const response = await fetchFn("/moblin-hud/api/pair", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body,
    });
    return { attempted: true, paired: response.ok };
  }

  function select(documentObject, name) {
    return documentObject.querySelector(`[data-hud-${name}]`);
  }

  function setText(element, value) {
    if (element) element.textContent = String(value);
  }

  function createRenderer(documentObject) {
    const elements = {
      server: select(documentObject, "server"),
      title: select(documentObject, "title"),
      message: select(documentObject, "message"),
      bitrate: select(documentObject, "bitrate"),
      trend: select(documentObject, "trend"),
      source: select(documentObject, "source"),
      youtube: select(documentObject, "youtube"),
      heartbeat: select(documentObject, "heartbeat"),
      standby: select(documentObject, "standby"),
      recommendation: select(documentObject, "recommendation"),
      reason: select(documentObject, "reason"),
      updated: select(documentObject, "updated"),
      cpu: select(documentObject, "cpu"),
      memory: select(documentObject, "memory"),
      rawState: select(documentObject, "raw-state"),
      confidence: select(documentObject, "confidence"),
      reasonCodes: select(documentObject, "reason-codes"),
      serverTime: select(documentObject, "server-time"),
    };

    return {
      render(status) {
        const route = status.currentRoute;
        const standby = status.standbyRoute;
        documentObject.body.dataset.hudState = status.health.level;
        setText(elements.server, route?.displayName || "Активный сервер не определён");
        setText(elements.title, status.health.title);
        setText(elements.message, status.health.message);
        setText(elements.bitrate, formatBitrate(route?.inputBitrateBps));
        setText(elements.trend, formatTrend(route?.bitrateTrend));
        setText(elements.source, formatSource(route?.source));
        setText(elements.youtube, formatYouTube(route?.youtubeForwardState));
        setText(elements.heartbeat, formatHeartbeat(route?.heartbeatAgeSeconds));
        setText(elements.standby, standby?.displayName || "Нет готового");
        setText(elements.recommendation, recommendationLabel(status.recommendation));
        setText(elements.reason, status.recommendation.reason);
        setText(elements.updated, formatGeneratedAt(status.generatedAt));
        setText(elements.cpu, formatPercent(route?.hostCpuPercent));
        setText(elements.memory, formatMemory(route?.hostMemoryAvailableBytes, route?.hostMemoryTotalBytes));
        setText(elements.rawState, route ? `${status.streamState} · ${route.overallState}` : status.streamState);
        setText(elements.confidence, confidenceLabel(status.recommendation.confidence));
        const codes = [...status.health.reasonCodes, status.recommendation.reasonCode]
          .filter((value, index, values) => values.indexOf(value) === index);
        setText(elements.reasonCodes, codes.length ? codes.join(", ") : "—");
        setText(elements.serverTime, status.generatedAt || "—");
      },
      offline(mode = "monitoring") {
        documentObject.body.dataset.hudState = mode;
        setText(elements.server, "Сервер мониторинга");
        setText(elements.title, mode === "revoked" ? "Доступ HUD отключён" : "Нет связи с панелью мониторинга");
        setText(elements.message, mode === "revoked"
          ? "Создайте новую одноразовую привязку в панели администратора."
          : "Связь с панелью потеряна. Повторяем безопасно.");
        setText(elements.bitrate, "—");
        setText(elements.trend, "Нет свежих данных");
        setText(elements.source, "—");
        setText(elements.youtube, "—");
        setText(elements.heartbeat, "Нет связи");
        setText(elements.standby, "—");
        setText(elements.recommendation, mode === "revoked" ? "Требуется новая привязка" : "Не переключайте сервер по этому экрану");
        setText(elements.reason, mode === "revoked" ? "Сессия устройства отозвана или истекла." : "Достоверная оценка временно невозможна.");
        setText(elements.updated, mode === "revoked" ? "Сессия завершена" : "Ожидаем восстановление");
      },
    };
  }

  function initializeHud(windowObject) {
    const documentObject = windowObject.document;
    if (documentObject[HUD_INSTANCE_KEY]) return documentObject[HUD_INSTANCE_KEY];
    // Document-owned identity also survives the script being evaluated twice.
    const instance = { started: false };
    Object.defineProperty(documentObject, HUD_INSTANCE_KEY, { value: instance });
    const start = () => {
      if (instance.started) return;
      instance.started = true;
      const renderer = createRenderer(documentObject);
      const audio = new AlertAudio({
        audioContextClass: windowObject.AudioContext || windowObject.webkitAudioContext,
        onStateChange(instance) {
          setText(
            select(documentObject, "sound"),
            instance.enabled ? "Звуковые предупреждения включены" : "Включить звуковые предупреждения",
          );
          const mute = select(documentObject, "mute");
          if (mute) mute.disabled = !instance.enabled;
        },
      });
      let previousLevel = null;
      let terminalSession = false;
      let pageSuspended = false;
      let pairingFinished = false;
      const poller = new HudPoller({
        fetchFn: windowObject.fetch.bind(windowObject),
        isHidden: () => documentObject.hidden,
        onStatus(status) {
          renderer.render(status);
          audio.notify(previousLevel, status.health.level);
          previousLevel = status.health.level;
        },
        onMonitoringOffline() {
          renderer.offline("monitoring");
          previousLevel = null;
        },
        onRevoked() {
          terminalSession = true;
          previousLevel = null;
          renderer.offline("revoked");
        },
        abortControllerClass: windowObject.AbortController,
      });

      select(documentObject, "sound")?.addEventListener("click", () => {
        void audio.toggle().catch(() => {
          audio.destroy();
          audio.onStateChange(audio);
        });
      });
      select(documentObject, "mute")?.addEventListener("click", () => {
        audio.mute();
      });
      const details = select(documentObject, "details");
      select(documentObject, "details-open")?.addEventListener("click", () => {
        if (details) details.hidden = false;
      });
      select(documentObject, "details-close")?.addEventListener("click", () => {
        if (details) details.hidden = true;
      });
      select(documentObject, "logout")?.addEventListener("click", async () => {
        terminalSession = true;
        poller.stop();
        audio.destroy();
        try {
          await windowObject.fetch("/moblin-hud/api/logout", {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
          });
        } catch (_error) {
          // The local session is still hidden and polling remains stopped.
        }
        if (details) details.hidden = true;
        renderer.offline("revoked");
      });

      documentObject.addEventListener("visibilitychange", () => {
        if (!terminalSession && !pageSuspended && pairingFinished) {
          poller.restart(documentObject.hidden ? 10_000 : 0);
        }
      });
      windowObject.addEventListener("pagehide", () => {
        pageSuspended = true;
        poller.stop();
        audio.destroy();
        audio.onStateChange(audio);
        previousLevel = null;
      });
      windowObject.addEventListener("pageshow", () => {
        pageSuspended = false;
        if (!terminalSession && pairingFinished) poller.start();
      });

      const pairController = new windowObject.AbortController();
      const pairTimeout = windowObject.setTimeout(() => pairController.abort(), REQUEST_TIMEOUT_MS);
      const pairing = pairFromFragment({
        location: windowObject.location,
        history: windowObject.history,
        alreadyPaired: documentObject.body?.dataset.hudPaired === "true",
        fetchFn: (url, options) => windowObject.fetch(url, { ...options, signal: pairController.signal }),
      });
      void pairing
        .catch(() => renderer.offline("monitoring"))
        .finally(() => {
          windowObject.clearTimeout(pairTimeout);
          pairingFinished = true;
          if (!terminalSession && !pageSuspended) poller.start();
        });
    };

    if (windowObject.document.readyState === "loading") {
      windowObject.document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
      start();
    }
    return instance;
  }

  return {
    ALERT_COOLDOWN_MS,
    MUTE_DURATION_MS,
    REQUEST_TIMEOUT_MS,
    AlertAudio,
    HudPoller,
    confidenceLabel,
    createRenderer,
    formatBitrate,
    formatGeneratedAt,
    formatHeartbeat,
    formatMemory,
    formatSource,
    formatTrend,
    formatYouTube,
    initializeHud,
    nextPollDelay,
    normalizeStatus,
    pairFromFragment,
    parsePairToken,
    recommendationLabel,
    safeDisplayName,
    shouldSoundTransition,
  };
});
