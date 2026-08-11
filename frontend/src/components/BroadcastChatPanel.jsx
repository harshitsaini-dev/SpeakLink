import React from "react";
import { Send, MessageSquareOff, MessageSquare, Lock, Globe, Trash2 } from "lucide-react";
import { api } from "@/lib/api";

/**
 * The host's side of the web audience chat.
 *
 * Chat exists because a web listener can hear the announcement and otherwise
 * has no way to say "we cannot hear you". This panel is where the operator
 * reads that and answers.
 *
 * THREE THINGS THIS PANEL IS CAREFUL ABOUT
 *
 * Private means private. In PRIVATE mode a listener's message is addressed to
 * the host alone, and that is a property of the stored row - the badge here
 * reports what the server recorded, it does not decide it. A panel that
 * decided visibility client-side would be one refetch away from publishing it.
 *
 * A deleted message keeps its place. The row and the author stay, the words
 * go. Everyone in the room already saw it; removing the row would make the
 * transcript claim a conversation that did not happen.
 *
 * The host is never silenced by their own switch. Turning chat off stops the
 * AUDIENCE typing - the operator may still need to answer the last question
 * before the room goes quiet, so their composer stays live.
 */

const POLL_MS = 3000;

export default function BroadcastChatPanel({ sessionId }) {
  const [state, setState] = React.useState(null);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const listRef = React.useRef(null);

  const load = React.useCallback(async () => {
    if (!sessionId) return;
    try {
      const { data } = await api.get(`/broadcast/sessions/${sessionId}/chat`);
      setState(data);
      setError("");
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Chat is unavailable.");
    }
  }, [sessionId]);

  React.useEffect(() => {
    load();
    // Polling, not a socket. Chat is a handful of short messages a minute, and
    // a second socket per operator is a second thing that can silently die
    // while the page still looks connected.
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Follow the conversation, the way a chat should. Only the list scrolls -
  // the card itself must not grow, or it would push the page layout around
  // every time somebody typed.
  React.useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [state?.messages?.length]);

  const send = async (event) => {
    event.preventDefault();
    const body = draft.trim();
    if (!body || busy) return;
    setBusy(true);
    try {
      await api.post(`/broadcast/sessions/${sessionId}/chat`, { body });
      setDraft("");
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Message not sent.");
    } finally { setBusy(false); }
  };

  const updateSettings = async (patch) => {
    setBusy(true);
    try {
      const { data } = await api.put(
        `/broadcast/sessions/${sessionId}/chat/settings`, patch);
      setState(data);
      setError("");
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Could not change chat.");
    } finally { setBusy(false); }
  };

  const remove = async (messageId) => {
    setBusy(true);
    try {
      const { data } = await api.post(
        `/broadcast/sessions/${sessionId}/chat/messages/${messageId}/delete`);
      setState(data);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Could not remove it.");
    } finally { setBusy(false); }
  };

  const enabled = state?.chat_enabled ?? true;
  const isPrivate = state?.chat_mode === "PRIVATE";
  const messages = state?.messages || [];

  return (
    <div className="flex h-full min-h-[20rem] flex-col border border-slate-200 bg-white rounded-md shadow-sm"
         data-testid="broadcast-chat-card">
      <div className="shrink-0 border-b border-slate-200 p-3">
        <div className="flex items-center gap-2">
          <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">
            Web Chat
          </div>
          {!enabled && (
            <span data-testid="chat-off-badge"
                  className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-slate-600">
              Off
            </span>
          )}
          {isPrivate && (
            <span data-testid="chat-private-badge"
                  className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-900">
              Private
            </span>
          )}
        </div>

        {/* Two switches, each saying what it does to the AUDIENCE. */}
        <div className="mt-2 flex flex-wrap gap-2">
          <button type="button" data-testid="chat-toggle-enabled" disabled={busy}
                  onClick={() => updateSettings({ chat_enabled: !enabled })}
                  title={enabled
                    ? "Stop listeners typing. You can still reply."
                    : "Let listeners type again."}
                  className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40">
            {enabled ? <MessageSquareOff size={13} /> : <MessageSquare size={13} />}
            {enabled ? "Turn chat off" : "Turn chat on"}
          </button>
          <button type="button" data-testid="chat-toggle-mode" disabled={busy}
                  onClick={() => updateSettings({ chat_mode: isPrivate ? "PUBLIC" : "PRIVATE" })}
                  title={isPrivate
                    ? "Public: listeners see each other's messages."
                    : "Private: a listener's message reaches only you."}
                  className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40">
            {isPrivate ? <Globe size={13} /> : <Lock size={13} />}
            {isPrivate ? "Make public" : "Make private"}
          </button>
        </div>
      </div>

      {/* Only this scrolls. */}
      <div ref={listRef} data-testid="chat-messages"
           className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
        {messages.length === 0 && (
          <p className="text-sm text-slate-500" data-testid="chat-empty">
            Nothing yet. Messages from web listeners appear here.
          </p>
        )}
        {messages.map((message) => (
          <div key={message.id} data-testid={`chat-message-${message.id}`}
               className={`group rounded px-2 py-1.5 text-sm ${
                 message.author_kind === "HOST"
                   ? "bg-blue-50 border border-blue-100"
                   : "bg-slate-50 border border-slate-100"}`}>
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold text-slate-800">
                {message.author_kind === "HOST" ? "You" : message.author_name}
              </span>
              {message.visibility === "PRIVATE" && (
                <span data-testid={`chat-private-${message.id}`}
                      className="text-[10px] font-bold uppercase tracking-wider text-amber-800">
                  private
                </span>
              )}
              {!message.deleted && (
                <button type="button" data-testid={`chat-delete-${message.id}`}
                        onClick={() => remove(message.id)} disabled={busy}
                        title="Remove this message for everyone"
                        className="ml-auto text-slate-400 opacity-0 transition group-hover:opacity-100 hover:text-red-700">
                  <Trash2 size={13} />
                </button>
              )}
            </div>
            {message.deleted ? (
              <p data-testid={`chat-removed-${message.id}`}
                 className="italic text-slate-500">Removed by the host</p>
            ) : (
              // Rendered as TEXT by React, never as markup. Escaping on the way
              // in would corrupt a message that legitimately contains < or &.
              <p className="whitespace-pre-wrap break-words text-slate-800">{message.body}</p>
            )}
          </div>
        ))}
      </div>

      {error && (
        <p role="alert" data-testid="chat-error"
           className="shrink-0 border-t border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700">
          {error}
        </p>
      )}

      <form onSubmit={send} className="shrink-0 border-t border-slate-200 p-2">
        <div className="flex gap-2">
          <label htmlFor="chat-compose" className="sr-only">Message the web audience</label>
          <input id="chat-compose" data-testid="chat-input" value={draft}
                 onChange={(e) => setDraft(e.target.value)} maxLength={500}
                 placeholder={enabled ? "Reply to the audience…"
                                      : "Chat is off for listeners. You can still reply."}
                 className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          <button type="submit" data-testid="chat-send" disabled={busy || !draft.trim()}
                  className="inline-flex items-center gap-1 rounded bg-blue-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-800 disabled:bg-slate-400">
            <Send size={14} /> Send
          </button>
        </div>
      </form>
    </div>
  );
}
