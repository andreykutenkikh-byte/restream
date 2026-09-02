"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  buildAdminPasswordPayload,
  buildYouTubeKeyPayload,
  buildYouTubePayload,
  formatAge,
  formatBitrate,
  normalizeRelayStatus,
  previewUpdateIsCurrent,
  relayCommandOutcome,
  relayViewModel,
  sanitizeSrtUrl,
  wipeSecretObject,
} = require("../../app/static/relay-dashboard.js");

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
    admin_password: "panel-password",
  }), {
    url: "rtmps://a.rtmp.youtube.com/live2",
    stream_key: "stream-key_123",
    admin_password: "panel-password",
  });
  assert.throws(() => buildYouTubePayload({
    url: "rtmp://a.rtmp.youtube.com/live2",
    stream_key: "secret",
    admin_password: "password",
  }));
  assert.throws(() => buildYouTubePayload({
    url: "rtmps://a.rtmp.youtube.com/live2#secret",
    stream_key: "secret",
    admin_password: "password",
  }));
  assert.deepEqual(buildAdminPasswordPayload({ admin_password: "password" }), { admin_password: "password" });
});

test("configured YouTube can rotate only the stream key", () => {
  assert.deepEqual(buildYouTubeKeyPayload({
    stream_key: " new-key_456 ",
    admin_password: "panel-password",
  }), {
    stream_key: "new-key_456",
    admin_password: "panel-password",
  });
  assert.throws(() => buildYouTubeKeyPayload({
    stream_key: "key with spaces",
    admin_password: "panel-password",
  }));
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
