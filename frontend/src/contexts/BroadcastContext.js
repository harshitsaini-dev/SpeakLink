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

  const load = useCallback(async () => {
    const { data } = await api.get("/broadcast/current");
    setCurrent(data);
    return data;
  }, []);

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
        "This browser cannot record WebM/Opus audio. SpeakLink will not send a " +
        "different format silently. Try a current Chrome or Edge browser."
      );
    }

    await api.post(`/broadcast/sessions/${session.id}/start`);

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
      wsUrl: `${wsUrl("/ws/broadcaster")}?ticket=${encodeURIComponent(uplink.ticket)}`,
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
    await bc.start();
    broadcasterRef.current = bc;
    await load();
  }, [load, waitForReceiverReady]);

  const stopBroadcast = useCallback(async () => {
    setError("");
    if (current?.session?.id) {
      await api.post(`/broadcast/sessions/${current.session.id}/stop`);
    }
    if (broadcasterRef.current) {
      await broadcasterRef.current.stop();
      broadcasterRef.current = null;
    }
    setMeter(0);
    setBroadcasterStatus("idle");
    await load();
  }, [current, load]);

  const emergencyStop = useCallback(async () => {
    setError("");
    await api.post("/broadcast/emergency-stop");
    if (broadcasterRef.current) {
      await broadcasterRef.current.stop();
      broadcasterRef.current = null;
    }
    setMeter(0);
    setBroadcasterStatus("idle");
    await load();
  }, [load]);

  const value = {
    current, load, isLive,
    meter, broadcasterStatus, error, setError,
    startBroadcast, stopBroadcast, emergencyStop,
    hasActiveBroadcaster: () => Boolean(broadcasterRef.current),
  };

  return <BroadcastCtx.Provider value={value}>{children}</BroadcastCtx.Provider>;
}

export const useBroadcast = () => useContext(BroadcastCtx);
