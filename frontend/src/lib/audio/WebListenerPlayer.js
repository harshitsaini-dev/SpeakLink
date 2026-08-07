/* Public web listener: whole WebM Clusters -> MediaSource -> <audio>.
 *
 * WHY THIS IS NOT ReceiverPlayer
 *
 * The Store ReceiverPlayer appends whatever chunk arrives and trims when the
 * element has drifted past thirty seconds. That is right for a Store, whose
 * Receiver is fed raw broadcaster chunks and is expected to sit on a shop PC.
 *
 * A web listener is fed whole Clusters by the relay, must never accumulate
 * minutes of audio in a phone's memory, and has to report truthfully what it is
 * actually doing so the broadcaster's panel is not fiction. Those are different
 * enough that sharing one class would mean a flag on every method - so this is
 * a separate, smaller player, and the parts that genuinely are the same (the
 * mime, sequence mode, the append queue) are the same.
 *
 * It never touches a Store, an output device, or a volume. The listener's own
 * device volume is the only volume there is.
 */

export const LISTENER_MIME = "audio/webm;codecs=opus";

// A listener should stay near the live edge. Beyond this the browser is holding
// audio nobody will hear, on a device that may have very little memory.
const MAX_BUFFER_SECONDS = 20;
const TRIM_TO_SECONDS = 8;

export const ListenerPlaybackState = {
  CONNECTING: "CONNECTING",
  READY_TO_PLAY: "READY_TO_PLAY",
  BUFFERING: "BUFFERING",
  LISTENING: "LISTENING",
  PAUSED: "PAUSED",
  ERROR: "ERROR",
};

export class WebListenerPlayer {
  constructor(audioElement, { onState, onError } = {}) {
    this.audio = audioElement;
    this.mediaSource = null;
    this.sourceBuffer = null;
    this.queue = [];
    this.opened = false;
    this.onState = onState || (() => {});
    this.onError = onError || (() => {});
    this.state = ListenerPlaybackState.CONNECTING;
  }

  static supported() {
    return !!(window.MediaSource && window.MediaSource.isTypeSupported
              && window.MediaSource.isTypeSupported(LISTENER_MIME));
  }

  _setState(next) {
    if (this.state === next) return;
    this.state = next;
    this.onState(next);
  }

  attach() {
    if (!WebListenerPlayer.supported()) {
      this.onError("This browser cannot play EchoCast audio.");
      this._setState(ListenerPlaybackState.ERROR);
      return false;
    }
    this.mediaSource = new MediaSource();
    this.audio.src = URL.createObjectURL(this.mediaSource);

    this.mediaSource.addEventListener("sourceopen", () => {
      try {
        this.sourceBuffer = this.mediaSource.addSourceBuffer(LISTENER_MIME);
        // The listener joined at whatever point they joined, so their timeline
        // starts at zero rather than thirty seconds into a Broadcast they did
        // not hear.
        this.sourceBuffer.mode = "sequence";
        this.sourceBuffer.addEventListener("updateend", () => this._flush());
        this.opened = true;
        this._flush();
      } catch (error) {
        this.onError("Could not start audio: " + error.message);
        this._setState(ListenerPlaybackState.ERROR);
      }
    }, { once: true });

    // Playback state is read from real media events rather than assumed. The
    // broadcaster's panel shows this, and a panel that says Listening about a
    // browser that never started is worse than one that says nothing.
    this.audio.addEventListener("playing", () =>
      this._setState(ListenerPlaybackState.LISTENING));
    this.audio.addEventListener("waiting", () =>
      this._setState(ListenerPlaybackState.BUFFERING));
    this.audio.addEventListener("stalled", () =>
      this._setState(ListenerPlaybackState.BUFFERING));
    this.audio.addEventListener("pause", () =>
      this._setState(ListenerPlaybackState.PAUSED));
    return true;
  }

  /** Try to start. Returns false when the browser refused - not when it failed. */
  async play() {
    try {
      await this.audio.play();
      return true;
    } catch (error) {
      // An autoplay refusal is not an error and must not be reported as one.
      // The listener is asked for a tap, and until they give it the state is
      // READY_TO_PLAY - never LISTENING.
      this._setState(ListenerPlaybackState.READY_TO_PLAY);
      return false;
    }
  }

  pushCluster(data) {
    if (!data || !data.byteLength) return;
    this.queue.push(new Uint8Array(data));
    this._flush();
  }

  _flush() {
    if (!this.opened || !this.sourceBuffer || this.sourceBuffer.updating) return;

    // Trim before appending, so memory is bounded by time rather than by how
    // long the listener has had the tab open.
    try {
      const buffered = this.sourceBuffer.buffered;
      if (buffered.length) {
        const start = buffered.start(0);
        const end = buffered.end(buffered.length - 1);
        if (end - start > MAX_BUFFER_SECONDS) {
          const removeUntil = Math.min(end - TRIM_TO_SECONDS,
                                       this.audio.currentTime - 1);
          if (removeUntil > start) {
            this.sourceBuffer.remove(start, removeUntil);
            return;                       // updateend calls back in
          }
        }
      }
    } catch (ignored) {
      // A buffer that cannot be inspected is not a reason to stop playing.
    }

    if (!this.queue.length) return;
    try {
      this.sourceBuffer.appendBuffer(this.queue.shift());
    } catch (error) {
      if (error.name === "QuotaExceededError") {
        // Drop what has already been heard and try again on the next frame.
        try {
          this.sourceBuffer.remove(0, Math.max(0, this.audio.currentTime - 2));
        } catch (ignored) { /* nothing further to do */ }
        return;
      }
      this.onError("Audio buffer error: " + error.message);
      this._setState(ListenerPlaybackState.ERROR);
    }
  }

  detach() {
    try {
      if (this.mediaSource && this.mediaSource.readyState === "open") {
        this.mediaSource.endOfStream();
      }
    } catch (ignored) { /* already closed */ }
    try {
      this.audio.pause();
      this.audio.removeAttribute("src");
      this.audio.load();
    } catch (ignored) { /* already gone */ }
    this.mediaSource = null;
    this.sourceBuffer = null;
    this.queue = [];
    this.opened = false;
  }
}
