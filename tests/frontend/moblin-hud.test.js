"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  ALERT_COOLDOWN_MS,
  MUTE_DURATION_MS,
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
} = require("../../app/static/moblin-hud.js");

const template = fs.readFileSync(path.join(__dirname, "../../app/templates/moblin_hud.html"), "utf8");
const javascript = fs.readFileSync(path.join(__dirname, "../../app/static/moblin-hud.js"), "utf8");
const stylesheet = fs.readFileSync(path.join(__dirname, "../../app/static/moblin-hud.css"), "utf8");

const validToken = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-ABCD";

function statusPayload(overrides = {}) {
  return {
    scope: "stream_monitor",
    generated_at: "2026-09-04T08:00:00+00:00",
    stream_state: "active",
    health: {
      level: "green",
      title: "Эфир стабилен",
      message: "Входящий поток и отправка в YouTube работают.",
      reason_codes: ["healthy"],
      confidence: "active_path_measured",
    },
    current_route: {
      route_id: "relay-hk",
      display_name: "HK relay",
      kind: "relay",
      source: "LIVE",
      input_bitrate_bps: 7_500_000,
      ema_input_bitrate_bps: 7_300_000,
      stable_baseline_bps: 7_400_000,
      bitrate_trend: "stable",
      youtube_forward_state: "active",
      overall_state: "healthy",
      heartbeat_age_seconds: 1.2,
      host_cpu_percent: 32.4,
      host_memory_available_bytes: 2 * 1024 ** 3,
      host_memory_total_bytes: 4 * 1024 ** 3,
    },
    standby_route: {
      route_id: "relay-backup",
      display_name: "Backup relay",
      kind: "relay",
      source: "SLATE",
      input_bitrate_bps: null,
      ema_input_bitrate_bps: null,
      stable_baseline_bps: null,
      bitrate_trend: "unknown",
      youtube_forward_state: "inactive",
      overall_state: "ready",
      heartbeat_age_seconds: 2,
      host_cpu_percent: 12,
      host_memory_available_bytes: 3 * 1024 ** 3,
      host_memory_total_bytes: 4 * 1024 ** 3,
    },
    recommendation: {
      action: "stay",
      target_route_id: null,
      target_display_name: null,
      confidence: "active_path_measured",
      reason: "Текущий входящий поток стабилен.",
      reason_code: "current_route_healthy",
      route_to_target_measured: false,
    },
    ...overrides,
  };
}

class FakeAbortController {
  constructor() {
    this.signal = { aborted: false };
  }

  abort() {
    this.signal.aborted = true;
  }
}

function response(payload, { status = 200, ok = status >= 200 && status < 300 } = {}) {
  return { status, ok, json: async () => payload };
}

test("pairing fragment accepts only one exact URL-safe high-entropy token", () => {
  assert.equal(parsePairToken(`#pair=${validToken}`), validToken);
  assert.equal(parsePairToken(`#pair=${validToken}&next=evil`), null);
  assert.equal(parsePairToken(`#other=${validToken}`), null);
  assert.equal(parsePairToken("#pair=short"), null);
  assert.equal(parsePairToken(`#pair=${"a".repeat(129)}`), null);
  assert.equal(parsePairToken(`#pair=${"a".repeat(39)}%2F`), null);
});

test("valid pairing clears the fragment immediately before same-origin POST", async () => {
  const calls = [];
  const result = await pairFromFragment({
    location: { hash: `#pair=${validToken}`, pathname: "/moblin-hud", search: "" },
    history: { replaceState: (...args) => calls.push(["history", ...args]) },
    fetchFn: async (url, options) => {
      calls.push(["fetch", url, options]);
      return response({ paired: true });
    },
  });
  assert.deepEqual(result, { attempted: true, paired: true });
  assert.equal(calls[0][0], "history");
  assert.equal(calls[0][3], "/moblin-hud");
  assert.equal(calls[1][0], "fetch");
  assert.equal(calls[1][1], "/moblin-hud/api/pair");
  assert.equal(calls[1][2].credentials, "same-origin");
  assert.equal(calls[1][2].cache, "no-store");
  assert.deepEqual(JSON.parse(calls[1][2].body), { token: validToken });
});

