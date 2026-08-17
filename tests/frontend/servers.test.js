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
  buildBootstrapPayload,
  createPollBudget,
  normalizeNodeStatus,
  safeDisplayString,
  transientPollDelay,
} = require("../../app/static/servers.js");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(path.join(root, "app/templates/servers.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app/static/servers.js"), "utf8");
const styles = fs.readFileSync(path.join(root, "app/static/styles.css"), "utf8");

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
  assert.equal(passwordFields.length, 2);
  for (const field of passwordFields) {
    assert.match(field, /autocomplete="new-password"/);
    assert.match(field, /spellcheck="false"/);
  }
  assert.match(source, /passwordInput\.value = ""/);
  assert.match(source, /payload\.password = ""/);
  assert.match(source, /sudoPayload\.sudo_password = ""/);
  assert.match(source, /sudoPassword = ""/);
});

test("server rendering never uses HTML injection or browser persistence", () => {
  assert.doesNotMatch(source, /innerHTML/);
  assert.doesNotMatch(source, /localStorage/);
  assert.doesNotMatch(source, /sessionStorage/);
  assert.match(source, /\.textContent = text/);
  assert.match(source, /replaceChildren/);
});

test("self-test action is enabled only for operable node states", () => {
  assert.match(
    source,
    /selfTest\.disabled = !\["ready", "degraded", "offline"\]\.includes\(status\)/,
  );
  assert.match(source, /control_latency_ms/);
  assert.match(source, /metricRow\(metrics, "Связь с панелью", controlLatencyLabel\)/);
  assert.match(
    source,
    /revoke\.disabled = !\["ready", "degraded", "offline"\]\.includes\(status\)/,
  );
});

test("UI includes progress, sudo, revoke confirmation, and mobile layout", () => {
  assert.match(template, /data-install-steps/);
  assert.match(template, /data-sudo-password/);
  assert.match(template, /data-revoke-dialog/);
  assert.match(source, /Готов к назначению/);
  assert.match(styles, /@media \(max-width: 680px\)/);
  assert.match(styles, /\.server-grid/);
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
