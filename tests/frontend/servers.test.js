"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  MAX_COMMAND_POLL_ATTEMPTS,
  MAX_POLL_ATTEMPTS,
  MAX_TRANSIENT_POLL_FAILURES,
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
  nodeStatusPresentation,
  relayCommandOutcome,
  relayIsOperable,
  relayIsSafelyStopped,
  relayViewModel,
  safeDisplayString,
  sanitizeSrtUrl,
  sortNodesForDisplay,
  transientPollDelay,
  wipeSecretObject,
} = require("../../app/static/servers.js");

const root = path.resolve(__dirname, "../..");
const baseTemplate = fs.readFileSync(path.join(root, "app/templates/base.html"), "utf8");
const template = fs.readFileSync(path.join(root, "app/templates/servers.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app/static/servers.js"), "utf8");
const styles = fs.readFileSync(path.join(root, "app/static/styles.css"), "utf8");

test("server onboarding has no operator-selected OS or package manager", () => {
  const dialog = template.match(/<dialog[\s\S]*?<\/dialog>/)?.[0] || "";
  const inputNames = [...dialog.matchAll(/<input name="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(inputNames, [
    "address",
    "username",
    "password",
    "port",
    "expected_host_fingerprint",
  ]);
  assert.doesNotMatch(dialog, /<select|name="(?:os|distribution|package_manager|docker_mode)"/i);
});

test("bootstrap payload accepts only a bounded numeric SSH port", () => {
  assert.deepEqual(
    buildBootstrapPayload({
      address: " example.test ",
      username: " root ",
      password: "temporary",
      port: "22",
      expected_host_fingerprint: "",
    }),
    {
      address: "example.test",
      username: "root",
      password: "temporary",
      port: 22,
      expected_host_fingerprint: null,
    },
  );
  assert.throws(() => buildBootstrapPayload({ port: "0" }), TypeError);
  assert.throws(() => buildBootstrapPayload({ port: "65536" }), TypeError);
  assert.throws(() => buildBootstrapPayload({ port: "22.5" }), TypeError);
});

test("polling has a finite fifteen-minute budget and terminal states", () => {
  const budget = createPollBudget(2);
  assert.equal(budget.consume(), true);
  assert.equal(budget.consume(), true);
  assert.equal(budget.consume(), false);
  assert.equal(budget.remaining, 0);
  assert.equal(MAX_POLL_ATTEMPTS, 600);
  assert.equal(MAX_COMMAND_POLL_ATTEMPTS, 40);
  assert.equal(MAX_TRANSIENT_POLL_FAILURES, 5);
  assert.deepEqual([...TERMINAL_JOB_STATES].sort(), ["cancelled", "completed", "failed"]);
});

test("transient polling retries are bounded and back off", () => {
  assert.equal(transientPollDelay(1), 1500);
  assert.equal(transientPollDelay(2), 3000);
  assert.equal(transientPollDelay(4), 12000);
  assert.equal(transientPollDelay(5), 12000);
  assert.equal(transientPollDelay(6), null);
  assert.equal(transientPollDelay(0), null);
});

test("unknown node states fail closed and display strings remove controls", () => {
  assert.equal(normalizeNodeStatus("ready"), "ready");
  assert.equal(normalizeNodeStatus("unexpected"), "offline");
  assert.equal(safeDisplayString("server\n01\u0000"), "server 01");
});

test("password fields use browser-safe attributes and are cleared after acceptance", () => {
  const passwordFields = template.match(/<input[^>]+type="password"[^>]*>/g) || [];
  assert.equal(passwordFields.length, 4);
  for (const field of passwordFields) {
    assert.match(field, /autocomplete="new-password"/);
    assert.match(field, /spellcheck="false"/);
  }
  assert.match(source, /passwordInput\.value = ""/);
  assert.match(source, /payload\.password = ""/);
  assert.match(source, /sudoPayload\.sudo_password = ""/);
  assert.match(source, /sudoPassword = ""/);
});

