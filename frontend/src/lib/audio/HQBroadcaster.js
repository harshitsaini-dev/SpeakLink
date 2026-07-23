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
    this.meterRAF = null;
    this._mime = null;
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

    // Meter
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = this.audioCtx.createMediaStreamSource(this.stream);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 512;
    src.connect(this.analyser);
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

    this.recorder = new MediaRecorder(this.stream, { mimeType: mime, audioBitsPerSecond: 32000 });
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
    const buf = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      this.analyser.getByteTimeDomainData(buf);
      // rms
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      this.onMeter(Math.min(1, rms * 3));
      this.meterRAF = requestAnimationFrame(tick);
    };
    tick();
  }

  async stop() {
    try { if (this.recorder && this.recorder.state !== "inactive") this.recorder.stop(); } catch { /* */ }
    try { if (this.stream) this.stream.getTracks().forEach((t) => t.stop()); } catch { /* */ }
    try { if (this.ws && this.ws.readyState <= 1) this.ws.close(); } catch { /* */ }
    try { if (this.meterRAF) cancelAnimationFrame(this.meterRAF); } catch { /* */ }
    try { if (this.audioCtx) await this.audioCtx.close(); } catch { /* */ }
    this.stream = null; this.recorder = null; this.ws = null; this.analyser = null; this.audioCtx = null;
    this.onStatus("stopped");
  }
}