test("invalid fragment is erased without any network request", async () => {
  const cleanUrls = [];
  let requests = 0;
  const result = await pairFromFragment({
    location: { hash: "#pair=invalid token", pathname: "/moblin-hud", search: "?mode=hud" },
    history: { replaceState: (_state, _title, url) => cleanUrls.push(url) },
    fetchFn: async () => { requests += 1; },
  });
  assert.deepEqual(result, { attempted: true, paired: false });
  assert.deepEqual(cleanUrls, ["/moblin-hud?mode=hud"]);
  assert.equal(requests, 0);
});

test("page without a pairing fragment neither navigates nor pairs", async () => {
  let touched = false;
  const result = await pairFromFragment({
    location: { hash: "", pathname: "/moblin-hud", search: "" },
    history: { replaceState: () => { touched = true; } },
    fetchFn: async () => { touched = true; },
  });
  assert.deepEqual(result, { attempted: false, paired: false });
  assert.equal(touched, false);
});

test("an authenticated document clears a reused pairing fragment without replaying it", async () => {
  const cleared = [];
  const result = await pairFromFragment({
    location: { hash: `#pair=${validToken}`, pathname: "/moblin-hud", search: "" },
    history: { replaceState: (_state, _title, url) => cleared.push(url) },
    alreadyPaired: true,
    fetchFn: async () => { assert.fail("valid cookie must not replay one-time token"); },
  });
  assert.deepEqual(result, { attempted: false, paired: true });
  assert.deepEqual(cleared, ["/moblin-hud"]);
});

test("normalizer accepts only the dedicated stream_monitor scope", () => {
  assert.throws(() => normalizeStatus(statusPayload({ scope: "admin" })), /scope/);
  assert.throws(() => normalizeStatus(null), /scope/);
  assert.equal(normalizeStatus(statusPayload()).streamState, "active");
});

test("normalizer copies only the explicit secret-free route fields", () => {
  const raw = statusPayload();
  raw.current_route.address = "203.0.113.4";
  raw.current_route.srt_url = "srt://example.invalid?passphrase=secret";
  raw.current_route.youtube_key = "never-render-this";
  raw.admin_session = "admin-secret";
  const normalized = normalizeStatus(raw);
  const serialized = JSON.stringify(normalized);
  assert.doesNotMatch(serialized, /203\.0\.113\.4|srt:\/\/|never-render-this|admin-secret/);
  assert.equal(normalized.currentRoute.displayName, "HK relay");
});

test("route and target labels fail closed when they contain an address", () => {
  assert.equal(safeDisplayName("relay 176.98.181.225"), "Сервер не определён");
  assert.equal(safeDisplayName("https://relay.example"), "Сервер не определён");
  const raw = statusPayload();
  raw.current_route.display_name = "176.98.181.225";
  raw.recommendation.target_display_name = "srt://secret.example";
  const normalized = normalizeStatus(raw);
  assert.equal(normalized.currentRoute.displayName, "Сервер не определён");
  assert.equal(normalized.recommendation.targetDisplayName, "Резервный сервер");
});

test("unknown enums, oversized numbers, and unsafe reason codes fail closed", () => {
  const raw = statusPayload({ stream_state: "root" });
  raw.health.level = "purple";
  raw.health.reason_codes = ["healthy", "bad reason", "x".repeat(81)];
  raw.current_route.input_bitrate_bps = 2_000_000_000;
  raw.current_route.host_cpu_percent = 101;
  raw.recommendation.action = "automatic_switch";
  raw.recommendation.confidence = "end_to_end_measured";
  raw.recommendation.route_to_target_measured = true;
  const normalized = normalizeStatus(raw);
  assert.equal(normalized.streamState, "idle");
  assert.equal(normalized.health.level, "unknown");
  assert.deepEqual(normalized.health.reasonCodes, ["healthy"]);
  assert.equal(normalized.currentRoute.inputBitrateBps, null);
  assert.equal(normalized.currentRoute.hostCpuPercent, null);
  assert.equal(normalized.recommendation.action, "watch");
  assert.equal(normalized.recommendation.confidence, "active_path_measured");
  assert.equal(normalized.recommendation.routeToTargetMeasured, false);
});

