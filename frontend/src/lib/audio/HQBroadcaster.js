/* HQ Mic broadcaster: MediaRecorder -> WebSocket (binary chunks) */

export class HQBroadcaster {
  constructor({ wsUrl, onMeter, onStatus, onError }) {
    this.wsUrl = wsUrl;
    this.onMeter = onMeter || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onError = onError || (() => {});
    this.stream = null;
    this.recorder = null;
    this.ws = null;
    this.audioCtx = null;
    this.analyser = null;
    this.sentAnalyser = null;
    this.source = null;
    this.gainNode = null;
    this.destination = null;
    this.meterRAF = null;
    this._mime = null;
    // 0-100, the operator's chosen level. Kept separate from `_muted` so
    // unmuting restores it: folding mute into "volume 0" would lose the number
    // and unmute would have nothing to go back to.
    this._volumePercent = 100;
    this._muted = false;
  }

  /** 0-100 -> 0.0-1.0, and never above unity.
   *
   * The ceiling is 1.0 on purpose. Above it a gain node simply multiplies, so
   * anything already near full scale clips - and clipping a live announcement
   * is worse than a quiet one. Real make-up gain needs compression and
   * limiting, which is a later feature, not a wider slider.
   */
  _effectiveGain() {
    if (this._muted) return 0;
    const clamped = Math.max(0, Math.min(100, this._volumePercent));
    return clamped / 100;
  }

  get volumePercent() { return this._volumePercent; }

  get muted() { return this._muted; }

  /** True when nothing is reaching the Stores, whatever the reason. */
  get effectivelySilent() { return this._effectiveGain() === 0; }

  /** Set the microphone level, 0-100. Takes effect immediately. */
  setVolumePercent(percent) {
    const value = Number(percent);
    if (!Number.isFinite(value)) return;
    this._volumePercent = Math.max(0, Math.min(100, Math.round(value)));
    this._applyGain();
  }

  /** Mute or unmute WITHOUT touching the chosen level.
   *
   * Deliberately does not stop the recorder, the microphone track, the
   * WebSocket or the session. Muting by stopping the microphone would end the
   * broadcast, release the Store leases, and require a new one to resume -
   * an operator covering a cough would take the estate off air.
   */
  setMuted(muted) {
    this._muted = Boolean(muted);
    this._applyGain();
  }

  _applyGain() {
    if (!this.gainNode || !this.audioCtx) return;
    const value = this._effectiveGain();
    // setTargetAtTime rather than assignment: an instant jump in gain is a
    // step discontinuity in the waveform, which is audible as a click on the
    // shop floor. 10 ms is below human reaction time and well above the
    // period of any frequency that matters.
    try {
      this.gainNode.gain.setTargetAtTime(value, this.audioCtx.currentTime, 0.01);
    } catch {
      this.gainNode.gain.value = value;
    }
  }

