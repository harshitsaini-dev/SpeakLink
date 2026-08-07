import React from "react";
import { Link } from "react-router-dom";
import { Mic, MicOff, Play, Square, AlertOctagon, Search, RefreshCcw, Users, Wifi, WifiOff, Radio } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { useBroadcast } from "@/contexts/BroadcastContext";
import { elapsedSeconds } from "@/lib/time";
import StatusBadge from "@/components/StatusBadge";
import StoreAudioControl from "@/components/StoreAudioControl";
import { useStoreAudioControl } from "@/lib/audio/useStoreAudioControl";
import WebAudiencePanel from "@/components/WebAudiencePanel";

export const ONLY_WITH_LINK = "only_with_link";

//: Every mode reaches the web room. Only With Link is the one that reaches
//: NOTHING ELSE - it is a Broadcast with an audience and no shop, not a
//: physical Broadcast whose Store selection came out empty.
const PHYSICAL_TARGET_MODES = [
  { value: "selected", label: "Selected Stores" },
  { value: "all", label: "All Stores" },
  { value: "region", label: "By Zone" },
  { value: "city", label: "By City" },
  { value: "online_only", label: "Online Stores Only" },
];
const LINK_ONLY_MODE = { value: ONLY_WITH_LINK, label: "Only With Link" };

// The only play_status values the backend ever writes (_persist_receiver_ack in
// backend/server.py): pending, audio_receiving, playback_confirmed,
// playback_error, device_error, stopped. There is no "playing" and no "failed",
// so the summary must not invent one - a Store is never shown as playing just
// because a command was sent to it.
const RECEIVING_STATUSES = ["audio_receiving"];
const CONFIRMED_STATUSES = ["playback_confirmed"];
const ERROR_STATUSES = ["playback_error", "device_error"];

// Timezone-independent: elapsed is derived from epoch values via
// elapsedSeconds(), never from a formatted local clock string, so it cannot
// drift with the browser's timezone (see frontend/src/lib/time.js).
function useTimer(startedAtIso) {
  const [elapsed, setElapsed] = React.useState(() => elapsedSeconds(startedAtIso));
  React.useEffect(() => {
    if (!startedAtIso) { setElapsed(0); return undefined; }
    setElapsed(elapsedSeconds(startedAtIso));
    const id = setInterval(() => setElapsed(elapsedSeconds(startedAtIso)), 250);
    return () => clearInterval(id);
  }, [startedAtIso]);
  return elapsed;
}

