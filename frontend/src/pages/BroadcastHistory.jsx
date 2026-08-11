import React from "react";
import RecordingActions from "@/components/RecordingActions";
import { useRecordingPlayback } from "@/contexts/RecordingPlaybackContext";
import { api } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import { formatIst, parseUtcMs, elapsedSeconds } from "@/lib/time";
import { RefreshCw, Archive, ArchiveRestore, Trash2 } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { useAdminList, useBulkSelection } from "@/lib/adminList";
import {
  FilterBar, SearchInput, FilterSelect, FilterDate, ListState, Pager,
  BulkBar, DestructiveModal,
} from "@/components/AdminFilters";

const fmt = (s) => formatIst(s);
const dur = (a, b) => {
  if (!a) return "—";
  const endMs = b ? parseUtcMs(b) : Date.now();
  const s = elapsedSeconds(a, endMs ?? Date.now());
  const m = Math.floor(s / 60); const r = s % 60;
  return `${m}m ${String(r).padStart(2, "0")}s`;
};

export default function BroadcastHistory() {
  const { can } = useAuth();
  // The player itself lives in Layout so it survives navigation. History only
  // says WHICH recording to play, and asks for it to start.
  const { active, playRecording, forgetRecording } = useRecordingPlayback();
  const list = useAdminList("/broadcast/history/search", {
    q: "", status: "", date_from: "", date_to: "", started_by: "",
    store_id: "", city: "", region: "",
    include_archived: false, archived_only: false,
  });
  const selection = useBulkSelection({
    items: list.items, total: list.total, filters: list.filters,
  });

  const [open, setOpen] = React.useState(null);
  const [confirming, setConfirming] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [actionError, setActionError] = React.useState("");
  const [users, setUsers] = React.useState([]);
  const [options, setOptions] = React.useState({ regions: [], cities: [], stores: [] });

  React.useEffect(() => {
    Promise.all([
      api.get("/users/search", { params: { page_size: 200 } }).catch(() => null),
      api.get("/receivers/filter-options").catch(() => null),
    ]).then(([u, o]) => {
      if (u) setUsers(u.data.items || []);
      if (o) setOptions(o.data);
    });
  }, []);

  const openDetail = async (id) => {
    const { data } = await api.get(`/broadcast/sessions/${id}`);
    setOpen(data);
  };

  const runBulk = async (path, extra = {}) => {
    setBusy(true);
    setActionError("");
    try {
      await api.post(path, { ...selection.toRequest(), ...extra });
      selection.clear();
      setConfirming(false);
      await list.reload();
    } catch (failure) {
      setActionError(failure?.response?.data?.detail || "That action could not be completed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4" data-testid="history-page">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">Broadcast History</h1>
        <button data-testid="history-refresh-btn" onClick={list.reload}
                className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q} onChange={(v) => list.setFilter("q", v)}
                     placeholder="Search campaign name…" testId="history-search" />
        <FilterSelect label="Status" testId="history-status" allLabel="Any status"
                      value={list.filters.status}
                      options={[{ value: "active", label: "Active" },
                                { value: "completed", label: "Completed" },
                                { value: "failed", label: "Failed" }]}
                      onChange={(v) => list.setFilter("status", v)} />
        <FilterDate label="From" testId="history-date-from" value={list.filters.date_from}
                    onChange={(v) => list.setFilter("date_from", v)} />
        <FilterDate label="To" testId="history-date-to" value={list.filters.date_to}
                    onChange={(v) => list.setFilter("date_to", v)} />
        <FilterSelect label="User" testId="history-user" allLabel="Any user"
                      value={list.filters.started_by}
                      options={users.map((u) => ({ value: String(u.id), label: u.username }))}
                      onChange={(v) => list.setFilter("started_by", v)} />
        <FilterSelect label="Zone" testId="history-zone" allLabel="All Zones"
                      value={list.filters.region} options={options.regions}
                      onChange={(v) => list.setFilter("region", v)} />
        <FilterSelect label="City" testId="history-city" allLabel="All Cities"
                      value={list.filters.city} options={options.cities}
                      onChange={(v) => list.setFilter("city", v)} />
        <FilterSelect label="Store" testId="history-store" allLabel="All Stores"
                      value={list.filters.store_id}
                      options={options.stores.map((s) => ({
                        value: String(s.id), label: `${s.store_name} (${s.store_code})` }))}
                      onChange={(v) => list.setFilter("store_id", v)} />
        <FilterSelect label="Archived" testId="history-archived" allLabel="Not archived"
                      value={list.filters.archived_only ? "only"
                             : list.filters.include_archived ? "all" : ""}
                      options={[{ value: "all", label: "Show archived too" },
                                { value: "only", label: "Archived only" }]}
                      onChange={(v) => list.setFilters((f) => ({
                        ...f,
                        include_archived: v === "all",
                        archived_only: v === "only",
                      }))} />
      </FilterBar>

      {(can("broadcast_history.archive") || can("broadcast_history.delete_permanently")) && (
        <BulkBar selection={selection} total={list.total} pageCount={list.items.length}>
          {can("broadcast_history.archive") && (
            <>
              <button type="button" data-testid="history-archive-selected" disabled={busy}
                      onClick={() => runBulk("/broadcast/history/archive")}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-300 rounded bg-white hover:bg-slate-50 disabled:opacity-40">
                <Archive size={12} /> Archive
              </button>
              <button type="button" data-testid="history-unarchive-selected" disabled={busy}
                      onClick={() => runBulk("/broadcast/history/unarchive")}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-300 rounded bg-white hover:bg-slate-50 disabled:opacity-40">
                <ArchiveRestore size={12} /> Unarchive
              </button>
            </>
          )}
          {can("broadcast_history.delete_permanently") && (
            <button type="button" data-testid="history-delete-selected" disabled={busy}
                    onClick={() => setConfirming(true)}
                    className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-300 text-red-700 rounded bg-white hover:bg-red-50 disabled:opacity-40">
              <Trash2 size={12} /> Delete Permanently
            </button>
          )}
        </BulkBar>
      )}

      {actionError && !confirming && (
        <div role="alert" data-testid="history-action-error"
             className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
          {actionError}
        </div>
      )}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2 w-8"></th>
              <th className="px-3 py-2">#</th>
              <th className="px-3 py-2">Campaign</th>
              <th className="px-3 py-2">Mode</th>
              <th className="px-3 py-2">Targets</th>
              <th className="px-3 py-2">Started</th>
              <th className="px-3 py-2">Duration</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Recording</th>
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={9} onRetry={list.reload}
                       emptyText="No broadcast sessions match these filters." />
            {!list.loading && !list.error && list.items.map((s) => (
              <tr key={s.id} data-testid={`history-row-${s.id}`}
                  className="border-b border-slate-100 even:bg-slate-50/50">
                <td className="px-3 py-2">
                  <input type="checkbox" data-testid={`history-select-${s.id}`}
                         checked={selection.isSelected(s.id)}
                         disabled={selection.mode === "filtered"}
                         onChange={() => selection.toggleRow(s.id)} />
                </td>
                <td className="px-3 py-2 font-mono text-xs text-slate-500 cursor-pointer"
                    onClick={() => openDetail(s.id)}>#{s.id}</td>
                <td className="px-3 py-2 font-medium cursor-pointer hover:text-blue-700"
                    onClick={() => openDetail(s.id)}>
                  {s.campaign_name}
                  {s.archived_at && (
                    <span data-testid={`history-archived-${s.id}`}
                          className="ml-2 inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-slate-100 text-slate-600 border border-slate-300">
                      Archived
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-slate-600 uppercase tracking-wider">{s.target_mode}</td>
                <td className="px-3 py-2">{s.selected_store_count}{" "}
                  <span className="text-slate-400 text-xs">({s.online_store_count} online)</span></td>
                <td className="px-3 py-2 text-xs">{fmt(s.started_at)}</td>
                <td className="px-3 py-2 font-mono text-xs">{dur(s.started_at, s.ended_at)}</td>
                <td className="px-3 py-2"><StatusBadge status={s.status} /></td>
                <td className="px-3 py-2">
                  <RecordingActions
                    sessionId={s.id}
                    recording={s.recording}
                    isActive={active?.id === s.id}
                    onPlay={() => playRecording(s)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pager page={list.page} pages={list.pages} total={list.total}
               hasMore={list.hasMore} onPage={list.setPage} />
      </div>

      {confirming && (
        <DestructiveModal
          testIdPrefix="history-delete"
          title="Permanently delete broadcast sessions?"
          count={selection.selectedCount} countNoun="session"
          confirmWord="DELETE"
          warning="These sessions and their per-Store target rows are removed for good. The record of this deletion is kept in a separate administrative audit that this action cannot touch."
          busy={busy} error={actionError}
          onCancel={() => { setConfirming(false); setActionError(""); }}
          onConfirm={({ typed, acknowledged }) => {
            // If the recording being played is among these, stop it before the
            // rows go: audio must not keep coming out of a deleted broadcast.
            // isSelected covers both an explicit id list and Select All
            // Filtered, where the ids are never enumerated client-side.
            if (active && selection.isSelected(active.id)) forgetRecording(active.id);
            return runBulk("/broadcast/history/delete-permanently",
                           { confirm: typed, acknowledged });
          }}
        />
      )}

      {open && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" data-testid="history-detail-modal">
          <div className="bg-white rounded-md shadow-xl max-w-3xl w-full max-h-[80vh] flex flex-col">
            <div className="p-5 border-b border-slate-200 flex items-center justify-between">
              <div>
                <div className="text-xs text-slate-500 uppercase tracking-widest">Session #{open.id}</div>
                <div className="text-lg font-semibold">{open.campaign_name}</div>
              </div>
              <button data-testid="close-detail-btn" onClick={() => setOpen(null)} className="text-slate-500 hover:text-slate-900">✕</button>
            </div>
            <div className="p-5 grid grid-cols-2 md:grid-cols-4 gap-3 border-b border-slate-200 text-sm">
              <div><div className="text-[10px] uppercase text-slate-500">Mode</div><div>{open.target_mode}</div></div>
              <div><div className="text-[10px] uppercase text-slate-500">Started</div><div>{fmt(open.started_at)}</div></div>
              <div><div className="text-[10px] uppercase text-slate-500">Ended</div><div>{fmt(open.ended_at)}</div></div>
              <div><div className="text-[10px] uppercase text-slate-500">Status</div><StatusBadge status={open.status}/></div>
            </div>
            <div className="p-5 overflow-y-auto">
              <div className="text-xs uppercase text-slate-500 mb-2">Targets ({open.targets?.length || 0})</div>
              <table className="w-full text-sm">
                <thead className="text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="px-2 py-1.5">Store</th>
                    <th className="px-2 py-1.5">Play Status</th>
                    <th className="px-2 py-1.5">Started</th>
                    <th className="px-2 py-1.5">Stopped</th>
                    <th className="px-2 py-1.5">Error</th>
                  </tr>
                </thead>
                <tbody>
                  {(open.targets || []).map((t) => (
                    <tr key={t.id} className="border-b border-slate-100" data-testid={`history-target-${t.store_id}`}>
                      <td className="px-2 py-1.5 text-xs">
                        {t.store_name ? (
                          <>
                            <span className="font-medium">{t.store_name}</span>
                            {t.store_code && <span className="font-mono text-slate-500"> ({t.store_code})</span>}
                            {t.store_deleted && (
                              <span data-testid={`history-target-deleted-badge-${t.store_id}`}
                                    className="ml-1 inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-red-50 text-red-700 border border-red-200">
                                Deleted
                              </span>
                            )}
                          </>
                        ) : (
                          <span className="font-mono">{t.store_id}</span>
                        )}
                      </td>
                      <td className="px-2 py-1.5"><StatusBadge status={t.play_status}/></td>
                      <td className="px-2 py-1.5 text-xs">{fmt(t.started_playing_at)}</td>
                      <td className="px-2 py-1.5 text-xs">{fmt(t.stopped_at)}</td>
                      <td className="px-2 py-1.5 text-xs text-red-700">{t.error_message || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
