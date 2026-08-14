import React from "react";
import { api } from "@/lib/api";
import { Play, Pause, RefreshCw, Volume2, Upload, Trash2, Plus, X } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager } from "@/components/AdminFilters";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Recorded announcements: what is playing in every shop, and the templates
 * that decide what plays where.
 *
 * WHY THE STATUS TABLE IS THE FIRST THING ON THE PAGE
 *
 * The question this page is opened to answer is almost never "what templates
 * exist". It is "why is that shop talking", or "why is it not". So the live
 * state of every Store comes first, searchable, and the templates sit
 * underneath as the thing you set up once and rarely touch again.
 *
 * DUCKED IS SHOWN AS ITSELF, NOT AS PAUSED
 *
 * A Store standing aside for a live broadcast is not the same as one somebody
 * paused, and the difference is the only thing that explains why one comes
 * back on its own and the other does not. Collapsing them into "Paused" would
 * make the console's behaviour look arbitrary.
 */

const STATE_STYLES = {
  PLAYING: "bg-emerald-100 text-emerald-800 border-emerald-200",
  PAUSED: "bg-amber-100 text-amber-800 border-amber-200",
  DUCKED: "bg-sky-100 text-sky-800 border-sky-200",
  STOPPED: "bg-slate-100 text-slate-600 border-slate-200",
};

const STATE_EXPLANATION = {
  PLAYING: "playing now",
  PAUSED: "paused by a person - it will NOT come back on its own",
  DUCKED: "standing aside for a live broadcast - it resumes by itself",
  STOPPED: "nothing chosen",
};

function StateBadge({ state }) {
  return (
    <span title={STATE_EXPLANATION[state] || ""}
          data-testid={`announcement-state-${state}`}
          className={`inline-block px-2 py-0.5 text-xs font-medium rounded-full border ${STATE_STYLES[state] || STATE_STYLES.STOPPED}`}>
      {state === "DUCKED" ? "Broadcast" : state.charAt(0) + state.slice(1).toLowerCase()}
    </span>
  );
}