function fmtDur(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export default function BroadcastConsole() {
  const { can } = useAuth();
  const canDeliverToStores = React.useCallback(
    () => can("broadcast.store_delivery"), [can]);
  const {
    current, load: loadBroadcast, isLive, meter, micLevels, broadcasterStatus,
    micVolumePercent, micMuted, setMicVolume, setMicMute, micEffectivelySilent,
    error, setError, startBroadcast: startBroadcastAudio,
    stopBroadcast: stopBroadcastAudio, emergencyStop: emergencyStopAudio,
    active, isStoreBusyForOthers,
  } = useBroadcast();

  const [stores, setStores] = React.useState([]);
  const [meta, setMeta] = React.useState({ regions: [], cities: [] });
  const [q, setQ] = React.useState("");
  const [selectedIds, setSelectedIds] = React.useState(new Set());
  // A link-only broadcaster has exactly one mode available, so that is where
  // their form starts rather than on a Store mode they cannot use.
  const [targetMode, setTargetMode] = React.useState(
    () => (canDeliverToStores() ? "selected" : ONLY_WITH_LINK));
  const [region, setRegion] = React.useState("");
  const [city, setCity] = React.useState("");
  const [campaign, setCampaign] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  // Emergency Stop gets its OWN confirmation, deliberately not the ordinary
  // Start/Stop dialog: it terminates every operator's broadcast, and reusing a
  // dialog somebody clicks through routinely is how that becomes a reflex.
  const [emergencyConfirmOpen, setEmergencyConfirmOpen] = React.useState(false);
  const [emergencyResult, setEmergencyResult] = React.useState(null);
  // Output control belongs to THIS broadcast. No session, no controls: there
  // is nothing to be loud on and nothing the backend would accept.
  const liveSessionId = active?.mine?.session_id ?? current?.session?.id ?? null;
  const mayControlAudio = can("store_audio.control");
  const storeAudio = useStoreAudioControl({
    sessionId: isLive ? liveSessionId : null,
    canControl: mayControlAudio,
  });

  const [micTest, setMicTest] = React.useState({ on: false, level: 0 });
  const testAudioRef = React.useRef(null);

  // The broadcast TARGET catalog, not the administrative Store list.
  //
  // This used to be GET /stores plus GET /stores/meta/regions-cities, both of
  // which require "View Store Management". An operator allowed to broadcast
  // but not to manage Stores therefore got 403 on both, and this page rendered
  // an empty table with no explanation. Targeting and management are separate
  // capabilities, so they now have separate endpoints; this one is gated on
  // the same permission that opens the Console.
  //
  // Regions and cities arrive in the same response, derived from the very
  // Stores listed here, so a scoped operator's dropdowns can no longer offer a
  // region they have no Store in.
  // Physical Store delivery is now its own right, separate from being allowed
  // to broadcast at all: an account may host a Broadcast without ever being
  // able to put sound into a shop. For such an operator the target inventory is
  // a 403, so it is not requested - a page that fetches what it may not have
  // and then hides the error is how the empty-table bug happened before.
  const mayDeliverToStores = canDeliverToStores();

  const load = React.useCallback(async () => {
    if (mayDeliverToStores) {
      const { data } = await api.get("/broadcast/target-stores");
      setStores(data.stores || []);
      setMeta({ regions: data.regions || [], cities: data.cities || [] });
    }
    await loadBroadcast();
  }, [loadBroadcast, mayDeliverToStores]);

  React.useEffect(() => { load(); }, [load]);

  // Poll while mounted so the store list and target counts are fresh. The
  // live/broadcaster/meter state itself is polled by BroadcastProvider, above
  // this route, so it survives navigating away from this page and back.
  React.useEffect(() => {
    const id = setInterval(() => load(), 3000);
    return () => clearInterval(id);
  }, [load]);

  // ---- rehydrate the form from the LIVE broadcast -------------------------
  //
  // campaign, targetMode and selectedIds are component-local state, so they die
  // when this route unmounts. The broadcast itself does not: BroadcastProvider
  // sits above the router and keeps the microphone, the socket and the session.
  // Navigating away and back therefore left an operator looking at an empty
  // form while forty Stores were being broadcast to - the Console said "no
  // Stores chosen" about a broadcast that was very much choosing them.
  //
  // The live session is the authority here, not this component and not
  // localStorage: a stored draft outlives Stop, Emergency Stop, another tab and
  // a server restart, and would confidently redraw a broadcast that had ended.
  // `current` comes from GET /api/broadcast/current, which already returns only
  // the caller's OWN session, already carries campaign_name and target_mode,
  // and already applies Store Scope to the target list server-side.
  //
  // Keyed by session id so it runs once per broadcast rather than on every
  // poll. Re-applying on each poll would be invisible today - every input is
  // disabled while live - and would silently start fighting the operator the
  // day any of them becomes editable.
  const rehydratedSessionRef = React.useRef(null);
  React.useEffect(() => {
    const liveSession = current?.live ? current.session : null;

    if (!liveSession) {
      // The broadcast ended - Stop, Emergency Stop, or somebody else's stop.
      // Clear only what was restored FROM it, so the next draft starts empty
      // rather than inheriting a finished campaign and its Stores.
      if (rehydratedSessionRef.current !== null) {
        rehydratedSessionRef.current = null;
        setCampaign("");
        setTargetMode("selected");
        setSelectedIds(new Set());
        setRegion("");
        setCity("");
      }
      return;
    }

    if (rehydratedSessionRef.current === liveSession.id) return;
    rehydratedSessionRef.current = liveSession.id;

    setCampaign(liveSession.campaign_name || "");
    if (liveSession.target_mode) setTargetMode(liveSession.target_mode);
    // Matched on stable Store id, never on Store Code text: a code can be
    // released and reissued to a different shop after a permanent deletion.
    setSelectedIds(new Set(
      (current.targets || [])
        .map((target) => target.store_id)
        .filter((storeId) => storeId !== null && storeId !== undefined),
    ));
  }, [current]);

  // Region and City are not columns on a session, so they cannot be read back
  // directly - they are derived from the Stores actually being targeted. Kept
  // separate from the effect above because it needs the Store catalog, which
  // arrives on its own schedule.
  React.useEffect(() => {
    if (!current?.live || !stores.length) return;
    const targeted = new Set((current.targets || []).map((t) => t.store_id));
    const first = stores.find((s) => targeted.has(s.id));
    if (!first) return;
    if (current.session?.target_mode === "region") setRegion(first.region || "");
    if (current.session?.target_mode === "city") setCity(first.city || "");
  }, [current, stores]);

  const filteredStores = React.useMemo(() => {
    const ql = q.toLowerCase();
    return stores.filter((s) =>
      !ql || s.store_name.toLowerCase().includes(ql) || s.store_code.toLowerCase().includes(ql) || s.city.toLowerCase().includes(ql)
    );
  }, [stores, q]);

  const resolveTargetStoreIds = () => {
    // Link-only has no physical destination at all, which is the point of it.
    if (targetMode === ONLY_WITH_LINK) return [];
    if (targetMode === "all") return stores.map((s) => s.id);
    if (targetMode === "selected") return Array.from(selectedIds);
    if (targetMode === "region") return stores.filter((s) => s.region === region).map((s) => s.id);
    if (targetMode === "city") return stores.filter((s) => s.city === city).map((s) => s.id);
    if (targetMode === "online_only") return stores.filter((s) => s.is_online_store).map((s) => s.id);
    return [];
  };
  // While a broadcast is LIVE the session's own target list is the truth, not
  // whatever the draft inputs would compute. That matters most for the Region
  // and City modes: those recompute from a dropdown value that is not stored on
  // the session at all, so after a remount they would resolve to an empty set
  // and the Console would show a live broadcast reaching nothing. It is also
  // simply more honest - these are the Stores the backend is streaming to.
  const liveTargetIds = current?.live
    ? (current.targets || [])
        .map((target) => target.store_id)
        .filter((storeId) => storeId !== null && storeId !== undefined)
    : null;
  const targetIds = liveTargetIds ?? resolveTargetStoreIds();
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

  const startBroadcast = async () => {
    setBusy(true);
    try {
      await startBroadcastAudio({ campaign, targetMode, ids: resolveTargetStoreIds(), region, city });
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Failed to start broadcast");
    } finally { setBusy(false); setConfirmOpen(false); }
  };

  const stopBroadcast = async () => {
    setBusy(true);
    try {
      await stopBroadcastAudio();
    } catch (e) { setError(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
  };

  const emergencyStop = async () => {
    setBusy(true);
    setEmergencyResult(null);
    try {
      const outcome = await emergencyStopAudio();
      const count = (outcome?.session_ids || []).length;
      setEmergencyResult({
        ok: true,
        message: count
          ? `Emergency Stop: ${count} broadcast${count === 1 ? "" : "s"} stopped.`
          : "Emergency Stop: there were no active broadcasts.",
      });
    } catch (e) {
      // A partial failure must never read as success. The operator has to know
      // that something is still on air so they can act on it.
      if (e?.emergencyIncomplete) {
        setEmergencyResult({ ok: false, message: e.message });
      }
      setError(e?.response?.data?.detail?.message
               || e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
      setEmergencyConfirmOpen(false);
    }
  };

  const startedAtIso = current?.session?.started_at || null;
  const elapsed = useTimer(startedAtIso);

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

          {/* Mic level and gain, only while live */}
          {isLive && (
            <div className="mt-4 space-y-3">
              {/* TWO meters. The upper one is the raw microphone; the lower
                  one is what is actually leaving for the Stores. They differ
                  whenever the gain is below 100 and they differ completely
                  when muted - which is the whole point, because a single
                  pre-gain meter let an operator watch their voice move a bar
                  while the shops heard nothing. */}
              <div>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-slate-500 mb-1">
                  <span>Mic Input</span>
                  <span className="text-slate-400 normal-case tracking-normal">before volume</span>
                </div>
                <div className="h-2 bg-slate-100 rounded overflow-hidden">
                  <div data-testid="mic-input-meter"
                       className="h-full bg-slate-400 transition-all"
                       style={{ width: `${Math.min(100, Math.round((micLevels?.input ?? 0) * 100))}%` }} />
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-slate-500 mb-1">
                  <span>Sent to Stores</span>
                  {micEffectivelySilent && (
                    <span data-testid="mic-muted-badge"
                          className="rounded border border-red-400 bg-red-100 px-1.5 py-0.5 text-[10px] font-bold tracking-wider text-red-800">
                      MUTED — STORES HEAR NOTHING
                    </span>
                  )}
                </div>
                <div className="h-3 bg-slate-100 rounded overflow-hidden">
                  <div data-testid="mic-sent-meter"
                       className={`h-full transition-all ${micEffectivelySilent ? "bg-red-300"
                         : "bg-gradient-to-r from-emerald-500 via-yellow-400 to-red-500"}`}
                       style={{ width: `${micEffectivelySilent ? 0 : Math.min(100, Math.round(meter * 100))}%` }} />
                </div>
              </div>

              <div className="flex items-center gap-2">
                <button type="button" data-testid="mic-mute-toggle"
                        aria-pressed={micMuted}
                        onClick={() => setMicMute(!micMuted)}
                        className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-xs font-semibold ${
                          micMuted ? "border-red-400 bg-red-100 text-red-800"
                                   : "border-slate-300 text-slate-700"}`}>
                  {micMuted ? <MicOff size={13} /> : <Mic size={13} />}
                  {micMuted ? "Unmute" : "Mute"}
                </button>
                <label htmlFor="mic-volume" className="sr-only">HQ microphone volume</label>
                <input id="mic-volume" data-testid="mic-volume-slider"
                       type="range" min="0" max="100" step="1"
                       value={micVolumePercent}
                       aria-valuetext={`${micVolumePercent} percent`}
                       onChange={(e) => setMicVolume(e.target.value)}
                       className="flex-1 accent-blue-600" />
                <span data-testid="mic-volume-value"
                      className="w-10 text-right text-xs font-mono text-slate-700">
                  {micVolumePercent}%
                </span>
              </div>
            </div>
          )}
        </div>

        {/* Emergency stop */}
        <div className="border border-red-200 bg-red-50 rounded-md shadow-sm p-4 flex flex-col justify-between">
          <div className="text-xs font-bold uppercase tracking-[0.15em] text-red-900 flex items-center gap-1"><AlertOctagon size={14}/> Safety Control</div>
          {can("broadcast.emergency_stop") ? (
            <button
              data-testid="emergency-stop-btn"
              disabled={busy}
              onClick={() => setEmergencyConfirmOpen(true)}
              className="h-24 mt-3 w-full text-lg sm:text-2xl font-bold bg-red-600 hover:bg-red-700 active:bg-red-800 disabled:bg-red-300 text-white rounded-lg shadow-md border border-red-800 uppercase tracking-widest flex items-center justify-center gap-3 transition-all"
            >
              <AlertOctagon size={26} /> Emergency Stop All
            </button>
          ) : (
            <p className="mt-3 text-xs text-red-800">
              Your account cannot Emergency Stop. Ask an administrator.
            </p>
          )}
        </div>
      </div>

      {/* MY ACTIVE BROADCAST */}
      {active?.mine && (
        <div data-testid="my-active-broadcast"
             className="border border-blue-200 bg-blue-50/60 rounded-md shadow-sm p-4">
          <div className="text-xs font-bold uppercase tracking-[0.15em] text-blue-900">
            Your Active Broadcast
          </div>
          <div className="mt-2 flex flex-wrap items-baseline gap-x-6 gap-y-1">
            <span data-testid="my-active-campaign" className="text-lg font-semibold text-slate-900">
              {active.mine.campaign_name}
            </span>
            <span className="text-xs text-slate-600">
              Started <span data-testid="my-active-started">{active.mine.started_at || "—"}</span>
            </span>
            <span className="text-xs text-slate-600">
              <span data-testid="my-active-target-count">{active.mine.target_store_count}</span>
              {" "}Store{active.mine.target_store_count === 1 ? "" : "s"}
            </span>
          </div>
        </div>
      )}

      {/* A COMPACT LINK, NOT A LIST.
          This used to render every other operator's broadcast as table rows.
          That was readable with one concurrent broadcast and unusable with
          twenty: the Console is where somebody speaks into a microphone, and
          it was growing without bound behind the controls they came for.
          Supervision moved to its own page, and what stays here is one line
          whose height does not depend on how many broadcasts are live -
          fifty active sessions render exactly the same badge as one.
          Shown only for accounts holding broadcast.active_view; without it
          the backend sends active_count: null, so even the NUMBER is
          withheld rather than hidden client-side. */}
      {active?.may_manage_active && (
        <div data-testid="active-broadcasts-badge"
             className="border border-slate-200 bg-white rounded-md shadow-sm px-4 py-3 flex items-center justify-between gap-4">
          <div className="text-sm text-slate-700">
            <span className="font-bold uppercase tracking-[0.15em] text-xs text-slate-500 mr-2">
              Active Broadcasts
            </span>
            <span data-testid="active-broadcasts-count" className="font-semibold text-slate-900">
              {active.active_count ?? 0}
            </span>
          </div>
          <Link to="/active-broadcasts" data-testid="active-broadcasts-link"
                className="text-sm font-medium text-blue-700 hover:text-blue-900 hover:underline">
            View →
          </Link>
        </div>
      )}

      {emergencyResult && (
        <div role="alert" data-testid="emergency-result"
             className={`rounded-md border px-3 py-2 text-sm ${emergencyResult.ok
               ? "border-slate-300 bg-slate-50 text-slate-800"
               : "border-red-400 bg-red-100 text-red-900 font-semibold"}`}>
          {emergencyResult.message}
        </div>
      )}

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
            {!mayDeliverToStores && (
              <div data-testid="no-store-delivery-notice"
                   className="rounded-md border border-slate-200 bg-slate-50 p-3">
                <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1">
                  Target Mode
                </p>
                <p className="text-sm font-semibold text-slate-900"
                   data-testid="link-only-mode">
                  Only With Link
                </p>
                <p className="text-sm text-slate-700 mt-1">
                  This Broadcast reaches web listeners through a shared link.
                  It does not play in any Store.
                </p>
                <p className="text-xs text-slate-500 mt-1">
                  Ask an administrator for &ldquo;Broadcast to Stores / Zones&rdquo;
                  if you need to broadcast to shops.
                </p>
              </div>
            )}
            {mayDeliverToStores && (
            <div>
              <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Target Mode</label>
              <select
                data-testid="target-mode-select"
                value={targetMode} onChange={(e) => setTargetMode(e.target.value)}
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                disabled={isLive}
              >
                {[...PHYSICAL_TARGET_MODES, LINK_ONLY_MODE].map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>
            )}

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
              can("broadcast.start") && (
                <button
                  data-testid="start-broadcast-btn"
                  onClick={() => setConfirmOpen(true)}
                  disabled={busy || !campaign.trim()
                    || (targetMode !== ONLY_WITH_LINK && targetIds.length === 0)
                    || (targetMode === "region" && !region)
                    || (targetMode === "city" && !city)}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold text-white bg-blue-700 hover:bg-blue-800 disabled:bg-slate-400"
                >
                  <Play size={16}/> Start Live Broadcast
                </button>
              )
            ) : (
              can("broadcast.stop") && (
                <button data-testid="stop-broadcast-btn" onClick={stopBroadcast} disabled={busy}
                        className="inline-flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold text-white bg-slate-800 hover:bg-slate-900">
                  <Square size={16}/> Stop Broadcast
                </button>
              )
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

      {/* Store list. Omitted entirely without physical delivery: a disabled
          selector would still print the name of every Store the account may
          not reach, which is the leak the control is meant to prevent. */}
      {mayDeliverToStores && targetMode !== ONLY_WITH_LINK && (
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
                {isLive && mayControlAudio && (
                  <th className="px-3 py-2" title="Controls the SpeakLink audio output on the Store PC. The amplifier's physical volume control is separate.">
                    Store Output
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {filteredStores.map((s) => {
                const isTarget = targetIds.includes(s.id);
                const t = targetsById.get(s.id);
                const playStatus = isLive && isTarget ? (t?.play_status || "pending") : "—";
                // Held by SOMEBODY ELSE's broadcast. A Store carrying this
                // account's own broadcast is not "busy" to them - it is theirs,
                // and marking it unavailable would be nonsense on their own
                // console.
                const busyElsewhere = isStoreBusyForOthers(s.id);
                return (
                  <tr key={s.id} data-testid={`store-row-${s.store_code}`} className={`border-b border-slate-100 even:bg-slate-50/50 ${isTarget ? "bg-blue-50/40" : ""} ${busyElsewhere ? "bg-amber-50/60" : ""}`}>
                    {targetMode === "selected" && (
                      <td className="px-3 py-2">
                        <input type="checkbox" data-testid={`store-checkbox-${s.store_code}`}
                               checked={selectedIds.has(s.id)} onChange={() => toggleStore(s.id)}
                               disabled={isLive || busyElsewhere}
                               title={busyElsewhere
                                 ? `${s.store_code} is currently in use by another broadcast.`
                                 : undefined}
                               className="w-4 h-4"/>
                      </td>
                    )}
                    <td className="px-3 py-2 font-mono text-xs text-slate-700">
                      {s.store_code}
                      {busyElsewhere && (
                        // Deliberately says WHAT, never WHO. Naming the other
                        // operator or their campaign here would publish a
                        // directory of everyone's live broadcasts to anyone
                        // who can open this page.
                        <span data-testid={`store-busy-${s.store_code}`}
                              title={`${s.store_code} is currently in use by another broadcast.`}
                              className="ml-2 inline-block rounded border border-amber-400 bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-900">
                          In use
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-slate-900 font-medium">{s.store_name}</td>
                    <td className="px-3 py-2 text-slate-600">{s.city} · <span className="text-slate-500">{s.region}</span></td>
                    <td className="px-3 py-2"><StatusBadge status={s.status} testid={`store-status-${s.store_code}`} /></td>
                    <td className="px-3 py-2">
                      {playStatus === "—" ? <span className="text-slate-400 text-xs">—</span> :
                        <StatusBadge status={playStatus} testid={`play-status-${s.store_code}`} />}
                    </td>
                    {isLive && mayControlAudio && (
                      <td className="px-3 py-2">
                        {/* Only a Store this broadcast is actually targeting.
                            Offering a volume control for a Store that is not
                            receiving the announcement would be a control that
                            does nothing, on a row that looks identical. */}
                        {isTarget ? (
                          <StoreAudioControl
                            store={s}
                            state={storeAudio.states[s.id]}
                            error={storeAudio.errors[s.id]}
                            online={storeAudio.states[s.id]?.online ?? true}
                            supported={storeAudio.states[s.id]?.supported ?? false}
                            disabled={busy}
                            onVolumeChange={(value) => storeAudio.setVolume(s.id, value)}
                            onMuteToggle={(value) => storeAudio.setMuted(s.id, value)}
                          />
                        ) : (
                          <span className="text-slate-400 text-xs">—</span>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
              {filteredStores.length === 0 && (
                <tr><td colSpan={isLive && mayControlAudio ? 7 : 6} className="px-3 py-6 text-center text-slate-500">No stores found.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      )}

      {/* The web audience, kept entirely separate from the Store table above.
          Stores and listeners are different delivery classes with different
          failure modes, and one merged list would invite reading a listener's
          Buffering as a shop problem. */}
      {liveSessionId && <WebAudiencePanel sessionId={liveSessionId} />}

      {/* Confirm Modal */}
      {emergencyConfirmOpen && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
             data-testid="emergency-confirm-modal">
          <div className="bg-white rounded-lg w-full max-w-md p-5 space-y-3">
            <h3 className="font-bold text-red-900 uppercase tracking-wide">
              Stop all active broadcasts?
            </h3>
            <p className="text-sm text-slate-800">
              This stops <strong>every active SpeakLink broadcast</strong>, including
              broadcasts started by other operators - not only your own. Every
              targeted Store is told to stop and every Store is released.
            </p>
            <p className="text-xs text-slate-600">
              SpeakLink cannot confirm that a speaker has fallen silent. This sends
              the stop command and releases the Stores.
            </p>
            <div className="flex gap-2 pt-1">
              <button type="button" data-testid="emergency-cancel-btn"
                      onClick={() => setEmergencyConfirmOpen(false)}
                      className="flex-1 px-4 py-2 border border-slate-300 rounded-md text-sm">
                Cancel
              </button>
              <button type="button" data-testid="emergency-confirm-btn" disabled={busy}
                      onClick={emergencyStop}
                      className="flex-1 px-4 py-2 bg-red-700 text-white rounded-md text-sm font-bold uppercase disabled:opacity-50">
                {busy ? "Stopping…" : "Stop All Broadcasts"}
              </button>
            </div>
          </div>
        </div>
      )}

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