test("YouTube payload accepts only RTMPS and compacts accidental whitespace", () => {
  assert.deepEqual(
    buildYouTubeConfigPayload({
      url: "  rtmps://example.test/live2\r\n",
      stream_key: " abcd-efgh\n-ijkl ",
      admin_password: "must-not-be-sent",
    }),
    {
      url: "rtmps://example.test/live2",
      stream_key: "abcd-efgh-ijkl",
    },
  );
  assert.throws(
    () => buildYouTubeConfigPayload({ url: "rtmp://example.test/live2", stream_key: "x" }),
    TypeError,
  );
  assert.throws(
    () => buildYouTubeConfigPayload({ url: "rtmps://example.test/live2#bad", stream_key: "x" }),
    TypeError,
  );
  assert.throws(
    () => buildYouTubeConfigPayload({ url: "rtmps://example.test/live2", stream_key: " \n" }),
    TypeError,
  );
  assert.deepEqual(buildAdminPasswordPayload({ admin_password: "secret" }), { admin_password: "secret" });
});

test("relay status helpers fail closed and never accept injected SRT output", () => {
  assert.equal(hasMoblinRelayCapability({ capabilities: ["ping", "moblin_relay"] }), true);
  assert.equal(hasMoblinRelayCapability({ capabilities: ["ping"] }), false);
  assert.equal(sanitizeSrtUrl("srt://relay.example:8890?streamid=publish:test"), "srt://relay.example:8890?streamid=publish:test");
  assert.equal(sanitizeSrtUrl("javascript:alert(1)"), "");
  assert.equal(sanitizeSrtUrl("srt://relay.example/\n<img src=x>"), "");
  assert.deepEqual(
    normalizeRelayStatus({
      available: true,
      status: {
        service: "active",
        source: "live",
        youtube_url_configured: true,
        youtube_key_configured: true,
        portrait_profile: true,
      },
    }),
    {
      available: true,
      service: "active",
      enabled: false,
      mainProcess: "unknown",
      srtListener: "unknown",
      source: "LIVE",
      youtubeForward: "unknown",
      overall: "unknown",
      youtubeUrlConfigured: true,
      youtubeKeyConfigured: true,
      portraitProfile: true,
      errorCode: null,
      lastSeenAt: "",
    },
  );
  assert.equal(normalizeRelayStatus({ available: true, status: { service: "<img>" } }).service, "unknown");
  const stoppedRelay = normalizeRelayStatus({
    available: true,
    status: { service: "inactive", main_process: "stopped", overall: "offline" },
  });
  assert.equal(relayIsOperable(stoppedRelay), true);
  assert.equal(relayIsSafelyStopped(stoppedRelay), true);
  assert.equal(
    relayIsSafelyStopped(normalizeRelayStatus({
      available: true,
      status: { service: "inactive", main_process: "running" },
    })),
    false,
  );
  assert.equal(
    relayIsSafelyStopped(normalizeRelayStatus({
      available: true,
      status: { service: "unknown", main_process: "stopped" },
    })),
    false,
  );
  assert.equal(relayIsOperable(normalizeRelayStatus({ available: false, status: { overall: "healthy" } })), false);
  assert.equal(relayCommandOutcome({ state: "completed", completion_status: "ok" }), "success");
  assert.equal(relayCommandOutcome({ state: "completed", completion_status: "conflict" }), "conflict");
  assert.equal(relayCommandOutcome({ state: "completed" }), "failed");
  assert.equal(relayCommandOutcome({ state: "acknowledged" }), "pending");
});