export default function Announcements() {
  const { can } = useAuth();
  const status = useAdminList("/announcements/status", { q: "", zone: "", state: "" });
  const [templates, setTemplates] = React.useState([]);
  const [audio, setAudio] = React.useState([]);
  // Two jobs, kept apart on purpose.
  //
  // The Console is what somebody opens twenty times a day, usually in a hurry,
  // to answer "why is that shop talking". Setup is what somebody does once a
  // fortnight when a campaign changes. On one screen the second buries the
  // first: the operator scrolls past a recordings list to reach a Pause button
  // for a shop that is annoying customers right now.
  const [tab, setTab] = React.useState("console");
  const [building, setBuilding] = React.useState(false);
  const [stores, setStores] = React.useState([]);
  const [deleting, setDeleting] = React.useState(null);
  const [deletingTemplate, setDeletingTemplate] = React.useState(null);
  const [busy, setBusy] = React.useState("");
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");

  const mayControl = can("announcements.control");
  const mayControlAll = can("announcements.control_all");
  const mayUpload = can("announcements.upload");
  const mayManageTemplates = can("announcements.templates.manage");
  const mayDeletePermanently = can("announcements.delete_permanently");

  const loadSupporting = React.useCallback(() => {
    api.get("/announcements/templates", { params: { page_size: 200 } })
      .then(({ data }) => setTemplates(data.items || []))
      .catch(() => { /* the status list reports a failure already */ });
    api.get("/announcements/audio", { params: { page_size: 200 } })
      .then(({ data }) => setAudio(data.items || []))
      .catch(() => {});
    // The Stores this ACCOUNT may see, from the same scoped endpoint the other
    // admin pages use - so a template can never be pointed at a shop its
    // author is not allowed to open.
    api.get("/receivers/filter-options")
      .then(({ data }) => setStores(data.stores || []))
      .catch(() => {});
  }, []);

  React.useEffect(() => { loadSupporting(); }, [loadSupporting]);

  // Refreshed periodically, but never while a filter is being typed: a table
  // that reorders under the cursor is worse than a slightly stale one.
  React.useEffect(() => {
    const id = setInterval(() => { if (!status.loading) status.reload(); }, 8000);
    return () => clearInterval(id);
  }, [status]);

  async function act(label, request) {
    setBusy(label);
    setError("");
    setMessage("");
    try {
      const { data } = await request();
      setMessage(typeof data?.note === "string" ? data.note : `${label} - done.`);
      status.reload();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || `${label} could not be completed. Try again.`);
    } finally {
      setBusy("");
    }
  }

  const zones = Array.from(new Set((status.items || [])
    .map((row) => row.zone).filter(Boolean)));

  return (
    <div className="space-y-6" data-testid="announcements-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Announcements</h1>
          <p className="text-sm text-slate-500">
            Recorded promotions, and what every shop is playing right now.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button data-testid="announcements-refresh" onClick={status.reload}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
          {mayControlAll && (
            <>
              <button data-testid="announcements-play-all"
                      disabled={busy !== ""}
                      onClick={() => act("Play All",
                        () => api.post("/announcements/play-all"))}
                      className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50">
                <Play className="w-4 h-4" /> Play All
              </button>
              <button data-testid="announcements-pause-all"
                      disabled={busy !== ""}
                      onClick={() => act("Pause All",
                        () => api.post("/announcements/pause-all"))}
                      className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-amber-600 hover:bg-amber-700 disabled:opacity-50">
                <Pause className="w-4 h-4" /> Pause All
              </button>
            </>
          )}
        </div>
      </div>

      {message && (
        <div data-testid="announcements-message"
             className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {message}
        </div>
      )}
      {error && (
        <div data-testid="announcements-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}

      <nav className="flex gap-1 border-b border-slate-200" data-testid="announcements-tabs">
        {[["console", "Play console"], ["setup", "Templates & recordings"]].map(
          ([key, label]) => (
            <button key={key} onClick={() => setTab(key)}
                    data-testid={`announcements-tab-${key}`}
                    aria-current={tab === key ? "page" : undefined}
                    className={`px-4 py-2 text-sm border-b-2 -mb-px ${
                      tab === key
                        ? "border-slate-900 text-slate-900 font-medium"
                        : "border-transparent text-slate-500 hover:text-slate-800"}`}>
              {label}
            </button>
          ))}
      </nav>

      {tab === "console" && (<>
      {/* ---- What is playing right now ---- */}
      <section className="bg-white rounded-lg border border-slate-200">
        <div className="px-4 py-3 border-b border-slate-200">
          <h2 className="font-semibold text-slate-900">Live status</h2>
          <p className="text-xs text-slate-500">
            Every active Store appears here, including shops that have never
            played anything - those are the ones somebody is usually looking for.
          </p>
        </div>

        <FilterBar onClear={status.clearFilters} activeCount={status.activeCount}
                   total={status.total} loading={status.loading}>
          <SearchInput value={status.filters.q}
                       onChange={(value) => status.setFilter("q", value)}
                       placeholder="Store, template or recording…"
                       testId="announcements-search" />
          <FilterSelect label="Zone" testId="announcements-zone" allLabel="All Zones"
                        value={status.filters.zone}
                        onChange={(value) => status.setFilter("zone", value)}
                        options={zones.map((zone) => ({ value: zone, label: zone }))} />
          <FilterSelect label="State" testId="announcements-state" allLabel="Any state"
                        value={status.filters.state}
                        onChange={(value) => status.setFilter("state", value)}
                        options={[
                          { value: "PLAYING", label: "Playing" },
                          { value: "PAUSED", label: "Paused by a person" },
                          { value: "DUCKED", label: "Standing aside for a broadcast" },
                          { value: "STOPPED", label: "Nothing chosen" },
                        ]} />
        </FilterBar>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="text-left px-4 py-2">Store</th>
                <th className="text-left px-4 py-2">Zone</th>
                <th className="text-left px-4 py-2">State</th>
                <th className="text-left px-4 py-2">Playing</th>
                <th className="text-left px-4 py-2">Volume</th>
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              <ListState loading={status.loading} error={status.error}
                         empty={!status.items.length} colSpan={6}
                         onRetry={status.reload}
                         emptyText="No Store matches these filters." />
              {!status.loading && !status.error && status.items.map((row) => (
                <tr key={row.store_id} className="border-t border-slate-100"
                    data-testid={`announcement-row-${row.store_id}`}>
                  <td className="px-4 py-2">
                    <div className="font-medium text-slate-900">{row.store_name}</div>
                    <div className="text-xs text-slate-500">{row.store_code}</div>
                  </td>
                  <td className="px-4 py-2 text-slate-600">{row.zone}</td>
                  <td className="px-4 py-2"><StateBadge state={row.state} /></td>
                  <td className="px-4 py-2 text-slate-600">
                    {row.template_name
                      ? <>
                          <div>{row.audio_title || "-"}</div>
                          <div className="text-xs text-slate-400">{row.template_name}</div>
                        </>
                      : <span className="text-slate-400">nothing chosen</span>}
                  </td>
                  <td className="px-4 py-2">
                    <VolumeControl row={row} disabled={!can("announcements.volume")}
                                   onDone={status.reload} />
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex items-center justify-end gap-1">
                      {mayControl && row.state !== "PLAYING" && (
                        <button data-testid={`announcement-play-${row.store_id}`}
                                disabled={busy !== ""}
                                title={row.state === "DUCKED"
                                  ? "A broadcast is playing here; the announcement resumes by itself"
                                  : "Resume this Store"}
                                onClick={() => act(`Play ${row.store_code}`,
                                  () => api.post(`/announcements/stores/${row.store_id}/play`))}
                                className="p-1.5 rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-50">
                          <Play className="w-4 h-4" />
                        </button>
                      )}
                      {mayControl && row.state !== "STOPPED" && row.state !== "PAUSED" && (
                        <button data-testid={`announcement-pause-${row.store_id}`}
                                disabled={busy !== ""}
                                onClick={() => act(`Pause ${row.store_code}`,
                                  () => api.post(`/announcements/stores/${row.store_id}/pause`))}
                                className="p-1.5 rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-50">
                          <Pause className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Pager page={status.page} pages={status.pages} total={status.total}
               hasMore={status.hasMore} onPage={status.setPage} />
      </section>

      </>)}

      {tab === "setup" && (<>
      {/* ---- Templates ---- */}
      <section className="bg-white rounded-lg border border-slate-200">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold text-slate-900">Templates</h2>
            <p className="text-xs text-slate-500">
              Decide once what plays where and until when. After that there is
              nothing to choose - only play and pause.
            </p>
          </div>
          {mayManageTemplates && (
            <button data-testid="template-new"
                    onClick={() => setBuilding((open) => !open)}
                    className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800">
              <Plus className="w-4 h-4" /> {building ? 'Cancel' : 'New template'}
            </button>
          )}
        </div>
        {building && mayManageTemplates && (
          <TemplateBuilder audio={audio} zones={zones} stores={stores}
                           onCancel={() => setBuilding(false)}
                           onCreated={() => { setBuilding(false); loadSupporting(); }} />
        )}
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left px-4 py-2">Template</th>
              <th className="text-left px-4 py-2">Plays in</th>
              <th className="text-left px-4 py-2">Window</th>
              <th className="text-right px-4 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {templates.length === 0 && (
              <tr><td colSpan={4} className="px-4 py-6 text-center text-slate-500">
                No templates yet.
              </td></tr>
            )}
            {templates.map((template) => (
              <tr key={template.id} className="border-t border-slate-100"
                  data-testid={`template-row-${template.id}`}>
                <td className="px-4 py-2">
                  <div className="font-medium text-slate-900">{template.name}</div>
                  <div className="text-xs text-slate-500">{template.description}</div>
                </td>
                <td className="px-4 py-2 text-slate-600">
                  {(template.items || []).map((item, index) => (
                    <div key={index} className="text-xs">
                      {item.audio_title || "recording"} -{" "}
                      {item.zone ? `zone ${item.zone}`
                                 : (item.store_name || `store ${item.store_id}`)}
                    </div>
                  ))}
                </td>
                <td className="px-4 py-2">
                  {/* The window, in words. A template that is not playing and
                      does not say why is the thing an operator rings up about. */}
                  <span className={template.is_live ? "text-slate-600" : "text-amber-700"}
                        data-testid={`template-window-${template.id}`}>
                    {template.window}
                  </span>
                </td>
                <td className="px-4 py-2 text-right">
                  <div className="flex items-center justify-end gap-1">
                    {mayControl && (
                      <button data-testid={`template-play-${template.id}`}
                              disabled={busy !== "" || !template.is_live}
                              onClick={() => act(`Play ${template.name}`,
                                () => api.post(`/announcements/templates/${template.id}/play`))}
                              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-300 text-sm hover:bg-slate-50 disabled:opacity-40">
                        <Play className="w-4 h-4" /> Play
                      </button>
                    )}
                    {/* Named for what they do, like the recordings above.
                        Archiving takes a template out of the list and keeps
                        it; deleting removes it and every line under it. */}
                    {mayManageTemplates && (
                      <button data-testid={`template-archive-${template.id}`}
                              disabled={busy !== ""}
                              onClick={() => act(`Archive ${template.name}`,
                                async () => {
                                  const response = await api.delete(
                                    `/announcements/templates/${template.id}`);
                                  loadSupporting();
                                  return response;
                                })}
                              className="px-2 py-1 rounded border border-slate-300 text-sm hover:bg-slate-50 disabled:opacity-50">
                        Archive
                      </button>
                    )}
                    {mayDeletePermanently && (
                      <button data-testid={`template-delete-${template.id}`}
                              onClick={() => setDeletingTemplate(template)}
                              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-rose-300 text-sm text-rose-700 hover:bg-rose-50">
                        <Trash2 className="w-4 h-4" /> Delete
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {deletingTemplate && (
          <div className="px-4 py-4 border-t border-rose-200 bg-rose-50 space-y-2"
               data-testid="template-delete-confirm">
            <p className="text-sm text-rose-900">
              Delete <strong>{deletingTemplate.name}</strong> permanently? The
              template and every line under it are removed. Any shop currently
              playing it is stopped - a plan deleted while shops run it would
              leave them playing something with no name, and no row here to
              press Pause on. What already played stays in Announcement
              History.
            </p>
            <DeleteConfirmation
              onCancel={() => setDeletingTemplate(null)}
              onConfirm={(word) => act(`Delete ${deletingTemplate.name}`,
                async () => {
                  const response = await api.post(
                    `/announcements/templates/${deletingTemplate.id}/delete-permanently`,
                    { confirmation: word });
                  setDeletingTemplate(null);
                  loadSupporting();
                  return response;
                })} />
          </div>
        )}
      </section>

      {/* ---- Recordings ---- */}
      <section className="bg-white rounded-lg border border-slate-200">
        <div className="px-4 py-3 border-b border-slate-200">
          <h2 className="font-semibold text-slate-900">Recordings</h2>
          <p className="text-xs text-slate-500">
            Listen to one here before sending it to a shop - nobody at HQ can
            hear what a Store is playing.
          </p>
        </div>
        {mayUpload && (
          <div className="px-4 py-3 border-b border-slate-200">
            <UploadRecording onUploaded={loadSupporting} />
          </div>
        )}
        <ul className="divide-y divide-slate-100">
          {audio.length === 0 && (
            <li className="px-4 py-6 text-center text-slate-500">No recordings yet.</li>
          )}
          {audio.map((recording) => (
            <li key={recording.id} className="px-4 py-3 flex items-center justify-between gap-3"
                data-testid={`recording-${recording.id}`}>
              <div>
                <div className="font-medium text-slate-900">{recording.title}</div>
                <div className="text-xs text-slate-500">
                  {recording.original_filename} - {(recording.byte_size / 1024).toFixed(0)} KB
                </div>
              </div>
              {/* Two actions, honestly named.
                  A wastebin icon that archived was the interface saying
                  "deleted" and meaning "hidden": the recording left the list
                  and the bytes stayed on the server. Archiving is the
                  everyday, reversible one and now says so in words; deleting
                  is separate, needs its own right, and asks for the same
                  confirmation word as every other permanent delete here. */}
              <div className="flex items-center gap-2">
                {mayUpload && (
                  <button data-testid={`recording-archive-${recording.id}`}
                          onClick={() => act(`Archive ${recording.title}`, async () => {
                            const response = await api.delete(
                              `/announcements/audio/${recording.id}`);
                            loadSupporting();
                            return response;
                          })}
                          className="px-2 py-1 rounded border border-slate-300 text-sm hover:bg-slate-50">
                    Archive
                  </button>
                )}
                {mayDeletePermanently && (
                  <button data-testid={`recording-delete-${recording.id}`}
                          onClick={() => setDeleting(recording)}
                          className="inline-flex items-center gap-1 px-2 py-1 rounded border border-rose-300 text-sm text-rose-700 hover:bg-rose-50">
                    <Trash2 className="w-4 h-4" /> Delete
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
        {deleting && (
          <div className="px-4 py-4 border-t border-rose-200 bg-rose-50 space-y-2"
               data-testid="recording-delete-confirm">
            <p className="text-sm text-rose-900">
              Delete <strong>{deleting.title}</strong> permanently? The
              recording and its file are removed and cannot be recovered.
              Archive it instead if you only want it out of the list.
            </p>
            <DeleteConfirmation
              onCancel={() => setDeleting(null)}
              onConfirm={(word) => act(`Delete ${deleting.title}`, async () => {
                const response = await api.post(
                  `/announcements/audio/${deleting.id}/delete-permanently`,
                  { confirmation: word });
                setDeleting(null);
                loadSupporting();
                return response;
              })} />
          </div>
        )}
      </section>
      </>)}
    </div>
  );
}

/**
 * The per-Store level.
 *
 * Committed on release rather than on every pixel of movement: dragging a
 * slider would otherwise send fifty requests, and the shop would hear the
 * level stepping rather than moving.
 */
function VolumeControl({ row, disabled, onDone }) {
  const [value, setValue] = React.useState(row.volume_percent ?? 80);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => { setValue(row.volume_percent ?? 80); },
                  [row.volume_percent]);

  async function commit() {
    setSaving(true);
    try {
      await api.post(`/announcements/stores/${row.store_id}/volume`,
                     { volume_percent: value });
      onDone?.();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <Volume2 className="w-4 h-4 text-slate-400" />
      <input type="range" min="0" max="100" value={value} disabled={disabled || saving}
             data-testid={`announcement-volume-${row.store_id}`}
             onChange={(event) => setValue(Number(event.target.value))}
             onMouseUp={commit} onTouchEnd={commit} onKeyUp={commit}
             className="w-24" />
      <span className="text-xs text-slate-500 w-8 tabular-nums">{value}%</span>
    </div>
  );
}

function UploadRecording({ onUploaded }) {
  const [title, setTitle] = React.useState("");
  const [file, setFile] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState("");

  async function submit(event) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setFailure("");
    const form = new FormData();
    form.append("file", file);
    form.append("title", title || file.name);
    try {
      await api.post("/announcements/audio", form);
      setTitle("");
      setFile(null);
      event.target.reset();
      onUploaded?.();
    } catch (error) {
      setFailure(error?.response?.data?.detail || "That recording was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-center gap-2"
          data-testid="recording-upload-form">
      <input type="text" value={title} placeholder="What is it called?"
             onChange={(event) => setTitle(event.target.value)}
             data-testid="recording-title"
             className="px-3 py-2 border border-slate-300 rounded-md text-sm" />
      {/* The browser's native file button is the one control on this page
          that looks like it belongs to a different decade, and it also gives
          no room to show WHICH file is chosen. Hidden, and driven by a label
          styled like every other button here - the input is still a real file
          input, so keyboard and screen-reader behaviour is unchanged. */}
      <label className="inline-flex items-center gap-2 px-3 py-2 border border-slate-300
                        rounded-md text-sm cursor-pointer hover:bg-slate-50">
        <input type="file" accept="audio/*" data-testid="recording-file"
               onChange={(event) => setFile(event.target.files?.[0] || null)}
               className="sr-only" />
        <Upload className="w-4 h-4" />
        Choose a recording
      </label>
      <span className="text-sm text-slate-600" data-testid="recording-chosen">
        {file ? file.name : "No file chosen yet"}
      </span>
      <button type="submit" disabled={busy || !file} data-testid="recording-upload"
              className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800 disabled:opacity-50">
        <Upload className="w-4 h-4" /> {busy ? "Uploading…" : "Upload"}
      </button>
      {failure && <span className="text-sm text-rose-700" data-testid="recording-upload-error">{failure}</span>}
    </form>
  );
}


/**
 * Building a template.
 *
 * The whole point of the feature is that this is done ONCE: afterwards nobody
 * chooses a recording for a shop again, they only press play and pause. So the
 * form asks for everything that decision needs - including when it stops -
 * rather than leaving the expiry to somebody's memory.
 *
 * A line names a Store or a zone, never both. The server enforces that too;
 * here it is a choice rather than two fields, so the impossible combination
 * cannot be typed in the first place.
 */
function TemplateBuilder({ audio, zones, stores, onCancel, onCreated }) {
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [startsAt, setStartsAt] = React.useState("");
  const [expiresAt, setExpiresAt] = React.useState("");
  const [lines, setLines] = React.useState([
    { audio_id: "", target: "zone", zone: "", store_id: "", store_ids: [],
      volume_percent: 80 },
  ]);
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState("");

  const setLine = (index, changes) => setLines((current) =>
    current.map((line, position) =>
      position === index ? { ...line, ...changes } : line));

  async function submit(event) {
    event.preventDefault();
    setFailure("");
    // "Several Stores" is expanded here into one line per Store rather than
    // being a third kind of line on the server. A template line names one
    // Store or one zone - that rule is what makes "which Stores does this
    // reach" answerable in one place - and a third shape would have to be
    // resolved by every reader of the table.
    const items = lines
      .filter((line) => line.audio_id)
      .flatMap((line) => {
        const common = {
          audio_id: Number(line.audio_id),
          volume_percent: Number(line.volume_percent),
        };
        if (line.target === "zone") return [{ ...common, zone: line.zone }];
        if (line.target === "stores") {
          return (line.store_ids || []).map((id) => ({
            ...common, store_id: Number(id) }));
        }
        return [{ ...common, store_id: Number(line.store_id) }];
      });
    if (!items.length) {
      setFailure("Add at least one recording and where it plays.");
      return;
    }
    setBusy(true);
    try {
      await api.post("/announcements/templates", {
        name,
        description,
        // Sent as an instant, not a date. "Expires on the 5th" is ambiguous
        // about whether the 5th is included, and a jingle that runs a day too
        // long is a promotion that outlives its own price.
        starts_at: startsAt ? new Date(startsAt).toISOString() : null,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        items,
      });
      onCreated?.();
    } catch (error) {
      setFailure(error?.response?.data?.detail || "That template was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} data-testid="template-builder"
          className="px-4 py-4 border-b border-slate-200 bg-slate-50 space-y-3">
      <div className="flex flex-wrap gap-2">
        <input value={name} onChange={(event) => setName(event.target.value)}
               placeholder="Template name" data-testid="template-name" required
               className="px-3 py-2 border border-slate-300 rounded-md text-sm min-w-[220px]" />
        <input value={description} onChange={(event) => setDescription(event.target.value)}
               placeholder="What is it for? (optional)" data-testid="template-description"
               className="px-3 py-2 border border-slate-300 rounded-md text-sm flex-1 min-w-[220px]" />
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">Starts</span>
          <input type="datetime-local" value={startsAt} data-testid="template-starts"
                 onChange={(event) => setStartsAt(event.target.value)}
                 className="px-3 py-2 border border-slate-300 rounded-md text-sm" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">Stops</span>
          <input type="datetime-local" value={expiresAt} data-testid="template-expires"
                 onChange={(event) => setExpiresAt(event.target.value)}
                 className="px-3 py-2 border border-slate-300 rounded-md text-sm" />
        </label>
        <p className="text-xs text-slate-500 max-w-sm">
          Leave both empty to run until somebody stops it. An expiry is how a
          festival jingle stops on its own instead of playing into February.
        </p>
      </div>

      <div className="space-y-2">
        {lines.map((line, index) => (
          <div key={index} className="flex flex-wrap items-center gap-2"
               data-testid={`template-line-${index}`}>
            <select value={line.audio_id} required
                    data-testid={`template-line-audio-${index}`}
                    onChange={(event) => setLine(index, { audio_id: event.target.value })}
                    className="px-3 py-2 border border-slate-300 rounded-md text-sm min-w-[200px]">
              <option value="">Which recording?</option>
              {audio.map((recording) => (
                <option key={recording.id} value={recording.id}>{recording.title}</option>
              ))}
            </select>

            <select value={line.target} data-testid={`template-line-target-${index}`}
                    onChange={(event) => setLine(index, { target: event.target.value })}
                    className="px-3 py-2 border border-slate-300 rounded-md text-sm">
              <option value="zone">a whole zone</option>
              <option value="store">one Store</option>
              <option value="stores">several Stores</option>
            </select>

            {line.target === "stores" ? (
              <div className="flex flex-col gap-1">
                <select multiple value={line.store_ids}
                        data-testid={`template-line-stores-${index}`}
                        onChange={(event) => setLine(index, {
                          store_ids: Array.from(event.target.selectedOptions,
                                                (option) => option.value) })}
                        className="px-3 py-2 border border-slate-300 rounded-md text-sm min-w-[260px] h-28">
                  {stores.map((store) => (
                    <option key={store.id} value={store.id}>
                      {store.store_name} ({store.store_code})
                    </option>
                  ))}
                </select>
                <span className="text-xs text-slate-500">
                  {line.store_ids.length
                    ? `${line.store_ids.length} chosen`
                    : "Ctrl-click or drag to choose more than one"}
                </span>
              </div>
            ) : line.target === "zone" ? (
              <select value={line.zone} required
                      data-testid={`template-line-zone-${index}`}
                      onChange={(event) => setLine(index, { zone: event.target.value })}
                      className="px-3 py-2 border border-slate-300 rounded-md text-sm min-w-[160px]">
                <option value="">Which zone?</option>
                {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
              </select>
            ) : (
              <select value={line.store_id} required
                      data-testid={`template-line-store-${index}`}
                      onChange={(event) => setLine(index, { store_id: event.target.value })}
                      className="px-3 py-2 border border-slate-300 rounded-md text-sm min-w-[220px]">
                <option value="">Which Store?</option>
                {stores.map((store) => (
                  <option key={store.id} value={store.id}>
                    {store.store_name} ({store.store_code})
                  </option>
                ))}
              </select>
            )}

            <label className="flex items-center gap-2 text-sm text-slate-600">
              <Volume2 className="w-4 h-4 text-slate-400" />
              <input type="range" min="0" max="100" value={line.volume_percent}
                     data-testid={`template-line-volume-${index}`}
                     onChange={(event) => setLine(index,
                       { volume_percent: Number(event.target.value) })}
                     className="w-24" />
              <span className="tabular-nums w-8">{line.volume_percent}%</span>
            </label>

            {lines.length > 1 && (
              <button type="button" data-testid={`template-line-remove-${index}`}
                      onClick={() => setLines((current) =>
                        current.filter((_, position) => position !== index))}
                      className="p-1.5 rounded border border-slate-300 hover:bg-white">
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        ))}
        <button type="button" data-testid="template-line-add"
                onClick={() => setLines((current) => [...current,
                  { audio_id: "", target: "zone", zone: "", store_id: "",
                    store_ids: [], volume_percent: 80 }])}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md border border-slate-300 text-sm hover:bg-white">
          <Plus className="w-4 h-4" /> Another line
        </button>
      </div>

      {failure && (
        <p className="text-sm text-rose-800" data-testid="template-builder-error">{failure}</p>
      )}

      <div className="flex items-center gap-2">
        <button type="submit" disabled={busy} data-testid="template-save"
                className="px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800 disabled:opacity-50">
          {busy ? "Saving…" : "Save template"}
        </button>
        <button type="button" onClick={onCancel} data-testid="template-cancel"
                className="px-3 py-2 rounded-md text-sm border border-slate-300 hover:bg-white">
          Cancel
        </button>
      </div>
    </form>
  );
}


/**
 * The typed confirmation, inline rather than a browser dialog.
 *
 * The same shape the Devices page uses and for the same reason: a modal's
 * default button can fire on its own in an automated or headless session, so
 * the dialog returns "confirmed" with nothing typed - a confirmation that did
 * not confirm. Read from this field, on the main thread, and handed to the
 * server as data; the server compares it again, which is the real check.
 */
function DeleteConfirmation({ onConfirm, onCancel }) {
  const [word, setWord] = React.useState("");
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="text-sm text-rose-900">
        Type DELETE to confirm:
      </label>
      <input value={word} onChange={(event) => setWord(event.target.value)}
             data-testid="recording-delete-word"
             className="px-3 py-2 border border-rose-300 rounded-md text-sm w-32" />
      <button data-testid="recording-delete-confirm-btn"
              disabled={word.trim().toUpperCase() !== "DELETE"}
              onClick={() => onConfirm(word)}
              className="px-3 py-2 rounded-md text-sm text-white bg-rose-700 hover:bg-rose-800 disabled:opacity-40">
        Delete permanently
      </button>
      <button data-testid="recording-delete-cancel" onClick={onCancel}
              className="px-3 py-2 rounded-md text-sm border border-slate-300 hover:bg-white">
        Cancel
      </button>
    </div>
  );
}
