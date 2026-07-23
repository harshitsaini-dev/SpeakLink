/* Store Receiver player: appends incoming webm/opus chunks into MediaSource
   attached to an <audio> element. Requires a user gesture to unlock playback. */

export class ReceiverPlayer {
  constructor(audioEl, { onStatus, onError } = {}) {
    this.audio = audioEl;
    this.mediaSource = null;
    this.sourceBuffer = null;
    this.queue = [];
    this.mime = 'audio/webm;codecs=opus';
    this.onStatus = onStatus || (() => {});
    this.onError = onError || (() => {});
    this._opened = false;
  }

  supported() {
    return !!window.MediaSource && MediaSource.isTypeSupported(this.mime);
  }

  attach() {
    if (!this.supported()) {
      this.onError("MediaSource / opus not supported in this browser.");
      return false;
    }
    this.mediaSource = new MediaSource();
    this.audio.src = URL.createObjectURL(this.mediaSource);
    this.mediaSource.addEventListener("sourceopen", () => {
      try {
        this.sourceBuffer = this.mediaSource.addSourceBuffer(this.mime);
        this.sourceBuffer.mode = "sequence";
        this.sourceBuffer.addEventListener("updateend", () => this._flush());
        this._opened = true;
        this._flush();
      } catch (e) {
        this.onError("Failed to init audio buffer: " + e.message);
      }
    });
    return true;
  }

  async play() {
    try { await this.audio.play(); this.onStatus("playing"); }
    catch (e) { this.onError("Autoplay blocked: " + e.message); }
  }

  pushChunk(arrayBuffer) {
    if (!arrayBuffer || arrayBuffer.byteLength === 0) return;
    this.queue.push(new Uint8Array(arrayBuffer));
    this._flush();
  }

  _flush() {
    if (!this._opened || !this.sourceBuffer) return;
    if (this.sourceBuffer.updating) return;
    if (this.queue.length === 0) return;
    // Trim old buffered ranges to avoid unbounded memory
    try {
      const buffered = this.sourceBuffer.buffered;
      if (buffered.length > 0 && this.audio.currentTime > 30) {
        const removeUntil = this.audio.currentTime - 20;
        if (removeUntil > buffered.start(0)) {
          this.sourceBuffer.remove(buffered.start(0), removeUntil);
          return; // updateend will call _flush
        }
      }
    } catch { /* noop */ }
    try {
      const chunk = this.queue.shift();
      this.sourceBuffer.appendBuffer(chunk);
    } catch (e) {
      // Reset on quota
      if (e.name === "QuotaExceededError") {
        try { this.sourceBuffer.remove(0, this.audio.currentTime - 5); } catch { /* */ }
      } else {
        this.onError("Buffer error: " + e.message);
      }
    }
  }

  reset() {
    this.queue = [];
    try {
      if (this.sourceBuffer && !this.sourceBuffer.updating) {
        const b = this.sourceBuffer.buffered;
        if (b.length > 0) this.sourceBuffer.remove(b.start(0), b.end(b.length - 1));
      }
    } catch { /* noop */ }
  }

  detach() {
    try { if (this.mediaSource && this.mediaSource.readyState === "open") this.mediaSource.endOfStream(); } catch { /* */ }
    try { this.audio.pause(); this.audio.removeAttribute("src"); this.audio.load(); } catch { /* */ }
    this.mediaSource = null; this.sourceBuffer = null; this.queue = []; this._opened = false;
  }
}
