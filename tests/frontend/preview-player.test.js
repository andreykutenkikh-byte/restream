"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { IngestPreviewController } = require("../../app/static/preview-player.js");
const VendoredHls = require("../../app/static/vendor/hls.min.js");

class FakeVideo {
  constructor({ nativeHls = false, playRejects = false } = {}) {
    this.nativeHls = nativeHls;
    this.playRejects = playRejects;
    this.listeners = new Map();
    this.attributes = new Map();
    this.src = "";
    this.muted = false;
    this.videoWidth = 0;
    this.videoHeight = 0;
    this.playCalls = 0;
    this.pauseCalls = 0;
    this.loadCalls = 0;
  }

  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) || [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }

  dispatch(name) {
    for (const callback of this.listeners.get(name) || []) callback({ type: name });
  }

  canPlayType(type) {
    return this.nativeHls && type === "application/vnd.apple.mpegurl" ? "probably" : "";
  }

  play() {
    this.playCalls += 1;
    return this.playRejects ? Promise.reject(new Error("autoplay denied")) : Promise.resolve();
  }

  pause() {
    this.pauseCalls += 1;
  }

  load() {
    this.loadCalls += 1;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    if (name === "src" && this.src) return this.src;
    return this.attributes.get(name) || null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "src") this.src = "";
  }
}

class FakeHls {
  static Events = { ERROR: "error", MANIFEST_PARSED: "manifestParsed" };
  static ErrorTypes = { MEDIA_ERROR: "mediaError", NETWORK_ERROR: "networkError" };
  static instances = [];

  static isSupported() {
    return true;
  }

  constructor(config) {
    this.config = config;
    this.handlers = new Map();
    this.destroyCalls = 0;
    this.recoverCalls = 0;
    FakeHls.instances.push(this);
  }

  on(name, callback) {
    this.handlers.set(name, callback);
  }

  emit(name, data = {}) {
    this.handlers.get(name)?.(name, data);
  }

  loadSource(source) {
    this.source = source;
  }

  attachMedia(video) {
    this.video = video;
  }

  recoverMediaError() {
    this.recoverCalls += 1;
  }

  destroy() {
    this.destroyCalls += 1;
  }
}

class FakeTimers {
  constructor() {
    this.nextId = 1;
    this.callbacks = new Map();
    this.delays = [];
  }

  setTimeout(callback, delay) {
    const id = this.nextId;
    this.nextId += 1;
    this.callbacks.set(id, callback);
    this.delays.push(delay);
    return id;
  }

  clearTimeout(id) {
    this.callbacks.delete(id);
  }

  runNext() {
    const entry = this.callbacks.entries().next().value;
    if (!entry) return false;
    const [id, callback] = entry;
    this.callbacks.delete(id);
    callback();
    return true;
  }
}

function makeController({
  nativeHls = false,
  playRejects = false,
  retryDelays = [1, 2],
  onResolution = () => {},
} = {}) {
  FakeHls.instances = [];
  const video = new FakeVideo({ nativeHls, playRejects });
  const container = { dataset: {} };
  const timers = new FakeTimers();
  const controller = new IngestPreviewController({
    video,
    container,
    hlsClass: FakeHls,
    retryDelays,
    onResolution,
    setTimeoutFn: (callback, delay) => timers.setTimeout(callback, delay),
    clearTimeoutFn: (timer) => timers.clearTimeout(timer),
  });
  return { container, controller, timers, video };
}

test("vendored hls.js is pinned to the reviewed version", () => {
  assert.equal(VendoredHls.version, "1.6.16");
});

test("native Safari HLS uses the fixed same-origin URL once", () => {
  const { container, controller, video } = makeController({ nativeHls: true });

  controller.setStreamState("live");
  controller.setStreamState("live");

  assert.equal(video.src, "/api/ingest/preview/index.m3u8");
  assert.equal(video.playCalls, 1);
  assert.equal(FakeHls.instances.length, 0);
  assert.equal(container.dataset.previewState, "loading");
  video.dispatch("playing");
  assert.equal(container.dataset.previewState, "live");
});

test("autoplay rejection reveals standard controls for manual playback", async () => {
  const { container, controller, video } = makeController({
    nativeHls: true,
    playRejects: true,
  });

  controller.setStreamState("live");
  await Promise.resolve();

  assert.equal(video.playCalls, 1);
  assert.equal(container.dataset.previewState, "live");
  assert.equal(controller.streamState, "live");
});

