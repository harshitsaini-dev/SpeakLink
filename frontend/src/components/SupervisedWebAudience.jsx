import React from "react";
import { X, UserCheck, UserX, LogOut, Copy, KeyRound } from "lucide-react";
import { api } from "@/lib/api";
import { FilterSelect } from "@/components/AdminFilters";
import { PLAYBACK_LABEL, listenerLink } from "@/components/WebAudiencePanel";

/**
 * One live Broadcast's web audience, opened from Active Broadcasts.
 *
 * Deliberately renders from the SERVER's capability flags rather than from role
 * names. A control that appears because the frontend guessed a role would
 * eventually appear for somebody the backend refuses, and the refusal is the
 * one that matters - so if the server did not say `can_kick`, there is no Kick
 * button, and if it did, pressing it will work.
 *
 * The room's public code is a credential: anybody holding it may attempt to
 * join. So it is shown only when the server sent it, which it does only for a
 * caller holding broadcast.view_ownership. This component never infers it.
 */

//: The same bounded cadence the supervision list itself polls at. Stopped on
//: unmount, so closing the panel stops the traffic.
const POLL_MS = 4000;

export default function SupervisedWebAudience({ sessionId, campaignName, onClose }) {
  const [state, setState] = React.useState(null);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [audienceQuery, setAudienceQuery] = React.useState("");
  const [audienceState, setAudienceState] = React.useState("");
  const [audienceAdmitted, setAudienceAdmitted] = React.useState("");

  // Applied to both lists by the same rule, so a name typed once narrows the
  // requests AND the listeners. Somebody looking for a person does not know
  // which of the two they are in - that is usually the question.
  const matches = React.useCallback((person, { withState } = {}) => {
    const needle = audienceQuery.trim().toLowerCase();
    if (needle && !String(person.display_name || "").toLowerCase().includes(needle)) {
      return false;
    }
    const named = (raw) => String(raw || "").split(",").map((v) => v.trim())
      .filter(Boolean);
    const states = named(audienceState);
    if (withState && states.length && !states.includes(person.playback_state)) {
      return false;
    }
    const admitted = named(audienceAdmitted);
    if (withState && admitted.length && !admitted.includes(person.admitted_by)) {
      return false;
    }
    return true;
  }, [audienceQuery, audienceState, audienceAdmitted]);

  // A join request has not played anything and was admitted by nobody yet, so
  // those two filters do not apply to it. Applying them anyway would make a
  // pending person vanish the moment somebody filtered by "Playing", which
  // reads as the request having been answered.
  const waiting = (state?.waiting || []).filter((person) => matches(person));
  const listeners = (state?.listeners || []).filter(
    (person) => matches(person, { withState: true }));
  const [copied, setCopied] = React.useState(null);

  const load = React.useCallback(async () => {
    try {
      const { data } = await api.get(
        `/broadcast/active-management/${sessionId}/web-audience`);
      setState(data);
      setError("");
    } catch (failure) {
      const status = failure?.response?.status;
      setError(status === 403
        ? "You do not have permission to manage this web audience."
        : status === 404
          ? "This broadcast has no web audience."
          : "The web audience could not be loaded.");
    }
  }, [sessionId]);

  React.useEffect(() => { load(); }, [load]);

  React.useEffect(() => {
    if (!sessionId) return undefined;
    const id = setInterval(load, POLL_MS);
    // Closing the panel stops the polling. A timer that outlived its panel
    // would keep asking about a Broadcast nobody is looking at.
    return () => clearInterval(id);
  }, [sessionId, load]);

  const act = React.useCallback(async (request) => {
    setBusy(true);
    try {
      await request();
      await load();
      setError("");
    } catch (failure) {
      setError(failure?.response?.data?.detail || "That action did not complete.");
    } finally {
      setBusy(false);
    }
  }, [load]);

  const copy = React.useCallback(async (what, value) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    } catch (ignored) {
      setError("Could not copy. Select and copy manually.");
    }
  }, []);

  const can = state?.capabilities || {};
  const counts = state?.counts || {};

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
         data-testid="supervised-audience-modal">
      <div className="bg-white rounded-md shadow-xl max-w-2xl w-full max-h-[85vh] overflow-y-auto">
        <div className="flex items-center gap-3 border-b border-slate-200 p-4">
          <div>
            <h3 className="font-semibold text-slate-900">Web Audience</h3>
            <p className="text-xs text-slate-500"
               data-testid="supervised-audience-campaign">
              {campaignName || state?.campaign_name || "—"}
            </p>
          </div>
          <button onClick={onClose} data-testid="supervised-audience-close"
                  className="ml-auto p-1.5 rounded hover:bg-slate-100">
            <X size={16} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {error && (
            <p data-testid="supervised-audience-error"
               className="text-sm text-red-600">{error}</p>
          )}

          {state && (
            <>
              {/* ---- room identity, only if the server sent it ---- */}
              {can.can_view_room_credentials && state.public_code && (
                <div className="rounded border border-slate-200 p-3"
                     data-testid="supervised-room-credentials">
                  <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1">
                    Web Room
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono font-bold text-slate-900"
                          data-testid="supervised-room-code">{state.public_code}</span>
                    <button data-testid="supervised-copy-id"
                            onClick={() => copy("id", state.public_code)}
                            className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
                      <Copy size={13} /> {copied === "id" ? "Copied" : "Copy ID"}
                    </button>
                    <button data-testid="supervised-copy-link"
                            onClick={() => copy("link", listenerLink(state.public_code))}
                            className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
                      <Copy size={13} /> {copied === "link" ? "Copied" : "Copy Link"}
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    {state.password ? (
                      <>
                        <span className="font-mono font-bold text-slate-900"
                              data-testid="supervised-room-password">{state.password}</span>
                        <button data-testid="supervised-copy-password"
                                onClick={() => copy("password", state.password)}
                                className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
                          <Copy size={13} /> {copied === "password" ? "Copied" : "Copy Password"}
                        </button>
                      </>
                    ) : (
                      // SpeakLink stores only a hash. Printing asterisks would
                      // imply it knows a value it is merely hiding.
                      <span className="inline-flex items-center gap-1 text-sm text-slate-600"
                            data-testid="supervised-password-unavailable">
                        <KeyRound size={13} />
                        Password not recoverable — the broadcaster can generate a new one
                      </span>
                    )}
                  </div>
                </div>
              )}

              {/* ---- counts, kept separate ---- */}
              <div className="grid grid-cols-3 gap-2">
                <Count testId="supervised-count-waiting" label="Waiting" value={counts.waiting} />
                <Count testId="supervised-count-connected" label="Connected" value={counts.connected} />
                <Count testId="supervised-count-listening" label="Listening" value={counts.listening} />
              </div>

              {can.can_toggle_auto_approve && (
                <label className="flex items-start gap-2 rounded border border-slate-200 p-3 cursor-pointer">
                  <input type="checkbox" data-testid="supervised-auto-approve"
                         checked={!!state.auto_approve} disabled={busy}
                         onChange={(event) => act(() => api.put(
                           `/broadcast/active-management/${sessionId}/web-audience/auto-approve`,
                           { auto_approve: event.target.checked }))}
                         className="mt-0.5" />
                  <span>
                    <span className="text-sm font-semibold text-slate-900">Auto Approve</span>
                    <span className="block text-xs text-slate-500">
                      Anyone with this Broadcast ID or link can request and enter
                      immediately.
                    </span>
                  </span>
                </label>
              )}

              {/* Search and filter, over BOTH lists.
                  Nine listeners fit on a screen; two hundred do not, and the
                  second is exactly when somebody needs to find one person -
                  the one who reported no sound, the one who should not be in
                  the room. Filtered in the browser deliberately: this panel
                  already holds the whole audience, so a round trip would add
                  latency to a list that is already here. */}
              <div className="flex flex-wrap items-end gap-2"
                   data-testid="supervised-audience-filters">
                <label className="flex-1 min-w-[180px]">
                  <span className="sr-only">Search the audience</span>
                  <input value={audienceQuery} data-testid="supervised-audience-search"
                         onChange={(event) => setAudienceQuery(event.target.value)}
                         placeholder="Search by name…"
                         className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm" />
                </label>
                <FilterSelect label="Playback" testId="supervised-audience-state"
                              allLabel="Any state" value={audienceState}
                              onChange={setAudienceState}
                              options={Object.entries(PLAYBACK_LABEL).map(
                                ([value, label]) => ({ value, label }))} />
                <FilterSelect label="Admitted by" testId="supervised-audience-admitted"
                              allLabel="Any way" value={audienceAdmitted}
                              onChange={setAudienceAdmitted}
                              options={[{ value: "password", label: "Password" },
                                        { value: "approved", label: "Approved" }]} />
                {(audienceQuery || audienceState || audienceAdmitted) && (
                  <button data-testid="supervised-audience-clear"
                          onClick={() => { setAudienceQuery(""); setAudienceState("");
                                           setAudienceAdmitted(""); }}
                          className="rounded border border-slate-300 px-2 py-1.5 text-sm hover:bg-slate-50">
                    Clear
                  </button>
                )}
              </div>

              {/* ---- pending ---- */}
              {(state.waiting || []).length > 0 && (
                <div data-testid="supervised-waiting-list">
                  <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-2">
                    Join Requests
                    {waiting.length !== (state.waiting || []).length && (
                      <span className="ml-2 font-normal normal-case tracking-normal text-slate-400"
                            data-testid="supervised-waiting-count">
                        showing {waiting.length} of {(state.waiting || []).length}
                      </span>
                    )}
                  </p>
                  <ul className="divide-y divide-slate-200 rounded border border-slate-200">
                    {waiting.map((person) => (
                      <li key={person.id}
                          data-testid={`supervised-waiting-${person.id}`}
                          className="flex items-center gap-3 px-3 py-2 text-sm">
                        <span className="font-medium text-slate-900">{person.display_name}</span>
                        <span className="text-xs text-slate-500">{person.requested_at || ""}</span>
                        {can.can_approve && (
                          <span className="ml-auto flex gap-1">
                            <button data-testid={`supervised-approve-${person.id}`}
                                    disabled={busy}
                                    onClick={() => act(() => api.post(
                                      `/broadcast/active-management/${sessionId}/web-audience/${person.id}/approve`))}
                                    className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-60">
                              <UserCheck size={13} /> Approve
                            </button>
                            <button data-testid={`supervised-deny-${person.id}`}
                                    disabled={busy}
                                    onClick={() => act(() => api.post(
                                      `/broadcast/active-management/${sessionId}/web-audience/${person.id}/deny`))}
                                    className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-60">
                              <UserX size={13} /> Deny
                            </button>
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* ---- admitted ---- */}
              <div data-testid="supervised-listener-list">
                <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-2">
                  Web Listeners
                  {listeners.length !== (state.listeners || []).length && (
                    <span className="ml-2 font-normal normal-case tracking-normal text-slate-400"
                          data-testid="supervised-listener-count">
                      showing {listeners.length} of {(state.listeners || []).length}
                    </span>
                  )}
                </p>
                {(state.listeners || []).length === 0 ? (
                  <p className="text-sm text-slate-500"
                     data-testid="supervised-listeners-empty">
                    Nobody has joined this Broadcast.
                  </p>
                ) : listeners.length === 0 ? (
                  <p className="text-sm text-slate-500"
                     data-testid="supervised-listeners-no-match">
                    No listener matches those filters.
                  </p>
                ) : (
                  <ul className="divide-y divide-slate-200 rounded border border-slate-200">
                    {listeners.map((person) => (
                      <li key={person.id}
                          data-testid={`supervised-listener-${person.id}`}
                          className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                        <span className="font-medium text-slate-900">{person.display_name}</span>
                        <span className="text-xs text-slate-500">
                          {person.admitted_by === "password" ? "Password" : "Approved"}
                        </span>
                        <span data-testid={`supervised-state-${person.id}`}
                              className="rounded bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-700">
                          {PLAYBACK_LABEL[person.playback_state] || person.playback_state}
                        </span>
                        {person.stale && (
                          <span className="text-xs text-amber-700"
                                data-testid={`supervised-stale-${person.id}`}>stale</span>
                        )}
                        {can.can_kick && (
                          <button data-testid={`supervised-kick-${person.id}`}
                                  disabled={busy}
                                  onClick={() => act(() => api.post(
                                    `/broadcast/active-management/${sessionId}/web-audience/${person.id}/kick`))}
                                  className="ml-auto inline-flex items-center gap-1 rounded border border-red-300 px-2 py-1 text-xs text-red-700 hover:bg-red-50 disabled:opacity-60">
                            <LogOut size={13} /> Kick
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <p className="text-xs text-slate-500">
                &ldquo;Listening&rdquo; means the listener&rsquo;s browser is
                playing. It can&rsquo;t confirm their device volume or that
                anyone can actually hear it.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Count({ testId, label, value }) {
  return (
    <div className="rounded border border-slate-200 p-2.5" data-testid={testId}>
      <div className="text-[10px] uppercase tracking-widest text-slate-500">{label}</div>
      <div className="font-mono text-xl font-bold text-slate-900">{value ?? 0}</div>
    </div>
  );
}
