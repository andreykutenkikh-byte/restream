((root, factory) => {
  "use strict";

  const exports = factory();
  if (typeof module === "object" && module.exports) module.exports = exports;
  if (root) root.IngestPreviewController = exports.IngestPreviewController;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  const PLAYABLE_STATES = new Set(["live", "unstable"]);
  const PREVIEW_STATES = new Set(["offline", "loading", "live", "error"]);
  const DEFAULT_RETRY_DELAYS = Object.freeze([1000, 2000, 4000]);

  class IngestPreviewController {
    constructor({
      video,
      container,
      hlsClass = null,
      sourceUrl = "/api/ingest/preview/index.m3u8",
      onResolution = () => {},
      onStateChange = () => {},
      setTimeoutFn = (callback, delay) => globalThis.setTimeout(callback, delay),
      clearTimeoutFn = (timer) => globalThis.clearTimeout(timer),
      retryDelays = DEFAULT_RETRY_DELAYS,
      mediaRecoveryLimit = 1,
      hlsConfig = {},
    }) {
      if (!video || !container) throw new TypeError("video and container are required");
      if (!Array.isArray(retryDelays) || retryDelays.some((delay) => delay < 0)) {
        throw new TypeError("retryDelays must contain non-negative delays");
      }

      this.video = video;
      this.container = container;
      this.hlsClass = hlsClass;
      this.sourceUrl = sourceUrl;
      this.onResolution = onResolution;
      this.onStateChange = onStateChange;
      this.setTimeoutFn = setTimeoutFn;
      this.clearTimeoutFn = clearTimeoutFn;
      this.retryDelays = [...retryDelays];
      this.mediaRecoveryLimit = Math.max(0, Number(mediaRecoveryLimit) || 0);
      this.hlsConfig = { ...hlsConfig };

      this.hls = null;
      this.mode = null;
      this.retryTimer = null;
      this.retryGeneration = 0;
      this.networkRetryCount = 0;
      this.mediaRecoveryCount = 0;
      this.blockedAfterError = false;
      this.suspended = false;
      this.streamState = "offline";
      this.previewState = "offline";

      this.handlePlaying = () => {
        if (!this.suspended && PLAYABLE_STATES.has(this.streamState) && this.mode) {
          this.setPreviewState("live");
        }
      };
      this.reportResolution = () => {
        if (this.suspended || !PLAYABLE_STATES.has(this.streamState) || !this.mode) return;
        const width = Number(this.video.videoWidth);
        const height = Number(this.video.videoHeight);
        if (Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0) {
          this.onResolution(Math.round(width), Math.round(height));
        }
      };
      this.handleLoadedMetadata = () => {
        if (this.suspended || !PLAYABLE_STATES.has(this.streamState) || !this.mode) return;
        this.reportResolution();
        // Metadata proves that the same-origin source is playable. Reveal the
        // native controls even when the browser declined muted autoplay.
        this.setPreviewState("live");
      };
      this.handleResize = () => this.reportResolution();
      this.handleNativeError = () => {
        if (this.mode === "native" && PLAYABLE_STATES.has(this.streamState)) {
          this.scheduleNetworkRetry();
        }
      };

      this.video.addEventListener("playing", this.handlePlaying);
      this.video.addEventListener("loadedmetadata", this.handleLoadedMetadata);
      this.video.addEventListener("resize", this.handleResize);
      this.video.addEventListener("error", this.handleNativeError);
      this.video.muted = true;
      this.setPreviewState("offline");
    }

    setPreviewState(state) {
      const normalized = PREVIEW_STATES.has(state) ? state : "error";
      if (this.previewState === normalized && this.container.dataset.previewState === normalized) {
        return;
      }
      this.previewState = normalized;
      this.container.dataset.previewState = normalized;
      this.onStateChange(normalized);
    }

    setStreamState(state) {
      if (this.suspended) return;
      const normalized = String(state || "offline").toLowerCase();
      const wasPlayable = PLAYABLE_STATES.has(this.streamState);
      const isPlayable = PLAYABLE_STATES.has(normalized);
      this.streamState = normalized;

      if (!isPlayable) {
        const previewState = normalized === "connecting" ? "loading" : "offline";
        this.stopMedia({ previewState, resetBudget: true, clearResolution: true });
        return;
      }

      if (!wasPlayable) {
        this.resetRetryBudget();
        this.video.muted = true;
      }
      this.ensureStarted();
    }

    ensureStarted() {
      if (
        this.suspended ||
        !PLAYABLE_STATES.has(this.streamState) ||
        this.mode ||
        this.retryTimer !== null ||
        this.blockedAfterError
      ) {
        return;
      }

      this.setPreviewState("loading");
      const nativeHls = this.video.canPlayType?.("application/vnd.apple.mpegurl");
      if (nativeHls) {
        this.mode = "native";
        this.video.src = this.sourceUrl;
        this.tryPlay();
        return;
      }

      if (!this.hlsClass || typeof this.hlsClass.isSupported !== "function" || !this.hlsClass.isSupported()) {
        this.failPermanently();
        return;
      }

      let hls;
      try {
        hls = new this.hlsClass({
          enableWorker: false,
          manifestLoadingMaxRetry: 1,
          manifestLoadingRetryDelay: 500,
          manifestLoadingMaxRetryTimeout: 2000,
          levelLoadingMaxRetry: 1,
          levelLoadingRetryDelay: 500,
          fragLoadingMaxRetry: 1,
          fragLoadingRetryDelay: 500,
          ...this.hlsConfig,
        });
      } catch (_error) {
        this.failPermanently();
        return;
      }

      this.hls = hls;
      this.mode = "hls";
      const events = this.hlsClass.Events || {};
      if (events.ERROR) {
        hls.on(events.ERROR, (_event, data) => {
          if (this.hls !== hls || !data?.fatal) return;
          const mediaError = this.hlsClass.ErrorTypes?.MEDIA_ERROR || "mediaError";
          if (data.type === mediaError || data.type === "mediaError") {
            this.handleFatalMediaError();
          } else {
            this.scheduleNetworkRetry();
          }
        });
      }
      if (events.MANIFEST_PARSED) {
        hls.on(events.MANIFEST_PARSED, () => {
          if (this.hls === hls) this.tryPlay();
        });
      }

      try {
        hls.loadSource(this.sourceUrl);
        hls.attachMedia(this.video);
      } catch (_error) {
        this.scheduleNetworkRetry();
      }
    }

    tryPlay() {
      const revealManualControls = () => {
        if (!this.suspended && PLAYABLE_STATES.has(this.streamState) && this.mode) {
          this.setPreviewState("live");
        }
      };
      try {
        const result = this.video.play();
        if (result && typeof result.catch === "function") result.catch(revealManualControls);
      } catch (_error) {
        revealManualControls();
      }
    }

    handleFatalMediaError() {
      if (!this.hls) return;
      if (this.mediaRecoveryCount < this.mediaRecoveryLimit) {
        this.mediaRecoveryCount += 1;
        this.setPreviewState("loading");
        try {
          this.hls.recoverMediaError();
          return;
        } catch (_error) {
          // Fall through to the bounded terminal state.
        }
      }
      this.failPermanently();
    }

    scheduleNetworkRetry() {
      if (this.suspended || !PLAYABLE_STATES.has(this.streamState)) return;
      this.stopMedia({ previewState: "loading", resetBudget: false, clearResolution: false });

      if (this.networkRetryCount >= this.retryDelays.length) {
        this.failPermanently();
        return;
      }

      const delay = this.retryDelays[this.networkRetryCount];
      this.networkRetryCount += 1;
      const generation = ++this.retryGeneration;
      this.retryTimer = this.setTimeoutFn(() => {
        this.retryTimer = null;
        if (
          generation !== this.retryGeneration ||
          this.suspended ||
          !PLAYABLE_STATES.has(this.streamState)
        ) {
          return;
        }
        this.ensureStarted();
      }, delay);
    }

    retry() {
      if (this.suspended || !PLAYABLE_STATES.has(this.streamState)) return false;
      this.cancelRetry();
      this.stopMedia({ previewState: "loading", resetBudget: false, clearResolution: false });
      this.resetRetryBudget();
      this.ensureStarted();
      return true;
    }

    resetRetryBudget() {
      this.networkRetryCount = 0;
      this.mediaRecoveryCount = 0;
      this.blockedAfterError = false;
    }

    cancelRetry() {
      this.retryGeneration += 1;
      if (this.retryTimer !== null) {
        this.clearTimeoutFn(this.retryTimer);
        this.retryTimer = null;
      }
    }

    failPermanently() {
      this.cancelRetry();
      this.stopMedia({ previewState: "error", resetBudget: false, clearResolution: false });
      this.blockedAfterError = true;
    }

    stopMedia({ previewState = "offline", resetBudget = true, clearResolution = true } = {}) {
      this.cancelRetry();
      const hadMedia = Boolean(this.mode || this.hls || this.video.getAttribute?.("src") || this.video.src);
      if (this.hls) {
        const hls = this.hls;
        this.hls = null;
        try {
          hls.destroy();
        } catch (_error) {
          // Teardown must continue even if a third-party cleanup hook fails.
        }
      }
      this.mode = null;
      if (hadMedia) {
        try {
          this.video.pause();
        } catch (_error) {
          // Continue clearing the media source.
        }
        this.video.removeAttribute("src");
        try {
          this.video.load();
        } catch (_error) {
          // Some test doubles and detached elements do not implement load().
        }
      }
      if (resetBudget) this.resetRetryBudget();
      if (clearResolution) this.onResolution(null, null);
      this.setPreviewState(previewState);
    }

    suspend(previewState = "offline") {
      this.suspended = true;
      this.streamState = "offline";
      this.stopMedia({ previewState, resetBudget: true, clearResolution: true });
    }

    resume() {
      this.suspended = false;
    }
  }

  return { IngestPreviewController };
});
