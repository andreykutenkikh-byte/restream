"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  formatLastSeen,
  pairingPayloadIsSafe,
} = require("../../app/static/moblin-hud-admin.js");

const source = fs.readFileSync(
  path.join(__dirname, "../../app/static/moblin-hud-admin.js"),
  "utf8",
);

test("admin pairing accepts fragment links and rejects query secrets", () => {
  assert.equal(pairingPayloadIsSafe({
    pairing_url: "https://restream.example/moblin-hud#pair=secret",
    moblin_url: "moblin://?encoded",
  }), true);
  assert.equal(pairingPayloadIsSafe({
    pairing_url: "https://restream.example/moblin-hud?pair=secret",
    moblin_url: "moblin://?encoded",
  }), false);
});

test("admin device age formatter is bounded and human readable", () => {
  const now = Date.parse("2026-09-04T12:00:30Z");
  assert.equal(formatLastSeen("2026-09-04T12:00:25Z", now), "Только что");
  assert.equal(formatLastSeen("2026-09-04T11:59:30Z", now), "1 мин. назад");
  assert.equal(formatLastSeen(null, now), "Ещё не открывался");
});

test("admin HUD renderer avoids credential storage and HTML sinks", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
  assert.doesNotMatch(source, /\.innerHTML\s*=/);
  assert.match(source, /\.textContent\s*=/);
  assert.match(source, /replaceChildren/);
});

test("admin template does not expose session or stream credentials", () => {
  const template = fs.readFileSync(
    path.join(__dirname, "../../app/templates/dashboard.html"),
    "utf8",
  );
  const section = template.split('data-moblin-hud-admin')[1];
  assert.ok(section);
  assert.doesNotMatch(section.split("</section>")[0], /stream[_ -]?key|ssh|srt:\/\//i);
});
