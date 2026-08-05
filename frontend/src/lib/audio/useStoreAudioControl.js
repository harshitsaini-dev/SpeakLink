import React from "react";
import { api } from "@/lib/api";

/**
 * Per-Store output volume and mute for the broadcast this operator is running.
 *
 * COALESCING, NOT DEBOUNCING
 *
 * Dragging a slider fires an onChange for every pixel. Sending each one would
 * put hundreds of messages a second on a socket that is also carrying audio.
 * Debouncing alone is the obvious fix and the wrong one: it makes the FIRST
 * movement wait too, so the control feels dead for a moment before it jumps.
 *
 * This sends the first change immediately and then, per Store, at most one
 * every SEND_INTERVAL_MS - always the LATEST value, never a queued backlog of
 * intermediate ones. Nobody cares that the slider passed through 47 on its way
 * to 70; the final value is the whole message, so an intermediate one can be
 * discarded rather than delayed.
 *
 * The pending value is held per Store, so a slow Store cannot hold up another.
 *
 * WHY REQUESTED AND APPLIED ARE BOTH KEPT
 *
 * `requested` is what this operator asked for and is what the slider renders,
 * so the control stays where they put it. `applied` is what the Store said it
 * did, and only arrives on an acknowledgement. A successful POST means the
 * command was sent - never that a shop got quieter.
 */

// Fast enough that a drag feels continuous, slow enough that a five-second
// drag across forty Stores cannot produce more than a few hundred messages.
export const SEND_INTERVAL_MS = 120;

export function useStoreAudioControl({ sessionId, canControl }) {
  const [states, setStates] = React.useState({});
  const [errors, setErrors] = React.useState({});
  // Per-Store timer and latest-unsent value. Refs, not state: changing them
  // must not re-render, and the render loop must not reset them.
  const timers = React.useRef({});
  const pendingValue = React.useRef({});
  const mounted = React.useRef(true);

  React.useEffect(() => () => {
    mounted.current = false;
    Object.values(timers.current).forEach((id) => clearTimeout(id));
    timers.current = {};
  }, []);

  const applyResponse = React.useCallback((rows) => {
    if (!mounted.current || !Array.isArray(rows)) return;
    setStates((previous) => {
      const next = { ...previous };
      for (const row of rows) {
        const existing = next[row.store_id];
        // A response describing an OLDER command must not overwrite a newer
        // one. The same rule the backend and the Receiver both apply: latest
        // command id wins, everywhere, so a slow reply cannot walk a slider
        // backwards after the operator has already moved on.
        if (existing && row.last_command_id < existing.last_command_id) continue;
        next[row.store_id] = row;
      }
      return next;
    });
  }, []);

  const refresh = React.useCallback(async () => {
    if (!sessionId || !canControl) return;
    try {
      const { data } = await api.get(`/broadcast/sessions/${sessionId}/audio-control`);
      applyResponse(data.stores);
    } catch {
      // A failed refresh must not wipe what the operator can see; the next
      // one will correct it.
    }
  }, [sessionId, canControl, applyResponse]);

  React.useEffect(() => { refresh(); }, [refresh]);

  const send = React.useCallback(async (storeId, body) => {
    try {
      const { data } = await api.post(
        `/broadcast/sessions/${sessionId}/audio-control`,
        { store_id: storeId, ...body },
      );
      applyResponse(data.stores);
      setErrors((previous) => {
        if (!previous[storeId]) return previous;
        const next = { ...previous };
        delete next[storeId];
        return next;
      });
    } catch (failure) {
      const status = failure?.response?.status;
      const detail = failure?.response?.data?.detail;
      setErrors((previous) => ({
        ...previous,
        [storeId]: status === 409
          ? "Broadcast is no longer active"
          : (detail || "Could not apply output volume"),
      }));
    }
  }, [sessionId, applyResponse]);

  const schedule = React.useCallback((storeId, body) => {
    if (!sessionId || !canControl) return;
    // Optimistic ONLY for the requested value the slider renders. `applied`
    // is deliberately untouched: it may only ever come from the Store.
    setStates((previous) => ({
      ...previous,
      [storeId]: {
        ...(previous[storeId] || { store_id: storeId, requested_volume_percent: 100,
                                   requested_muted: false, last_command_id: 0,
                                   last_acknowledged_command_id: 0 }),
        ...("volume_percent" in body
          ? { requested_volume_percent: body.volume_percent } : {}),
        ...("muted" in body ? { requested_muted: body.muted } : {}),
        pending: true,
      },
    }));

    if (timers.current[storeId]) {
      // Already rate-limited: keep only the newest value. The queued timer
      // will pick it up, so nothing accumulates.
      pendingValue.current[storeId] = { ...pendingValue.current[storeId], ...body };
      return;
    }

    send(storeId, body);
    timers.current[storeId] = setTimeout(() => {
      delete timers.current[storeId];
      const queued = pendingValue.current[storeId];
      delete pendingValue.current[storeId];
      if (queued) schedule(storeId, queued);
    }, SEND_INTERVAL_MS);
  }, [sessionId, canControl, send]);

  const setVolume = React.useCallback((storeId, percent) => {
    const value = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
    schedule(storeId, { volume_percent: value });
  }, [schedule]);

  // Sends ONLY `muted`, saying nothing about volume, so the operator's chosen
  // level survives and unmuting restores it without the client remembering a
  // copy that could drift from the server's.
  const setMuted = React.useCallback((storeId, muted) => {
    schedule(storeId, { muted: Boolean(muted) });
  }, [schedule]);

  return { states, errors, setVolume, setMuted, refresh };
}
