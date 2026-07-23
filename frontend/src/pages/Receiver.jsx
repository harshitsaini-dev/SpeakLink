import React from "react";
import { useSearchParams } from "react-router-dom";
import { Radio, Volume2, VolumeX, Wifi, WifiOff, Play, AlertTriangle } from "lucide-react";
import { api, API_BASE, wsUrl } from "@/lib/api";
import { ReceiverPlayer } from "@/lib/audio/ReceiverPlayer";

const BG =
  "https://images.unsplash.com/photo-1621947081720-86970823b77a?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NDk1NzZ8MHwxfHNlYXJjaHwxfHxzb3VuZCUyMHdhdmUlMjBhYnN0cmFjdCUyMGJhY2tncm91bmR8ZW58MHx8fHwxNzgzNDIzMzQ1fDA&ixlib=rb-4.1.0&q=85";

export default function Receiver() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [store, setStore] = React.useState(null);
  const [status, setStatus] = React.useState("initializing"); // initializing|waiting|playing|error|disconnected|unauthorized
  const [audioEnabled, setAudioEnabled] = React.useState(false);
  const [lastCommand, setLastCommand] = React.useState("—");
  const [errMsg, setErrMsg] = React.useState("");
  const [campaign, setCampaign] = React.useState("");
  const audioElRef = React.useRef(null);
  const playerRef = React.useRef(null);
  const wsRef = React.useRef(null);
  const reconnectRef = React.useRef({ delay: 1000, timer: null, stopped: false });
  const heartbeatRef = React.useRef(null);

  // Verify token
  React.useEffect(() => {
    if (!token) { setStatus("unauthorized"); return; }
    (async () => {
      try {
        const { data } = await api.get("/receiver/verify", { params: { token } });
        if (!data.ok) { setStatus("unauthorized"); return; }
        setStore(data.store); setStatus("waiting");
      } catch { setStatus("unauthorized"); }
    })();
  }, [token]);

  const reportEvent = React.useCallback((type, details) => {
    try {
      fetch(`${API_BASE}/receiver/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, event_type: type, details }),
      });
    } catch { /* noop */ }
  }, [token]);

  const audioEnabledRef = React.useRef(false);
  React.useEffect(() => { audioEnabledRef.current = audioEnabled; }, [audioEnabled]);

  const connectWS = React.useCallback(() => {
    if (reconnectRef.current.stopped) return;
    const url = `${wsUrl(`/ws/receiver/${token}`)}`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectRef.current.delay = 1000;
      setStatus((s) => (s === "playing" ? s : "waiting"));
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      heartbeatRef.current = setInterval(() => {
        try { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "heartbeat" })); } catch { /* */ }
      }, 5000);
    };

    ws.onmessage = (evt) => {
      if (typeof evt.data === "string") {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === "play") {
            setLastCommand(`PLAY (${msg.campaign || "session " + msg.session_id})`);
            setCampaign(msg.campaign || "");
            if (audioEnabledRef.current && playerRef.current) {
              playerRef.current.reset();
              playerRef.current.play();
              setStatus("playing");
              try { ws.send(JSON.stringify({ type: "play_ack" })); } catch { /* */ }
            } else {
              setStatus("waiting");
              setErrMsg("Audio not enabled on this device. Click Enable Audio.");
            }
          } else if (msg.type === "stop") {
            setLastCommand(`STOP${msg.reason ? ` (${msg.reason})` : ""}`);
            if (playerRef.current) playerRef.current.reset();
            setStatus("waiting");
            setCampaign("");
            try { ws.send(JSON.stringify({ type: "stop_ack" })); } catch { /* */ }
          }
        } catch { /* noop */ }
      } else if (evt.data instanceof ArrayBuffer) {
        if (playerRef.current) playerRef.current.pushChunk(evt.data);
      }
    };

    ws.onerror = () => { /* onclose will handle reconnect */ };
    ws.onclose = () => {
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      setStatus((s) => (s === "unauthorized" ? s : "disconnected"));
      if (reconnectRef.current.stopped) return;
      reconnectRef.current.timer = setTimeout(() => {
        reconnectRef.current.delay = Math.min(30000, reconnectRef.current.delay * 2);
        connectWS();
      }, reconnectRef.current.delay);
    };
  }, [token]);

  React.useEffect(() => {
    if (!store) return;
    reconnectRef.current.stopped = false;
    connectWS();
    return () => {
      reconnectRef.current.stopped = true;
      if (reconnectRef.current.timer) clearTimeout(reconnectRef.current.timer);
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      try { wsRef.current && wsRef.current.close(); } catch { /* */ }
    };
    // eslint reconnect deps intentional
  }, [store, connectWS]);

  const enableAudio = async () => {
    setErrMsg("");
    try {
      if (!audioElRef.current) return;
      const player = new ReceiverPlayer(audioElRef.current, {
        onStatus: () => {},
        onError: (m) => { setErrMsg(m); reportEvent("error", m); },
      });
      if (!player.supported()) { setErrMsg("Browser does not support MediaSource/opus audio playback."); return; }
      player.attach();
      // Play silent audio to unlock autoplay
      audioElRef.current.muted = false;
      audioElRef.current.volume = 1.0;
      await audioElRef.current.play().catch(() => {});
      playerRef.current = player;
      setAudioEnabled(true);
      reportEvent("audio_enabled", "user gesture");
    } catch (e) {
      setErrMsg("Failed to enable audio: " + e.message);
    }
  };

  if (status === "unauthorized") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900 text-white p-6">
        <div className="text-center max-w-md" data-testid="receiver-unauthorized">
          <AlertTriangle className="mx-auto mb-4 text-red-400" size={48}/>
          <h1 className="text-2xl font-bold">Invalid Receiver Token</h1>
          <p className="text-slate-400 mt-2 text-sm">This receiver URL is not authorized. Ask HQ admin for the correct link from Store Management.</p>
        </div>
      </div>
    );
  }

  const statusColor = {
    initializing: "bg-slate-700",
    waiting: "bg-slate-700",
    playing: "bg-blue-700",
    disconnected: "bg-amber-700",
    error: "bg-red-700",
  }[status] || "bg-slate-700";

  return (
    <div className="min-h-screen relative flex flex-col text-white" data-testid="receiver-page">
      <div className="absolute inset-0">
        <img src={BG} alt="" className="w-full h-full object-cover opacity-40"/>
        <div className={`absolute inset-0 bg-gradient-to-br ${status === "playing" ? "from-blue-900/80 via-slate-900/85 to-blue-800/70" : "from-slate-900/90 via-slate-900/85 to-slate-800/80"}`}/>
      </div>

      <header className="relative z-10 p-4 flex items-center justify-between border-b border-white/10">
        <div className="flex items-center gap-2"><Radio className="text-red-500" size={20}/><span className="font-bold tracking-wide">EchoCast Receiver</span></div>
        <div className="text-xs uppercase tracking-widest text-slate-300">Store Kiosk</div>
      </header>

      <main className="relative z-10 flex-1 flex items-center justify-center p-6">
        <div className="w-full max-w-2xl text-center space-y-8">
          <div>
            <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Store</div>
            <div className="text-3xl md:text-5xl font-bold tracking-tight mt-1" data-testid="receiver-store-name">{store?.store_name || "…"}</div>
            <div className="text-sm text-slate-400 mt-2 font-mono">{store?.store_code} · {store?.city}, {store?.region}</div>
          </div>

          <div className={`inline-flex items-center gap-3 px-6 py-3 rounded-full ${statusColor} shadow-lg`} data-testid="receiver-status-pill">
            {status === "playing" ? <span className="live-dot"/> : status === "disconnected" ? <WifiOff size={16}/> : <Wifi size={16}/>}
            <span className="font-bold uppercase tracking-[0.15em] text-sm">
              {status === "playing" ? "LIVE · ON AIR" : status}
            </span>
          </div>

          {campaign && (
            <div className="text-lg text-blue-200">Campaign: <span className="font-semibold">{campaign}</span></div>
          )}

          {!audioEnabled ? (
            <div className="pt-4 space-y-3">
              <button
                data-testid="enable-audio-btn"
                onClick={enableAudio}
                className="inline-flex items-center gap-3 px-8 py-4 bg-emerald-500 hover:bg-emerald-600 text-white text-xl font-bold uppercase tracking-widest rounded-md shadow-lg transition-colors"
              >
                <Play size={22}/> Enable Audio
              </button>
              <div className="text-xs text-slate-400">One-time setup required by browser autoplay policy.</div>
            </div>
          ) : (
            <div className="text-sm text-emerald-300 flex items-center gap-2 justify-center"><Volume2 size={16}/> Audio ready — waiting for HQ commands</div>
          )}

          {errMsg && (
            <div className="text-sm text-red-200 bg-red-900/40 border border-red-500/40 rounded-md px-4 py-2 inline-block" data-testid="receiver-error">
              {errMsg}
            </div>
          )}
        </div>
      </main>

      <footer className="relative z-10 p-4 border-t border-white/10 text-xs text-slate-400 flex flex-wrap items-center gap-4 justify-between">
        <div>Last Command: <span className="font-mono text-slate-200" data-testid="receiver-last-command">{lastCommand}</span></div>
        <div>Audio: <span className="font-mono text-slate-200">{audioEnabled ? <><Volume2 size={12} className="inline"/> ON</> : <><VolumeX size={12} className="inline"/> OFF</>}</span></div>
      </footer>

      {/* audio element — hidden but active */}
      <audio ref={audioElRef} autoPlay data-testid="receiver-audio-element" />
    </div>
  );
}
