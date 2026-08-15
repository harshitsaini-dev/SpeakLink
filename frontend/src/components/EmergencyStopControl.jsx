import React from "react";
import { createPortal } from "react-dom";
import { AlertOctagon } from "lucide-react";
import { useBroadcast } from "@/contexts/BroadcastContext";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Stop every active broadcast, from anywhere in SpeakLink.
 *
 * It lives in the sidebar rather than on the Console because of WHEN it is
 * used: something wrong is going out over the speakers and it has to stop now.
 * On the Console it was reachable only from one page - an operator watching
 * Receiver Status or Broadcast History had to navigate first, which is exactly
 * the moment navigation is worth the least.
 *
 * It is NOT Stop Broadcast. Stop ends the session you are running; this ends
 * every session anybody is running, which is why it keeps its own permission,
 * a typed confirmation step, and a sentence naming the blast radius. An
 * operator who meant to silence one Store must not silence forty-four by
 * pressing the nearest red thing.
 *
 * The confirmation is a dialog rather than a hold-to-confirm: under pressure a
 * timed gesture is a control that fails when it is needed, and the danger here
 * is confusing this with Stop, not pressing it by accident.
 */
export default function EmergencyStopControl() {
  const { can } = useAuth();
  const { emergencyStop } = useBroadcast();
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);

  // No permission, no button. The backend refuses it either way - this is
  // presentation, not the boundary.
  if (!can("broadcast.emergency_stop")) return null;

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const outcome = await emergencyStop();
      const count = (outcome?.session_ids || []).length;
      setResult({
        ok: true,
        message: count
          ? `Emergency Stop: ${count} broadcast${count === 1 ? "" : "s"} stopped.`
          : "Emergency Stop: there were no active broadcasts.",
      });
    } catch (e) {
      setResult({
        ok: false,
        message: e?.response?.data?.detail || e.message || "Emergency Stop failed.",
      });
    } finally {
      setBusy(false);
      setOpen(false);
    }
  };

  return (
    <>
      <button
        type="button"
        data-testid="emergency-stop-btn"
        onClick={() => setOpen(true)}
        disabled={busy}
        title="Stop every active broadcast, including other operators'."
        // Deliberately NOT glass. Everything else on this screen is a pane
        // you can see through; this is the one control that must read as a
        // physical button, and translucency would make the loudest action in
        // the product look like a label.
        className="w-full flex items-center justify-center gap-2 rounded-xl px-3 py-4
                   text-base font-bold uppercase tracking-wider text-white
                   bg-gradient-to-b from-red-500 to-red-700
                   shadow-[0_6px_18px_rgba(220,38,38,0.35)]
                   ring-1 ring-inset ring-red-300/40
                   hover:from-red-500 hover:to-red-600
                   active:from-red-700 active:to-red-800
                   disabled:from-red-300 disabled:to-red-400 disabled:shadow-none"
      >
        <AlertOctagon size={20} /> Emergency Stop
      </button>

      {result && (
        // Stays until it is dismissed. An outcome that vanished on its own
        // would be an outcome an operator can miss, and "there were no active
        // broadcasts" is exactly the answer somebody needs to read twice.
        <div role="alert" data-testid="emergency-result"
             className={`mt-2 rounded border px-2 py-1.5 text-[11px] leading-snug ${result.ok
               ? "border-slate-700 bg-slate-800 text-slate-200"
               : "border-red-500 bg-red-950 text-red-200 font-semibold"}`}>
          {result.message}
          <button type="button" data-testid="emergency-result-dismiss"
                  onClick={() => setResult(null)}
                  className="ml-2 underline">
            Dismiss
          </button>
        </div>
      )}

      {/* PORTALLED TO THE BODY, and it has to be.
          The sidebar carries a translate for its slide-in, and an ancestor
          with a transform becomes the containing block for position:fixed -
          so a "full screen" overlay rendered here was clipped to a 16rem
          column and the confirmation appeared squeezed inside the navigation.
          The portal takes it out of that subtree entirely. */}
      {open && createPortal((
        <div className="fixed inset-0 z-50 scrim flex items-center justify-center p-4"
             data-testid="emergency-confirm-modal">
          <div className="glass w-full max-w-md p-5 space-y-3">
            <h3 className="font-bold text-red-900 uppercase tracking-wide">
              Stop all active broadcasts?
            </h3>
            <p className="text-sm text-strong">
              This stops <strong>every active SpeakLink broadcast</strong>, including
              broadcasts started by other operators - not only your own. Every
              targeted Store is told to stop and every Store is released.
            </p>
            <p className="text-xs text-body">
              SpeakLink cannot confirm that a speaker has fallen silent. This sends
              the stop command and releases the Stores.
            </p>
            <div className="flex gap-2 pt-1">
              <button type="button" data-testid="emergency-cancel-btn"
                      onClick={() => setOpen(false)}
                      className="flex-1 px-4 py-2 border border-line-strong rounded-md text-sm">
                Cancel
              </button>
              <button type="button" data-testid="emergency-confirm-btn" disabled={busy}
                      onClick={run}
                      className="flex-1 px-4 py-2 bg-red-700 text-white rounded-md text-sm font-bold uppercase disabled:opacity-50">
                {busy ? "Stopping…" : "Stop All Broadcasts"}
              </button>
            </div>
          </div>
        </div>
      ), document.body)}
    </>
  );
}