test("operator-friendly formatters stay bounded and unambiguous", () => {
  assert.equal(formatBitrate(7_500_000), "7.5 Мбит/с");
  assert.equal(formatBitrate(850_000), "850 Кбит/с");
  assert.equal(formatBitrate(null), "—");
  assert.equal(formatTrend("falling"), "Битрейт снижается");
  assert.equal(formatSource("LIVE"), "Moblin LIVE");
  assert.equal(formatSource("SLATE"), "Заставка");
  assert.equal(formatYouTube("active"), "Передаётся");
  assert.equal(formatYouTube("failed"), "Ошибка");
});

test("heartbeat and resource formatters reject misleading values", () => {
  assert.equal(formatHeartbeat(1.2), "Сейчас");
  assert.equal(formatHeartbeat(31), "31 с назад");
  assert.equal(formatHeartbeat(-1), "Нет данных");
  assert.equal(formatMemory(2 * 1024 ** 3, 4 * 1024 ** 3), "2.0 ГБ (50%)");
  assert.equal(formatMemory(5, 4), "—");
});

test("generated timestamp is displayed as age without locale-dependent data", () => {
  assert.equal(formatGeneratedAt("2026-09-04T08:00:00Z", Date.parse("2026-09-04T08:00:01Z")), "Обновлено сейчас");
  assert.equal(formatGeneratedAt("2026-09-04T08:00:00Z", Date.parse("2026-09-04T08:00:08Z")), "Обновлено 8 с назад");
  assert.equal(formatGeneratedAt("invalid"), "Время неизвестно");
});

test("manual switch wording never implies automatic failover", () => {
  const label = recommendationLabel({ action: "switch_recommended", targetDisplayName: "Backup relay" });
  assert.equal(label, "Переключите Moblin вручную: Backup relay");
  assert.doesNotMatch(label, /автомат/i);
  assert.equal(confidenceLabel("standby_server_readiness"), "Измерена только готовность резервного сервера");
  assert.equal(confidenceLabel("active_path_measured"), "Измерен активный входящий путь");
});

test("poll cadence is 2s live or alert, 5s idle, and 10s hidden", () => {
  const greenLive = normalizeStatus(statusPayload());
  const idle = normalizeStatus(statusPayload({ stream_state: "idle", current_route: null }));
  const redIdleRaw = statusPayload({ stream_state: "idle", current_route: null });
  redIdleRaw.health.level = "red";
  assert.equal(nextPollDelay({ status: greenLive }), 2000);
  assert.equal(nextPollDelay({ status: idle }), 5000);
  assert.equal(nextPollDelay({ status: normalizeStatus(redIdleRaw) }), 2000);
  assert.equal(nextPollDelay({ status: greenLive, hidden: true }), 10_000);
});

test("network errors back off exponentially and cap at 15 seconds", () => {
  assert.equal(nextPollDelay({ errorCount: 1 }), 2000);
  assert.equal(nextPollDelay({ errorCount: 2 }), 4000);
  assert.equal(nextPollDelay({ errorCount: 4 }), 15_000);
  assert.equal(nextPollDelay({ errorCount: 20 }), 15_000);
  assert.equal(nextPollDelay({ errorCount: 20, hidden: true }), 10_000);
});

