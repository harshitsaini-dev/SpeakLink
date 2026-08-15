import React from "react";
import { api } from "@/lib/api";
import { Play, RefreshCw, Plus, X, Volume2 } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager, SortableTh, ExportButton } from "@/components/AdminFilters";
import { BulkBar, BulkDeleteConfirm, useBulkSelection } from "@/components/BulkBar";
import { useAuth } from "@/contexts/AuthContext";
import AnnouncementRooms from "@/components/AnnouncementRooms";

/**
 * Templates: the plan for what plays where, and until when.
 *
 * Its own page rather than a section under the play console. They are used at
 * completely different rates - the console many times a day, this once a
 * fortnight - and sharing a screen meant scrolling past the fortnightly thing
 * to reach the hourly one. It also means this page can have its own search,
 * filters and pagination, which a section could not.
 */
function describeTarget(item) {
  return item.zone ? `zone ${item.zone}`
                   : (item.store_name || `store ${item.store_id}`);
}

function PlaysIn({ items, testId, name }) {
  const [open, setOpen] = React.useState(false);
  const [needle, setNeedle] = React.useState("");
  const rows = items || [];

  // Escape closes it, because a panel over the table that can only be closed
  // with the mouse is a panel somebody fights with.
  React.useEffect(() => {
    if (!open) return undefined;
    const key = (event) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [open]);

  if (rows.length === 0) {
    return <span className="text-sm text-muted">nowhere yet</span>;
  }
  if (rows.length === 1) {
    const only = rows[0];
    return (
      <span className="text-sm text-body"
            data-testid={`template-plays-in-${testId}`}>
        {describeTarget(only)}
        <span className="text-faint"> · </span>
        <span className="text-muted">{only.audio_title || "recording"}</span>
      </span>
    );
  }

  const term = needle.trim().toLowerCase();
  const shown = term
    ? rows.filter((item) =>
        `${item.audio_title || ""} ${describeTarget(item)}`.toLowerCase()
          .includes(term))
    : rows;

  return (
    <span data-testid={`template-plays-in-${testId}`}>
      {/* Sized and coloured like every other cell in this table.
          It was text-xs and blue while its neighbours were text-sm and
          slate, so the one thing that opens something looked like a stray
          hyperlink dropped into a row rather than part of the column. It is
          still visibly a control - underlined on hover, and it says what it
          will show - but it sits on the same line as the text beside it. */}
      <button type="button" onClick={() => setOpen(true)}
              data-testid={`template-plays-in-toggle-${testId}`}
              className="text-sm font-medium text-body underline decoration-slate-300
                         underline-offset-2 hover:text-strong hover:decoration-slate-500">
        {rows.length} places
      </button>
      {open && (
        <div className="fixed inset-0 z-40 bg-surface-muted/40 flex items-center justify-center p-6"
             data-testid={`template-plays-in-overlay-${testId}`}
             onClick={() => setOpen(false)}>
          {/* Stopping the click here so a click INSIDE the panel does not
              close the thing being read. */}
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col glass shadow-2xl"
               onClick={(event) => event.stopPropagation()}>
            <div className="flex items-start justify-between gap-4 px-5 py-4 border-b border-line">
              <div className="min-w-0">
                <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted">
                  Plays in
                </div>
                {/* The template's name is the heading; the count is a
                    subordinate fact and is sized like one. Both on one line
                    at the same weight made neither of them the title. */}
                <h2 className="mt-0.5 truncate text-base font-semibold leading-6 text-strong">
                  {name}
                </h2>
                <p className="text-xs leading-5 text-muted">
                  {rows.length} places
                </p>
              </div>
              {/* A real close control: an icon with a hit area, not a
                  lower-case letter x sitting at a different size to
                  everything around it. */}
              <button type="button" onClick={() => setOpen(false)}
                      aria-label="Close"
                      data-testid={`template-plays-in-close-${testId}`}
                      className="-mr-1 -mt-1 shrink-0 rounded p-1.5 text-faint hover:bg-surface-muted hover:text-body">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="border-b border-line px-5 py-3">
              {/* Forty names is a scroll; two hundred is a hunt. */}
              <input value={needle} placeholder="Search a shop or Zone…"
                     onChange={(event) => setNeedle(event.target.value)}
                     data-testid={`template-plays-in-search-${testId}`}
                     className="w-full rounded-md border border-line-strong px-3 py-2 text-sm
                                placeholder:text-faint focus:border-line-strong focus:outline-none" />
            </div>
            {/* Two columns, so a forty-shop campaign is one glance instead of
                a column of text taller than the screen. The recording is the
                quiet half of each line and the place is the loud half, which
                is the way round people read this list. */}
            <ul className="grid gap-x-6 gap-y-1 overflow-y-auto px-5 py-3 sm:grid-cols-2">
              {shown.map((item, index) => (
                <li key={index}
                    className="flex items-baseline justify-between gap-3 border-b border-line py-1 text-sm last:border-0">
                  <span className="truncate font-medium text-strong">
                    {describeTarget(item)}
                  </span>
                  <span className="shrink-0 text-xs text-muted">
                    {item.audio_title || "recording"}
                  </span>
                </li>
              ))}
              {shown.length === 0 && (
                <li className="py-1 text-sm text-muted sm:col-span-2">
                  Nothing here matches that.
                </li>
              )}
            </ul>
            <div className="border-t border-line px-5 py-2 text-xs text-muted">
              Showing {shown.length} of {rows.length}
            </div>
          </div>
        </div>
      )}
    </span>
  );
}


/** Every button on a table row: same height, same padding, same text. */
const ROW_BUTTON =
  "inline-flex h-8 items-center justify-center gap-1 rounded border px-2.5 text-sm "
  + "leading-none disabled:opacity-40";
const ROW_BUTTON_PLAIN =
  `${ROW_BUTTON} border-line-strong text-body hover:bg-surface-muted`;
const ROW_BUTTON_DANGER =
  `${ROW_BUTTON} border-rose-300 text-rose-700 hover:bg-rose-50`;


export default function AnnouncementTemplates() {
  const { can } = useAuth();
  const list = useAdminList("/announcements/templates", {
    q: "", status: "active", zone: "", window: "", store_id: "", sort: "", dir: "asc",
  });
  const bulk = useBulkSelection(list);
  const [audio, setAudio] = React.useState([]);
  const [stores, setStores] = React.useState([]);
  const [zones, setZones] = React.useState([]);
  const [building, setBuilding] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const [deleting, setDeleting] = React.useState(false);
  const [linksFor, setLinksFor] = React.useState(null);
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
      .then(({ data }) => {
        setStores(data.stores || []);
        // Zones come from the ESTATE, not from the rows on screen. Deriving
        // them from the current page emptied the dropdown exactly when it was
        // needed: filter to something that matches nothing and the control
        // that would let you widen the filter has no options left.
        setZones((data.regions || []).map((region) =>
          typeof region === "string" ? region : region.value || region.label));
      })
      .catch(() => {});
  }, []);

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
          <h1 className="text-2xl font-bold tracking-tight text-strong">Templates</h1>
          <p className="text-sm text-muted">
            Decide once what plays where and until when. After that there is
            nothing to choose - only play and pause.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ExportButton dataset="announcement-templates" list={list} testId="templates-export" />
          <button data-testid="templates-refresh" onClick={list.reload}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted">
            <RefreshCw size={14} /> Refresh
          </button>
          {mayManage && (
            <button data-testid="template-new"
                    onClick={() => setBuilding((open) => !open)}
                    className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-surface-muted hover:bg-surface-muted">
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

      {(building || editing) && mayManage && (
        <TemplateBuilder audio={audio} zones={zones} stores={stores}
                         editing={editing}
                         key={editing ? `edit-${editing.id}` : "new"}
                         onCancel={() => { setBuilding(false); setEditing(null); }}
                         onCreated={() => {
                           setBuilding(false); setEditing(null); list.reload();
                         }} />
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
        {/* Which shops a plan touches is the other way people look for one -
            "what is scheduled for these six" rather than "what is in this
            zone". Both, because a zone with an exception in it is the normal
            case and neither filter alone describes it. */}
        <FilterSelect label="Store" testId="templates-store" allLabel="All Stores"
                      value={list.filters.store_id}
                      onChange={(value) => list.setFilter("store_id", value)}
                      options={stores.map((store) => ({
                        value: String(store.id),
                        label: `${store.store_name} (${store.store_code})` }))} />
        <FilterSelect label="Status" testId="templates-status" allLabel="All"
                      value={list.filters.status}
                      onChange={(value) => list.setFilter("status", value)}
                      options={[{ value: "active", label: "Active" },
                                { value: "archived", label: "Archived" }]} />
        {/* The window is the column people scan, so it is the filter they
            reach for: "which of these have already expired" is the question
            behind most of the tidying that happens on this page. */}
        <FilterSelect label="Window" testId="templates-window" allLabel="Any"
                      value={list.filters.window}
                      onChange={(value) => list.setFilter("window", value)}
                      options={[{ value: "live", label: "Running now" },
                                { value: "scheduled", label: "Starts later" },
                                { value: "expired", label: "Already expired" }]} />
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

      {linksFor && (
        <AnnouncementRooms templateId={linksFor.id} templateName={linksFor.name} />
      )}

      <div className="glass rounded-xl overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-surface-muted text-left text-[11px] uppercase tracking-wider text-muted border-b border-line">
            <tr>
              {(mayManage || mayDelete) && <th className="px-3 py-2 w-8"></th>}
              <SortableTh column="name" label="Template" list={list} />
              <SortableTh column="plays_in" label="Plays in" list={list} />
              <SortableTh column="window" label="Window" list={list} />
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={5} onRetry={list.reload}
                       emptyText="No template matches these filters." />
            {!list.loading && !list.error && list.items.map((template) => (
              <tr key={template.id} data-testid={`template-row-${template.id}`}
                  className="border-b border-line even:bg-surface-alt">
                {(mayManage || mayDelete) && (
                  <td className="px-3 py-2">
                    <input type="checkbox" checked={bulk.isChosen(template.id)}
                           data-testid={`template-select-${template.id}`}
                           onChange={() => bulk.toggle(template.id)} />
                  </td>
                )}
                <td className="px-3 py-2">
                  <div className="font-medium text-strong">{template.name}</div>
                  <div className="text-xs text-muted">{template.description}</div>
                </td>
                <td className="px-3 py-2 text-body align-top">
                  {/* A count first, the list on request.
                      A template aimed at forty shops printed forty lines and
                      pushed every other row off the screen - so the column
                      that was meant to say "where this plays" made the table
                      unreadable. One line answers the ordinary question; the
                      names are one click away for the rare one. */}
                  <PlaysIn items={template.items} testId={template.id}
                           name={template.name} />
                </td>
                <td className="px-3 py-2">
                  {/* The window in words. A template that is not playing and
                      does not say why is the thing an operator rings up
                      about. */}
                  <span className={template.is_live ? "text-body" : "text-amber-700"}
                        data-testid={`template-window-${template.id}`}>
                    {template.window}
                  </span>
                </td>
                <td className="px-3 py-2">
                  {/* A flex row with one gap, rather than each button
                      carrying its own mr-1: the margin version leaves a
                      trailing gap after the last button, so the column never
                      quite lines up with the heading above it. */}
                  <div className="flex flex-wrap items-center justify-end gap-1.5">
                  {mayControl && (
                    <button data-testid={`template-play-${template.id}`}
                            disabled={busy !== "" || !template.is_live}
                            onClick={() => act(`Play ${template.name}`,
                              () => api.post(`/announcements/templates/${template.id}/play`))}
                            className={ROW_BUTTON_PLAIN}>
                      <Play className="h-4 w-4" /> Play
                    </button>
                  )}
                  {mayManage && (
                    <button data-testid={`template-edit-${template.id}`}
                            onClick={() => { setEditing(template); setBuilding(false); }}
                            className={ROW_BUTTON_PLAIN}>
                      Edit
                    </button>
                  )}
                  {mayManage && (
                    <button data-testid={`template-archive-${template.id}`}
                            disabled={busy !== ""}
                            onClick={() => act(`Archive ${template.name}`,
                              () => api.delete(`/announcements/templates/${template.id}`))}
                            className={ROW_BUTTON_PLAIN}>
                      Archive
                    </button>
                  )}
                  <button data-testid={`template-links-${template.id}`}
                          onClick={() => setLinksFor(
                            linksFor === template.id ? null : template)}
                          className={ROW_BUTTON_PLAIN}>
                    Links
                  </button>
                  {mayDelete && (
                    <button data-testid={`template-delete-${template.id}`}
                            onClick={() => { bulk.chooseOnly(template.id);
                                             setDeleting(true); }}
                            className={ROW_BUTTON_DANGER}>
                      Delete
                    </button>
                  )}
                  </div>
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

/** An ISO instant as the local value a datetime-local input wants. */
function forInput(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`
         + `T${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/**
 * A saved template's lines, back in the shape the form edits.
 *
 * Lines aimed at the same recording and volume are regrouped into one
 * "several Stores" line, because that is how they were entered. Showing forty
 * separate one-Store lines for something typed as one would make an edit a
 * re-entry.
 */
function linesFrom(template) {
  const items = template?.items;
  if (!items || !items.length) return null;
  const zoneLines = items.filter((item) => item.zone).map((item) => ({
    audio_id: String(item.audio_id), target: "zone", zone: item.zone,
    store_id: "", store_ids: [], volume_percent: item.volume_percent ?? 80,
  }));
  const grouped = new Map();
  for (const item of items.filter((entry) => !entry.zone)) {
    const key = `${item.audio_id}:${item.volume_percent ?? 80}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        audio_id: String(item.audio_id), target: "stores", zone: "",
        store_id: "", store_ids: [], volume_percent: item.volume_percent ?? 80,
      });
    }
    grouped.get(key).store_ids.push(String(item.store_id));
  }
  return [...zoneLines, ...grouped.values()];
}

/**
 * Building a template.
 *
 * The whole point of the feature is that this is done ONCE: afterwards nobody
 * chooses a recording for a shop again, they only press play and pause. So the
 * form asks for everything that decision needs - including when it stops -
 * rather than leaving the expiry to somebody's memory.
 */
function TemplateBuilder({ audio, zones, stores, onCancel, onCreated,
                          editing = null }) {
  // One form for both jobs. A separate edit form would be the same twenty
  // controls with the same validation, kept in step by hand - and the day
  // they drift is the day a template can be created with something it cannot
  // be edited to.
  const [name, setName] = React.useState(editing?.name || "");
  const [description, setDescription] = React.useState(editing?.description || "");
  const [startsAt, setStartsAt] = React.useState(forInput(editing?.starts_at));
  const [expiresAt, setExpiresAt] = React.useState(forInput(editing?.expires_at));
  // The DAILY window, which is a different question from the campaign's dates:
  // "runs all October" and "10:00 to 22:00" are both true at once, and one
  // field could not have said both.
  const [dailyStart, setDailyStart] = React.useState(editing?.daily_start || "");
  const [dailyEnd, setDailyEnd] = React.useState(editing?.daily_end || "");
  const [dailyDays, setDailyDays] = React.useState(
    (editing?.daily_days || "").split(",").filter(Boolean));
  const [lines, setLines] = React.useState(() => linesFrom(editing) || [
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
      const body = {
        name, description,
        // Sent as an instant, not a date. "Expires on the 5th" is ambiguous
        // about whether the 5th is included, and a jingle that runs a day too
        // long is a promotion that outlives its own price.
        starts_at: startsAt ? new Date(startsAt).toISOString() : null,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        daily_start: dailyStart,
        daily_end: dailyEnd,
        daily_days: dailyDays.join(","),
        items,
      };
      if (editing) {
        await api.put(`/announcements/templates/${editing.id}`, body);
      } else {
        await api.post("/announcements/templates", body);
      }
      onCreated?.();
    } catch (error) {
      setFailure(error?.response?.data?.detail
                 || (editing ? "That change was not accepted."
                             : "That template was not accepted."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} data-testid="template-builder"
          className="border border-line rounded-md bg-surface-muted px-4 py-4 space-y-3">
      <div className="flex flex-wrap gap-2">
        <input value={name} onChange={(event) => setName(event.target.value)}
               placeholder="Template name" data-testid="template-name" required
               className="px-3 py-2 border border-line-strong rounded-md text-sm min-w-[220px]" />
        <input value={description} onChange={(event) => setDescription(event.target.value)}
               placeholder="What is it for? (optional)" data-testid="template-description"
               className="px-3 py-2 border border-line-strong rounded-md text-sm flex-1 min-w-[220px]" />
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-muted">Starts</span>
          <input type="datetime-local" value={startsAt} data-testid="template-starts"
                 onChange={(event) => setStartsAt(event.target.value)}
                 className="px-3 py-2 border border-line-strong rounded-md text-sm" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-muted">Stops</span>
          <input type="datetime-local" value={expiresAt} data-testid="template-expires"
                 onChange={(event) => setExpiresAt(event.target.value)}
                 className="px-3 py-2 border border-line-strong rounded-md text-sm" />
        </label>
        <p className="text-xs text-muted max-w-sm">
          Leave both empty to run until somebody stops it. An expiry is how a
          festival jingle stops on its own instead of playing into February.
        </p>
      </div>

      {/* SHOP HOURS.
          Separate from the dates above on purpose: those say for how many
          weeks the promotion runs, these say when in the day it is audible.
          Somebody who wants "all October, 10 to 10" has to be able to say
          both, and it is the second one they otherwise have to remember to do
          by hand twice a day. */}
      <div className="flex flex-wrap items-end gap-3 glass rounded-xl px-3 py-3">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-muted">
            Plays from
          </span>
          <input type="time" value={dailyStart} data-testid="template-daily-start"
                 onChange={(event) => setDailyStart(event.target.value)}
                 className="px-3 py-2 border border-line-strong rounded-md text-sm" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-muted">
            Until
          </span>
          <input type="time" value={dailyEnd} data-testid="template-daily-end"
                 onChange={(event) => setDailyEnd(event.target.value)}
                 className="px-3 py-2 border border-line-strong rounded-md text-sm" />
        </label>
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-muted">
            On these days
          </span>
          <div className="flex flex-wrap gap-1">
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, index) => {
              const value = String(index);
              const chosen = dailyDays.includes(value);
              return (
                <button key={day} type="button"
                        data-testid={`template-daily-day-${index}`}
                        onClick={() => setDailyDays((current) => (
                          current.includes(value)
                            ? current.filter((entry) => entry !== value)
                            : [...current, value]))}
                        className={`h-8 rounded border px-2 text-xs ${
                          chosen
                            ? "border-slate-900 bg-surface-muted text-white"
                            : "border-line-strong text-body hover:bg-surface-muted"}`}>
                  {day}
                </button>
              );
            })}
          </div>
        </div>
        <p className="max-w-sm text-xs text-muted">
          It starts and stops itself - 10:00 to 22:00 means silent at 22:00.
          No days chosen means every day. Times are this HQ machine's clock.
          Pausing a shop by hand still wins: the window will not un-pause it.
        </p>
      </div>

      <div className="space-y-2">
        {lines.map((line, index) => (
          <div key={index} className="flex flex-wrap items-start gap-2"
               data-testid={`template-line-${index}`}>
            <select value={line.audio_id} required
                    data-testid={`template-line-audio-${index}`}
                    onChange={(event) => setLine(index, { audio_id: event.target.value })}
                    className="px-3 py-2 border border-line-strong rounded-md text-sm min-w-[200px]">
              <option value="">Which recording?</option>
              {audio.map((recording) => (
                <option key={recording.id} value={recording.id}>{recording.title}</option>
              ))}
            </select>

            <select value={line.target} data-testid={`template-line-target-${index}`}
                    onChange={(event) => setLine(index, { target: event.target.value })}
                    className="px-3 py-2 border border-line-strong rounded-md text-sm">
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
                          className="px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface">
                    Select all
                  </button>
                  <button type="button"
                          data-testid={`template-line-stores-none-${index}`}
                          onClick={() => setLine(index, { store_ids: [] })}
                          className="px-2 py-1 rounded border border-line-strong text-xs hover:bg-surface">
                    Clear
                  </button>
                  <span className="text-xs text-muted">
                    {line.store_ids.length
                      ? `${line.store_ids.length} of ${stores.length} chosen`
                      : "none chosen yet"}
                  </span>
                </div>
                <div className="max-h-40 overflow-y-auto border border-line-strong rounded-md bg-surface px-2 py-1 min-w-[260px]">
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
                        <span className="text-xs text-faint">({store.store_code})</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            ) : line.target === "zone" ? (
              <select value={line.zone} required
                      data-testid={`template-line-zone-${index}`}
                      onChange={(event) => setLine(index, { zone: event.target.value })}
                      className="px-3 py-2 border border-line-strong rounded-md text-sm min-w-[160px]">
                <option value="">Which zone?</option>
                {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
              </select>
            ) : (
              <select value={line.store_id} required
                      data-testid={`template-line-store-${index}`}
                      onChange={(event) => setLine(index, { store_id: event.target.value })}
                      className="px-3 py-2 border border-line-strong rounded-md text-sm min-w-[220px]">
                <option value="">Which Store?</option>
                {stores.map((store) => (
                  <option key={store.id} value={store.id}>
                    {store.store_name} ({store.store_code})
                  </option>
                ))}
              </select>
            )}

            <label className="flex items-center gap-2 text-sm text-body">
              <Volume2 className="w-4 h-4 text-faint" />
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
                      className="p-1.5 rounded border border-line-strong hover:bg-surface">
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        ))}
        <button type="button" data-testid="template-line-add"
                onClick={() => setLines((current) => [...current,
                  { audio_id: "", target: "zone", zone: "", store_id: "",
                    store_ids: [], volume_percent: 80 }])}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md border border-line-strong text-sm hover:bg-surface">
          <Plus className="w-4 h-4" /> Another line
        </button>
      </div>

      {failure && (
        <p className="text-sm text-rose-800" data-testid="template-builder-error">{failure}</p>
      )}

      <div className="flex items-center gap-2">
        <button type="submit" disabled={busy} data-testid="template-save"
                className="px-3 py-2 rounded-md text-sm text-white bg-surface-muted hover:bg-surface-muted disabled:opacity-50">
          {busy ? "Saving…" : "Save template"}
        </button>
        <button type="button" onClick={onCancel} data-testid="template-cancel"
                className="px-3 py-2 rounded-md text-sm border border-line-strong hover:bg-surface">
          Cancel
        </button>
      </div>
    </form>
  );
}
