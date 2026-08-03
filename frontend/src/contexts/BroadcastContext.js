import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, getToken, wsUrl } from "@/lib/api";
import { HQBroadcaster } from "@/lib/audio/HQBroadcaster";
import { createBeforeUnloadGuard } from "@/lib/beforeUnloadGuard";

const BroadcastCtx = createContext(null);

/**
 * Owns the live broadcast's audio pipeline and session polling ABOVE the
 * router's <Outlet/>, not inside BroadcastConsole.
 *
 * Defect this fixes: the mic-level meter went to zero after navigating away
 * from Broadcast Console and back, even though the broadcast was still LIVE.
 * `broadcaster`, `meter`, and `current` used to live in BroadcastConsole's own
 * component state, so unmounting that page (a route change) discarded them;
 * remounting created a fresh `broadcaster = null` with no way to reattach to
 * the still-running MediaStream/AudioContext/WebSocket. This provider is
 * mounted once, above the Router's route content, for as long as the HQ user
 * is signed in - so its `useRef`-held HQBroadcaster instance and its meter
 * state survive every route change. BroadcastConsole becomes a consumer, not
 * an owner.
 */
export function BroadcastProvider({ children }) {
  const [current, setCurrent] = useState(null);
  const [meter, setMeter] = useState(0);
  const [broadcasterStatus, setBroadcasterStatus] = useState("idle");
  const [error, setError] = useState("");
  const broadcasterRef = useRef(null);
  const hqWsRef = useRef(null);
  const guardRef = useRef(null);
  if (!guardRef.current) guardRef.current = createBeforeUnloadGuard();

  // The permission-aware view of EVERY live broadcast, redacted server-side.
  // See GET /api/broadcast/active: `mine` is your own in full, `busy_store_ids`
  // is Scope-filtered and says only that a Store is unavailable, and `sessions`
  // carries owner/campaign ONLY for accounts holding broadcast.view_ownership.
  // The frontend never reconstructs what the backend withheld.
  const [active, setActive] = useState({
    mine: null, sessions: [], busy_store_ids: [], may_view_ownership: false,
    may_view_targets: false, may_manage_active: false, active_count: null,
  });

  // Which active-state request is the current one. Responses can arrive out of
  // order - a request issued during a Start can land after the refresh that
  // followed it - and applying the older one would show a Store as free
  // moments after the backend told us it was taken.
  const activeRequestId = useRef(0);

  const loadActive = useCallback(async () => {
    const mine = ++activeRequestId.current;
    try {
      const { data } = await api.get("/broadcast/active");
      if (mine !== activeRequestId.current) return null;   // superseded
      setActive({
        mine: data?.mine ?? null,
        sessions: data?.sessions ?? [],
        busy_store_ids: data?.busy_store_ids ?? [],
        may_view_ownership: Boolean(data?.may_view_ownership),
        may_view_targets: Boolean(data?.may_view_targets),
        // Drives the compact Console badge only. Null when the account may
        // not open the supervision page - the backend withholds the number
        // itself rather than sending it for the UI to hide.
        may_manage_active: Boolean(data?.may_manage_active),
        active_count: data?.active_count ?? null,
      });
      return data;
    } catch {
      // The caller's own error handling owns the message; leaving the last
      // known state in place is better than blanking the console on one
      // failed poll.
      return null;
    }
  }, []);

  const load = useCallback(async () => {
    const { data } = await api.get("/broadcast/current");
    setCurrent(data);
    await loadActive();
    return data;
  }, [loadActive]);

  // Poll while signed in, independent of which page is mounted, so LIVE state
  // and the receiver acknowledgement counts stay fresh even on pages other
  // than the Console.
  useEffect(() => {
    if (!getToken()) return undefined;
    load().catch(() => {});
    const id = setInterval(() => { load().catch(() => {}); }, 3000);
    return () => clearInterval(id);
  }, [load]);

  // HQ dashboard status socket - a faster nudge on top of the poll above.
  // Ticket-based, single-use; see BroadcastConsole's original note.
  useEffect(() => {
    if (!getToken()) return undefined;
    let socket = null;
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.post("/auth/ws-ticket", { audience: "hq" });
        if (cancelled) return;
        socket = new WebSocket(`${wsUrl("/ws/hq")}?ticket=${encodeURIComponent(data.ticket)}`);
        hqWsRef.current = socket;
        socket.onmessage = () => { load().catch(() => {}); };
        socket.onclose = () => { hqWsRef.current = null; };
      } catch { /* the 3s poll above still keeps state honest without this */ }
    })();
    return () => {
      cancelled = true;
      try { if (socket) socket.close(); } catch { /* */ }
    };
  }, [load]);

  const isLive = Boolean(current?.live);

  // The one and only place the native unload-confirmation is installed or
  // removed. Tied to `isLive`, not to which page is mounted, so navigating
  // inside the SPA never touches it and Stop/Emergency Stop remove it the
  // instant `current.live` goes false.
  useEffect(() => {
    guardRef.current.sync(isLive);
  }, [isLive]);

  useEffect(() => () => guardRef.current.teardown(), []);

  const waitForReceiverReady = useCallback(async (targetIdList, timeoutMs = 20000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const { data } = await api.get("/broadcast/current");
        const ready = (data?.ready_receivers || []).filter((id) => targetIdList.includes(id));
        if (ready.length > 0) return ready;
      } catch { /* keep polling until the deadline */ }
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    return [];
  }, []);

  const startBroadcast = useCallback(async ({ campaign, targetMode, ids, region, city }) => {
    setError("");
    if (!campaign.trim()) throw new Error("Please enter a campaign name");
    if (ids.length === 0) throw new Error("No stores selected for broadcast");

    const { data: session } = await api.post("/broadcast/sessions", {
      campaign_name: campaign.trim(),
      target_mode: targetMode,
      store_ids: targetMode === "selected" ? ids : undefined,
      region: targetMode === "region" ? region : undefined,
      city: targetMode === "city" ? city : undefined,
    });

    if (!HQBroadcaster.supportedMime()) {
      throw new Error(
        "This browser cannot record WebM/Opus audio. EchoCast will not send a " +
        "different format silently. Try a current Chrome or Edge browser."
      );
    }

    try {
      await api.post(`/broadcast/sessions/${session.id}/start`);
    } catch (failure) {
      // STORE_BUSY: somebody claimed one of these Stores between this browser
      // rendering them as free and this request arriving. The local busy state
      // is advisory; the backend's answer is authoritative.
      //
      // Nothing local was started yet - no microphone, no socket - and nothing
      // must be: the whole selection was refused, so broadcasting to "the rest"
      // would put a campaign on air half-targeted without the operator knowing.
      await loadActive();
      const detail = failure?.response?.data?.detail;
      if (detail && detail.code === "STORE_BUSY") {
        const conflict = new Error(
          detail.message ||
          "One or more selected Stores are currently in use by another broadcast."
        );
        conflict.storeBusy = true;
        conflict.busyStoreIds = detail.busy_store_ids || [];
        conflict.busyStoreCodes = detail.busy_store_codes || [];
        throw conflict;
      }
      throw failure;
    }

    setBroadcasterStatus("waiting for receiver readiness");
    const readyIds = await waitForReceiverReady(ids);
    if (readyIds.length === 0) {
      throw new Error(
        "No Receiver reported READY, so no audio was sent. Check that the " +
        "Receiver is running and that FFmpeg is available on it."
      );
    }

    const { data: uplink } = await api.post("/auth/ws-ticket", { audience: "broadcaster" });
    const bc = new HQBroadcaster({
      // session_id is required now. The microphone socket is bound to ONE
      // broadcast server-side, and the server re-reads ownership from the
      // database rather than trusting this value - so sending someone else's
      // id is refused rather than honoured. Passing our own is simply how the
      // socket knows which of several concurrent broadcasts it feeds.
      wsUrl: `${wsUrl("/ws/broadcaster")}?ticket=${encodeURIComponent(uplink.ticket)}`
             + `&session_id=${encodeURIComponent(session.id)}`,
      onMeter: (l) => setMeter(l),
      onStatus: (s) => setBroadcasterStatus(s),
      onError: (m) => setError(m),
    });
    // Do NOT create a second capture: if a broadcaster is somehow already
    // owned (should not happen while isLive is correctly gated by the
    // console's own disabled state), stop it first rather than layering a
    // second MediaStream/MediaRecorder/WebSocket on top.
    if (broadcasterRef.current) {
      await broadcasterRef.current.stop();
    }
    try {
      await bc.start();
    } catch (failure) {
      // A failed start must not leave the microphone open. bc.start() acquires
      // a MediaStream before it opens the socket, so an error anywhere after
      // that point leaves the browser's recording indicator lit with nothing
      // listening - the operator would reasonably believe they were on air.
      try { await bc.stop(); } catch { /* already torn down */ }
      setMeter(0);
      setBroadcasterStatus("idle");
      throw failure;
    }
    broadcasterRef.current = bc;
    await load();
  }, [load, loadActive, waitForReceiverReady]);

  const stopBroadcast = useCallback(async () => {
    setError("");
    // MY session, named explicitly. Never a global stop: the backend refuses
    // anyone else's anyway, but sending an id that is not ours would be a
    // request we had no business making.
    const sessionId = active.mine?.session_id ?? current?.session?.id;
    if (sessionId) {
      await api.post(`/broadcast/sessions/${sessionId}/stop`);
    }
    if (broadcasterRef.current) {
      await broadcasterRef.current.stop();
      broadcasterRef.current = null;
    }
    setMeter(0);
    setBroadcasterStatus("idle");
    await load();
  }, [active, current, load]);

  const emergencyStop = useCallback(async () => {
    setError("");
    let response;
    try {
      response = await api.post("/broadcast/emergency-stop");
    } catch (failure) {
      // Stop this browser's own microphone regardless: whatever happened
      // server-side, an operator who pressed EMERGENCY STOP must not be left
      // holding a live microphone.
      if (broadcasterRef.current) {
        try { await broadcasterRef.current.stop(); } catch { /* */ }
        broadcasterRef.current = null;
      }
      setMeter(0);
      setBroadcasterStatus("idle");
      await load();

      const detail = failure?.response?.data?.detail;
      if (detail && detail.code === "EMERGENCY_STOP_INCOMPLETE") {
        const partial = new Error(
          "SOME BROADCASTS ARE STILL LIVE. " +
          (detail.message || "Not every broadcast could be stopped.")
        );
        partial.emergencyIncomplete = true;
        partial.stoppedSessionIds = detail.stopped_session_ids || [];
        partial.failedSessionIds = detail.failed_session_ids || [];
        throw partial;
      }
      throw failure;
    }

    if (broadcasterRef.current) {
      await broadcasterRef.current.stop();
      broadcasterRef.current = null;
    }
    setMeter(0);
    setBroadcasterStatus("idle");
    await load();
    return response?.data ?? null;
  }, [load]);

  const value = {
    current, load, isLive,
    active, loadActive,
    // Advisory only - the backend's STORE_BUSY is authoritative. A Store this
    // account's own broadcast is using is NOT "busy" to them; it is theirs.
    isStoreBusyForOthers: (storeId) => (
      (active.busy_store_ids || []).includes(storeId)
      && !(active.mine?.target_store_ids || []).includes(storeId)
    ),
    meter, broadcasterStatus, error, setError,
    startBroadcast, stopBroadcast, emergencyStop,
    hasActiveBroadcaster: () => Boolean(broadcasterRef.current),
  };

  return <BroadcastCtx.Provider value={value}>{children}</BroadcastCtx.Provider>;
}

export const useBroadcast = () => useContext(BroadcastCtx);
