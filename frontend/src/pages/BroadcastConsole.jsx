import React from "react";
import { Link } from "react-router-dom";
import { Mic, MicOff, Play, Square, AlertOctagon, Search, RefreshCcw, Users, Wifi, WifiOff, Radio } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { FilterSelect, SearchableSelect } from "@/components/AdminFilters";
import { useBroadcast } from "@/contexts/BroadcastContext";
import { elapsedSeconds } from "@/lib/time";
import StatusBadge from "@/components/StatusBadge";
import StoreAudioControl from "@/components/StoreAudioControl";
import { useStoreAudioControl } from "@/lib/audio/useStoreAudioControl";
import WebAudiencePanel from "@/components/WebAudiencePanel";
import BroadcastChatPanel from "@/components/BroadcastChatPanel";

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

//: A Store is ONLINE when its Receiver is currently reachable. `status` is
//: painted by the backend from the live Receiver connection inventory on every
//: inventory read - it is the only connectivity truth the Console has.
//:
//: NOT `is_online_store`, which is the Store Management checkbox labelled
//: Online / Physical: an e-commerce classification that defaults to false. That
//: column is what "Online Stores Only" used to filter on, which is why a
//: console showing BP ONLINE resolved zero targets.
export function isReceiverOnline(store) {
  return store?.status === "online" || store?.status === "playing";
}

const STORE_PAGE_SIZES = [10, 20, 50];

//: Every mode this Console can put in a confirmation dialog.
const ALL_TARGET_MODES = [...PHYSICAL_TARGET_MODES, LINK_ONLY_MODE];

