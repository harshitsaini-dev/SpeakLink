import React from "react";
import { Link2, Copy, KeyRound, RefreshCw, UserCheck, UserX, LogOut } from "lucide-react";
import { api } from "@/lib/api";
import { FilterSelect } from "@/components/AdminFilters";

/**
 * The broadcaster's view of their web audience.
 *
 * Kept entirely separate from the Store table above it. Stores and web
 * listeners are different delivery classes with different failure modes and
 * different meanings of "working", and mixing them into one list would invite
 * reading a listener's Buffering as a shop problem.
 *
 * The counts are deliberately not collapsed. Approved-but-not-connected is not
 * connected, and connected is not listening: a single "audience: 7" would let
 * an operator believe seven people can hear them when none of them has pressed
 * play.
 */

export const PLAYBACK_LABEL = {
  LISTENING: "Listening",
  BUFFERING: "Buffering",
  PAUSED: "Paused",
  READY_TO_PLAY: "Ready",
  CONNECTED: "Connected",
  DISCONNECTED: "Not connected",
};

/** Where a listener should point their browser, on whatever host HQ is served from. */
export function listenerLink(publicCode) {
  if (!publicCode) return "";
  const origin = typeof window !== "undefined" && window.location
    ? window.location.origin : "";
  return `${origin}/listen/${publicCode}`;
}

/**
 * The same link, carrying the join password.
 *
 * Whoever receives this is one step from the door instead of two - which is
 * the point, and also exactly the risk: a link that forwards is a password
 * that forwards, and a screenshot of it is the password in a photograph. So
 * it is a SEPARATE button beside the plain link rather than a replacement for
 * it, and the panel says which is which.
 *
 * Only available while this page still holds the generated password. After a
 * refresh nothing but the hash exists, so the button is not offered at all -
 * an empty `k=` would send people to a form that silently rejects them.
 */
export function oneClickListenerLink(publicCode, password) {
  if (!publicCode || !password) return "";
  return `${listenerLink(publicCode)}?k=${encodeURIComponent(password)}`;
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 2) return "now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