test("hls.js player is not recreated by repeated live status polls", () => {
  const { controller, video } = makeController();

  controller.setStreamState("live");
  controller.setStreamState("unstable");
  controller.setStreamState("live");

  assert.equal(FakeHls.instances.length, 1);
  assert.equal(FakeHls.instances[0].source, "/api/ingest/preview/index.m3u8");
  assert.equal(FakeHls.instances[0].video, video);
  assert.equal(FakeHls.instances[0].config.enableWorker, false);
});

test("connecting does not create a player and offline destroys and clears it", () => {
  const { container, controller, video } = makeController();

  controller.setStreamState("connecting");
  assert.equal(FakeHls.instances.length, 0);
  assert.equal(container.dataset.previewState, "loading");

  controller.setStreamState("live");
  const hls = FakeHls.instances[0];
  video.src = "blob:test";
  controller.setStreamState("offline");

  assert.equal(hls.destroyCalls, 1);
  assert.equal(video.src, "");
  assert.equal(video.pauseCalls, 1);
  assert.equal(video.loadCalls, 1);
  assert.equal(container.dataset.previewState, "offline");
});

test("fatal media recovery is bounded and never changes ingest state", () => {
  const { container, controller } = makeController();
  controller.setStreamState("live");
  const hls = FakeHls.instances[0];

  hls.emit(FakeHls.Events.ERROR, { fatal: true, type: FakeHls.ErrorTypes.MEDIA_ERROR });
  assert.equal(hls.recoverCalls, 1);
  assert.equal(controller.streamState, "live");

  hls.emit(FakeHls.Events.ERROR, { fatal: true, type: FakeHls.ErrorTypes.MEDIA_ERROR });
  assert.equal(hls.destroyCalls, 1);
  assert.equal(container.dataset.previewState, "error");
  assert.equal(controller.streamState, "live");
});

test("fatal network retry has a finite backoff budget and manual retry resets it", () => {
  const { container, controller, timers } = makeController({ retryDelays: [10, 20] });
  controller.setStreamState("live");

  FakeHls.instances[0].emit(FakeHls.Events.ERROR, {
    fatal: true,
    type: FakeHls.ErrorTypes.NETWORK_ERROR,
  });
  assert.deepEqual(timers.delays, [10]);
  timers.runNext();

  FakeHls.instances[1].emit(FakeHls.Events.ERROR, {
    fatal: true,
    type: FakeHls.ErrorTypes.NETWORK_ERROR,
  });
  assert.deepEqual(timers.delays, [10, 20]);
  timers.runNext();

  FakeHls.instances[2].emit(FakeHls.Events.ERROR, {
    fatal: true,
    type: FakeHls.ErrorTypes.NETWORK_ERROR,
  });
  assert.equal(container.dataset.previewState, "error");
  assert.equal(timers.callbacks.size, 0);

  controller.setStreamState("live");
  assert.equal(FakeHls.instances.length, 3);
  assert.equal(controller.retry(), true);
  assert.equal(FakeHls.instances.length, 4);
});

test("offline invalidates a queued retry", () => {
  const { controller, timers } = makeController();
  controller.setStreamState("live");
  FakeHls.instances[0].emit(FakeHls.Events.ERROR, {
    fatal: true,
    type: FakeHls.ErrorTypes.NETWORK_ERROR,
  });

  controller.setStreamState("offline");

  assert.equal(timers.callbacks.size, 0);
  assert.equal(timers.runNext(), false);
  assert.equal(FakeHls.instances.length, 1);
});

test("metadata and resize events keep the actual LIVE resolution current", () => {
  const resolutions = [];
  const { controller, video } = makeController({
    onResolution: (width, height) => resolutions.push([width, height]),
  });
  controller.setStreamState("live");
  video.videoWidth = 1920;
  video.videoHeight = 1080;

  video.dispatch("loadedmetadata");
  video.videoWidth = 1080;
  video.videoHeight = 1920;
  video.dispatch("resize");
  controller.setStreamState("offline");

  assert.deepEqual(resolutions, [
    [1920, 1080],
    [1080, 1920],
    [null, null],
  ]);
});

test("suspension tears down immediately and ignores stale live updates", () => {
  const { controller } = makeController();
  controller.setStreamState("live");
  const first = FakeHls.instances[0];

  controller.suspend();
  controller.setStreamState("live");

  assert.equal(first.destroyCalls, 1);
  assert.equal(FakeHls.instances.length, 1);
  controller.resume();
  controller.setStreamState("live");
  assert.equal(FakeHls.instances.length, 2);
});