// The only play_status values the backend ever writes (_persist_receiver_ack in
// backend/server.py): pending, audio_receiving, playback_confirmed,
// playback_error, device_error, stopped. There is no "playing" and no "failed",
// so the summary must not invent one - a Store is never shown as playing just
// because a command was sent to it.
//: lifecycle_state values that mean "this Store is in the broadcast". Kept
//: separate from play_status on purpose: this is the operator's intent and
//: HQ's delivery, while play_status is what the Receiver reported about sound.
//: A Store can be ACTIVE here and silent there.
const PARTICIPATING_STATES = ["ADDING", "PREPARING", "ACTIVE", "PAUSING", "PAUSED"];

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
    stopBroadcast: stopBroadcastAudio,
    active, isStoreBusyForOthers,
  } = useBroadcast();

  const [stores, setStores] = React.useState([]);
  const [meta, setMeta] = React.useState({ regions: [], cities: [] });
  const [q, setQ] = React.useState("");
  // Store picker filters. Deliberately separate from the TARGET modes below:
  // filtering by a Zone changes which rows are visible, never who is targeted.
  const [filterZone, setFilterZone] = React.useState("");
  const [filterCity, setFilterCity] = React.useState("");
  const [filterStatus, setFilterStatus] = React.useState("all");
  //: Sorting the PICKER, in the browser.
  //:
  //: Everywhere else sorting goes to the server, because the browser holds one
  //: page and sorting it would order fifty rows while claiming to order three
  //: hundred. Here the opposite is true: this table is fed from the Stores
  //: this account can already see, all of them, held in memory - so sorting
  //: them here orders the whole thing, and a round trip would buy nothing.
  const [storeSort, setStoreSort] = React.useState({ column: "", dir: "asc" });
  const [storePage, setStorePage] = React.useState(1);
  const [storePageSize, setStorePageSize] = React.useState(STORE_PAGE_SIZES[0]);
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
    const ql = q.trim().toLowerCase();
    const kept = stores.filter((store) => {
      // Code, name, city and Zone: an operator searching "UN ZONE" or
      // "Dwarka" means the same kind of thing as searching "BP".
      const matchesSearch = !ql
        || (store.store_name || "").toLowerCase().includes(ql)
        || (store.store_code || "").toLowerCase().includes(ql)
        || (store.city || "").toLowerCase().includes(ql)
        || (store.region || "").toLowerCase().includes(ql);
      // A filter may name several values now, comma-separated, exactly like
      // every other filter on the site. One value is a list of one.
      const named = (raw) => String(raw || "").split(",")
        .map((entry) => entry.trim()).filter(Boolean);
      const zones = named(filterZone);
      const cities = named(filterCity);
      const statuses = named(filterStatus === "all" ? "" : filterStatus);
      const matchesZone = !zones.length || zones.includes(store.region);
      const matchesCity = !cities.length || cities.includes(store.city);
      const matchesStatus = !statuses.length
        || (statuses.includes("online") && isReceiverOnline(store))
        || (statuses.includes("offline") && !isReceiverOnline(store));
      return matchesSearch && matchesZone && matchesCity && matchesStatus;
    });

    const ordered = [...kept];
    if (storeSort.column) {
      const read = {
        store_code: (row) => row.store_code,
        store_name: (row) => row.store_name,
        city: (row) => `${row.city} ${row.region}`,
        status: (row) => (isReceiverOnline(row) ? "online" : "offline"),
        // What the Store is doing in THIS broadcast, which is a different
        // question from whether it is connected.
        //
        // Read from `current` rather than from the targetsById map: that map
        // is built further down the component, and reaching it from here
        // would be a use-before-define that throws on the first render.
        play_status: (row) => ((current?.targets || [])
          .find((target) => target.store_id === row.id)?.play_status || ""),
      }[storeSort.column];
      ordered.sort((left, right) => {
        const a = String(read(left) ?? "").toLowerCase();
        const b = String(read(right) ?? "").toLowerCase();
        return storeSort.dir === "desc" ? b.localeCompare(a) : a.localeCompare(b);
      });
    }
    return ordered;
  }, [stores, q, filterZone, filterCity, filterStatus, storeSort, current]);

  // Filtering changes what "page 3" means, so staying on it would show an empty
  // table. The SELECTION is untouched: it belongs to the broadcast, not to
  // whichever rows happen to be visible.
  React.useEffect(() => {
    setStorePage(1);
  }, [q, filterZone, filterCity, filterStatus, storePageSize]);

  const storePageCount = Math.max(
    1, Math.ceil(filteredStores.length / storePageSize));
  const safeStorePage = Math.min(storePage, storePageCount);
  const visibleStores = React.useMemo(() => {
    const start = (safeStorePage - 1) * storePageSize;
    return filteredStores.slice(start, start + storePageSize);
  }, [filteredStores, safeStorePage, storePageSize]);

  const clearStoreFilters = () => {
    setQ("");
    setFilterZone("");
    setFilterCity("");
    setFilterStatus("all");
  };

  const resolveTargetStoreIds = () => {
    // Link-only has no physical destination at all, which is the point of it.
    if (targetMode === ONLY_WITH_LINK) return [];
    if (targetMode === "all") return stores.map((s) => s.id);
    if (targetMode === "selected") return Array.from(selectedIds);
    if (targetMode === "region") return stores.filter((s) => s.region === region).map((s) => s.id);
    if (targetMode === "city") return stores.filter((s) => s.city === city).map((s) => s.id);
    // Receiver connectivity, not the Online / Physical business flag. The
    // server resolves this again at Start, so this is a preview - but a preview
    // that disagreed with the server would be worse than none.
    if (targetMode === "online_only") return stores.filter(isReceiverOnline).map((s) => s.id);
    return [];
  };
  // While a broadcast is LIVE the session's own target list is the truth, not
  // whatever the draft inputs would compute. That matters most for the Region
  // and City modes: those recompute from a dropdown value that is not stored on
  // the session at all, so after a remount they would resolve to an empty set
  // and the Console would show a live broadcast reaching nothing. It is also
  // simply more honest - these are the Stores the backend is streaming to.
  //
  // A target that was REMOVED mid-broadcast is not a target any more. It stays
  // in the response because the row is the record of what happened, so it is
  // filtered here rather than deleted there - otherwise a Store taken out of a
  // live broadcast would keep being counted as one it reaches.
  const liveTargetIds = current?.live
    ? (current.targets || [])
        .filter((target) => PARTICIPATING_STATES.includes(
          target.lifecycle_state || "ACTIVE"))
        .map((target) => target.store_id)
        .filter((storeId) => storeId !== null && storeId !== undefined)
    : null;
  const targetIds = liveTargetIds ?? resolveTargetStoreIds();
  const targetStores = stores.filter((s) => targetIds.includes(s.id));
  const onlineCount = targetStores.filter(isReceiverOnline).length;
  const offlineCount = targetStores.length - onlineCount;
  // Authorised physical Stores that Online Stores Only would leave out. Only
  // meaningful in that mode, where "offline" describes exclusions rather than
  // targets an operator chose.
  const excludedOfflineCount = stores.filter((store) => !isReceiverOnline(store)).length;

  const toggleStore = (id) => {
    setSelectedIds((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  };
  // Two DIFFERENT bulk actions, each saying which it is. A single "Select all"
  // beside a paginated table means one thing to the person who can see ten rows
  // and another to the code, and the difference is a broadcast.
  const selectPage = () => setSelectedIds((previous) => {
    const next = new Set(previous);
    visibleStores.forEach((store) => next.add(store.id));
    return next;
  });
  const selectAllFiltered = () => setSelectedIds((previous) => {
    const next = new Set(previous);
    filteredStores.forEach((store) => next.add(store.id));
    return next;
  });
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

  // ---- Adding and removing ONE Store while the broadcast is on air.
  //
  // Both are slow enough to be worth showing: an add waits for the Store's
  // Receiver to report ready before it can join at the live edge, which can
  // take seconds. So the row is marked busy for the duration and the failure
  // is reported ON THE ROW, not in the page-wide error bar - an operator
  // adding one shop out of forty needs to know WHICH one refused.
  const [rowBusyStoreId, setRowBusyStoreId] = React.useState(null);
  const [rowErrors, setRowErrors] = React.useState({});
  //: A Zone action and its per-Store outcome. Kept as the whole response
  //: rather than a count, because "12 of 17 succeeded" leaves an operator to
  //: guess which five - and which five is the only part they can act on.
  const [zoneBusy, setZoneBusy] = React.useState(false);
  const [zoneResult, setZoneResult] = React.useState(null);
  const [zoneScope, setZoneScope] = React.useState({ region: "", city: "" });
  const sessionId = current?.session?.id;

  const setRowError = (storeId, message) =>
    setRowErrors((previous) => ({ ...previous, [storeId]: message }));

  const addStoreLive = async (store) => {
    if (!sessionId) return;
    setRowBusyStoreId(store.id);
    setRowError(store.id, "");
    try {
      await api.post(`/broadcast/sessions/${sessionId}/targets`,
                     { store_id: store.id });
      await loadBroadcast();
    } catch (e) {
      setRowError(store.id, e?.response?.data?.detail || e.message
                  || "Could not add this Store.");
    } finally { setRowBusyStoreId(null); }
  };

  const pauseStoreLive = async (store) => {
    if (!sessionId) return;
    setRowBusyStoreId(store.id);
    setRowError(store.id, "");
    try {
      await api.post(`/broadcast/sessions/${sessionId}/targets/${store.id}/pause`);
      await loadBroadcast();
    } catch (e) {
      setRowError(store.id, e?.response?.data?.detail || e.message
                  || "Could not pause this Store.");
    } finally { setRowBusyStoreId(null); }
  };

  const resumeStoreLive = async (store) => {
    if (!sessionId) return;
    setRowBusyStoreId(store.id);
    setRowError(store.id, "");
    try {
      await api.post(`/broadcast/sessions/${sessionId}/targets/${store.id}/resume`);
      await loadBroadcast();
    } catch (e) {
      setRowError(store.id, e?.response?.data?.detail || e.message
                  || "Could not resume this Store.");
    } finally { setRowBusyStoreId(null); }
  };

  const runZoneAction = async (action) => {
    if (!sessionId || zoneBusy) return;
    setZoneBusy(true);
    setZoneResult(null);
    try {
      const { data } = await api.post(
        `/broadcast/sessions/${sessionId}/targets/bulk`,
        {
          action,
          ...(zoneScope.region ? { region: zoneScope.region } : {}),
          ...(zoneScope.city ? { city: zoneScope.city } : {}),
        });
      setZoneResult(data);
      await loadBroadcast();
    } catch (e) {
      setZoneResult({
        action,
        error: e?.response?.data?.detail || e.message
          || "That Zone action did not run.",
        results: [],
      });
    } finally { setZoneBusy(false); }
  };

  const removeStoreLive = async (store) => {
    if (!sessionId) return;
    setRowBusyStoreId(store.id);
    setRowError(store.id, "");
    try {
      await api.delete(`/broadcast/sessions/${sessionId}/targets/${store.id}`);
      await loadBroadcast();
    } catch (e) {
      setRowError(store.id, e?.response?.data?.detail || e.message
                  || "Could not remove this Store.");
    } finally { setRowBusyStoreId(null); }
  };

  const stopBroadcast = async () => {
    setBusy(true);
    try {
      await stopBroadcastAudio();
    } catch (e) { setError(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
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
      {/* THE CONSOLE BLOCK: one grid, six cards, the shape an operator reads.
          Left two columns run Status -> Active Broadcast -> Controls and
          Targets. The right column is the chat, spanning all three rows.

          One grid rather than three stacked ones, because a tall card can only
          span rows that belong to the same grid - and equal heights across a
          row is something grid does for free and hand-tuned heights never
          quite manage. */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 lg:auto-rows-min lg:items-stretch">
        <div className="lg:col-span-4 lg:h-full flex flex-col border border-slate-200 bg-white rounded-md shadow-sm p-4"
             data-testid="broadcast-status-card">
          {/* Label, state and clock on ONE line. The clock was 5xl, which
              made a card that only reports taller than the card you operate.
              min-w-0 with truncate on the campaign name so a long name
              shortens instead of pushing the clock off the row. */}
          <div className="flex items-center gap-3">
            <div className="text-[11px] font-bold uppercase tracking-[0.15em] text-slate-500 shrink-0">
              Status
            </div>
            <div className="flex min-w-0 flex-1 items-center gap-2">
              {isLive ? (
                <>
                  <span className="live-dot shrink-0" />
                  <span className="shrink-0 text-sm font-bold uppercase tracking-widest text-red-600" data-testid="live-indicator">LIVE</span>
                  <span className="truncate text-sm text-slate-700" title={current?.session?.campaign_name}>
                    · {current?.session?.campaign_name}
                  </span>
                </>
              ) : (
                <span className="text-sm font-semibold uppercase tracking-widest text-slate-500">Idle · Ready</span>
              )}
            </div>
            <div className="shrink-0 font-mono text-4xl md:text-5xl tracking-tighter text-slate-900" data-testid="live-timer">
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


        {/* A CEILING, and the list scrolls inside it. Uncapped, ten
            listeners made this card taller than the viewport: the page
            grew because the room did. */}
        <div className="lg:col-start-1 lg:col-span-4 lg:row-start-2 lg:max-h-[32rem] lg:overflow-y-auto"
             data-testid="console-audience-card">
          {liveSessionId ? (
            <WebAudiencePanel sessionId={liveSessionId} compact />
          ) : (
            <div className="lg:h-full border border-slate-200 bg-white rounded-md shadow-sm p-4">
              <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">
                Web Audience
              </div>
              <p className="mt-2 text-sm text-slate-500">
                A shareable listener link is created when this Broadcast starts.
              </p>
            </div>
          )}
        </div>

        {/* Target summary */}

        {/* WEB CHAT, the full height of the block on the right.
            It is tall on purpose: a conversation is a list that grows, and a
            card that grew with it would push Targets and Controls around the
            page every time somebody typed. Placed beside the Web Audience
            because they are the same people.

            The card is here and says so plainly while the backend half is
            being finished. An empty box implying a working feature would be
            worse than a sentence saying it is not one yet. */}
        <div className="lg:col-start-9 lg:col-span-4 lg:row-start-1 lg:row-span-4 lg:h-full lg:sticky lg:top-0 lg:max-h-[calc(100vh-9rem)]">
          {liveSessionId ? (
            <BroadcastChatPanel sessionId={liveSessionId} />
          ) : (
            // No session, no room, nothing to say in it. An empty message list
            // would imply a conversation that has not started rather than one
            // that cannot exist yet.
            <div className="flex h-full min-h-[22rem] flex-col border border-slate-200 bg-white rounded-md shadow-sm p-4"
                 data-testid="chat-card">
              <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">
                Web Chat
              </div>
              <div className="flex flex-1 flex-col items-center justify-center py-8 text-center">
                <p className="max-w-[16rem] text-sm text-slate-500">
                  Chat opens with the Broadcast. Listeners who join through the
                  link can message you here.
                </p>
              </div>
            </div>
          )}
        </div>

      {active?.mine && (
        <div data-testid="my-active-broadcast"
             className="lg:col-start-1 lg:col-span-8 lg:row-start-3 border border-blue-200 bg-blue-50/60 rounded-md shadow-sm p-4">
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

      {active?.may_manage_active && (
        <div data-testid="active-broadcasts-badge"
             className="lg:col-start-1 lg:col-span-8 lg:row-start-4 border border-slate-200 bg-white rounded-md shadow-sm px-4 py-3 flex items-center justify-between gap-4">
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

        <div className="lg:col-start-5 lg:col-span-4 lg:row-start-1 lg:h-full border border-slate-200 bg-white rounded-md shadow-sm p-4 space-y-3"
             data-testid="console-controls-card">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold text-slate-900">Broadcast Controls</h2>
            {error && <div data-testid="console-error" className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1">{error}</div>}
          </div>

          <div className="grid grid-cols-1 gap-3">
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
                {ALL_TARGET_MODES.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>
            )}

            {targetMode === "region" && (
              <div>
                <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Zone</label>
                {/* Several zones, not one. A campaign is almost never one
                    zone, and with a single-value picker "the North and the
                    South" meant either two broadcasts - two microphones, two
                    sets of leases, two things to remember to stop - or
                    picking every shop by hand. */}
                <FilterSelect testId="region-select" allLabel="— select —"
                              value={region} onChange={setRegion}
                              options={meta.regions} disabled={isLive} />
              </div>
            )}
            {targetMode === "city" && (
              <div>
                <label className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">City</label>
                <FilterSelect testId="city-select" allLabel="— select —"
                              value={city} onChange={setCity}
                              options={meta.cities} disabled={isLive} />
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

        <div className="lg:col-start-5 lg:col-span-4 lg:row-start-2 lg:h-full border border-slate-200 bg-white rounded-md shadow-sm p-4 space-y-2.5" data-testid="target-summary">
          <div className="flex items-center gap-2">
            <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">Broadcast Targets</div>
            {targetMode === ONLY_WITH_LINK && (
              // Zero targets is correct here, so it is labelled rather than
              // left looking like a Broadcast that failed to find any.
              <span data-testid="web-only-badge"
                    className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-slate-600">
                Web only
              </span>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            {/* TARGETS rather than SELECTED: nobody selects anything in the
                automatic modes, and "Selected 0" beside a live Zone broadcast
                reads as a fault. It means the same thing in every mode - the
                Stores this broadcast would reach. */}
            <StatCard label="Targets" value={targetIds.length} testid="stat-selected" icon={<Users size={14} className="text-slate-500"/>} />
            <StatCard label="Online" value={onlineCount} testid="stat-online" icon={<Wifi size={14} className="text-emerald-600"/>} color="emerald" />
            <StatCard
              label={targetMode === "online_only" ? "Excluded" : "Offline"}
              value={targetMode === "online_only" ? excludedOfflineCount : offlineCount}
              testid="stat-offline"
              icon={<WifiOff size={14} className="text-slate-400"/>} />
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

      {/* ZONE ACTIONS, live only.
          One action across a whole Zone or City, for the moment something is
          wrong in one part of the estate and clicking through forty rows is
          not a plan. Add and Resume are the slow pair - each waits for a
          Receiver to report ready - and the button says so rather than
          looking hung.

          Neither selector is required and they combine, so "the DELHI shops in
          NORTH" is one action. Both empty is refused by the backend, because
          an empty selector would mean the whole estate. */}
      {isLive && mayDeliverToStores && targetMode !== ONLY_WITH_LINK && (
        <div className="border border-slate-200 bg-white rounded-md shadow-sm p-4"
             data-testid="zone-actions">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">
              Zone Actions
            </div>
            <label htmlFor="zone-action-region" className="sr-only">Zone</label>
            <SearchableSelect testId="zone-action-region" placeholder="Any Zone"
                              value={zoneScope.region} disabled={zoneBusy}
                              options={meta.regions || []}
                              onChange={(value) => setZoneScope(
                                (was) => ({ ...was, region: value }))} />
            <label htmlFor="zone-action-city" className="sr-only">City</label>
            <SearchableSelect testId="zone-action-city" placeholder="Any City"
                              value={zoneScope.city} disabled={zoneBusy}
                              options={meta.cities || []}
                              onChange={(value) => setZoneScope(
                                (was) => ({ ...was, city: value }))} />

            <div className="ml-auto flex flex-wrap gap-2">
              {[
                { action: "add", label: "Add all", slow: true,
                  className: "border-blue-300 text-blue-800 hover:bg-blue-50" },
                { action: "pause", label: "Pause all", slow: false,
                  className: "border-amber-300 text-amber-800 hover:bg-amber-50" },
                { action: "resume", label: "Resume all", slow: true,
                  className: "border-emerald-300 text-emerald-800 hover:bg-emerald-50" },
                { action: "remove", label: "Remove all", slow: false,
                  className: "border-red-300 text-red-800 hover:bg-red-50" },
              ].map((option) => (
                <button key={option.action} type="button"
                        data-testid={`zone-${option.action}`}
                        disabled={zoneBusy || (!zoneScope.region && !zoneScope.city)}
                        onClick={() => runZoneAction(option.action)}
                        title={option.slow
                          ? "Each Store waits for its Receiver to report ready, so this can take a few seconds per shop."
                          : undefined}
                        className={`rounded border bg-white px-2 py-1 text-xs font-semibold disabled:opacity-40 ${option.className}`}>
                  {zoneBusy ? "Working…" : option.label}
                </button>
              ))}
            </div>
          </div>

          {!zoneScope.region && !zoneScope.city && (
            <p className="mt-2 text-xs text-slate-500" data-testid="zone-needs-scope">
              Choose a Zone or a City first. Without one this would mean every
              Store in the estate.
            </p>
          )}

          {zoneResult && (
            <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-2"
                 data-testid="zone-result">
              {zoneResult.error ? (
                <p role="alert" className="text-sm text-red-700">{zoneResult.error}</p>
              ) : (
                <>
                  <p className="text-sm text-slate-800">
                    <span className="font-semibold uppercase">{zoneResult.action}</span>
                    {" — "}
                    <span data-testid="zone-result-summary">
                      {zoneResult.succeeded} of {zoneResult.requested} Store
                      {zoneResult.requested === 1 ? "" : "s"}
                    </span>
                  </p>
                  {/* Only the refusals are listed. A wall of green ticks buries
                      the two rows that need an operator's attention. */}
                  {(zoneResult.results || []).filter((row) => !row.ok).map((row) => (
                    <p key={row.store_id} data-testid={`zone-failed-${row.store_id}`}
                       className="mt-1 text-xs text-red-700">
                      {(stores.find((s) => s.id === row.store_id) || {}).store_code
                        || `Store ${row.store_id}`}: {row.detail}
                    </p>
                  ))}
                </>
              )}
            </div>
          )}
        </div>
      )}

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
          {/* The same multi-value control the rest of the site uses.
              Picking targets is where naming several zones matters MOST: a
              campaign is almost never one zone, and with a single-value
              filter the operator had to tick shops from one zone, change the
              filter, and trust that the first lot were still selected. */}
          <FilterSelect label="" testId="stores-filter-zone" allLabel="All Zones"
                        value={filterZone} onChange={setFilterZone}
                        options={(meta.regions || []).map((zone) => ({
                          value: zone, label: zone }))} />
          <FilterSelect label="" testId="stores-filter-city" allLabel="All Cities"
                        value={filterCity} onChange={setFilterCity}
                        options={(meta.cities || []).map((city) => ({
                          value: city, label: city }))} />
          <FilterSelect label="" testId="stores-filter-status" allLabel="All Statuses"
                        value={filterStatus === "all" ? "" : filterStatus}
                        onChange={(value) => setFilterStatus(value || "all")}
                        options={[{ value: "online", label: "Online" },
                                  { value: "offline", label: "Offline" }]} />
          <button data-testid="stores-clear-filters" onClick={clearStoreFilters}
                  className="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">
            Clear filters
          </button>
          {targetMode === "selected" && (
            <>
              {/* Two named actions rather than one ambiguous "Select all".
                  Beside a paginated table that phrase means one thing to
                  somebody seeing ten rows and another to the code. */}
              <button data-testid="select-page-btn" onClick={selectPage}
                      className="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">
                Select page ({visibleStores.length})
              </button>
              <button data-testid="select-all-filtered-btn" onClick={selectAllFiltered}
                      className="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">
                Select all {filteredStores.length} filtered
              </button>
              <button data-testid="clear-selection-btn" onClick={clearSelection}
                      className="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">
                Clear selection
              </button>
            </>
          )}
        </div>

        {/* What the filters actually produced, and where in it we are. The
            total is of AUTHORISED Stores: a scoped operator is never told how
            much fleet they cannot see. */}
        <div className="px-4 py-2 border-b border-slate-200 flex flex-wrap items-center gap-3 text-xs text-slate-600">
          <span data-testid="stores-result-count">
            {filteredStores.length === 0
              ? "No Stores match these filters"
              : `Showing ${(safeStorePage - 1) * storePageSize + 1}\u2013${
                  Math.min(safeStorePage * storePageSize, filteredStores.length)
                } of ${filteredStores.length}`}
            {filteredStores.length !== stores.length && ` (of ${stores.length} authorised)`}
          </span>
          {targetMode === "selected" && (
            // Selection is independent of the visible page, so it is reported
            // separately - otherwise hiding a selected Store looks like losing
            // it.
            <span data-testid="stores-selected-count" className="font-semibold text-slate-800">
              {selectedIds.size} selected
            </span>
          )}
          <span className="ml-auto flex items-center gap-2">
            <label className="text-slate-500">Per page</label>
            <select data-testid="stores-page-size" value={storePageSize}
                    onChange={(e) => setStorePageSize(Number(e.target.value))}
                    className="px-1.5 py-1 border border-slate-300 rounded bg-white">
              {STORE_PAGE_SIZES.map((size) => (
                <option key={size} value={size}>{size}</option>
              ))}
            </select>
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
              <tr>
                {targetMode === "selected" && <th className="px-3 py-2 w-10"></th>}
                <PickerTh column="store_code" label="Code" sort={storeSort}
                          onSort={setStoreSort} />
                <PickerTh column="store_name" label="Name" sort={storeSort}
                          onSort={setStoreSort} />
                <PickerTh column="city" label="City / Zone" sort={storeSort}
                          onSort={setStoreSort} />
                <PickerTh column="status" label="Status" sort={storeSort}
                          onSort={setStoreSort} />
                <PickerTh column="play_status" label="Play Status"
                          sort={storeSort} onSort={setStoreSort} />
                {isLive && (
                  <th className="px-3 py-2" title="Add or remove this Store without interrupting the rest of the broadcast.">
                    In Broadcast
                  </th>
                )}
                {isLive && mayControlAudio && (
                  <th className="px-3 py-2" title="Controls the SpeakLink audio output on the Store PC. The amplifier's physical volume control is separate.">
                    Store Output
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {visibleStores.map((s) => {
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
                    {isLive && (
                      // Add / Remove for ONE Store, mid-broadcast. The state
                      // shown is lifecycle_state - whether the Store is IN the
                      // broadcast - never play_status, which is about sound.
                      <td className="px-3 py-2 align-top">
                        <LiveTargetAction
                          store={s}
                          inBroadcast={isTarget}
                          state={t?.lifecycle_state}
                          busy={rowBusyStoreId === s.id}
                          // One at a time. Two adds in flight would each wait
                          // on a different Receiver while the operator has no
                          // way to tell which row the next answer belongs to.
                          disabled={busy || rowBusyStoreId !== null}
                          busyElsewhere={busyElsewhere}
                          online={isReceiverOnline(s)}
                          error={rowErrors[s.id]}
                          onAdd={() => addStoreLive(s)}
                          onRemove={() => removeStoreLive(s)}
                          onPause={() => pauseStoreLive(s)}
                          onResume={() => resumeStoreLive(s)}
                        />
                      </td>
                    )}
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
                <tr><td colSpan={6 + (isLive ? 1 : 0) + (isLive && mayControlAudio ? 1 : 0)}
                        className="px-3 py-6 text-center text-slate-500">No stores found.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="p-3 border-t border-slate-200 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-slate-600" data-testid="stores-page-info">
            Page {safeStorePage} of {storePageCount}
          </span>
          <span className="ml-auto flex gap-2">
            <button data-testid="stores-prev-page"
                    disabled={safeStorePage <= 1}
                    onClick={() => setStorePage(safeStorePage - 1)}
                    className="px-3 py-1.5 rounded-md border border-slate-300 disabled:opacity-40 hover:bg-slate-100">
              Previous
            </button>
            <button data-testid="stores-next-page"
                    disabled={safeStorePage >= storePageCount}
                    onClick={() => setStorePage(safeStorePage + 1)}
                    className="px-3 py-1.5 rounded-md border border-slate-300 disabled:opacity-40 hover:bg-slate-100">
              Next
            </button>
          </span>
        </div>
      </div>
      )}

      {/* The web audience, kept entirely separate from the Store table above.
          Stores and listeners are different delivery classes with different
          failure modes, and one merged list would invite reading a listener's
          Buffering as a shop problem. */}


      {confirmOpen && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" data-testid="confirm-modal">
          <div className="bg-white rounded-md shadow-xl max-w-md w-full p-6">
            <div className="flex items-start gap-3 mb-3">
              <div className="p-2 rounded bg-red-100 text-red-700"><Radio size={20}/></div>
              <div>
                <h3 className="text-lg font-semibold">Confirm Live Broadcast</h3>
                {/* A link-only broadcast reaches nobody in a shop, and telling
                    an operator otherwise at the moment they confirm is the
                    worst place to be wrong. */}
                <p className="text-sm text-slate-500" data-testid="confirm-delivery-copy">
                  {targetMode === ONLY_WITH_LINK
                    ? "Your voice will be broadcast to approved web listeners in real time. No Store will play it."
                    : "Your voice will be transmitted to the selected stores in real-time."}
                </p>
              </div>
            </div>
            <div className="bg-slate-50 border border-slate-200 rounded-md p-3 text-sm space-y-1 my-3">
              <div><span className="text-slate-500">Campaign:</span> <span className="font-medium">{campaign}</span></div>
              <div><span className="text-slate-500">Target Mode:</span> <span className="font-medium">{ALL_TARGET_MODES.find((m) => m.value === targetMode)?.label}</span></div>
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

/**
 * Add or remove ONE Store while the broadcast is on air.
 *
 * Three honest states, and the middle one is the reason this is a component
 * rather than a button:
 *
 *   in the broadcast      - Remove, and the lifecycle state in words
 *   not in it, reachable  - Add
 *   not in it, unreachable- no action, and WHY not
 *
 * A Store whose Receiver is offline, or which another broadcast is holding,
 * gets no Add button at all rather than one that fails when pressed. The
 * backend refuses both cases anyway; offering the control would be a promise
 * the page already knows it cannot keep.
 *
 * PREPARING is shown while an add is in flight because it can take seconds -
 * the Receiver has to acknowledge before the Store can join at the live edge.
 * Silence for that long reads as a dead button.
 */
function LiveTargetAction({
  store, inBroadcast, state, busy, disabled, busyElsewhere, online, error,
  onAdd, onRemove, onPause, onResume,
}) {
  const code = store.store_code;
  const settling = state === "ADDING" || state === "PREPARING"
    || state === "REMOVING" || state === "PAUSING";
  const paused = state === "PAUSED";
  return (
    <div className="space-y-1">
      {inBroadcast ? (
        <div className="flex flex-wrap items-center gap-2">
          {/* PAUSE IS NOT REMOVE, and the buttons say which is which. A paused
              Store keeps its place in the broadcast and its lease, so nobody
              else can take the shop while it is quiet; a removed one is let
              go. Offering only one of them would push operators into using
              Remove for a thirty-second silence. */}
          {paused ? (
            <button type="button" data-testid={`resume-store-${code}`}
                    onClick={onResume} disabled={disabled || busy}
                    title={`Bring ${code} back into this broadcast. It rejoins at the live edge.`}
                    className="rounded border border-emerald-300 bg-white px-2 py-1 text-xs font-semibold text-emerald-800 hover:bg-emerald-50 disabled:opacity-40">
              {busy ? "Resuming…" : "Resume"}
            </button>
          ) : (
            <button type="button" data-testid={`pause-store-${code}`}
                    onClick={onPause} disabled={disabled || busy}
                    title={`Silence ${code} without taking it out. Its place in this broadcast is kept.`}
                    className="rounded border border-amber-300 bg-white px-2 py-1 text-xs font-semibold text-amber-800 hover:bg-amber-50 disabled:opacity-40">
              {busy ? "Pausing…" : "Pause"}
            </button>
          )}
          <button type="button" data-testid={`remove-store-${code}`}
                  onClick={onRemove} disabled={disabled || busy}
                  title={`Take ${code} out of this broadcast. The other Stores keep playing.`}
                  className="rounded border border-red-300 bg-white px-2 py-1 text-xs font-semibold text-red-800 hover:bg-red-50 disabled:opacity-40">
            {busy ? "Removing…" : "Remove"}
          </button>
          {(settling || paused) && (
            <span data-testid={`target-state-${code}`}
                  className={`text-[10px] uppercase tracking-wider ${
                    paused ? "text-amber-800 font-bold" : "text-amber-700"}`}>
              {state}
            </span>
          )}
        </div>
      ) : busyElsewhere ? (
        // Says WHAT, never WHO - the same rule the In-use badge follows.
        <span data-testid={`add-blocked-${code}`} className="text-xs text-amber-800">
          In another broadcast
        </span>
      ) : !online ? (
        <span data-testid={`add-blocked-${code}`} className="text-xs text-slate-500">
          Receiver offline
        </span>
      ) : (
        <button type="button" data-testid={`add-store-${code}`}
                onClick={onAdd} disabled={disabled || busy}
                title={`Add ${code} to this broadcast. It joins at the live edge, not from the beginning.`}
                className="rounded border border-blue-300 bg-white px-2 py-1 text-xs font-semibold text-blue-800 hover:bg-blue-50 disabled:opacity-40">
          {busy ? "Adding…" : "Add"}
        </button>
      )}
      {error && (
        // On the row, because an operator adding one shop out of forty needs
        // to know which one refused and why.
        <p role="alert" data-testid={`target-error-${code}`}
           className="max-w-[16rem] text-[11px] leading-snug text-red-700">
          {error}
        </p>
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

/**
 * A sortable heading for the Store picker.
 *
 * Deliberately not the shared SortableTh: that one drives a SERVER query,
 * because those tables hold one page of a longer list. This table holds every
 * Store the account can see, already in memory - so ordering it here orders
 * all of it, and a round trip would buy nothing.
 */
function PickerTh({ column, label, sort, onSort }) {
  const active = sort.column === column;
  const toggle = () => {
    if (!active) return onSort({ column, dir: "asc" });
    if (sort.dir === "asc") return onSort({ column, dir: "desc" });
    // Third click restores the list's own order, so there is a way back.
    return onSort({ column: "", dir: "asc" });
  };
  return (
    <th className="px-3 py-2"
        aria-sort={active ? (sort.dir === "desc" ? "descending" : "ascending")
                          : "none"}>
      <button type="button" onClick={toggle} data-testid={`picker-sort-${column}`}
              className="inline-flex items-center gap-1 hover:text-slate-900">
        {label}
        <span aria-hidden="true" className={active ? "text-slate-900" : "text-slate-300"}>
          {active ? (sort.dir === "desc" ? "↓" : "↑") : "⇅"}
        </span>
      </button>
    </th>
  );
}