export default function WebAudiencePanel({ sessionId, compact = false }) {
  const [room, setRoom] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [copied, setCopied] = React.useState(null);
  //: Finding ONE person in an audience.
  //:
  //: Nine listeners fit on a screen and two hundred do not, and the second is
  //: exactly when somebody needs a particular person - the one who reported no
  //: sound, the one who should not be in the room. Filtered in the browser
  //: because this panel already holds the whole audience: a round trip would
  //: add latency to a list that is already here.
  const [audienceQuery, setAudienceQuery] = React.useState("");
  const [audienceState, setAudienceState] = React.useState("");

  const load = React.useCallback(async () => {
    if (!sessionId) return;
    try {
      const { data } = await api.get(`/broadcast/sessions/${sessionId}/web-room`);
      setRoom(data);
      setError(null);
    } catch (failure) {
      if (failure?.response?.status !== 404) {
        setError("Could not read the web audience.");
      }
    }
  }, [sessionId]);

  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => {
    if (!sessionId) return undefined;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [sessionId, load]);

  const act = React.useCallback(async (request) => {
    setBusy(true);
    setError(null);
    try {
      const { data } = await request();
      setRoom(data);
    } catch (failure) {
      setError(failure?.response?.data?.detail || "That action did not complete.");
    } finally {
      setBusy(false);
    }
  }, []);

  const copy = React.useCallback(async (what, value) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    } catch (ignored) {
      setError("Could not copy. Select and copy manually.");
    }
  }, []);

  // No Broadcast, no panel. But a Broadcast whose room has not arrived yet is
  // a DIFFERENT state, and rendering nothing for it left an empty box in the
  // console with no explanation - indistinguishable, to the operator, from a
  // web audience feature that had broken. It says which it is now.
  if (!sessionId) return null;
  // Applied to both lists by the same rule: somebody looking for a person does
  // not know which of the two they are in, and that is usually the question.
  const named = (raw) => String(raw || "").split(",").map((v) => v.trim())
    .filter(Boolean);
  const matchesName = (person) => {
    const needle = audienceQuery.trim().toLowerCase();
    return !needle
      || String(person.display_name || "").toLowerCase().includes(needle);
  };
  // A join request has not played anything yet, so the playback filter does
  // not apply to it. Hiding a pending person under "Playing" would read as
  // the request having been answered.
  const waiting = (room?.waiting || []).filter(matchesName);
  const listeners = (room?.listeners || []).filter((person) => {
    if (!matchesName(person)) return false;
    const states = named(audienceState);
    return !states.length || states.includes(person.playback_state);
  });

  if (!room) {
    return (
      <div data-testid="web-audience-loading"
           className="h-full glass rounded-xl shadow-sm p-4">
        <div className="text-xs font-bold uppercase tracking-[0.15em] text-muted">
          Web Audience
        </div>
        <p className="mt-2 text-sm text-muted">
          {error || "Reading the listener link…"}
        </p>
      </div>
    );
  }

  const counts = room.counts || {};

  return (
    // In the Console row this is the VISIBLE card, so it is the thing that has
    // to reach the row's bottom edge - stretching an invisible wrapper around
    // it would leave the border ending early, which is the defect.
    <div className={`glass rounded-xl shadow-sm ${
           compact ? "lg:h-full lg:flex lg:flex-col" : ""}`}
         data-testid="web-audience-panel">
      <div className="p-4 border-b border-line flex flex-wrap items-center gap-3">
        <h3 className="font-semibold text-strong mr-auto flex items-center gap-2">
          <Link2 size={15} /> Web Audience
        </h3>
        {room.delivery === "unavailable" && (
          <span data-testid="web-delivery-unavailable"
                className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-800">
            Web audience unavailable
          </span>
        )}
      </div>

      <div className={`p-4 space-y-4 ${compact ? "lg:flex-1 lg:min-h-0 lg:overflow-y-auto" : ""}`}>
        {/* ---- identity and secrets ---- */}
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.1em] text-muted mb-1">
              Broadcast ID
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <span data-testid="web-room-code"
                    className="min-w-0 break-all font-mono text-lg font-bold text-strong">
                {room.public_code}
              </span>
              <button data-testid="web-copy-id"
                      onClick={() => copy("id", room.public_code)}
                      className="inline-flex shrink-0 items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted">
                <Copy size={14} /> {copied === "id" ? "Copied" : "Copy ID"}
              </button>
              <button data-testid="web-copy-link"
                      onClick={() => copy("link", listenerLink(room.public_code))}
                      className="inline-flex shrink-0 items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted">
                <Copy size={14} /> {copied === "link" ? "Copied" : "Copy Link"}
              </button>
              {room.password && (
                <button data-testid="web-copy-one-click"
                        title="This link carries the password. Anybody it reaches - forwarded, or in a screenshot - can join without typing anything."
                        onClick={() => copy("one-click",
                          oneClickListenerLink(room.public_code, room.password))}
                        className="inline-flex shrink-0 items-center gap-1 rounded border border-emerald-400 px-2 py-1 text-xs text-emerald-800 hover:bg-emerald-50">
                  <Copy size={14} />
                  {copied === "one-click" ? "Copied" : "Copy one-click link"}
                </button>
              )}
            </div>
          </div>

          <div>
            <p className="text-xs font-bold uppercase tracking-[0.1em] text-muted mb-1">
              Join Password
            </p>
            <div className="flex flex-wrap items-center gap-2">
              {/* Shown only while this page still holds the generated value.
                  After a refresh only the hash exists, and a masked placeholder
                  would imply SpeakLink knows something it does not. */}
              {room.password ? (
                <>
                  <span data-testid="web-room-password"
                        className="min-w-0 break-all font-mono text-lg font-bold text-strong">
                    {room.password}
                  </span>
                  <button data-testid="web-copy-password"
                          onClick={() => copy("password", room.password)}
                          className="inline-flex shrink-0 items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted">
                    <Copy size={14} /> {copied === "password" ? "Copied" : "Copy"}
                  </button>
                </>
              ) : (
                <span data-testid="web-password-configured"
                      className="inline-flex items-center gap-1 text-sm text-body">
                  <KeyRound size={14} /> Password configured
                </span>
              )}
              <button
                data-testid="web-rotate-password"
                disabled={busy}
                onClick={() => act(() => api.post(
                  `/broadcast/sessions/${sessionId}/web-room/password/rotate`))}
                className="inline-flex shrink-0 items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-60">
                <RefreshCw size={14} /> New Password
              </button>
            </div>
            {!room.password && (
              <p className="mt-1 text-xs text-muted">
                SpeakLink stores only a hash, so the password can&rsquo;t be shown
                again. Generate a new one to share it.
              </p>
            )}
          </div>
        </div>

        {/* ---- auto approve ---- */}
        <div className="rounded border border-line p-3">
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              data-testid="web-auto-approve"
              checked={!!room.auto_approve}
              disabled={busy}
              onChange={(event) => act(() => api.put(
                `/broadcast/sessions/${sessionId}/web-room/auto-approve`,
                { auto_approve: event.target.checked }))}
              className="mt-0.5"
            />
            <span>
              <span className="text-sm font-semibold text-strong">Auto Approve</span>
              {/* The consequence, stated plainly. This toggle turns a shared
                  link into an open door, and an operator should not have to
                  infer that. */}
              <span className="block text-xs text-muted">
                Anyone with this Broadcast ID or link can request and enter
                immediately, without you approving them.
              </span>
            </span>
          </label>
        </div>

        {/* ---- counts, deliberately separate ---- */}
        <div className="grid grid-cols-3 gap-2">
          <Count testId="web-count-waiting" label="Waiting" value={counts.waiting} />
          <Count testId="web-count-connected" label="Connected" value={counts.connected} />
          <Count testId="web-count-listening" label="Listening" value={counts.listening} />
        </div>

        {error && <p data-testid="web-audience-error" className="text-sm text-red-600">{error}</p>}

        {/* Search and filter, over BOTH lists. Nine listeners fit on a screen
            and two hundred do not, and the second is exactly when somebody
            needs one particular person. */}
        {((room.waiting || []).length + (room.listeners || []).length) > 0 && (
          <div className="flex flex-wrap items-center gap-2"
               data-testid="web-audience-filters">
            <input value={audienceQuery} data-testid="web-audience-search"
                   onChange={(event) => setAudienceQuery(event.target.value)}
                   placeholder="Search by name…"
                   className="flex-1 min-w-[160px] rounded border border-line-strong px-2 py-1 text-xs" />
            <FilterSelect label="" testId="web-audience-state" allLabel="Any state"
                          value={audienceState} onChange={setAudienceState}
                          options={Object.entries(PLAYBACK_LABEL).map(
                            ([value, label]) => ({ value, label }))} />
          </div>
        )}

        {/* ---- pending ---- */}
        {(room.waiting || []).length > 0 && (
          <div data-testid="web-join-requests">
            <p className="text-xs font-bold uppercase tracking-[0.1em] text-muted mb-2">
              Join Requests
              {waiting.length !== (room.waiting || []).length && (
                <span className="ml-2 font-normal normal-case tracking-normal text-faint"
                      data-testid="audience-waiting-count">
                  showing {waiting.length} of {(room.waiting || []).length}
                </span>
              )}
            </p>
            {/* Bounded, because a hundred people waiting must not push the
                Store table a thousand pixels down the page. The list scrolls
                inside itself and every action stays reachable. */}
            <ul className={`divide-y divide-line rounded border border-line ${compact ? "max-h-40 overflow-y-auto" : ""}`}>
              {waiting.map((person) => (
                <li key={person.id}
                    data-testid={`web-request-${person.id}`}
                    className="flex items-center gap-3 px-3 py-2">
                  <span className="font-medium text-strong">{person.display_name}</span>
                  <span className="ml-auto flex gap-1">
                    <button
                      data-testid={`web-approve-${person.id}`}
                      disabled={busy}
                      onClick={() => act(() => api.post(
                        `/broadcast/sessions/${sessionId}/web-participants/${person.id}/approve`))}
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-60">
                      <UserCheck size={14} /> Approve
                    </button>
                    <button
                      data-testid={`web-deny-${person.id}`}
                      disabled={busy}
                      onClick={() => act(() => api.post(
                        `/broadcast/sessions/${sessionId}/web-participants/${person.id}/deny`))}
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-60">
                      <UserX size={14} /> Deny
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ---- admitted ---- */}
        <div data-testid="web-listeners">
          <p className="text-xs font-bold uppercase tracking-[0.1em] text-muted mb-2">
            Web Listeners
          </p>
          {(room.listeners || []).length === 0 ? (
            <p className="text-sm text-muted" data-testid="web-listeners-empty">
              Nobody has joined yet. Share the link or the Broadcast ID.
            </p>
          ) : (
            <ul className={`divide-y divide-line rounded border border-line ${compact ? "max-h-48 overflow-y-auto" : ""}`}>
              {listeners.map((person) => (
                <li key={person.id}
                    data-testid={`web-listener-${person.id}`}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                  <span className="font-medium text-strong">{person.display_name}</span>
                  <span className="text-xs text-muted">
                    {person.admitted_by === "password" ? "Password" : "Approved"}
                  </span>
                  <span data-testid={`web-listener-state-${person.id}`}
                        className="rounded bg-surface-muted px-2 py-0.5 text-xs font-semibold text-body">
                    {PLAYBACK_LABEL[person.playback_state] || person.playback_state}
                  </span>
                  <span className="text-xs text-muted">
                    {person.stale ? (
                      <span data-testid={`web-listener-stale-${person.id}`}>
                        last seen {ago(person.seconds_since_seen)}
                      </span>
                    ) : ago(person.seconds_since_seen)}
                  </span>
                  <button
                    data-testid={`web-kick-${person.id}`}
                    disabled={busy}
                    onClick={() => act(() => api.post(
                      `/broadcast/sessions/${sessionId}/web-participants/${person.id}/kick`))}
                    className="ml-auto inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-60">
                    <LogOut size={14} /> Kick
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <p className="text-xs text-muted">
          &ldquo;Listening&rdquo; means the listener&rsquo;s browser is playing.
          It can&rsquo;t confirm their device volume or that anyone can actually
          hear it.
        </p>
      </div>
    </div>
  );
}

function Count({ testId, label, value }) {
  return (
    <div className="rounded border border-line p-2.5" data-testid={testId}>
      <div className="text-[10px] uppercase tracking-widest text-muted">{label}</div>
      <div className="font-mono text-xl font-bold text-strong">{value ?? 0}</div>
    </div>
  );
}