test("relay presentation separates server connection, stopped state, Moblin, and YouTube", () => {
  assert.deepEqual(nodeStatusPresentation("ready", true), {
    status: "ready",
    label: "Агент на связи",
    tone: "success",
  });
  assert.deepEqual(
    sortNodesForDisplay([
      { id: "generic-a", capabilities: [] },
      { id: "relay", capabilities: ["moblin_relay"] },
      { id: "generic-b", capabilities: [] },
    ]).map((node) => node.id),
    ["relay", "generic-a", "generic-b"],
  );

  const stopped = normalizeRelayStatus({
    available: true,
    status: {
      service: "inactive",
      main_process: "stopped",
      srt_listener: "closed",
      source: "NONE",
      youtube_forward: "inactive",
      overall: "ok",
      youtube_url_configured: true,
      youtube_key_configured: true,
      portrait_profile: true,
    },
  });
  const stoppedView = relayViewModel(stopped);
  assert.equal(stopped.source, "NONE");
  assert.equal(stoppedView.badgeLabel, "Готов к запуску");
  assert.equal(stoppedView.relayStateLabel, "Штатно остановлен");
  assert.equal(stoppedView.moblinLabel, "Запустится вместе с relay");
  assert.equal(stoppedView.youtubeLabel, "Настроен · отправка остановлена");
  assert.equal(stoppedView.startDisabled, false);
  assert.equal(stoppedView.stopDisabled, true);
  assert.equal(stoppedView.configureDisabled, false);

  const unavailableView = relayViewModel(normalizeRelayStatus({
    available: false,
    status: { service: "inactive", main_process: "stopped", overall: "offline" },
  }));
  assert.equal(unavailableView.badgeLabel, "Нет связи с сервером");
  assert.equal(unavailableView.startDisabled, true);
  assert.equal(unavailableView.stopDisabled, true);
  assert.equal(unavailableView.configureDisabled, true);

  const liveView = relayViewModel(normalizeRelayStatus({
    available: true,
    status: {
      service: "active",
      main_process: "running",
      srt_listener: "listening",
      source: "LIVE",
      youtube_forward: "active",
      overall: "healthy",
      youtube_url_configured: true,
      youtube_key_configured: true,
      portrait_profile: true,
    },
  }));
  assert.equal(liveView.badgeLabel, "Relay запущен");
  assert.equal(liveView.moblinLabel, "Поток поступает");
  assert.equal(liveView.youtubeLabel, "Поток отправляется");
  assert.equal(liveView.stopDisabled, false);
  assert.equal(liveView.configureDisabled, true);

  for (const unsafeStatus of [
    {
      service: "inactive",
      main_process: "stopped",
      srt_listener: "closed",
      source: "NONE",
      youtube_forward: "inactive",
      overall: "degraded",
      youtube_url_configured: true,
      youtube_key_configured: true,
      portrait_profile: true,
    },
    {
      service: "inactive",
      main_process: "stopped",
      srt_listener: "closed",
      source: "NONE",
      youtube_forward: "active",
      overall: "offline",
      youtube_url_configured: true,
      youtube_key_configured: true,
      portrait_profile: true,
    },
  ]) {
    const unsafeView = relayViewModel(normalizeRelayStatus({ available: true, status: unsafeStatus }));
    assert.equal(unsafeView.badgeLabel, "Нужна проверка");
    assert.equal(unsafeView.startDisabled, true);
    assert.equal(unsafeView.configureDisabled, true);
    assert.equal(unsafeView.clearDisabled, true);
  }

  const wrongProfileView = relayViewModel(normalizeRelayStatus({
    available: true,
    status: {
      service: "inactive",
      main_process: "stopped",
      srt_listener: "closed",
      source: "NONE",
      youtube_forward: "inactive",
      overall: "ok",
      youtube_url_configured: true,
      youtube_key_configured: true,
      portrait_profile: false,
    },
  }));
  assert.equal(wrongProfileView.badgeLabel, "Нужна проверка");
  assert.equal(wrongProfileView.startDisabled, true);
  assert.equal(wrongProfileView.configureDisabled, false);
});

test("relay secrets and revealed URLs have explicit cleanup helpers", () => {
  const secretField = { value: "stream-secret" };
  const outputField = { value: "srt://secret-url" };
  clearSensitiveFields({ querySelectorAll: () => [secretField, outputField] });
  assert.equal(secretField.value, "");
  assert.equal(outputField.value, "");

  const payload = {
    url: "rtmps://safe-endpoint/live2",
    stream_key: "stream-secret",
    admin_password: "admin-secret",
    public_url: "srt://public-secret",
    vpn_url: "srt://vpn-secret",
  };
  wipeSecretObject(payload);
  assert.deepEqual(payload, {
    url: "rtmps://safe-endpoint/live2",
    stream_key: "",
    admin_password: "",
    public_url: "",
    vpn_url: "",
  });
});

