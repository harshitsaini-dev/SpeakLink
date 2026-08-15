import React from "react";
import { api } from "@/lib/api";
import { RefreshCw, Upload } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager, SortableTh, ExportButton } from "@/components/AdminFilters";
import { BulkBar, BulkDeleteConfirm, useBulkSelection } from "@/components/BulkBar";
import { useAuth } from "@/contexts/AuthContext";
import { formatIst } from "@/lib/time";

/**
 * The recordings themselves.
 *
 * Its own page, with its own search, filters and pagination. As a section on
 * the console it could have none of those, and an estate that has been running
 * campaigns for a year has hundreds of these - a list with no search is a list
 * nobody can use.
 */
export default function AnnouncementRecordings() {
  const { can } = useAuth();
  const list = useAdminList("/announcements/audio", { q: "", status: "active", sort: "", dir: "asc" });
  const bulk = useBulkSelection(list);
  const [deleting, setDeleting] = React.useState(false);
  const [busy, setBusy] = React.useState("");
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");

  const mayUpload = can("announcements.upload");
  // The title only. The FILE is deliberately not replaceable: templates, the
  // history and every Store's cache point at a recording by id and by content
  // hash, so swapping the audio underneath would rewrite what a shop played
  // last week without leaving a trace.
  const [renaming, setRenaming] = React.useState(null);
  const [draftTitle, setDraftTitle] = React.useState("");
  const mayDelete = can("announcements.delete_permanently");

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
    <div className="space-y-4" data-testid="recordings-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-strong">Recordings</h1>
          <p className="text-sm text-muted">
            Listen to one here before sending it to a shop - nobody at HQ can
            hear what a Store is playing.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ExportButton dataset="announcement-recordings" list={list}
                        testId="recordings-export" />
          <button data-testid="recordings-refresh" onClick={list.reload}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted">
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </div>

      {note && (
        <div data-testid="recordings-note"
             className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {note}
        </div>
      )}
      {error && (
        <div data-testid="recordings-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}

      {mayUpload && <UploadRecording onUploaded={list.reload} />}

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q}
                     onChange={(value) => list.setFilter("q", value)}
                     placeholder="Title or file name…" testId="recordings-search" />
        <FilterSelect label="Status" testId="recordings-status" allLabel="All"
                      value={list.filters.status}
                      onChange={(value) => list.setFilter("status", value)}
                      options={[{ value: "active", label: "Active" },
                                { value: "archived", label: "Archived" }]} />
      </FilterBar>

      {(mayUpload || mayDelete) && (
        <BulkBar bulk={bulk} list={list} testIdPrefix="recordings" busy={busy !== ""}
                 onArchive={mayUpload ? () => act("Archive", async () => {
                   const response = await api.post("/announcements/audio/archive",
                                                   bulk.body());
                   bulk.clear();
                   return response;
                 }) : undefined}
                 onDelete={mayDelete ? () => setDeleting(true) : undefined} />
      )}

      {deleting && (
        <BulkDeleteConfirm
          count={bulk.count} noun="recording" testIdPrefix="recordings"
          warning={"The recordings and their files are removed and cannot be "
                   + "recovered. Any that a template still uses are kept and "
                   + "named in the answer - deleting one out from under a live "
                   + "campaign would leave a template that plays nothing and "
                   + "cannot say why."}
          onCancel={() => setDeleting(false)}
          onConfirm={(word) => act("Delete", async () => {
            const response = await api.post("/announcements/audio/delete",
                                            { ...bulk.body(), confirm: word });
            setDeleting(false);
            bulk.clear();
            return response;
          })} />
      )}

      <div className="glass rounded-xl overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-surface-muted text-left text-[11px] uppercase tracking-wider text-muted border-b border-line">
            <tr>
              {(mayUpload || mayDelete) && <th className="px-3 py-2 w-8"></th>}
              <SortableTh column="title" label="Recording" list={list} />
              <SortableTh column="byte_size" label="Size" list={list} />
              <SortableTh column="uploaded_at" label="Uploaded" list={list} />
              <SortableTh column="status" label="Status" list={list} />
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={6} onRetry={list.reload}
                       emptyText="No recording matches these filters." />
            {!list.loading && !list.error && list.items.map((row) => (
              <tr key={row.id} data-testid={`recording-${row.id}`}
                  className="border-b border-line even:bg-surface-alt">
                {(mayUpload || mayDelete) && (
                  <td className="px-3 py-2">
                    <input type="checkbox" checked={bulk.isChosen(row.id)}
                           data-testid={`recording-select-${row.id}`}
                           onChange={() => bulk.toggle(row.id)} />
                  </td>
                )}
                <td className="px-3 py-2">
                  {renaming === row.id ? (
                    <form data-testid={`recording-rename-form-${row.id}`}
                          className="flex flex-wrap items-center gap-2"
                          onSubmit={(event) => {
                            event.preventDefault();
                            act(`Rename ${row.title}`, async () => {
                              await api.put(`/announcements/audio/${row.id}`,
                                            { title: draftTitle });
                              setRenaming(null);
                            });
                          }}>
                      <input value={draftTitle} autoFocus required
                             data-testid={`recording-rename-input-${row.id}`}
                             onChange={(event) => setDraftTitle(event.target.value)}
                             className="px-2 py-1 text-sm border border-line-strong rounded-md" />
                      <button type="submit" disabled={busy !== ""}
                              data-testid={`recording-rename-save-${row.id}`}
                              className="px-2 py-1 rounded bg-surface-muted text-white text-xs disabled:opacity-50">
                        Save
                      </button>
                      <button type="button" onClick={() => setRenaming(null)}
                              className="px-2 py-1 rounded border border-line-strong text-xs">
                        Cancel
                      </button>
                    </form>
                  ) : (
                    <>
                      <div className="font-medium text-strong">{row.title}</div>
                      {/* The filename is shown and never edited: it is what
                          arrived, and rewriting it would make the record of
                          what was uploaded disagree with the upload. */}
                      <div className="text-xs text-muted">{row.original_filename}</div>
                    </>
                  )}
                </td>
                <td className="px-3 py-2 text-xs">{(row.byte_size / 1024).toFixed(0)} KB</td>
                <td className="px-3 py-2 text-xs">{formatIst(row.uploaded_at)}</td>
                <td className="px-3 py-2 text-xs">
                  {row.status === "archived"
                    ? <span className="text-amber-700">Archived</span>
                    : <span className="text-body">Active</span>}
                </td>
                <td className="px-3 py-2 text-right whitespace-nowrap">
                  {mayUpload && renaming !== row.id && (
                    <button data-testid={`recording-rename-${row.id}`}
                            onClick={() => { setRenaming(row.id); setDraftTitle(row.title); }}
                            className="px-2 py-1 rounded border border-line-strong text-sm hover:bg-surface-muted mr-1">
                      Rename
                    </button>
                  )}
                  {mayUpload && row.status !== "archived" && (
                    <button data-testid={`recording-archive-${row.id}`}
                            disabled={busy !== ""}
                            onClick={() => act(`Archive ${row.title}`,
                              () => api.delete(`/announcements/audio/${row.id}`))}
                            className="px-2 py-1 rounded border border-line-strong text-sm hover:bg-surface-muted mr-1">
                      Archive
                    </button>
                  )}
                  {mayDelete && (
                    <button data-testid={`recording-delete-${row.id}`}
                            onClick={() => { bulk.chooseOnly(row.id); setDeleting(true); }}
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
    <form onSubmit={submit} data-testid="recording-upload-form"
          className="flex flex-wrap items-center gap-2 glass rounded-xl px-3 py-3">
      <input type="text" value={title} placeholder="What is it called?"
             onChange={(event) => setTitle(event.target.value)}
             data-testid="recording-title"
             className="px-3 py-2 border border-line-strong rounded-md text-sm" />
      {/* The browser's native file button is the one control here that looks
          like it belongs to a different decade, and it has nowhere to show
          WHICH file is chosen. Hidden, driven by a styled label - still a real
          file input, so keyboard and screen-reader behaviour is unchanged. */}
      <label className="inline-flex items-center gap-2 px-3 py-2 border border-line-strong rounded-md text-sm cursor-pointer hover:bg-surface-muted">
        <input type="file" accept="audio/*" data-testid="recording-file"
               onChange={(event) => setFile(event.target.files?.[0] || null)}
               className="sr-only" />
        <Upload className="w-4 h-4" />
        Choose a recording
      </label>
      <span className="text-sm text-body" data-testid="recording-chosen">
        {file ? file.name : "No file chosen yet"}
      </span>
      <button type="submit" disabled={busy || !file} data-testid="recording-upload"
              className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-surface-muted hover:bg-surface-muted disabled:opacity-50">
        <Upload className="w-4 h-4" /> {busy ? "Uploading…" : "Upload"}
      </button>
      {failure && (
        <span className="text-sm text-rose-700" data-testid="recording-upload-error">
          {failure}
        </span>
      )}
    </form>
  );
}
