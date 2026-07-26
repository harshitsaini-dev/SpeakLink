import React from "react";
import { Mic, MicOff, Play, Square, AlertOctagon, Search, RefreshCcw, Users, Wifi, WifiOff, Radio } from "lucide-react";
import { api, getToken, wsUrl } from "@/lib/api";
import { HQBroadcaster } from "@/lib/audio/HQBroadcaster";
import StatusBadge from "@/components/StatusBadge";

const TARGET_MODES = [
  { value: "selected", label: "Selected Stores" },
  { value: "all", label: "All Stores" },
  { value: "region", label: "By Zone" },
  { value: "city", label: "By City" },
  { value: "online_only", label: "Online Stores Only" },
];

// The only play_status values the backend ever writes (_persist_receiver_ack in
// backend/server.py): pending, audio_receiving, playback_confirmed,
// playback_error, device_error, stopped. There is no "playing" and no "failed",
// so the summary must not invent one - a Store is never shown as playing just
// because a command was sent to it.
const RECEIVING_STATUSES = ["audio_receiving"];
const CONFIRMED_STATUSES = ["playback_confirmed"];
const ERROR_STATUSES = ["playback_error", "device_error"];

function useTimer(startTs) {
  const [elapsed, setElapsed] = React.useState(0);
  React.useEffect(() => {
    if (!startTs) return;
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startTs) / 1000)), 250);
    return () => clearInterval(id);
  }, [startTs]);
  return elapsed;
}

