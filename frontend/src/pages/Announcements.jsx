import React from "react";
import { api } from "@/lib/api";
import { Play, Pause, RefreshCw, Volume2, Upload, Trash2 } from "lucide-react";
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
  const [busy, setBusy] = React.useState("");
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");

  const mayControl = can("announcements.control");
  const mayControlAll = can("announcements.control_all");
  const mayUpload = can("announcements.upload");
  const mayManageTemplates = can("announcements.templates.manage");

  const loadSupporting = React.useCallback(() => {
    api.get("/announcements/templates", { params: { page_size: 200 } })
      .then(({ data }) => setTemplates(data.items || []))
      .catch(() => { /* the status list reports a failure already */ });
    api.get("/announcements/audio", { params: { page_size: 200 } })
      .then(({ data }) => setAudio(data.items || []))
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

      {/* ---- Templates ---- */}
      <section className="bg-white rounded-lg border border-slate-200">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-slate-900">Templates</h2>
            <p className="text-xs text-slate-500">
              Decide once what plays where and until when. After that there is
              nothing to choose - only play and pause.
            </p>
          </div>
        </div>
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
                              className="p-1.5 rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-50">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
              {mayUpload && (
                <button data-testid={`recording-archive-${recording.id}`}
                        onClick={() => act(`Archive ${recording.title}`, async () => {
                          const response = await api.delete(
                            `/announcements/audio/${recording.id}`);
                          loadSupporting();
                          return response;
                        })}
                        className="p-1.5 rounded border border-slate-300 hover:bg-slate-50">
                  <Trash2 className="w-4 h-4" />
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>
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
      <input type="file" accept="audio/*" data-testid="recording-file"
             onChange={(event) => setFile(event.target.files?.[0] || null)}
             className="text-sm" />
      <button type="submit" disabled={busy || !file} data-testid="recording-upload"
              className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800 disabled:opacity-50">
        <Upload className="w-4 h-4" /> {busy ? "Uploading…" : "Upload"}
      </button>
      {failure && <span className="text-sm text-rose-700" data-testid="recording-upload-error">{failure}</span>}
    </form>
  );
}
