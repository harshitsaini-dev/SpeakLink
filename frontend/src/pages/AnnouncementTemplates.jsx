import React from "react";
import { api } from "@/lib/api";
import { Play, RefreshCw, Plus, X, Volume2 } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager } from "@/components/AdminFilters";
import { BulkBar, BulkDeleteConfirm, useBulkSelection } from "@/components/BulkBar";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Templates: the plan for what plays where, and until when.
 *
 * Its own page rather than a section under the play console. They are used at
 * completely different rates - the console many times a day, this once a
 * fortnight - and sharing a screen meant scrolling past the fortnightly thing
 * to reach the hourly one. It also means this page can have its own search,
 * filters and pagination, which a section could not.
 */
export default function AnnouncementTemplates() {
  const { can } = useAuth();
  const list = useAdminList("/announcements/templates", {
    q: "", status: "active", zone: "",
  });
  const bulk = useBulkSelection(list);
  const [audio, setAudio] = React.useState([]);
  const [stores, setStores] = React.useState([]);
  const [building, setBuilding] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [busy, setBusy] = React.useState("");
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");

  const mayManage = can("announcements.templates.manage");
  const mayDelete = can("announcements.delete_permanently");
  const mayControl = can("announcements.control");

  React.useEffect(() => {
    api.get("/announcements/audio", { params: { page_size: 200 } })
      .then(({ data }) => setAudio(data.items || []))
      .catch(() => {});
    // The Stores this ACCOUNT may see, from the same scoped endpoint the other
    // admin pages use - so a template can never be pointed at a shop its
    // author is not allowed to open.
    api.get("/receivers/filter-options")
      .then(({ data }) => { setStores(data.stores || []); })
      .catch(() => {});
  }, []);

  const zones = Array.from(new Set((list.items || [])
    .flatMap((template) => (template.items || []).map((item) => item.zone))
    .filter(Boolean)));

  async function act(label, request) {
    setBusy(label);
    setError("");
    setNote("");
    try {
      const { data } = await request();
      if (data?.note) setNote(data.note);
      list.reload();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || `${label} could not be completed.`);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4" data-testid="templates-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Templates</h1>
          <p className="text-sm text-slate-500">
            Decide once what plays where and until when. After that there is
            nothing to choose - only play and pause.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button data-testid="templates-refresh" onClick={list.reload}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
            <RefreshCw size={14} /> Refresh
          </button>
          {mayManage && (
            <button data-testid="template-new"
                    onClick={() => setBuilding((open) => !open)}
                    className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-slate-900 hover:bg-slate-800">
              <Plus className="w-4 h-4" /> {building ? "Cancel" : "New template"}
            </button>
          )}
        </div>
      </div>

      {note && (
        <div data-testid="templates-note"
             className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {note}
        </div>
      )}
      {error && (
        <div data-testid="templates-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}

      {building && mayManage && (
        <TemplateBuilder audio={audio} zones={zones} stores={stores}
                         onCancel={() => setBuilding(false)}
                         onCreated={() => { setBuilding(false); list.reload(); }} />
      )}

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q}
                     onChange={(value) => list.setFilter("q", value)}
                     placeholder="Template name or description…"
                     testId="templates-search" />
        <FilterSelect label="Zone" testId="templates-zone" allLabel="All Zones"
                      value={list.filters.zone}
                      onChange={(value) => list.setFilter("zone", value)}
                      options={zones.map((zone) => ({ value: zone, label: zone }))} />
        <FilterSelect label="Status" testId="templates-status" allLabel="All"
                      value={list.filters.status}
                      onChange={(value) => list.setFilter("status", value)}
                      options={[{ value: "active", label: "Active" },
                                { value: "archived", label: "Archived" }]} />
      </FilterBar>

      {(mayManage || mayDelete) && (
        <BulkBar bulk={bulk} list={list} testIdPrefix="templates" busy={busy !== ""}
                 onArchive={mayManage ? () => act("Archive", async () => {
                   const response = await api.post("/announcements/templates/archive",
                                                   bulk.body());
                   bulk.clear();
                   return response;
                 }) : undefined}
                 onDelete={mayDelete ? () => setDeleting(true) : undefined} />
      )}

      {deleting && (
        <BulkDeleteConfirm
          count={bulk.count} noun="template" testIdPrefix="templates"
          warning={"The templates and every line under them are removed. Any "
                   + "shop currently playing one is stopped - a plan deleted "
                   + "while shops run it would leave them playing something "
                   + "with no name, and no row here to press Pause on. What "
                   + "already played stays in Announcement History."}
          onCancel={() => setDeleting(false)}
          onConfirm={(word) => act("Delete", async () => {
            const response = await api.post("/announcements/templates/delete",
                                            { ...bulk.body(), confirm: word });
            setDeleting(false);
            bulk.clear();
            return response;
          })} />
      )}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              {(mayManage || mayDelete) && <th className="px-3 py-2 w-8"></th>}
              <th className="px-3 py-2">Template</th>
              <th className="px-3 py-2">Plays in</th>
              <th className="px-3 py-2">Window</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={5} onRetry={list.reload}
                       emptyText="No template matches these filters." />
            {!list.loading && !list.error && list.items.map((template) => (
              <tr key={template.id} data-testid={`template-row-${template.id}`}
                  className="border-b border-slate-100 even:bg-slate-50/50">
                {(mayManage || mayDelete) && (
                  <td className="px-3 py-2">
                    <input type="checkbox" checked={bulk.isChosen(template.id)}
                           data-testid={`template-select-${template.id}`}
                           onChange={() => bulk.toggle(template.id)} />
                  </td>
                )}
                <td className="px-3 py-2">
                  <div className="font-medium text-slate-900">{template.name}</div>
                  <div className="text-xs text-slate-500">{template.description}</div>
                </td>
                <td className="px-3 py-2 text-slate-600">
                  {(template.items || []).map((item, index) => (
                    <div key={index} className="text-xs">
                      {item.audio_title || "recording"} -{" "}
                      {item.zone ? `zone ${item.zone}`
                                 : (item.store_name || `store ${item.store_id}`)}
                    </div>
                  ))}
                </td>
                <td className="px-3 py-2">
                  {/* The window in words. A template that is not playing and
                      does not say why is the thing an operator rings up
                      about. */}
                  <span className={template.is_live ? "text-slate-600" : "text-amber-700"}
                        data-testid={`template-window-${template.id}`}>
                    {template.window}
                  </span>
                </td>
                <td className="px-3 py-2 text-right whitespace-nowrap">
                  {mayControl && (
                    <button data-testid={`template-play-${template.id}`}
                            disabled={busy !== "" || !template.is_live}
                            onClick={() => act(`Play ${template.name}`,
                              () => api.post(`/announcements/templates/${template.id}/play`))}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-300 text-sm hover:bg-slate-50 disabled:opacity-40 mr-1">
                      <Play className="w-4 h-4" /> Play
                    </button>
                  )}
                  {mayManage && (
                    <button data-testid={`template-archive-${template.id}`}
                            disabled={busy !== ""}
                            onClick={() => act(`Archive ${template.name}`,
                              () => api.delete(`/announcements/templates/${template.id}`))}
                            className="px-2 py-1 rounded border border-slate-300 text-sm hover:bg-slate-50 mr-1">
                      Archive
                    </button>
                  )}
                  {mayDelete && (
                    <button data-testid={`template-delete-${template.id}`}
                            onClick={() => { bulk.chooseOnly(template.id);
                                             setDeleting(true); }}
                            className="px-2 py-1 rounded border border-rose-300 text-sm text-rose-700 hover:bg-rose-50">
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pager page={list.page} pages={list.pages} total={list.total}
               hasMore={list.hasMore} onPage={list.setPage} />
      </div>
    </div>
  );
}

/**
 * Building a template.
 *
 * The whole point of the feature is that this is done ONCE: afterwards nobody
 * chooses a recording for a shop again, they only press play and pause. So the
 * form asks for everything that decision needs - including when it stops -
 * rather than leaving the expiry to somebody's memory.
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
        name, description,
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
          className="border border-slate-200 rounded-md bg-slate-50 px-4 py-4 space-y-3">
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
          <div key={index} className="flex flex-wrap items-start gap-2"
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
              <div className="flex flex-col gap-1" data-testid={`template-line-stores-${index}`}>
                {/* Checkboxes, not a <select multiple>. That control needs
                    Ctrl-click to take a second item, gives no sign that it
                    does, and silently REPLACES the selection on an ordinary
                    click - so choosing a fifth Store looks like it deselected
                    the other four. With forty shops that is not a preference,
                    it is a control that does not work. */}
                <div className="flex items-center gap-2">
                  <button type="button"
                          data-testid={`template-line-stores-all-${index}`}
                          onClick={() => setLine(index, {
                            store_ids: stores.map((store) => String(store.id)) })}
                          className="px-2 py-1 rounded border border-slate-300 text-xs hover:bg-white">
                    Select all
                  </button>
                  <button type="button"
                          data-testid={`template-line-stores-none-${index}`}
                          onClick={() => setLine(index, { store_ids: [] })}
                          className="px-2 py-1 rounded border border-slate-300 text-xs hover:bg-white">
                    Clear
                  </button>
                  <span className="text-xs text-slate-500">
                    {line.store_ids.length
                      ? `${line.store_ids.length} of ${stores.length} chosen`
                      : "none chosen yet"}
                  </span>
                </div>
                <div className="max-h-40 overflow-y-auto border border-slate-300 rounded-md bg-white px-2 py-1 min-w-[260px]">
                  {stores.map((store) => {
                    const value = String(store.id);
                    const chosen = line.store_ids.includes(value);
                    return (
                      <label key={store.id}
                             className="flex items-center gap-2 py-0.5 text-sm cursor-pointer">
                        <input type="checkbox" checked={chosen}
                               data-testid={`template-line-store-${index}-${store.id}`}
                               onChange={() => setLine(index, {
                                 store_ids: chosen
                                   ? line.store_ids.filter((id) => id !== value)
                                   : [...line.store_ids, value] })} />
                        {store.store_name}
                        <span className="text-xs text-slate-400">({store.store_code})</span>
                      </label>
                    );
                  })}
                </div>
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
