"use strict";

const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const {
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
} = require("../../app/static/relay-dashboard.js");

const dashboardTemplate = fs.readFileSync(
  path.join(__dirname, "../../app/templates/dashboard.html"),
  "utf8",
);
  const dashboardJavascript = fs.readFileSync(
  path.join(__dirname, "../../app/static/relay-dashboard.js"),
  "utf8",
);

function status(overrides = {}) {
  return normalizeRelayStatus({
    available: true,
    last_seen_at: "2026-09-02T00:00:00Z",
    status: {
      service: "inactive",
      enabled: false,
      main_process: "stopped",
      srt_listener: "closed",
      source: "NONE",
      youtube_forward: "inactive",
      overall: "ok",
      youtube_url_configured: true,
      youtube_key_configured: true,
      portrait_profile: true,
      error_code: null,
      ...overrides,
    },
  });
}

test("old relay heartbeat remains compatible without bitrate", () => {
  const relay = status();
  assert.equal(relay.inputBitrateBps, null);
  assert.equal(relayViewModel(relay).startDisabled, false);
});

test("normal offline state remains operable before and after YouTube setup", () => {
  const configured = relayViewModel(status({ overall: "offline" }));
  assert.equal(configured.safelyStopped, true);
  assert.equal(configured.startDisabled, false);

  const unconfigured = relayViewModel(status({
    overall: "offline",
    youtube_url_configured: false,
    youtube_key_configured: false,
    error_code: "youtube_not_configured",
  }));
  assert.equal(unconfigured.safelyStopped, true);
  assert.equal(unconfigured.operable, true);
  assert.equal(unconfigured.configureDisabled, false);
  assert.equal(unconfigured.startDisabled, true);
  assert.equal(unconfigured.badgeLabel, "Нужна настройка");
});

test("null bitrate never becomes a misleading zero sample", () => {
  assert.equal(status({ input_bitrate_bps: null }).inputBitrateBps, null);
});

test("LIVE state exposes only bounded numeric bitrate", () => {
  assert.equal(status({ source: "LIVE", input_bitrate_bps: 3_850_000 }).inputBitrateBps, 3_850_000);
  assert.equal(status({ source: "LIVE", input_bitrate_bps: -1 }).inputBitrateBps, null);
  assert.equal(status({ source: "LIVE", input_bitrate_bps: 1_000_000_001 }).inputBitrateBps, null);
});

test("stale LIVE preview work cannot overwrite a newer offline state", () => {
  const live = status({ source: "LIVE" });
  const offline = status({ source: "NONE" });
  assert.equal(previewUpdateIsCurrent(7, 7, "relay-a", "relay-a", live), true);
  assert.equal(previewUpdateIsCurrent(7, 8, "relay-a", "relay-a", offline), false);
  assert.equal(previewUpdateIsCurrent(7, 7, "relay-a", "relay-b", live), false);
});

test("dialog request guard binds node, generation, and open lifecycle", () => {
  assert.equal(isCurrentDialogRequest("relay-a", "relay-a", 7, 7, true), true);
  assert.equal(isCurrentDialogRequest("relay-a", "relay-b", 7, 7, true), false);
  assert.equal(isCurrentDialogRequest("relay-a", "relay-a", 7, 8, true), false);
  assert.equal(isCurrentDialogRequest("relay-a", "relay-a", 7, 7, false), false);
  assert.equal(isCurrentDialogRequest("", "", 7, 7, true), false);
});

test("late secret completion cannot mutate a closed and reopened dialog", async () => {
  let currentNodeId = "relay-a";
  let currentGeneration = 11;
  let dialogOpen = true;
  const expectedNodeId = currentNodeId;
  const expectedGeneration = currentGeneration;
  const requestIsCurrent = () => isCurrentDialogRequest(
    expectedNodeId,
    currentNodeId,
    expectedGeneration,
    currentGeneration,
    dialogOpen,
  );
  let resolveResponse;
  const responsePromise = new Promise((resolve) => { resolveResponse = resolve; });
  const response = { public_url: "srt://relay.test:8890?secret=late" };
  const dialogState = {
    srtValue: "",
    busy: "reopened-dialog",
    error: "reopened-dialog-error",
    resetCount: 0,
    closeCount: 0,
  };

  const lateRequest = (async () => {
    const result = await responsePromise;
    if (!requestIsCurrent()) {
      wipeSecretObject(result);
      return;
    }
    dialogState.srtValue = result.public_url;
    dialogState.busy = false;
    dialogState.error = "";
    dialogState.resetCount += 1;
    dialogState.closeCount += 1;
  })();

  dialogOpen = false;
  currentGeneration += 1;
  dialogState.busy = false;
  dialogOpen = true;
  resolveResponse(response);
  await lateRequest;

  assert.equal(response.public_url, "");
  assert.deepEqual(dialogState, {
    srtValue: "",
    busy: false,
    error: "reopened-dialog-error",
    resetCount: 0,
    closeCount: 0,
  });

  currentNodeId = "relay-b";
  assert.equal(requestIsCurrent(), false);
});

test("bitrate and age labels are concise", () => {
  assert.equal(formatBitrate(3_850_000), "3.9 Мбит/с");
  assert.equal(formatBitrate(null), "—");
  assert.equal(formatAge("2026-09-02T00:00:00Z", Date.parse("2026-09-02T00:00:04Z")), "только что");
});