function fmtDur(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export default function BroadcastConsole() {
  const [stores, setStores] = React.useState([]);
  const [meta, setMeta] = React.useState({ regions: [], cities: [] });
  const [q, setQ] = React.useState("");
  const [selectedIds, setSelectedIds] = React.useState(new Set());
  const [targetMode, setTargetMode] = React.useState("selected");
  const [region, setRegion] = React.useState("");
  const [city, setCity] = React.useState("");
  const [campaign, setCampaign] = React.useState("");
  const [current, setCurrent] = React.useState(null); // {live, session, targets, online_receivers}
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [micTest, setMicTest] = React.useState({ on: false, level: 0 });
  const [broadcaster, setBroadcaster] = React.useState(null);
  const [meter, setMeter] = React.useState(0);
  const [broadcasterStatus, setBroadcasterStatus] = React.useState("idle");
  const hqWsRef = React.useRef(null);
  const testAudioRef = React.useRef(null);

  const load = React.useCallback(async () => {
    const [{ data: s }, { data: m }, { data: c }] = await Promise.all([
      api.get("/stores"),
      api.get("/stores/meta/regions-cities"),
      api.get("/broadcast/current"),
    ]);
    setStores(s); setMeta(m); setCurrent(c);
  }, []);

  React.useEffect(() => { load(); }, [load]);

  // Poll while live so status is fresh
  React.useEffect(() => {
    const id = setInterval(() => load(), 3000);
    return () => clearInterval(id);
  }, [load]);

  // HQ Dashboard status WS
  React.useEffect(() => {
    const t = getToken(); if (!t) return;
    const ws = new WebSocket(`${wsUrl("/ws/hq")}?token=${encodeURIComponent(t)}`);
    hqWsRef.current = ws;
    ws.onmessage = () => { load(); };
    ws.onclose = () => { hqWsRef.current = null; };
    return () => { try { ws.close(); } catch { /* */ } };
  }, [load]);

  const filteredStores = React.useMemo(() => {
    const ql = q.toLowerCase();
    return stores.filter((s) =>
      !ql || s.store_name.toLowerCase().includes(ql) || s.store_code.toLowerCase().includes(ql) || s.city.toLowerCase().includes(ql)
    );
  }, [stores, q]);

  const resolveTargetStoreIds = () => {
    if (targetMode === "all") return stores.map((s) => s.id);
    if (targetMode === "selected") return Array.from(selectedIds);
    if (targetMode === "region") return stores.filter((s) => s.region === region).map((s) => s.id);
    if (targetMode === "city") return stores.filter((s) => s.city === city).map((s) => s.id);
    if (targetMode === "online_only") return stores.filter((s) => s.is_online_store).map((s) => s.id);
    return [];
  };
  const targetIds = resolveTargetStoreIds();
  const targetStores = stores.filter((s) => targetIds.includes(s.id));
  const onlineCount = targetStores.filter((s) => s.status === "online" || s.status === "playing").length;
  const offlineCount = targetStores.length - onlineCount;

  const toggleStore = (id) => {
    setSelectedIds((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  };
  const selectAllFiltered = () => setSelectedIds(new Set(filteredStores.map((s) => s.id)));
  const clearSelection = () => setSelectedIds(new Set());

  const startMicTest = async () => {
    setError("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const audio = new (window.AudioContext || window.webkitAudioContext)();
      const src = audio.createMediaStreamSource(stream);
      const an = audio.createAnalyser(); an.fftSize = 512; src.connect(an);
      testAudioRef.current = { stream, audio, an, running: true };
      setMicTest({ on: true, level: 0 });
      const buf = new Uint8Array(an.frequencyBinCount);
      const tick = () => {
        if (!testAudioRef.current?.running) return;
        an.getByteTimeDomainData(buf);
        let sum = 0;
        for (const v of buf) { const x = (v - 128) / 128; sum += x * x; }
        setMicTest({ on: true, level: Math.min(1, Math.sqrt(sum / buf.length) * 3) });
        requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { setError("Microphone error: " + e.message); }
  };
  const stopMicTest = () => {
    const t = testAudioRef.current;
    if (t) { t.running = false; try { t.stream.getTracks().forEach((x) => x.stop()); } catch { /* */ } try { t.audio.close(); } catch { /* */ } }
    testAudioRef.current = null;
    setMicTest({ on: false, level: 0 });
  };

  // Poll the backend for an explicit receiver_ready acknowledgement. READY is
  // never inferred from CONNECTED, and never from a command having been sent.
  const waitForReceiverReady = React.useCallback(async (targetIdList, timeoutMs = 20000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const { data } = await api.get("/broadcast/current");
        const ready = (data?.ready_receivers || []).filter((id) => targetIdList.includes(id));
        if (ready.length > 0) return ready;
      } catch { /* keep polling until the deadline */ }
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    return [];
  }, []);

  const startBroadcast = async () => {
    setError(""); setBusy(true);
    try {
      if (!campaign.trim()) throw new Error("Please enter a campaign name");
      const ids = resolveTargetStoreIds();
      if (ids.length === 0) throw new Error("No stores selected for broadcast");
      // Create session
      const { data: session } = await api.post("/broadcast/sessions", {
        campaign_name: campaign.trim(),
        target_mode: targetMode,
        store_ids: targetMode === "selected" ? ids : undefined,
        region: targetMode === "region" ? region : undefined,
        city: targetMode === "city" ? city : undefined,
      });
      // Fail early and honestly if this browser cannot produce WebM/Opus.
      if (!HQBroadcaster.supportedMime()) {
        throw new Error(
          "This browser cannot record WebM/Opus audio. EchoCast will not send a " +
          "different format silently. Try a current Chrome or Edge browser."
        );
      }

      // Start the session first: the backend sends PREPARE to each targeted
      // Receiver, which runs its own FFmpeg/codec checks before reporting READY.
      await api.post(`/broadcast/sessions/${session.id}/start`);

      // Do not send a single audio byte until a Receiver has actually
      // acknowledged READY. A command being sent is not readiness.
      setBroadcasterStatus("waiting for receiver readiness");
      const readyIds = await waitForReceiverReady(ids);
      if (readyIds.length === 0) {
        throw new Error(
          "No Receiver reported READY, so no audio was sent. Check that the " +
          "Receiver is running and that FFmpeg is available on it."
        );
      }

      const bc = new HQBroadcaster({
        wsUrl: `${wsUrl("/ws/broadcaster")}?token=${encodeURIComponent(getToken())}`,
        onMeter: (l) => setMeter(l),
        onStatus: (s) => setBroadcasterStatus(s),
        onError: (m) => setError(m),
      });
      await bc.start();
      setBroadcaster(bc);
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Failed to start broadcast");
      // cleanup partial state
      // If broadcaster was created but session failed, stop broadcaster
    } finally { setBusy(false); setConfirmOpen(false); }
  };

  const stopBroadcast = async () => {
    setBusy(true); setError("");
    try {
      if (current?.session?.id) {
        await api.post(`/broadcast/sessions/${current.session.id}/stop`);
      }
      if (broadcaster) { await broadcaster.stop(); setBroadcaster(null); setMeter(0); }
      await load();
    } catch (e) { setError(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
  };

  const emergencyStop = async () => {
    setBusy(true); setError("");
    try {
      await api.post("/broadcast/emergency-stop");
      if (broadcaster) { await broadcaster.stop(); setBroadcaster(null); setMeter(0); }
      await load();
    } catch (e) { setError(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
  };

  const isLive = current?.live;
  const startedAt = current?.session?.started_at ? new Date(current.session.started_at).getTime() : null;
  const elapsed = useTimer(startedAt);

  const targetsById = React.useMemo(() => {
    const map = new Map();
    (current?.targets || []).forEach((t) => map.set(t.store_id, t));
    return map;
  }, [current]);

  const targetCounts = React.useMemo(() => {
    const targets = current?.targets || [];
    const count = (statuses) => targets.filter((t) => statuses.includes(t.play_status)).length;
    return {
      total: targets.length,
      receiving: count(RECEIVING_STATUSES),
      confirmed: count(CONFIRMED_STATUSES),
      errors: count(ERROR_STATUSES),
    };
  }, [current]);

  return (
    <div className="space-y-6" data-testid="broadcast-console">
      {/* HEADER: live status + emergency stop */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Live card */}
        <div className="lg:col-span-2 border border-slate-200 bg-white rounded-md shadow-sm p-5">
          <div className="flex items-center justify-between mb-3">
            <div>
              <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">Broadcast Status</div>
              <div className="mt-1 flex items-center gap-3">
                {isLive ? (
                  <>
                    <span className="live-dot" />
                    <span className="text-red-600 font-bold uppercase tracking-widest" data-testid="live-indicator">LIVE ON AIR</span>
                    <span className="text-slate-700 text-sm truncate">· {current?.session?.campaign_name}</span>
                  </>
                ) : (
                  <span className="text-slate-500 uppercase tracking-widest text-sm font-semibold">Idle · Ready to Broadcast</span>
                )}
              </div>
            </div>
            <div className="font-mono text-4xl md:text-5xl tracking-tighter text-slate-900" data-testid="live-timer">
              {isLive ? fmtDur(elapsed) : "00:00:00"}
            </div>
          </div>

          {/* VU meter when live */}
          {isLive && (
            <div className="mt-4">
              <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">Mic Input</div>
              <div className="h-3 bg-slate-100 rounded overflow-hidden">
                <div className="h-full bg-gradient-to-r from-emerald-500 via-yellow-400 to-red-500 transition-all"
                     style={{ width: `${Math.min(100, Math.round(meter * 100))}%` }} />
              </div>
            </div>
          )}
        </div>

        {/* Emergency stop */}
        <div className="border border-red-200 bg-red-50 rounded-md shadow-sm p-4 flex flex-col justify-between">
          <div className="text-xs font-bold uppercase tracking-[0.15em] text-red-900 flex items-center gap-1"><AlertOctagon size={14}/> Safety Control</div>
          <button
            data-testid="emergency-stop-btn"
            disabled={busy}
            onClick={emergencyStop}
            className="h-24 mt-3 w-full text-lg sm:text-2xl font-bold bg-red-600 hover:bg-red-700 active:bg-red-800 disabled:bg-red-300 text-white rounded-lg shadow-md border border-red-800 uppercase tracking-widest flex items-center justify-center gap-3 transition-all"
          >
            <AlertOctagon size={26} /> Emergency Stop
          </button>
        </div>
      </div>

      {/* CONTROLS */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 border border-slate-200 bg-white rounded-md shadow-sm p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-slate-900">Broadcast Controls</h2>
            {error && <div data-testid="console-error" className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1">{error}</div>}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Campaign Name</label>
              <input
                data-testid="campaign-name-input"
                value={campaign} onChange={(e) => setCampaign(e.target.value)}
                placeholder='e.g. "Diwali Festival Offer"'
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                disabled={isLive}
              />
            </div>
            <div>
              <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Target Mode</label>
              <select
                data-testid="target-mode-select"
                value={targetMode} onChange={(e) => setTargetMode(e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                disabled={isLive}
              >
                {TARGET_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>

            {targetMode === "region" && (
              <div>
                <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Zone</label>
                <select data-testid="region-select" value={region} onChange={(e) => setRegion(e.target.value)}
                        className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white" disabled={isLive}>
                  <option value="">— select —</option>
                  {meta.regions.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
            )}
            {targetMode === "city" && (
              <div>
                <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">City</label>
                <select data-testid="city-select" value={city} onChange={(e) => setCity(e.target.value)}
                        className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white" disabled={isLive}>
                  <option value="">— select —</option>
                  {meta.cities.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
            )}
          </div>

          {/* Action row */}
          <div className="flex flex-wrap items-center gap-2 pt-2">
            {!micTest.on ? (
              <button data-testid="mic-test-btn" onClick={startMicTest} disabled={isLive}
                      className="inline-flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-50">
                <Mic size={16}/> Mic Test
              </button>
            ) : (
              <div className="inline-flex items-center gap-3 px-3 py-2 rounded-md text-sm border border-emerald-200 bg-emerald-50 text-emerald-800">
                <Mic size={16}/>
                <div className="w-32 h-2 bg-emerald-100 rounded overflow-hidden">
                  <div className="h-full bg-emerald-500 transition-all" style={{ width: `${Math.round(micTest.level * 100)}%` }} />
                </div>
                <button data-testid="mic-test-stop-btn" onClick={stopMicTest} className="text-emerald-800 font-semibold">Stop</button>
              </div>
            )}

            {!isLive ? (
              <button
                data-testid="start-broadcast-btn"
                onClick={() => setConfirmOpen(true)}
                disabled={busy || targetIds.length === 0 || !campaign.trim() || (targetMode === "region" && !region) || (targetMode === "city" && !city)}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold text-white bg-blue-700 hover:bg-blue-800 disabled:bg-slate-400"
              >
                <Play size={16}/> Start Live Broadcast
              </button>
            ) : (
              <button data-testid="stop-broadcast-btn" onClick={stopBroadcast} disabled={busy}
                      className="inline-flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold text-white bg-slate-800 hover:bg-slate-900">
                <Square size={16}/> Stop Broadcast
              </button>
            )}

            <button data-testid="refresh-btn" onClick={load}
                    className="ml-auto inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-slate-600 border border-slate-200 hover:bg-slate-50">
              <RefreshCcw size={14}/> Refresh
            </button>
          </div>

          <div className="text-xs text-slate-500">Broadcaster: <span className="font-mono">{broadcasterStatus}</span></div>
        </div>

        {/* Target summary */}
        <div className="border border-slate-200 bg-white rounded-md shadow-sm p-5 space-y-3" data-testid="target-summary">
          <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">Broadcast Targets</div>
          <div className="grid grid-cols-3 gap-2">
            <StatCard label="Selected" value={targetIds.length} testid="stat-selected" icon={<Users size={14} className="text-slate-500"/>} />
            <StatCard label="Online" value={onlineCount} testid="stat-online" icon={<Wifi size={14} className="text-emerald-600"/>} color="emerald" />
            <StatCard label="Offline" value={offlineCount} testid="stat-offline" icon={<WifiOff size={14} className="text-slate-400"/>} />
          </div>
          {isLive && (
            <div className="pt-2 border-t border-slate-100 space-y-2">
              <div className="text-[11px] uppercase tracking-widest text-slate-500">
                Receiver Acknowledgements
                <span className="text-slate-400 normal-case tracking-normal"> · of {targetCounts.total}</span>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <StatCard label="Receiving" value={targetCounts.receiving} testid="stat-audio-receiving"
                          icon={<Radio size={14} className="text-amber-600"/>} />
                <StatCard label="Confirmed" value={targetCounts.confirmed} testid="stat-playback-confirmed"
                          icon={<Wifi size={14} className="text-blue-600"/>} />
                <StatCard label="Errors" value={targetCounts.errors} testid="stat-target-errors"
                          icon={<AlertOctagon size={14} className="text-red-600"/>} />
              </div>
              <p className="text-[10px] leading-snug text-slate-500">
                Confirmed means the Receiver's output device accepted decoded PCM frames.
                It does not mean sound was audible at a Store speaker.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Store list */}
      <div className="border border-slate-200 bg-white rounded-md shadow-sm">
        <div className="p-4 border-b border-slate-200 flex flex-wrap items-center gap-3">
          <h3 className="font-semibold text-slate-900 mr-auto">Stores {targetMode === "selected" && <span className="text-slate-500 font-normal text-sm">— pick receivers to include</span>}</h3>
          <div className="relative">
            <Search size={14} className="absolute left-2 top-2.5 text-slate-400"/>
            <input data-testid="stores-search" value={q} onChange={(e) => setQ(e.target.value)}
                   placeholder="Search stores…" className="pl-7 pr-3 py-2 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          </div>
          {targetMode === "selected" && (
            <>
              <button data-testid="select-all-filtered-btn" onClick={selectAllFiltered} className="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">Select all shown</button>
              <button data-testid="clear-selection-btn" onClick={clearSelection} className="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">Clear</button>
            </>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
              <tr>
                {targetMode === "selected" && <th className="px-3 py-2 w-10"></th>}
                <th className="px-3 py-2">Code</th>
                <th className="px-3 py-2">Name</th>
                <th className="px-3 py-2">City / Zone</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Play Status</th>
              </tr>
            </thead>
            <tbody>
              {filteredStores.map((s) => {
                const isTarget = targetIds.includes(s.id);
                const t = targetsById.get(s.id);
                const playStatus = isLive && isTarget ? (t?.play_status || "pending") : "—";
                return (
                  <tr key={s.id} data-testid={`store-row-${s.store_code}`} className={`border-b border-slate-100 even:bg-slate-50/50 ${isTarget ? "bg-blue-50/40" : ""}`}>
                    {targetMode === "selected" && (
                      <td className="px-3 py-2">
                        <input type="checkbox" data-testid={`store-checkbox-${s.store_code}`}
                               checked={selectedIds.has(s.id)} onChange={() => toggleStore(s.id)} disabled={isLive}
                               className="w-4 h-4"/>
                      </td>
                    )}
                    <td className="px-3 py-2 font-mono text-xs text-slate-700">{s.store_code}</td>
                    <td className="px-3 py-2 text-slate-900 font-medium">{s.store_name}</td>
                    <td className="px-3 py-2 text-slate-600">{s.city} · <span className="text-slate-500">{s.region}</span></td>
                    <td className="px-3 py-2"><StatusBadge status={s.status} testid={`store-status-${s.store_code}`} /></td>
                    <td className="px-3 py-2">
                      {playStatus === "—" ? <span className="text-slate-400 text-xs">—</span> :
                        <StatusBadge status={playStatus} testid={`play-status-${s.store_code}`} />}
                    </td>
                  </tr>
                );
              })}
              {filteredStores.length === 0 && (
                <tr><td colSpan="6" className="px-3 py-6 text-center text-slate-500">No stores found.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Confirm Modal */}
      {confirmOpen && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" data-testid="confirm-modal">
          <div className="bg-white rounded-md shadow-xl max-w-md w-full p-6">
            <div className="flex items-start gap-3 mb-3">
              <div className="p-2 rounded bg-red-100 text-red-700"><Radio size={20}/></div>
              <div>
                <h3 className="text-lg font-semibold">Confirm Live Broadcast</h3>
                <p className="text-sm text-slate-500">Your voice will be transmitted to the selected stores in real-time.</p>
              </div>
            </div>
            <div className="bg-slate-50 border border-slate-200 rounded-md p-3 text-sm space-y-1 my-3">
              <div><span className="text-slate-500">Campaign:</span> <span className="font-medium">{campaign}</span></div>
              <div><span className="text-slate-500">Target Mode:</span> <span className="font-medium">{TARGET_MODES.find((m) => m.value === targetMode)?.label}</span></div>
              <div><span className="text-slate-500">Stores:</span> <span className="font-medium">{targetIds.length}</span> ({onlineCount} online, {offlineCount} offline)</div>
            </div>
            {targetMode === "all" && (
              <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2 mb-2">
                Warning: this will broadcast to <b>ALL {stores.length} stores</b>.
              </div>
            )}
            <div className="flex gap-2 mt-4">
              <button data-testid="confirm-cancel-btn" onClick={() => setConfirmOpen(false)} className="flex-1 px-4 py-2 rounded-md border border-slate-300 text-sm hover:bg-slate-50">Cancel</button>
              <button data-testid="confirm-start-btn" onClick={startBroadcast} disabled={busy}
                      className="flex-1 px-4 py-2 rounded-md bg-red-600 hover:bg-red-700 text-white text-sm font-semibold flex items-center justify-center gap-2">
                <Play size={16}/> {busy ? "Starting…" : "Go Live"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, testid, icon, color }) {
  const textColor = color === "emerald" ? "text-emerald-700" : "text-slate-900";
  return (
    <div className="border border-slate-200 rounded-md p-2.5" data-testid={testid}>
      <div className="text-[10px] uppercase tracking-widest text-slate-500 flex items-center gap-1">{icon}{label}</div>
      <div className={`text-xl font-bold ${textColor} font-mono`}>{value}</div>
    </div>
  );
}
