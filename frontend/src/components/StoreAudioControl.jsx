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
    // The APPLIED value, reported by the Store - not the requested one.
    const applied = state.applied_muted ? "Muted" : `Applied ${state.applied_volume_percent}%`;
    return { text: applied, tone: "ok" };
  }
  return { text: "", tone: "muted" };
}

const TONE_CLASS = {
  ok: "text-emerald-700",
  // Amber, not red: needing an output re-selected is a task, not a fault.
  warn: "text-amber-700",
  pending: "text-slate-500",
  error: "text-red-700",
  muted: "text-slate-400",
};

export default function StoreAudioControl({
  store, state, error, online, supported, disabled,
  onVolumeChange, onMuteToggle,
}) {
  const requested = state?.requested_volume_percent ?? 100;
  const muted = state?.requested_muted ?? false;
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
                : "border-slate-300 text-slate-600"
        } ${unavailable ? "opacity-40 cursor-not-allowed" : "hover:bg-slate-50"}`}
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
        className="w-9 text-right text-xs font-mono text-slate-700"
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
