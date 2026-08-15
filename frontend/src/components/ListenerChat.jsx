import React from "react";
import { Send, ImagePlus } from "lucide-react";
import { api, API_BASE } from "@/lib/api";
import { formatIstTimeOfDay } from "@/lib/time";

/**
 * The listener's side of the chat: ask a question, read the answer.
 *
 * WHAT THIS COMPONENT IS NOT ALLOWED TO DECIDE
 *
 * Who may read a message. In private mode a listener's message is addressed to
 * the host alone, and that is decided by the server and stored on the row. This
 * page renders what it is given; it never filters, and it never assumes. A
 * client-side filter would be one refetch away from publishing somebody's
 * private message.
 *
 * It also never renders a message as markup. React escapes text nodes, which
 * is what makes a message containing <script> a message about <script>.
 *
 * Every refusal - chat off, muted, too fast - is enforced by the server and
 * repeated here only so the person can read it. The composer is disabled when
 * the server says chat is off, because offering a control that will be refused
 * is a promise the page already knows it cannot keep.
 */

const POLL_MS = 3000;

export default function ListenerChat() {
  const [state, setState] = React.useState(null);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const listRef = React.useRef(null);
  const fileRef = React.useRef(null);

  const load = React.useCallback(async () => {
    try {
      const { data } = await api.get("/listen/chat");
      setState(data);
    } catch {
      // Quiet on purpose. A listener whose session has gone is already being
      // told so by the page around this panel; a second red box about chat
      // would be noise on top of the message that matters.
    }
  }, []);

  React.useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  React.useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [state?.messages?.length]);

  const send = async (event) => {
    event.preventDefault();
    const body = draft.trim();
    if (!body || busy) return;
    setBusy(true);
    setError("");
    try {
      await api.post("/listen/chat", { body });
      setDraft("");
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Message not sent.");
    } finally { setBusy(false); }
  };

  const sendImage = async (file) => {
    if (!file || busy) return;
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("body", draft.trim());
      await api.post("/listen/chat/image", form);
      setDraft("");
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Image not sent.");
    } finally {
      setBusy(false);
      // Cleared so the same file can be picked again after a refusal.
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  if (!state) return null;

  const enabled = state.chat_enabled && !state.muted;
  const messages = state.messages || [];

  return (
    <div data-testid="listener-chat"
         className="mt-5 rounded-lg border border-line bg-surface-muted text-left">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[11px] font-bold uppercase tracking-[0.2em] text-faint">
          Chat
        </span>
        {state.chat_mode === "PRIVATE" && (
          <span data-testid="listener-chat-private"
                className="rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-300">
            Private
          </span>
        )}
      </div>

      <div ref={listRef} data-testid="listener-chat-messages"
           className="max-h-56 space-y-2 overflow-y-auto p-3">
        {messages.length === 0 && (
          <p className="text-sm text-muted" data-testid="listener-chat-empty">
            {state.chat_mode === "PRIVATE"
              ? "Only the broadcaster sees what you send here."
              : "Say something to the broadcaster."}
          </p>
        )}
        {messages.map((message) => (
          <div key={message.id} data-testid={`listener-chat-message-${message.id}`}
               className={`rounded px-2 py-1.5 text-sm ${
                 message.author_kind === "HOST"
                   ? "border border-blue-900 bg-blue-950/60"
                   : "border border-line bg-surface-muted"}`}>
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold text-body">
                {message.author_kind === "HOST"
                  ? "Broadcaster"
                  : (message.participant_id === state.me ? "You" : message.author_name)}
              </span>
              {message.visibility === "PRIVATE" && (
                <span className="text-[10px] font-bold uppercase tracking-wider text-amber-400">
                  private
                </span>
              )}
              <span data-testid={`listener-chat-time-${message.id}`}
                    className="ml-auto font-mono text-[10px] text-muted">
                {formatIstTimeOfDay(message.created_at)}
              </span>
            </div>
            {message.has_image && (
              // A plain src, and it works: the listener's session is a cookie,
              // which the browser attaches to an image request by itself. The
              // server still applies the same visibility rule to the bytes, so
              // this URL is not a way around private mode.
              <a href={`${API_BASE}/listen/chat/messages/${message.id}/image`}
                 target="_blank" rel="noreferrer">
                <img data-testid={`listener-chat-image-${message.id}`}
                     src={`${API_BASE}/listen/chat/messages/${message.id}/image`}
                     alt="Sent in chat"
                     className="mt-1 max-h-44 rounded border border-line object-contain" />
              </a>
            )}
            {message.deleted ? (
              <p className="italic text-muted">Removed by the broadcaster</p>
            ) : message.body ? (
              <p className="whitespace-pre-wrap break-words text-strong">{message.body}</p>
            ) : null}
          </div>
        ))}
      </div>

      {!state.chat_enabled && (
        <p data-testid="listener-chat-off"
           className="border-t border-line px-3 py-2 text-xs text-faint">
          The broadcaster has turned chat off.
        </p>
      )}
      {state.chat_enabled && state.muted && (
        // Said plainly. A composer that silently swallowed messages would be
        // worse than one that says why it will not send them.
        <p data-testid="listener-chat-muted"
           className="border-t border-line px-3 py-2 text-xs text-amber-300">
          The broadcaster has muted you in this chat. You can still listen.
        </p>
      )}
      {error && (
        <p role="alert" data-testid="listener-chat-error"
           className="border-t border-red-900 bg-red-950/60 px-3 py-2 text-xs text-red-200">
          {error}
        </p>
      )}

      <form onSubmit={send} className="flex gap-2 border-t border-line p-2">
        <label htmlFor="listener-chat-input" className="sr-only">Message the broadcaster</label>
        <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp"
               data-testid="listener-chat-image-input" className="hidden"
               onChange={(event) => sendImage(event.target.files?.[0])} />
        <button type="button" data-testid="listener-chat-attach"
                disabled={!enabled || busy}
                onClick={() => fileRef.current?.click()}
                title="Send a picture (PNG, JPEG or WebP)"
                className="rounded border border-line-strong px-2 text-body hover:bg-surface-muted disabled:opacity-40">
          <ImagePlus size={16} />
        </button>
        <input id="listener-chat-input" data-testid="listener-chat-input"
               value={draft} maxLength={500} disabled={!enabled}
               onChange={(event) => setDraft(event.target.value)}
               placeholder={enabled ? "Type a message…" : "Chat is not available"}
               className="min-w-0 flex-1 rounded border border-line-strong bg-surface-muted px-2 py-1.5 text-sm text-strong placeholder:text-body focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50" />
        <button type="submit" data-testid="listener-chat-send"
                disabled={!enabled || busy || !draft.trim()}
                className="inline-flex items-center gap-1 rounded bg-blue-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-800 disabled:bg-slate-700">
          <Send size={14} /> Send
        </button>
      </form>
    </div>
  );
}