test("poller uses one same-origin no-store request and normalizes response", async () => {
  const requests = [];
  const statuses = [];
  const timers = [];
  const poller = new HudPoller({
    fetchFn: async (...args) => { requests.push(args); return response(statusPayload()); },
    onStatus: (value) => statuses.push(value),
    setTimeoutFn: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeoutFn: () => {},
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 4;
  await poller.poll(4);
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], "/moblin-hud/api/status");
  assert.equal(requests[0][1].credentials, "same-origin");
  assert.equal(requests[0][1].cache, "no-store");
  assert.equal(statuses[0].health.level, "green");
  assert.equal(timers.at(-1).delay, 2000);
});

test("poller refuses to start a second request while one is active", async () => {
  let resolveFetch;
  let calls = 0;
  const pending = new Promise((resolve) => { resolveFetch = resolve; });
  const poller = new HudPoller({
    fetchFn: async () => { calls += 1; return pending; },
    setTimeoutFn: () => 1,
    clearTimeoutFn: () => {},
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 1;
  const first = poller.poll(1);
  await Promise.resolve();
  await poller.poll(1);
  assert.equal(calls, 1);
  resolveFetch(response(statusPayload()));
  await first;
});

test("visibility restart aborts in-flight work and ignores its stale result", async () => {
  let resolveFetch;
  const pending = new Promise((resolve) => { resolveFetch = resolve; });
  const statuses = [];
  const poller = new HudPoller({
    fetchFn: async () => pending,
    onStatus: (value) => statuses.push(value),
    setTimeoutFn: () => 1,
    clearTimeoutFn: () => {},
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 7;
  const oldPoll = poller.poll(7);
  await Promise.resolve();
  const controller = poller.controller;
  poller.restart();
  assert.equal(controller.signal.aborted, true);
  resolveFetch(response(statusPayload()));
  await oldPoll;
  assert.equal(statuses.length, 0);
});

test("restart waits for aborted fetch settlement before granting the next request slot", async () => {
  let finish;
  let requests = 0;
  const timers = new Map();
  let timerId = 0;
  const poller = new HudPoller({
    fetchFn: () => { requests += 1; return new Promise((resolve) => { finish = resolve; }); },
    setTimeoutFn: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeoutFn: (timer) => timers.delete(timer),
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 1;
  const first = poller.poll();
  const owner = poller.controller;
  poller.restart(10_000);
  poller.restart(0);
  assert.equal(owner.signal.aborted, true);
  assert.equal(poller.controller, owner);
  assert.equal([...timers.values()].filter((timer) => timer.delay === 0).length, 0);
  await poller.poll();
  assert.equal(requests, 1);
  finish(response(statusPayload()));
  await first;
  assert.equal(poller.controller, null);
  assert.equal([...timers.values()].filter((timer) => timer.delay === 0).length, 1);
  poller.stop();
  assert.equal(timers.size, 0);
});

test("a revoked session is a distinct terminal state", async () => {
  let revoked = 0;
  let offline = 0;
  const poller = new HudPoller({
    fetchFn: async () => response({}, { status: 401, ok: false }),
    onRevoked: () => { revoked += 1; },
    onMonitoringOffline: () => { offline += 1; },
    setTimeoutFn: () => 1,
    clearTimeoutFn: () => {},
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 2;
  await poller.poll(2);
  assert.equal(revoked, 1);
  assert.equal(offline, 0);
  assert.equal(poller.running, false);
});

test("ordinary request failures enter monitoring-offline and retry", async () => {
  const errors = [];
  const delays = [];
  const poller = new HudPoller({
    fetchFn: async () => { throw new Error("offline"); },
    onMonitoringOffline: (count) => errors.push(count),
    setTimeoutFn: (_callback, delay) => { delays.push(delay); return delays.length; },
    clearTimeoutFn: () => {},
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 3;
  await poller.poll(3);
  assert.deepEqual(errors, [1]);
  assert.equal(delays.at(-1), 2000);
  assert.equal(poller.running, true);
});

test("stop aborts and invalidates pending generations", () => {
  const poller = new HudPoller({
    fetchFn: async () => response(statusPayload()),
    setTimeoutFn: () => 11,
    clearTimeoutFn: () => {},
    abortControllerClass: FakeAbortController,
  });
  poller.running = true;
  poller.generation = 9;
  poller.controller = new FakeAbortController();
  const controller = poller.controller;
  poller.timer = 11;
  poller.stop();
  assert.equal(controller.signal.aborted, true);
  assert.equal(poller.running, false);
  assert.equal(poller.generation, 10);
  assert.equal(poller.timer, null);
});

test("alerts require a real worsening transition and honor cooldown", () => {
  assert.equal(shouldSoundTransition(null, "red", 1000), false);
  assert.equal(shouldSoundTransition("green", "yellow", 1000), true);
  assert.equal(shouldSoundTransition("green", "red", 1000), true);
  assert.equal(shouldSoundTransition("green", "black", 1000), true);
  assert.equal(shouldSoundTransition("yellow", "black", 1000), true);
  assert.equal(shouldSoundTransition("unknown", "green", 1000), false);
  assert.equal(shouldSoundTransition("yellow", "yellow", 2000), false);
  assert.equal(shouldSoundTransition("red", "yellow", 3000), false);
  assert.equal(shouldSoundTransition("red", "black", 3000), true);
  assert.equal(shouldSoundTransition("yellow", "red", 1000, 900), false);
  assert.equal(shouldSoundTransition("yellow", "red", 1000 + ALERT_COOLDOWN_MS, 1000), true);
  assert.equal(shouldSoundTransition("yellow", "red", 1100, 1000, ALERT_COOLDOWN_MS, "yellow"), true);
  assert.equal(shouldSoundTransition("red", "black", 1200, 1100, ALERT_COOLDOWN_MS, "red"), true);
  assert.equal(shouldSoundTransition("green", "red", 1300, 1200, ALERT_COOLDOWN_MS, "black"), false);
});

test("WebAudio is created only by an explicit user-triggered toggle", async () => {
  let instances = 0;
  const played = [];
  class FakeAudioContext {
    constructor() { instances += 1; this.currentTime = 1; this.destination = {}; }
    async resume() {}
    createOscillator() {
      return {
        frequency: { setValueAtTime: (value) => played.push(value) },
        connect() {}, start() {}, stop() {},
      };
    }
    createGain() {
      return {
        gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect() {},
      };
    }
  }
  const audio = new AlertAudio({ audioContextClass: FakeAudioContext, now: () => 1000 });
  assert.equal(instances, 0);
  assert.equal(audio.notify("green", "red"), false);
  await audio.toggle();
  assert.equal(instances, 1);
  assert.deepEqual(played, [660]);
  assert.equal(audio.notify("green", "red"), true);
  assert.equal(audio.notify("yellow", "red"), false);
  assert.deepEqual(played, [660, 330, 330]);
  assert.equal(audio.notify("red", "black"), true);
  assert.deepEqual(played, [660, 330, 330, 220, 220, 220]);
});

function ordinaryScriptWindow({ hash = "", paired = false, readyState = "complete" } = {}) {
  const listeners = (target) => {
    const handlers = new Map();
    target.addEventListener = (name, callback) => {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(callback);
    };
    target.dispatch = (name) => handlers.get(name)?.forEach((callback) => callback());
    return target;
  };
  const elements = new Map();
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const document = listeners({
    readyState, hidden: false, body: { dataset: { hudPaired: String(paired) } },
    querySelector: (selector) => {
      if (!elements.has(selector)) elements.set(selector, listeners({}));
      return elements.get(selector);
    },
  });
  const window = listeners({
    document,
    location: { hash, pathname: "/moblin-hud", search: "" },
    history: { replaceState: () => { window.location.hash = ""; } },
    AbortController: FakeAbortController,
    fetch: async (url) => {
      requests.push(url);
      return response(url.endsWith("/status") ? statusPayload() : { paired: true });
    },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  return { window, requests, timers, elements };
}

async function flushMicrotasks() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

async function runImmediatePoll(harness) {
  await flushMicrotasks();
  const immediate = [...harness.timers.entries()].find(([, timer]) => timer.delay === 0);
  assert.ok(immediate, "a status poll must be scheduled");
  harness.timers.delete(immediate[0]);
  immediate[1].callback();
  await flushMicrotasks();
}

test("ordinary window/document script initializes once across duplicate script loads", async () => {
  const harness = ordinaryScriptWindow({ hash: `#pair=${validToken}`, readyState: "loading" });
  vm.runInNewContext(javascript, { window: harness.window });
  vm.runInNewContext(javascript, { window: harness.window });
  harness.window.document.dispatch("DOMContentLoaded");
  harness.window.document.dispatch("DOMContentLoaded");
  await runImmediatePoll(harness);
  assert.deepEqual(harness.requests, ["/moblin-hud/api/pair", "/moblin-hud/api/status"]);
  assert.equal(harness.window.document.body.dataset.hudState, "green");
  const instance = initializeHud(harness.window);
  assert.equal(instance, initializeHud(harness.window));
  assert.equal(instance.started, true);
  assert.equal(harness.requests.length, 2);
  harness.window.dispatch("pagehide");
});

test("pagehide/pageshow resumes the same initialized page and logout stays terminal", async () => {
  const harness = ordinaryScriptWindow({ paired: true, hash: `#pair=${validToken}` });
  vm.runInNewContext(javascript, { window: harness.window });
  await runImmediatePoll(harness);
  assert.deepEqual(harness.requests, ["/moblin-hud/api/status"]);
  harness.window.dispatch("pagehide");
  assert.equal(harness.timers.size, 0);
  harness.window.document.dispatch("visibilitychange");
  harness.window.location.hash = `#pair=${validToken}`;
  harness.window.dispatch("hashchange");
  assert.equal(harness.window.location.hash, "");
  assert.equal(harness.timers.size, 0);
  harness.window.dispatch("pageshow");
  await runImmediatePoll(harness);
  assert.equal(harness.requests.length, 2);
  harness.elements.get("[data-hud-logout]").dispatch("click");
  await flushMicrotasks();
  harness.window.dispatch("pagehide");
  harness.window.dispatch("pageshow");
  harness.window.document.dispatch("visibilitychange");
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.window.document.body.dataset.hudState, "revoked");
});

test("same-document pairing-link reopen clears its fragment and reuses the confirmed session", async () => {
  const harness = ordinaryScriptWindow({ hash: `#pair=${validToken}` });
  vm.runInNewContext(javascript, { window: harness.window });
  await runImmediatePoll(harness);
  const instance = initializeHud(harness.window);
  harness.window.location.hash = `#pair=${validToken}`;
  harness.window.dispatch("hashchange");
  assert.equal(harness.window.location.hash, "");
  await runImmediatePoll(harness);
  assert.equal(initializeHud(harness.window), instance);
  assert.deepEqual(harness.requests, [
    "/moblin-hud/api/pair", "/moblin-hud/api/status", "/moblin-hud/api/status",
  ]);
  harness.window.dispatch("pagehide");
});

test("a new fragment alone cannot pair or revive an unconfirmed HUD document", async () => {
  const harness = ordinaryScriptWindow();
  vm.runInNewContext(javascript, { window: harness.window });
  await flushMicrotasks();
  const pendingTimers = [...harness.timers.keys()];
  harness.window.location.hash = `#pair=${validToken}`;
  harness.window.dispatch("hashchange");
  await flushMicrotasks();
  assert.equal(harness.window.location.hash, "");
  assert.deepEqual(harness.requests, []);
  assert.deepEqual([...harness.timers.keys()], pendingTimers);
  harness.window.dispatch("pagehide");
});

test("alert patterns use one, two, and three tones", async () => {
  const played = [];
  class FakeAudioContext {
    constructor() { this.currentTime = 0; this.destination = {}; }
    async resume() {}
    createOscillator() {
      return {
        frequency: { setValueAtTime: (value) => played.push(value) },
        connect() {}, start() {}, stop() {},
      };
    }
    createGain() {
      return {
        gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect() {},
      };
    }
  }
  const audio = new AlertAudio({ audioContextClass: FakeAudioContext });
  await audio.toggle();
  played.length = 0;
  audio.play("yellow");
  assert.deepEqual(played, [520]);
  played.length = 0;
  audio.play("red");
  assert.deepEqual(played, [330, 330]);
  played.length = 0;
  audio.play("black");
  assert.deepEqual(played, [220, 220, 220]);
});

test("mute is memory-only, lasts 60 seconds, and suppresses alerts", async () => {
  let now = 5000;
  class FakeAudioContext {
    constructor() { this.currentTime = 0; this.destination = {}; }
    async resume() {}
    createOscillator() { return { frequency: { setValueAtTime() {} }, connect() {}, start() {}, stop() {} }; }
    createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} }; }
  }
  const audio = new AlertAudio({ audioContextClass: FakeAudioContext, now: () => now });
  await audio.toggle();
  audio.mute();
  assert.equal(audio.mutedUntil, 5000 + MUTE_DURATION_MS);
  assert.equal(audio.notify("yellow", "red"), false);
  now = audio.mutedUntil;
  assert.equal(audio.notify("yellow", "red"), true);
});

test("renderer assigns API values through textContent and exposes offline vs revoked", () => {
  const values = new Map();
  const elements = new Map();
  for (const name of ["server", "title", "message", "bitrate", "trend", "source", "youtube", "heartbeat", "standby", "recommendation", "reason", "updated", "cpu", "memory", "raw-state", "confidence", "reason-codes", "server-time"]) {
    const element = {};
    Object.defineProperty(element, "textContent", { set: (value) => values.set(name, value) });
    elements.set(`[data-hud-${name}]`, element);
  }
  const fakeDocument = { body: { dataset: {} }, querySelector: (selector) => elements.get(selector) || null };
  const renderer = createRenderer(fakeDocument);
  renderer.render(normalizeStatus(statusPayload()));
  assert.equal(fakeDocument.body.dataset.hudState, "green");
  assert.equal(values.get("server"), "HK relay");
  assert.equal(values.get("bitrate"), "7.5 Мбит/с");
  renderer.offline("monitoring");
  assert.equal(values.get("title"), "Нет связи с панелью мониторинга");
  renderer.offline("revoked");
  assert.equal(values.get("title"), "Доступ HUD отключён");
});

test("HUD document is standalone, portrait-aware, and contains no media preview", () => {
  assert.match(template, /viewport-fit=cover/);
  assert.match(template, /moblin-hud\.css/);
  assert.match(template, /moblin-hud\.js/);
  assert.match(template, /только мониторинг/);
  assert.doesNotMatch(template, /extends\s+|<video|<iframe|hls|admin|csrf-token/i);
  assert.match(stylesheet, /env\(safe-area-inset-top\)/);
  assert.match(stylesheet, /height:\s*100dvh/);
  assert.match(stylesheet, /overflow:\s*hidden/);
});

test("frontend has no secret persistence, dynamic code, unsafe HTML, or external requests", () => {
  assert.doesNotMatch(javascript, /localStorage|sessionStorage|indexedDB|document\.cookie/);
  assert.doesNotMatch(javascript, /innerHTML|outerHTML|insertAdjacentHTML|eval\s*\(|new\s+Function/);
  assert.doesNotMatch(javascript, /https?:\/\/|srt:\/\/|rtmps?:\/\/|\.m3u8|WebSocket|EventSource/);
  assert.match(javascript, /textContent/);
  assert.match(javascript, /history\.replaceState/);
  assert.match(javascript, /addEventListener\("pagehide"[\s\S]*poller\.stop\(\)/);
  assert.match(javascript, /addEventListener\("visibilitychange"[\s\S]*poller\.restart\(/);
});
