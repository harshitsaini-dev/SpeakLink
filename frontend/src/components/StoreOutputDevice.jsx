import React from "react";
import { api } from "@/lib/api";
import { Speaker, RefreshCw } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Which speaker this Store plays through, changed from HQ.
 *
 * WHY THIS CARD IS SO CAUTIOUS
 *
 * Choosing the output device used to require standing at the Store PC, and
 * that was the protection rather than an inconvenience: whoever could get it
 * wrong could also hear the result. They picked a device, pressed Test Sound,
 * and confirmed they heard it before anything was saved.
 *
 * Nobody here can hear anything. A wrong choice makes a shop silent, and
 * silence is the one failure that produces no error, no disconnection and no
 * failed command - it simply plays to nobody until a customer complains.
 *
 * So this card:
 *
 *   * offers only speakers the STORE reported, never a text box;
 *   * says plainly that the list is a snapshot and offers to refresh it;
 *   * reports what the Store CONFIRMED it ended up on, never what was sent;
 *   * refuses to pretend. "Sent" is not "changed", and this never says the
 *     second when it only knows the first.
 */
export default function StoreOutputDevice({ storeId }) {
  const { can } = useAuth();
  const [state, setState] = React.useState(null);
  const [selected, setSelected] = React.useState("");
  const [busy, setBusy] = React.useState("");
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");

  const mayChange = can("receiver.set_output_device");

  const load = React.useCallback(async () => {
    try {
      const { data } = await api.get(`/stores/${storeId}/audio-output`);
      setState(data);
      setSelected((current) => current || data.applied_selector || "");
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || "The speaker settings could not be read.");
    }
  }, [storeId]);

  React.useEffect(() => { load(); }, [load]);

  // Polled while a change is outstanding, and only then. The Store answers in
  // its own time and the operator should not have to press Refresh to find
  // out - but polling for ever would be a request every few seconds for a
  // page nothing is happening on.
  const awaitingAnswer = Boolean(state?.requested_selector)
    && state?.requested_selector !== state?.applied_selector
    && !state?.last_result;
  React.useEffect(() => {
    if (!awaitingAnswer) return undefined;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [awaitingAnswer, load]);

  async function run(label, request) {
    setBusy(label);
    setError("");
    setNote("");
    try {
      const { data } = await request();
      if (data?.note) setNote(data.note);
      await load();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || `${label} could not be completed.`);
    } finally {
      setBusy("");
    }
  }

  // A failure must still be visible. Returning null here rendered NOTHING when
  // the settings could not be read - the error was set and then thrown away,
  // and the operator saw an empty gap where the speaker card should be. An
  // invisible failure on this card is worse than elsewhere: the question it
  // answers is "which speaker is this silent shop on".
  if (!state) {
    if (error) {
      return (
        <section className="border border-rose-200 bg-rose-50 rounded-md px-4 py-3"
                 data-testid="store-output-device">
          <h2 className="font-semibold text-slate-900 flex items-center gap-2">
            <Speaker className="w-4 h-4" /> Speaker
          </h2>
          <p className="text-sm text-rose-800" data-testid="store-output-error">{error}</p>
        </section>
      );
    }
    return null;
  }

  const devices = state.devices || [];

  return (
    <section className="border border-slate-200 rounded-md bg-white"
             data-testid="store-output-device">
      <div className="px-4 py-3 border-b border-slate-200 flex items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-900 flex items-center gap-2">
            <Speaker className="w-4 h-4" /> Speaker
          </h2>
          <p className="text-sm text-slate-600" data-testid="store-output-summary">
            {state.summary}
          </p>
        </div>
        {mayChange && (
          <button data-testid="store-output-refresh"
                  disabled={busy !== ""}
                  onClick={() => run("Refresh",
                    () => api.post(`/stores/${storeId}/audio-output/refresh`))}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50 disabled:opacity-50">
            <RefreshCw className="w-4 h-4" /> Ask the Store again
          </button>
        )}
      </div>

      <div className="px-4 py-3 space-y-3">
        {/* The list is another computer's answer at a moment, not something HQ
            knows. Saying so is what stops somebody choosing an endpoint that
            was unplugged this morning. */}
        <p className="text-xs text-slate-500" data-testid="store-output-provenance">
          {devices.length === 0
            ? "This Store has not reported its speakers yet. It has to be online at least once."
            : `Reported by the Store${state.reported_at ? ` at ${state.reported_at}` : ""}. If a speaker was just plugged in, ask the Store again.`}
        </p>

        {devices.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <select value={selected} disabled={!mayChange || busy !== ""}
                    onChange={(event) => setSelected(event.target.value)}
                    data-testid="store-output-select"
                    className="px-3 py-2 border border-slate-300 rounded-md text-sm min-w-[260px]">
              <option value="">Choose a speaker…</option>
              {devices.map((device) => (
                <option key={device.verified_selector || device.selector}
                        value={device.verified_selector || device.selector}>
                  {device.name}
                  {device.is_default ? " (Windows default)" : ""}
                  {device.looks_wireless ? " - wireless" : ""}
                </option>
              ))}
            </select>
            {mayChange && (
              <button data-testid="store-output-apply"
                      disabled={busy !== "" || !selected}
                      onClick={() => run("Change the speaker",
                        () => api.post(`/stores/${storeId}/audio-output`,
                                       { selector: selected }))}
                      className="px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800 disabled:opacity-50">
                Send to the Store
              </button>
            )}
          </div>
        )}

        {/* A wireless endpoint is a real hazard in a shop: it drops when the
            phone that owns it walks out of the building, and nobody at HQ
            hears it happen. */}
        {selected && devices.find((device) =>
          (device.verified_selector || device.selector) === selected
          && device.looks_wireless) && (
          <p className="text-sm text-amber-800" data-testid="store-output-wireless-warning">
            That looks like a wireless speaker. In a shop those drop out when
            the device that owns them leaves the building - and nobody at HQ
            will hear it happen.
          </p>
        )}

        {awaitingAnswer && (
          <p className="text-sm text-slate-600" data-testid="store-output-pending">
            Sent. Waiting for the Store to confirm which speaker it ended up on.
          </p>
        )}

        {state.last_result === "refused" && (
          <p className="text-sm text-rose-800" data-testid="store-output-refused">
            The Store refused the last change{state.last_error ? `: ${state.last_error}` : "."}
            {state.applied_device_name
              ? ` It is still playing through ${state.applied_device_name}.`
              : ""}
          </p>
        )}

        {note && <p className="text-sm text-slate-700" data-testid="store-output-note">{note}</p>}
        {error && <p className="text-sm text-rose-800" data-testid="store-output-error">{error}</p>}

        {/* Deliberately said out loud. This program cannot prove a speaker is
            connected, plugged into the right amplifier, or turned up - and a
            "verified" badge it had not earned would be worse than none. */}
        <p className="text-xs text-slate-500">
          HQ cannot hear this Store. Confirming the speaker is connected and
          audible is still a job for somebody in the shop.
        </p>
      </div>
    </section>
  );
}