  static supportedMime() {
    const opts = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
    ];
    for (const m of opts) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
    }
    return "";
  }

  async start() {
    const mime = HQBroadcaster.supportedMime();
    if (!mime) throw new Error("No supported audio recorder MIME type in this browser.");
    this._mime = mime;

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });

    // Gain stage and meters.
    //
    //   getUserMedia -> MediaStreamSource -> Gain -> MediaStreamDestination
    //                          |               |            |
    //                     inputAnalyser   sentAnalyser   MediaRecorder
    //
    // MediaRecorder used to record `this.stream` directly, which left nowhere
    // to put a volume control. It now records the destination node's stream
    // instead, so everything downstream - container, codec, bitrate, chunk
    // size, the WebSocket, the backend, the Stores - is unchanged. The gain
    // node is the only new thing in the path.
    //
    // TWO analysers, deliberately. The old single meter tapped the raw
    // microphone, so it kept dancing while muted and an operator could
    // reasonably read that as "the Stores are hearing me". Input level is
    // still useful - it says the microphone works - so both are measured and
    // the UI labels which is which.
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    this.source = this.audioCtx.createMediaStreamSource(this.stream);
    this.gainNode = this.audioCtx.createGain();
    this.gainNode.gain.value = this._effectiveGain();
    this.destination = this.audioCtx.createMediaStreamDestination();

    this.analyser = this.audioCtx.createAnalyser();   // input, pre-gain
    this.analyser.fftSize = 512;
    this.sentAnalyser = this.audioCtx.createAnalyser(); // post-gain
    this.sentAnalyser.fftSize = 512;

    this.source.connect(this.analyser);
    this.source.connect(this.gainNode);
    this.gainNode.connect(this.sentAnalyser);
    this.gainNode.connect(this.destination);
    this._runMeter();

    // WebSocket
    this.ws = new WebSocket(this.wsUrl);
    this.ws.binaryType = "arraybuffer";
    await new Promise((res, rej) => {
      this.ws.onopen = () => { this.onStatus("connected"); res(); };
      this.ws.onerror = (e) => { this.onError("WebSocket error"); rej(e); };
      this.ws.onclose = () => { this.onStatus("disconnected"); };
    });

    // Send an init text so server knows mime (optional)
    try { this.ws.send(JSON.stringify({ type: "init", mime })); } catch { /* noop */ }

    // The DESTINATION stream, not the raw microphone: this is what carries
    // the gain. Same mime type, same 32 kbps, same 250 ms chunks as before.
    this.recorder = new MediaRecorder(this.destination.stream,
                                      { mimeType: mime, audioBitsPerSecond: 32000 });
    this.recorder.ondataavailable = async (e) => {
      if (!e.data || e.data.size === 0) return;
      if (!this.ws || this.ws.readyState !== 1) return;
      try {
        const buf = await e.data.arrayBuffer();
        this.ws.send(buf);
      } catch { /* noop */ }
    };
    this.recorder.start(250); // 250ms chunks
    this.onStatus("recording");
  }

  _runMeter() {
    const inputBuf = new Uint8Array(this.analyser.frequencyBinCount);
    const sentBuf = new Uint8Array(this.sentAnalyser.frequencyBinCount);
    const rms = (analyser, buf) => {
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      return Math.min(1, Math.sqrt(sum / buf.length) * 3);
    };
    const tick = () => {
      const input = rms(this.analyser, inputBuf);
      // Measured after the gain node, so muting really does flatten it. This
      // is the number that corresponds to what the Stores receive.
      const sent = rms(this.sentAnalyser, sentBuf);
      // The first argument stays the SENT level so any existing caller that
      // ignores the second one shows the honest figure rather than a raw
      // microphone level that keeps moving while muted.
      this.onMeter(sent, { input, sent, muted: this._muted,
                           volumePercent: this._volumePercent });
      this.meterRAF = requestAnimationFrame(tick);
    };
    tick();
  }

  async stop() {
    try { if (this.recorder && this.recorder.state !== "inactive") this.recorder.stop(); } catch { /* */ }
    try { if (this.stream) this.stream.getTracks().forEach((t) => t.stop()); } catch { /* */ }
    try { if (this.ws && this.ws.readyState <= 1) this.ws.close(); } catch { /* */ }
    try { if (this.meterRAF) cancelAnimationFrame(this.meterRAF); } catch { /* */ }
    // Disconnect the graph before closing the context. Closing alone leaves
    // the nodes referencing each other, and the MediaStreamDestination holds a
    // live MediaStream of its own - a broadcast started and stopped repeatedly
    // would otherwise accumulate them for the lifetime of the page.
    for (const node of [this.source, this.gainNode, this.sentAnalyser, this.analyser]) {
      try { if (node) node.disconnect(); } catch { /* */ }
    }
    try {
      if (this.destination) this.destination.stream.getTracks().forEach((t) => t.stop());
    } catch { /* */ }
    try { if (this.audioCtx) await this.audioCtx.close(); } catch { /* */ }
    this.stream = null; this.recorder = null; this.ws = null; this.analyser = null; this.audioCtx = null;
    this.sentAnalyser = null; this.source = null; this.gainNode = null; this.destination = null;
    this.onStatus("stopped");
  }
}
