import React from "react";
import { api } from "@/lib/api";
import { formatIst } from "@/lib/time";
import { RefreshCw, Archive, Trash2 } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { useAdminList, useBulkSelection } from "@/lib/adminList";
import {
  FilterBar, SearchInput, FilterSelect, FilterDate, ListState, Pager,
  BulkBar, DestructiveModal,
} from "@/components/AdminFilters";

const LEVEL_COLOR = { info: "text-slate-700", warn: "text-amber-700", error: "text-red-700" };

export default function SystemLogs() {
  const { can } = useAuth();
  const list = useAdminList("/logs/search", {
    q: "", level: "", date_from: "", date_to: "",
    actor_user_id: "", store_id: "", device_public_id: "",
    include_archived: false, archived_only: false,
  });
  const selection = useBulkSelection({
    items: list.items, total: list.total, filters: list.filters,
  });
  const [confirming, setConfirming] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [actionError, setActionError] = React.useState("");
  const [users, setUsers] = React.useState([]);
  const [stores, setStores] = React.useState([]);

  React.useEffect(() => {
    Promise.all([
      api.get("/users/search", { params: { page_size: 200 } }).catch(() => null),
      api.get("/receivers/filter-options").catch(() => null),
    ]).then(([u, s]) => {
      if (u) setUsers(u.data.items || []);
      if (s) setStores(s.data.stores || []);
    });
  }, []);

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

  const coverage = list.meta?.entity_filter_coverage;

  return (
    <div className="space-y-4" data-testid="logs-page">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">System Logs</h1>
        <button data-testid="logs-refresh-btn" onClick={list.reload}
                className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q} onChange={(v) => list.setFilter("q", v)}
                     placeholder="Search message text…" testId="logs-search" />
        <FilterSelect label="Level" testId="log-level-filter" allLabel="All levels"
                      value={list.filters.level}
                      options={[{ value: "info", label: "Info" },
                                { value: "warn", label: "Warn" },
                                { value: "error", label: "Error" }]}
                      onChange={(v) => list.setFilter("level", v)} />
        <FilterDate label="From" testId="logs-date-from" value={list.filters.date_from}
                    onChange={(v) => list.setFilter("date_from", v)} />
        <FilterDate label="To" testId="logs-date-to" value={list.filters.date_to}
                    onChange={(v) => list.setFilter("date_to", v)} />
        <FilterSelect label="User" testId="logs-user" allLabel="Any user"
                      value={list.filters.actor_user_id}
                      options={users.map((u) => ({ value: String(u.id), label: u.username }))}
                      onChange={(v) => list.setFilter("actor_user_id", v)} />
        <FilterSelect label="Store" testId="logs-store" allLabel="Any Store"
                      value={list.filters.store_id}
                      options={stores.map((s) => ({ value: String(s.id), label: s.store_code }))}
                      onChange={(v) => list.setFilter("store_id", v)} />
        <FilterSelect label="Archived" testId="logs-archived" allLabel="Not archived"
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

      {coverage && (
        <p className="text-xs text-slate-500 border border-slate-200 rounded-md px-3 py-2 bg-slate-50"
           data-testid="logs-coverage-note">
          User / Store / Device filters apply to newer structured log entries
          ({coverage.rows_with_structured_entities} so far). Older logs remain
          searchable by text, level and date.
        </p>
      )}

      {(can("system_logs.archive") || can("system_logs.delete_permanently")) && (
        <BulkBar selection={selection} total={list.total} pageCount={list.items.length}>
          {can("system_logs.archive") && (
            <button type="button" data-testid="logs-archive-selected" disabled={busy}
                    onClick={() => runBulk("/logs/archive")}
                    className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-300 rounded bg-white hover:bg-slate-50 disabled:opacity-40">
              <Archive size={12} /> Archive
            </button>
          )}
          {can("system_logs.delete_permanently") && (
            <button type="button" data-testid="logs-delete-selected" disabled={busy}
                    onClick={() => setConfirming(true)}
                    className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-300 text-red-700 rounded bg-white hover:bg-red-50 disabled:opacity-40">
              <Trash2 size={12} /> Delete Permanently
            </button>
          )}
        </BulkBar>
      )}

      {actionError && !confirming && (
        <div role="alert" data-testid="logs-action-error"
             className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
          {actionError}
        </div>
      )}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2 w-8"></th>
              <th className="px-3 py-2 w-40">Time</th>
              <th className="px-3 py-2 w-24">Level</th>
              <th className="px-3 py-2">Message</th>
            </tr>
          </thead>
          <tbody className="font-mono text-xs">
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={4} onRetry={list.reload}
                       emptyText="No log entries match these filters." />
            {!list.loading && !list.error && list.items.map((row) => (
              <tr key={row.id} data-testid={`log-row-${row.id}`}
                  className="border-b border-slate-100 even:bg-slate-50/50">
                <td className="px-3 py-1.5">
                  <input type="checkbox" data-testid={`log-select-${row.id}`}
                         checked={selection.isSelected(row.id)}
                         disabled={selection.mode === "filtered"}
                         onChange={() => selection.toggleRow(row.id)} />
                </td>
                <td className="px-3 py-1.5 text-slate-500">{formatIst(row.created_at)}</td>
                <td className={`px-3 py-1.5 uppercase font-bold ${LEVEL_COLOR[row.level] || ""}`}>
                  {row.level}
                </td>
                <td className="px-3 py-1.5 text-slate-800">
                  {row.message}
                  {row.archived_at && (
                    <span data-testid={`log-archived-${row.id}`}
                          className="ml-2 font-sans inline-flex px-1.5 py-0.5 rounded text-[10px] uppercase bg-slate-100 text-slate-600 border border-slate-300">
                      Archived
                    </span>
                  )}
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
          testIdPrefix="logs-delete"
          title="Permanently delete log entries?"
          count={selection.selectedCount} countNoun="log entry"
          confirmWord="DELETE"
          warning="Deleted log entries cannot be recovered. The administrative deletion audit is kept in a separate record and is never removed by this action."
          busy={busy} error={actionError}
          onCancel={() => { setConfirming(false); setActionError(""); }}
          onConfirm={({ typed, acknowledged }) =>
            runBulk("/logs/delete-permanently", { confirm: typed, acknowledged })}
        />
      )}
    </div>
  );
}