test("late secret responses are rejected after close, navigation, or node change", () => {
  assert.equal(isCurrentSecretRequest("node-a", "node-a", 7, 7, true), true);
  assert.equal(isCurrentSecretRequest("node-a", "node-a", 7, 8, true), false);
  assert.equal(isCurrentSecretRequest("node-a", "node-b", 7, 7, true), false);
  assert.equal(isCurrentSecretRequest("node-a", "node-a", 7, 7, false), false);
  assert.equal(createRelayIdempotencyKey("reveal-moblin", "12345678-abcd"), "ui:reveal-moblin:12345678-abcd");
  assert.equal(createRelayIdempotencyKey("BAD ACTION", "12345678-abcd"), null);
});

test("server rendering never uses HTML injection or browser persistence", () => {
  assert.doesNotMatch(source, /innerHTML/);
  assert.doesNotMatch(source, /localStorage/);
  assert.doesNotMatch(source, /sessionStorage/);
  assert.match(source, /\.textContent = text/);
  assert.match(source, /replaceChildren/);
});

test("relay UI never persists or displays YouTube secrets", () => {
  const youtubeConfigForm = template.match(/<form[^>]+data-youtube-config-form[\s\S]*?<\/form>/)?.[0] || "";
  const youtubeClearForm = template.match(/<form[^>]+data-youtube-clear-form[\s\S]*?<\/form>/)?.[0] || "";
  const moblinUrlForm = template.match(/<form[^>]+data-moblin-url-form[\s\S]*?<\/form>/)?.[0] || "";
  assert.match(template, /data-youtube-config-form[^>]+method="dialog"/);
  assert.match(template, /data-youtube-config-form[^>]+autocomplete="off"/);
  assert.match(template, /name="stream_key"[\s\S]*?type="password"[\s\S]*?autocomplete="new-password"/);
  assert.doesNotMatch(youtubeConfigForm, /name="admin_password"/);
  assert.doesNotMatch(moblinUrlForm, /name="admin_password"/);
  assert.match(youtubeClearForm, /name="admin_password"[\s\S]*?type="password"[\s\S]*?autocomplete="new-password"/);
  assert.match(template, /data-sensitive-output/);
  assert.match(template, /data-youtube-clear-form[^>]+autocomplete="off"/);
  assert.match(template, /data-youtube-clear-form[^>]+method="dialog"/);
  assert.match(template, /data-moblin-url-form[^>]+method="dialog"/);
  assert.match(template, /data-youtube-config-dialog aria-labelledby="youtube-config-title"/);
  assert.match(template, /data-youtube-clear-dialog aria-labelledby="youtube-clear-title"/);
  assert.match(template, /data-moblin-url-dialog aria-labelledby="moblin-url-title"/);
  assert.match(template, /Relay должен быть остановлен/);
  assert.match(
    template,
    /Settings → Streams → профиль → SRT\(LA\) → Implementation \/ Реализация → Official/,
  );
  assert.match(template, /Latency: 2000 ms/);
  assert.match(template, /Big packets: ON/);
  assert.match(template, /SRT URL скопируйте целиком в поле URL и не редактируйте/);
  assert.match(template, /прямым YouTube RTMP URL, нажмите No/);
  assert.doesNotMatch(source, /\.innerHTML\s*=/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB/);
  assert.match(source, /wipeSecretObject\(payload\)/);
  assert.match(source, /wipeSecretObject\(response\)/);
  assert.match(source, /let requestBody = "\{\}";/);
  assert.doesNotMatch(source, /data-moblin-admin-password|data-youtube-admin-password|moblinReauth/);
  assert.doesNotMatch(source, /Проверьте RTMPS URL, stream key и пароль администратора/);
  assert.match(source, /window\.addEventListener\("pagehide"/);
  assert.match(source, /if \(!requestIsCurrent\(\)\) return;/);
  assert.match(source, /serverErrorCode\(payload\) !== "step_up_failed"/);
  assert.doesNotMatch(source, /console\.(?:log|debug|info|warn|error)/);
});

test("relay controls use safe endpoints and explicit start-stop confirmation", () => {
  assert.match(source, /\/relay\/configure-youtube/);
  assert.match(source, /\/relay\/reveal-moblin-url/);
  assert.match(source, /method: "DELETE"/);
  assert.match(source, /\/relay\/youtube/);
  assert.match(source, /\/relay\/\$\{encodeURIComponent\(action\)\}/);
  assert.match(source, /\/relay\/commands\/\$\{encodeURIComponent\(commandId\)\}/);
  assert.doesNotMatch(source, /waitForCommand\(nodeId, commandId\)[\s\S]{0,100}Relay/);
  assert.match(source, /window\.confirm\(confirmations\[action\]\)/);
  assert.match(source, /Это немедленно прервёт отправку потока/);
  assert.match(source, /!publicUrl && !vpnUrl && typeof response\?\.command_id === "string"/);
  assert.match(source, /createRelayIdempotencyKey\("reveal-moblin"\)/);
  assert.match(source, /waitForRelayCommand\(nodeId, response\.command_id, \{ isCurrent: requestIsCurrent \}\)/);
  assert.match(source, /`\$\{endpoint\}\?wait=0`/);
  assert.match(source, /SRT URL готовится/);
  assert.match(source, /youtubeUrlConfigured \? "Настроен" : "Не настроен"/);
  assert.match(source, /youtubeKeyConfigured \? "Настроен" : "Не настроен"/);
  assert.match(source, /if \(completed\) \{\s*showToast\("YouTube настроен"/);
  assert.match(source, /if \(completed\) \{\s*showToast\("YouTube очищен"/);
  assert.doesNotMatch(source, /setRelayMetric\([^\n]+stream[_ -]?key[^\n]+status\.stream/i);
});

test("self-test action is enabled only for operable node states", () => {
  assert.match(source, /if \(!relayCapable\) \{[\s\S]{0,240}"Проверить сервер"/);
  assert.match(
    source,
    /selfTest\.disabled = !\["ready", "degraded", "offline"\]\.includes\(status\)/,
  );
  assert.match(source, /control_latency_ms/);
  assert.match(source, /metricRowIfPresent\(metrics, "Связь с панелью", controlLatencyLabel\)/);
  assert.equal(canRevokeNode("connecting", true), true);
  assert.equal(canRevokeNode("connecting", false), false);
  assert.equal(canRevokeNode("ready", false), true);
  assert.equal(canRevokeNode("revoked", true), false);
  assert.match(source, /revoke\.disabled = !canRevokeNode\(status, relayCapable\)/);
});

test("UI includes progress, sudo, revoke confirmation, and mobile layout", () => {
  assert.match(template, /data-install-steps/);
  assert.match(template, /data-sudo-password/);
  assert.match(template, /data-revoke-dialog/);
  assert.match(template, /Серверы и диагностика/);
  assert.match(source, /Агент на связи/);
  assert.match(styles, /@media \(max-width: 680px\)/);
  assert.match(styles, /\.server-grid \{[\s\S]{0,140}align-items: start/);
  assert.match(styles, /\.server-card--relay \{[\s\S]{0,80}grid-column: 1 \/ -1/);
  assert.match(styles, /\.server-card \{[\s\S]{0,180}align-content: start/);
  assert.match(styles, /\.relay-summary \{[\s\S]{0,180}grid-template-columns: repeat\(4/);
  assert.match(styles, /\.servers__heading > \.button \{[\s\S]{0,80}display: inline-flex/);
  assert.match(source, /Получить SRT для Moblin/);
  assert.match(source, /Дополнительные действия/);
  assert.match(source, /data-relay-action-reason/);
  assert.match(source, /button\.disabled = true/);
  assert.match(baseTemplate, /styles\.css[^"\n]*\?v=20260902\.3/);
  assert.match(baseTemplate, /app\.js[^"\n]*\?v=20260902\.3/);
  assert.match(template, /servers\.js\?v=20260903\.1/);
  assert.match(source, /result\.safe_result\?\.status === "ok"/);
  assert.match(source, /Docker может остаться установленным на сервере/);
});

test("page reload discovers and resumes the active bootstrap including sudo pause", () => {
  assert.match(source, /apiRequest\("\/api\/nodes\/bootstrap\/active"\)/);
  assert.match(source, /activeJobId = jobId/);
  assert.match(source, /openDialog\(progressDialog\)/);
  assert.match(source, /state !== "needs_sudo_password"/);
  assert.match(source, /Связь с сервисом установки прервана\. Повторяем проверку/);
  assert.match(source, /активная задача будет восстановлена/);
});
