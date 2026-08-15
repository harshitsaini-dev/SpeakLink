import React from "react";
import { Volume2, VolumeX } from "lucide-react";

/**
 * One Store's SpeakLink output control, sized to live in a table row.
 *
 * Compact on purpose. Forty of these are on screen during a normal broadcast
 * and a hundred is plausible, so it is a slider, a number and a toggle on one
 * line - not a panel. Anything taller turns the Store list into a page nobody
 * can scan.
 *
 * WHAT IT CLAIMS
 *
 * This controls the SpeakLink audio output on the Store PC. The amplifier's
 * physical volume control is separate and nothing here can see or change it.
 * "Applied 60%" means the Store's software output is at 60% of the decoded
 * signal - never that the room is at 60% of anything, and never that anyone
 * heard it.
 */

function statusLabel(state, error, online, supported) {
  if (error) return { text: error, tone: "error" };
  if (!online) return { text: "Receiver offline", tone: "muted" };
  if (!supported) {
    // Four states, not two. "Not supported by this Receiver" used to cover a
    // build that predates master control AND a current build whose Store has
    // simply not re-selected its audio output since upgrading. Those need
    // completely different things - a new Store Kit, or half a minute in
    // Store Setup - and telling an operator the wrong one sends them to
    // rebuild software that was already correct.
    switch (state?.control_status) {
      case "needs_output_selection":
        return { text: "Re-select the Store audio output", tone: "warn" };
      case "unavailable":
        return { text: "Store audio output unavailable", tone: "error" };
      default:
        // "unknown" - the Receiver never reported capabilities at all, which
        // is what an older build does. Genuinely unsupported.
        return { text: "Not supported by this Receiver", tone: "muted" };
    }
  }
  if (!state) return { text: "", tone: "muted" };
  if (state.pending) return { text: "Sending…", tone: "pending" };
  if (state.result === "failed") {
    return { text: state.error_message || "Could not apply", tone: "error" };
  }
  if (state.result === "unsupported") {
    // The Receiver answered a command with "unsupported". Same four-state
    // reasoning as above: say WHICH kind, so the operator knows whether to
    // touch the Store PC or the Store Kit.
    if (state.control_status === "needs_output_selection") {
      return { text: "Re-select the Store audio output", tone: "warn" };
    }
    return { text: "Not supported by this Receiver", tone: "muted" };
  }
  if (state.result === "applied") {
    // If the Store has since reported a DIFFERENT value - somebody moved the
    // Windows slider at the till - then "Applied 80%" is history, not the
    // current state, and saying it would be the stale display this whole
    // feature exists to remove.
    const actual = state.actual_volume_percent;
    const hasActual = actual !== null && actual !== undefined;
    const drifted = hasActual
      && (actual !== state.applied_volume_percent
          || (state.actual_muted ?? false) !== (state.applied_muted ?? false));
    if (drifted) {
      return { text: state.actual_muted ? "Currently muted" : `Currently ${actual}%`,
               tone: "ok" };
    }
    const applied = state.applied_muted ? "Muted" : `Applied ${state.applied_volume_percent}%`;
    return { text: applied, tone: "ok" };
  }
  return { text: "", tone: "muted" };
}

const TONE_CLASS = {
  ok: "text-emerald-700",
  // Amber, not red: needing an output re-selected is a task, not a fault.
  warn: "text-amber-700",
  pending: "text-muted",
  error: "text-red-700",
  muted: "text-faint",
};

export default function StoreAudioControl({
  store, state, error, online, supported, disabled,
  onVolumeChange, onMuteToggle,
}) {
  // The slider follows what the Store is ACTUALLY doing, falling back to the
  // requested value only until the first reading arrives. HQ asking for 80%
  // and the person at the till moving it to 25% are both true, and only the
  // second describes the shop - so 25 is what an operator must see, without
  // touching anything first.
  //
  // While a command is in flight the requested value is shown instead: the
  // slider must not jump back under the operator's finger between the drag
  // and the acknowledgement.
  const actualVolume = state?.actual_volume_percent;
  const actualMuted = state?.actual_muted;
  const hasActual = actualVolume !== null && actualVolume !== undefined;
  const requested = (state?.pending || !hasActual)
    ? (state?.requested_volume_percent ?? 100)
    : actualVolume;
  const muted = (state?.pending || actualMuted === null || actualMuted === undefined)
    ? (state?.requested_muted ?? false)
    : actualMuted;
  const status = statusLabel(state, error, online, supported);
  const unavailable = disabled || !online || !supported;
  const sliderId = `store-volume-${store.store_code}`;

  return (
    <div className="flex items-center gap-2 min-w-[220px]">
      <button
        type="button"
        data-testid={`store-mute-${store.store_code}`}
        aria-pressed={muted}
        aria-label={`${muted ? "Unmute" : "Mute"} ${store.store_name} output`}
        disabled={unavailable}
        onClick={() => onMuteToggle(!muted)}
        className={`inline-flex items-center rounded border px-1.5 py-1 ${
          muted ? "border-red-400 bg-red-100 text-red-800"
                : "border-line-strong text-body"
        } ${unavailable ? "opacity-40 cursor-not-allowed" : "hover:bg-surface-muted"}`}
      >
        {muted ? <VolumeX size={13} /> : <Volume2 size={13} />}
      </button>

      <label htmlFor={sliderId} className="sr-only">
        {`${store.store_name} output volume`}
      </label>
      <input
        id={sliderId}
        data-testid={`store-volume-${store.store_code}`}
        type="range"
        min="0"
        max="100"
        step="1"
        value={requested}
        disabled={unavailable}
        aria-valuetext={`${requested} percent`}
        onChange={(event) => onVolumeChange(event.target.value)}
        className="w-24 accent-blue-600 disabled:opacity-40"
      />

      <span
        data-testid={`store-volume-value-${store.store_code}`}
        className="w-9 text-right text-xs font-mono text-body"
      >
        {muted ? "—" : `${requested}%`}
      </span>

      <span
        data-testid={`store-audio-status-${store.store_code}`}
        className={`text-[10px] truncate max-w-[130px] ${TONE_CLASS[status.tone]}`}
        title={status.text}
      >
        {status.text}
      </span>
    </div>
  );
}
