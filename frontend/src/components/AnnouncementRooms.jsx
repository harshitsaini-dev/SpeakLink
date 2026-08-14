import React from "react";
import { api } from "@/lib/api";
import { Link2, Copy, X } from "lucide-react";
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
        `/announcements/templates/${templateId}/room`, { label: templateName });
      setOpened(data);
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
  const live = rooms.filter((room) => room.status === "OPEN");

  return (
    <div className="border border-slate-200 rounded-md bg-white p-4 space-y-3"
         data-testid={`announcement-rooms-${templateId}`}>
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="font-semibold text-slate-900 flex items-center gap-2">
            <Link2 className="w-4 h-4" /> Listening links
          </h3>
          <p className="text-xs text-slate-500">
            A link with its own ID and password. Anybody holding it can hear
            this announcement without an account, until you close it.
          </p>
        </div>
        {mayManage && (
          <button onClick={openLink} disabled={busy} data-testid="room-open"
                  className="px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800 disabled:opacity-50">
            Open a link
          </button>
        )}
      </div>

      {error && <p className="text-sm text-rose-700" data-testid="room-error">{error}</p>}

      {opened && (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 space-y-2"
             data-testid="room-created">
          <p className="text-sm text-emerald-900">
            Link opened. <strong>The password is on screen once</strong> - it is
            stored only as a hash, so nothing can show it again. If it is lost,
            close this link and open another.
          </p>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-mono font-semibold" data-testid="room-code">
              {opened.room.public_code}
            </span>
            <button onClick={() => copy(opened.room.public_code, "id")}
                    data-testid="room-copy-id"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-300 text-xs hover:bg-white">
              <Copy className="w-3 h-3" /> Copy ID
            </button>
            <span className="font-mono font-semibold" data-testid="room-password">
              {opened.password_shown_once}
            </span>
            <button onClick={() => copy(opened.password_shown_once, "password")}
                    data-testid="room-copy-password"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-300 text-xs hover:bg-white">
              <Copy className="w-3 h-3" /> Copy password
            </button>
            <button onClick={() => copy(linkFor(opened.room), "link")}
                    data-testid="room-copy-link"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-300 text-xs hover:bg-white">
              <Copy className="w-3 h-3" /> Copy link
            </button>
            {copied && <span className="text-xs text-emerald-700">copied the {copied}</span>}
          </div>
        </div>
      )}

      {live.length === 0 ? (
        <p className="text-sm text-slate-500" data-testid="rooms-empty">
          No link is open for this announcement.
        </p>
      ) : (
        <ul className="divide-y divide-slate-100 border border-slate-200 rounded-md">
          {live.map((room) => (
            <li key={room.id} data-testid={`room-${room.id}`}
                className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
              <span className="font-mono font-semibold">{room.public_code}</span>
              <span className="text-xs text-slate-500">
                {room.listener_count} listening
              </span>
              <button onClick={() => copy(linkFor(room), "link")}
                      data-testid={`room-copy-${room.id}`}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-300 text-xs hover:bg-slate-50">
                <Copy className="w-3 h-3" /> Copy link
              </button>
              {mayManage && (
                <button onClick={() => close(room.id)} disabled={busy}
                        data-testid={`room-close-${room.id}`}
                        className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded border border-rose-300 text-xs text-rose-700 hover:bg-rose-50">
                  <X className="w-3 h-3" /> Close this link
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
