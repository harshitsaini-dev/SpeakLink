import React from "react";
import { api } from "@/lib/api";
import { Link2, Copy, X, Users } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Listening links for one template.
 *
 * WHY THE PASSWORD IS SHOWN ONCE AND SAYS SO
 *
 * It is stored only as a hash, so nothing can read it back - and a card that
 * quietly stopped showing it would leave somebody hunting for a button that
 * does not exist. The card says the password is on screen once, and offers
 * the honest remedy: close the link and open another.
 *
 * WHY CLOSING IS PROMINENT
 *
 * This is the one control that takes a link back. Everybody already holding
 * it is turned away at the same moment - which is the only thing "withdraw"
 * can mean for something that has left the building.
 */
export default function AnnouncementRooms({ templateId, templateName }) {
  const { can } = useAuth();
  const [rooms, setRooms] = React.useState([]);
  const [opened, setOpened] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [copied, setCopied] = React.useState("");
  // Credentials somebody chose. Empty means "let HQ coin one", which is still
  // the sensible default - this is for the link that has to be read out over
  // a phone or printed on a notice.
  const [wantedId, setWantedId] = React.useState("");
  const [wantedPassword, setWantedPassword] = React.useState("");
  const [openingForm, setOpeningForm] = React.useState(false);
  const [noPassword, setNoPassword] = React.useState(false);
  // Who is on each link. Kept per room id rather than for one expanded row,
  // so opening a second does not silently close the first.
  const [listeners, setListeners] = React.useState({});
  const [showing, setShowing] = React.useState(null);

  const mayManage = can("announcements.rooms.manage");

  const load = React.useCallback(() => {
    api.get("/announcements/rooms", { params: { template_id: templateId } })
      .then(({ data }) => setRooms(data.items || []))
      .catch(() => setError("The listening links could not be read."));
  }, [templateId]);

  React.useEffect(() => { load(); }, [load]);

  async function openLink() {
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post(
        `/announcements/templates/${templateId}/room`,
        { label: templateName,
          id: wantedId || undefined,
          password: noPassword ? undefined : (wantedPassword || undefined),
          no_password: noPassword });
      setOpened(data);
      setWantedId("");
      setWantedPassword("");
      setOpeningForm(false);
      load();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || "That link could not be opened.");
    } finally {
      setBusy(false);
    }
  }

  async function close(roomId) {
    setBusy(true);
    setError("");
    try {
      await api.post(`/announcements/rooms/${roomId}/close`);
      setOpened((current) => (current?.room?.id === roomId ? null : current));
      load();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || "That link could not be closed.");
    } finally {
      setBusy(false);
    }
  }

  const copy = async (value, what) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(what);
      setTimeout(() => setCopied(""), 2000);
    } catch {
      setError("This browser would not let the page copy. Select it by hand.");
    }
  };

  const linkFor = (room) => `${window.location.origin}${room.listen_path}`;
  // The link that carries its own password, so nobody has to type one. Said
  // out loud on the button, because whoever receives it - forwarded, or in a
  // screenshot - is one click from listening.
  const shareLinkFor = (created) =>
    `${window.location.origin}${created.share_link || created.room.listen_path}`;

  const loadListeners = async (roomId) => {
    if (showing === roomId) { setShowing(null); return; }
    setShowing(roomId);
    try {
      const { data } = await api.get(`/announcements/rooms/${roomId}/listeners`);
      setListeners((current) => ({ ...current, [roomId]: data.items || [] }));
    } catch {
      setError("Who is on that link could not be read.");
    }
  };

  const removeListener = async (roomId, listenerId) => {
    try {
      await api.post(
        `/announcements/rooms/${roomId}/listeners/${listenerId}/remove`);
      const { data } = await api.get(`/announcements/rooms/${roomId}/listeners`);
      setListeners((current) => ({ ...current, [roomId]: data.items || [] }));
      load();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || "That listener could not be removed.");
    }
  };

  const when = (value) => {
    if (!value) return "";
    const at = new Date(value);
    return Number.isNaN(at.getTime()) ? String(value) : at.toLocaleString();
  };
  const live = rooms.filter((room) => room.status === "OPEN");

  return (
    <div className="glass rounded-xl p-4 space-y-3"
         data-testid={`announcement-rooms-${templateId}`}>
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="font-semibold text-strong flex items-center gap-2">
            <Link2 className="w-4 h-4" /> Listening links
          </h3>
          <p className="text-xs text-muted">
            A link with its own ID and password. Anybody holding it can hear
            this announcement without an account, until you close it.
          </p>
        </div>
        {mayManage && (
          <button onClick={() => setOpeningForm((was) => !was)}
                  data-testid="room-open-form"
                  className="px-3 py-2 rounded-md text-sm text-white bg-surface-muted hover:bg-surface-muted">
            Open a link
          </button>
        )}
      </div>

      {mayManage && openingForm && (
        <div className="rounded-md border border-line bg-surface-muted p-3 space-y-3"
             data-testid="room-open-panel">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="text-[10px] uppercase tracking-widest text-muted">
                Listening ID
              </span>
              <input value={wantedId} data-testid="room-wanted-id"
                     onChange={(event) => setWantedId(event.target.value)}
                     placeholder="leave empty and HQ picks one"
                     className="mt-1 w-full px-2 py-1.5 text-sm border border-line-strong rounded-md font-mono" />
              <span className="text-[11px] text-muted">
                AN- is added for you. Letters, numbers and hyphens - it gets
                read out over a phone.
              </span>
            </label>
            <label className="block">
              <span className="text-[10px] uppercase tracking-widest text-muted">
                Password
              </span>
              <input value={wantedPassword} data-testid="room-wanted-password"
                     disabled={noPassword}
                     onChange={(event) => setWantedPassword(event.target.value)}
                     placeholder="leave empty and HQ picks one"
                     className="mt-1 w-full px-2 py-1.5 text-sm border border-line-strong rounded-md font-mono disabled:bg-surface-muted" />
            </label>
          </div>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" checked={noPassword} className="mt-1"
                   data-testid="room-no-password"
                   onChange={(event) => setNoPassword(event.target.checked)} />
            <span>
              No password - whoever holds the link is in.{" "}
              <span className="text-amber-700">
                A link forwards, and so does a screenshot of it. Choose this
                only where that is acceptable.
              </span>
            </span>
          </label>
          <button onClick={openLink} disabled={busy} data-testid="room-open"
                  className="px-3 py-2 rounded-md text-sm text-white bg-surface-muted hover:bg-surface-muted disabled:opacity-50">
            {busy ? "Opening..." : "Open this link"}
          </button>
        </div>
      )}

      {error && <p className="text-sm text-rose-700" data-testid="room-error">{error}</p>}

      {opened && (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 space-y-2"
             data-testid="room-created">
          <p className="text-sm text-emerald-900">
            {opened.password_shown_once ? (
              <>
                Link opened. <strong>The password is on screen once</strong> -
                it is stored only as a hash, so nothing can show it again. If
                it is lost, close this link and open another.
              </>
            ) : (
              <>
                Link opened, <strong>with no password</strong>. Anybody holding
                this link can listen - forwarded or screenshotted - until you
                close it.
              </>
            )}
          </p>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-mono font-semibold" data-testid="room-code">
              {opened.room.public_code}
            </span>
            <button onClick={() => copy(opened.room.public_code, "id")}
                    data-testid="room-copy-id"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface">
              <Copy className="w-3 h-3" /> Copy ID
            </button>
            {opened.password_shown_once && (
              <>
                <span className="font-mono font-semibold" data-testid="room-password">
                  {opened.password_shown_once}
                </span>
                <button onClick={() => copy(opened.password_shown_once, "password")}
                        data-testid="room-copy-password"
                        className="inline-flex items-center gap-1 px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface">
                  <Copy className="w-3 h-3" /> Copy password
                </button>
              </>
            )}
            {/* The link that needs no password typed at the other end. */}
            <button onClick={() => copy(shareLinkFor(opened), "one-click link")}
                    data-testid="room-copy-share-link"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-emerald-400 text-xs text-emerald-800 hover:bg-surface">
              <Copy className="w-3 h-3" /> Copy one-click link
            </button>
            <button onClick={() => copy(linkFor(opened.room), "link")}
                    data-testid="room-copy-link"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface">
              <Copy className="w-3 h-3" /> Copy link
            </button>
            {copied && <span className="text-xs text-emerald-700">copied the {copied}</span>}
          </div>
        </div>
      )}

      {live.length === 0 ? (
        <p className="text-sm text-muted" data-testid="rooms-empty">
          No link is open for this announcement.
        </p>
      ) : (
        <ul className="divide-y divide-line border border-line rounded-md">
          {live.map((room) => (
            <li key={room.id} data-testid={`room-${room.id}`}
                className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
              <span className="font-mono font-semibold">{room.public_code}</span>
              <span className="text-xs text-muted">
                {room.listener_count} listening
              </span>
              <button onClick={() => copy(linkFor(room), "link")}
                      data-testid={`room-copy-${room.id}`}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface-muted">
                <Copy className="w-3 h-3" /> Copy link
              </button>
              <button onClick={() => loadListeners(room.id)}
                      data-testid={`room-listeners-${room.id}`}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface-muted">
                <Users className="w-3 h-3" />
                {showing === room.id ? "Hide who" : "Who is listening"}
              </button>
              {mayManage && (
                <button onClick={() => close(room.id)} disabled={busy}
                        data-testid={`room-close-${room.id}`}
                        className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded border border-rose-300 text-xs text-rose-700 hover:bg-rose-50">
                  <X className="w-3 h-3" /> Close this link
                </button>
              )}
              {showing === room.id && (
                <ul className="w-full mt-2 border-t border-line pt-2 space-y-1"
                    data-testid={`room-listener-list-${room.id}`}>
                  {(listeners[room.id] || []).map((person) => (
                    <li key={person.id}
                        data-testid={`room-listener-${person.id}`}
                        className="flex items-center gap-3 text-xs text-body">
                      {/* A name somebody typed about themselves is worth
                          exactly that, so the times sit beside it: those this
                          program actually observed. */}
                      <span className="text-strong">
                        {person.display_name || "no name given"}
                      </span>
                      <span>joined {when(person.joined_at)}</span>
                      {person.last_seen_at && (
                        <span>last heard from {when(person.last_seen_at)}</span>
                      )}
                      {mayManage && (
                        <button onClick={() => removeListener(room.id, person.id)}
                                data-testid={`room-listener-remove-${person.id}`}
                                className="ml-auto px-2 py-0.5 rounded border border-rose-300 text-rose-700 hover:bg-rose-50">
                          Remove
                        </button>
                      )}
                    </li>
                  ))}
                  {(listeners[room.id] || []).length === 0 && (
                    <li className="text-xs text-muted">
                      Nobody has followed this link yet.
                    </li>
                  )}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