test("actions fail closed on inconsistent or failed relay state", () => {
  assert.equal(relayViewModel(status({ main_process: "running" })).startDisabled, true);
  assert.equal(relayViewModel(status({ overall: "failed", error_code: "relayctl_failed" })).configureDisabled, true);
  assert.equal(relayViewModel(status({ source: "LIVE", service: "active", enabled: true, main_process: "running", srt_listener: "listening", youtube_forward: "active" })).stopDisabled, false);
});

test("YouTube form accepts only exact secret-safe RTMPS data", () => {
  assert.deepEqual(buildYouTubePayload({
    url: "  rtmps://a.rtmp.youtube.com/live2  ",
    stream_key: "stream-key_123",
    admin_password: "stale-field-must-not-be-sent",
  }), {
    url: "rtmps://a.rtmp.youtube.com/live2",
    stream_key: "stream-key_123",
  });
  assert.throws(() => buildYouTubePayload({
    url: "rtmp://a.rtmp.youtube.com/live2",
    stream_key: "secret",
  }));
  assert.throws(() => buildYouTubePayload({
    url: "rtmps://a.rtmp.youtube.com/live2#secret",
    stream_key: "secret",
  }));
  assert.deepEqual(buildAdminPasswordPayload({ admin_password: "password" }), { admin_password: "password" });
});

test("configured YouTube can rotate only the stream key", () => {
  assert.deepEqual(buildYouTubeKeyPayload({
    stream_key: " new-key_456 ",
    admin_password: "stale-field-must-not-be-sent",
  }), {
    stream_key: "new-key_456",
  });
  assert.throws(() => buildYouTubeKeyPayload({
    stream_key: "key with spaces",
  }));
});

test("routine setup omits step-up password while destructive clear keeps it", () => {
  assert.doesNotMatch(dashboardTemplate, /data-dashboard-(?:youtube|moblin)-admin-password/);
  assert.equal((dashboardTemplate.match(/name="admin_password"/g) || []).length, 1);
  assert.match(dashboardTemplate, /data-dashboard-clear-admin-password/);
  assert.match(dashboardTemplate, /relay-dashboard\.js\?v=20260902\.4/);

  const moblinSubmit = dashboardJavascript
    .split("async function submitMoblin", 2)[1]
    .split("async function submitClear", 1)[0];
  assert.doesNotMatch(moblinSubmit, /admin_password|buildAdminPasswordPayload/);
  assert.match(moblinSubmit, /let body = "\{\}";/);
  assert.match(moblinSubmit, /await apiRequest[\s\S]*?if \(!requestIsCurrent\(\)\) return;/);
  assert.match(moblinSubmit, /finally \{[\s\S]*?if \(requestIsCurrent\(\)\) setBusy/);

  const youtubeSubmit = dashboardJavascript
    .split("async function submitYouTube", 2)[1]
    .split("async function submitMoblin", 1)[0];
  assert.match(youtubeSubmit, /await apiRequest[\s\S]*?if \(!requestIsCurrent\(\)\) return;/);
  assert.match(youtubeSubmit, /finally \{[\s\S]*?if \(requestIsCurrent\(\)\)/);

  const clearSubmit = dashboardJavascript
    .split("async function submitClear", 2)[1]
    .split("page.addEventListener", 1)[0];
  assert.match(clearSubmit, /await apiRequest[\s\S]*?if \(!requestIsCurrent\(\)\) return;/);
  assert.match(clearSubmit, /finally \{[\s\S]*?if \(requestIsCurrent\(\)\)/);
  assert.match(
    dashboardJavascript,
    /addEventListener\("pagehide"[\s\S]*?youtubeRequestGeneration \+= 1;[\s\S]*?moblinRequestGeneration \+= 1;[\s\S]*?clearRequestGeneration \+= 1;/,
  );
  const resetDialogSource = dashboardJavascript
    .split("function resetDialog", 2)[1]
    .split("function prepareYouTubeDialog", 1)[0];
  assert.match(resetDialogSource, /setBusy\(form, false\)/);
  assert.match(
    dashboardJavascript,
    /addEventListener\("pagehide"[\s\S]*?setBusy\(youtubeForm, false\)[\s\S]*?setBusy\(moblinForm, false\)[\s\S]*?setBusy\(clearForm, false\)/,
  );
});

test("Moblin URL dialog gives the exact official SRT settings", () => {
  assert.match(
    dashboardTemplate,
    /Settings → Streams → профиль → SRT\(LA\) → Implementation \/ Реализация → Official/,
  );
  assert.match(dashboardTemplate, /Latency: 2000 ms/);
  assert.match(dashboardTemplate, /Big packets: ON/);
  assert.match(dashboardTemplate, /SRT URL скопируйте целиком в поле URL и не редактируйте/);
});

test("SRT reveal accepts only a bounded SRT URL", () => {
  assert.equal(sanitizeSrtUrl("srt://example.test:8890?streamid=value"), "srt://example.test:8890?streamid=value");
  assert.equal(sanitizeSrtUrl("https://example.test/"), "");
  assert.equal(sanitizeSrtUrl("srt://example.test/a\nsecret"), "");
});

test("command outcomes and in-memory secret wiping are strict", () => {
  assert.equal(relayCommandOutcome({ state: "queued" }), "pending");
  assert.equal(relayCommandOutcome({ state: "completed", completion_status: "ok" }), "success");
  assert.equal(relayCommandOutcome({ state: "completed", completion_status: "conflict" }), "conflict");
  const payload = { stream_key: "sentinel", nested: { admin_password: "sentinel" } };
  wipeSecretObject(payload);
  assert.deepEqual(payload, { stream_key: "", nested: { admin_password: "" } });
});
