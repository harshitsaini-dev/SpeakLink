import React from "react";
import { Link2, Copy, KeyRound, RefreshCw, UserCheck, UserX, LogOut } from "lucide-react";
import { api } from "@/lib/api";

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

  if (!sessionId || !room) return null;

  const counts = room.counts || {};

  return (
    <div className="border border-slate-200 bg-white rounded-md shadow-sm"
         data-testid="web-audience-panel">
      <div className="p-4 border-b border-slate-200 flex flex-wrap items-center gap-3">
        <h3 className="font-semibold text-slate-900 mr-auto flex items-center gap-2">
          <Link2 size={15} /> Web Audience
        </h3>
        {room.delivery === "unavailable" && (
          <span data-testid="web-delivery-unavailable"
                className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-800">
            Web audience unavailable
          </span>
        )}
      </div>

      <div className="p-4 space-y-4">
        {/* ---- identity and secrets ---- */}
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1">
              Broadcast ID
            </p>
            <div className="flex items-center gap-2">
              <span data-testid="web-room-code"
                    className="font-mono text-lg font-bold text-slate-900">
                {room.public_code}
              </span>
              <button data-testid="web-copy-id"
                      onClick={() => copy("id", room.public_code)}
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
                <Copy size={14} /> {copied === "id" ? "Copied" : "Copy ID"}
              </button>
              <button data-testid="web-copy-link"
                      onClick={() => copy("link", listenerLink(room.public_code))}
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
                <Copy size={14} /> {copied === "link" ? "Copied" : "Copy Link"}
              </button>
            </div>
          </div>

          <div>
            <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1">
              Join Password
            </p>
            <div className="flex items-center gap-2">
              {/* Shown only while this page still holds the generated value.
                  After a refresh only the hash exists, and a masked placeholder
                  would imply SpeakLink knows something it does not. */}
              {room.password ? (
                <>
                  <span data-testid="web-room-password"
                        className="font-mono text-lg font-bold text-slate-900">
                    {room.password}
                  </span>
                  <button data-testid="web-copy-password"
                          onClick={() => copy("password", room.password)}
                          className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50">
                    <Copy size={14} /> {copied === "password" ? "Copied" : "Copy"}
                  </button>
                </>
              ) : (
                <span data-testid="web-password-configured"
                      className="inline-flex items-center gap-1 text-sm text-slate-600">
                  <KeyRound size={14} /> Password configured
                </span>
              )}
              <button
                data-testid="web-rotate-password"
                disabled={busy}
                onClick={() => act(() => api.post(
                  `/broadcast/sessions/${sessionId}/web-room/password/rotate`))}
                className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-60">
                <RefreshCw size={14} /> Generate New Password
              </button>
            </div>
            {!room.password && (
              <p className="mt-1 text-xs text-slate-500">
                SpeakLink stores only a hash, so the password can&rsquo;t be shown
                again. Generate a new one to share it.
              </p>
            )}
          </div>
        </div>

        {/* ---- auto approve ---- */}
        <div className="rounded border border-slate-200 p-3">
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
              <span className="text-sm font-semibold text-slate-900">Auto Approve</span>
              {/* The consequence, stated plainly. This toggle turns a shared
                  link into an open door, and an operator should not have to
                  infer that. */}
              <span className="block text-xs text-slate-500">
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

        {/* ---- pending ---- */}
        {(room.waiting || []).length > 0 && (
          <div data-testid="web-join-requests">
            <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-2">
              Join Requests
            </p>
            {/* Bounded, because a hundred people waiting must not push the
                Store table a thousand pixels down the page. The list scrolls
                inside itself and every action stays reachable. */}
            <ul className={`divide-y divide-slate-200 rounded border border-slate-200 ${compact ? "max-h-40 overflow-y-auto" : ""}`}>
              {room.waiting.map((person) => (
                <li key={person.id}
                    data-testid={`web-request-${person.id}`}
                    className="flex items-center gap-3 px-3 py-2">
                  <span className="font-medium text-slate-900">{person.display_name}</span>
                  <span className="ml-auto flex gap-1">
                    <button
                      data-testid={`web-approve-${person.id}`}
                      disabled={busy}
                      onClick={() => act(() => api.post(
                        `/broadcast/sessions/${sessionId}/web-participants/${person.id}/approve`))}
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-60">
                      <UserCheck size={14} /> Approve
                    </button>
                    <button
                      data-testid={`web-deny-${person.id}`}
                      disabled={busy}
                      onClick={() => act(() => api.post(
                        `/broadcast/sessions/${sessionId}/web-participants/${person.id}/deny`))}
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-60">
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
          <p className="text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-2">
            Web Listeners
          </p>
          {(room.listeners || []).length === 0 ? (
            <p className="text-sm text-slate-500" data-testid="web-listeners-empty">
              Nobody has joined yet. Share the link or the Broadcast ID.
            </p>
          ) : (
            <ul className={`divide-y divide-slate-200 rounded border border-slate-200 ${compact ? "max-h-48 overflow-y-auto" : ""}`}>
              {room.listeners.map((person) => (
                <li key={person.id}
                    data-testid={`web-listener-${person.id}`}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                  <span className="font-medium text-slate-900">{person.display_name}</span>
                  <span className="text-xs text-slate-500">
                    {person.admitted_by === "password" ? "Password" : "Approved"}
                  </span>
                  <span data-testid={`web-listener-state-${person.id}`}
                        className="rounded bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-700">
                    {PLAYBACK_LABEL[person.playback_state] || person.playback_state}
                  </span>
                  <span className="text-xs text-slate-500">
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
                    className="ml-auto inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-60">
                    <LogOut size={14} /> Kick
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <p className="text-xs text-slate-500">
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
    <div className="rounded border border-slate-200 p-2.5" data-testid={testId}>
      <div className="text-[10px] uppercase tracking-widest text-slate-500">{label}</div>
      <div className="font-mono text-xl font-bold text-slate-900">{value ?? 0}</div>
    </div>
  );
}
