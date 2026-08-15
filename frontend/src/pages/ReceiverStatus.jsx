import React from "react";
import { api } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import { RefreshCw } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager, SortableTh, ExportButton } from "@/components/AdminFilters";

/**
 * Receiver Status, filtered by the server.
 *
 * This page used to call GET /stores and group the whole list in React. That
 * is fine at 44 Stores and wrong in principle: the filter the operator sees
 * and the authorization the backend enforces have to be the same narrowing,
 * or a scoped account can be shown a total that includes Stores it may not
 * see. /api/receivers/search does both.
 *
 * The Zone/City/Store dropdowns come from /api/receivers/filter-options,
 * which is built from that same scoped query - so the options can never
 * mention a Zone whose Stores this account cannot open.
 */
export default function ReceiverStatus() {
  const list = useAdminList("/receivers/search", {
    q: "", region: "", city: "", store_id: "", status: "", has_primary: "",
    sort: "", dir: "asc",
  });
  const [options, setOptions] = React.useState({ regions: [], cities: [], stores: [] });

  React.useEffect(() => {
    let cancelled = false;
    api.get("/receivers/filter-options")
      .then(({ data }) => { if (!cancelled) setOptions({ regions: data.regions || [], cities: data.cities || [], stores: data.stores || [] }); })
      .catch(() => { /* the list's own error state already reports a failure */ });
    return () => { cancelled = true; };
  }, []);

  // Refresh live status periodically, but never while the operator is mid-typing
  // a filter - a list that reorders under the cursor is worse than a stale one.
  React.useEffect(() => {
    const id = setInterval(() => { if (!list.loading) list.reload(); }, 8000);
    return () => clearInterval(id);
  }, [list]);

  return (
    <div className="space-y-4" data-testid="receivers-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-strong">Receiver Status</h1>
          <p className="text-sm text-muted">Live connection status of every store receiver.</p>
        </div>
        <div className="flex items-center gap-2">
          <ExportButton dataset="receiver-status" list={list} testId="receivers-export" />
          <button data-testid="receivers-refresh-btn" onClick={list.reload}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted">
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </div>

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q}
                     onChange={(v) => list.setFilter("q", v)}
                     placeholder="Store code, name, or Device id…"
                     testId="receivers-search" />
        <FilterSelect label="Zone" testId="receivers-zone" allLabel="All Zones"
                      value={list.filters.region} options={options.regions}
                      onChange={(v) => list.setFilter("region", v)} />
        <FilterSelect label="City" testId="receivers-city" allLabel="All Cities"
                      value={list.filters.city} options={options.cities}
                      onChange={(v) => list.setFilter("city", v)} />
        <FilterSelect label="Store" testId="receivers-store" allLabel="All Stores"
                      value={list.filters.store_id}
                      options={options.stores.map((s) => ({
                        value: String(s.id), label: `${s.store_name} (${s.store_code})` }))}
                      onChange={(v) => list.setFilter("store_id", v)} />
        <FilterSelect label="Status" testId="receivers-status" allLabel="Any status"
                      value={list.filters.status}
                      options={[{ value: "online", label: "Online" },
                                { value: "offline", label: "Offline" }]}
                      onChange={(v) => list.setFilter("status", v)} />
        <FilterSelect label="Primary" testId="receivers-primary" allLabel="Any"
                      value={list.filters.has_primary}
                      options={[{ value: "true", label: "Has primary Device" },
                                { value: "false", label: "No primary Device" }]}
                      onChange={(v) => list.setFilter("has_primary", v)} />
      </FilterBar>

      <div className="glass rounded-xl overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-surface-muted text-left text-[11px] uppercase tracking-wider text-muted border-b border-line">
            <tr>
              <SortableTh column="store_code" label="Code" list={list} />
              <SortableTh column="store_name" label="Store" list={list} />
              <SortableTh column="city" label="City" list={list} />
              <SortableTh column="region" label="Zone" list={list} />
              <SortableTh column="device_count" label="Devices" list={list} />
              <SortableTh column="has_primary" label="Primary" list={list} />
              <SortableTh column="status" label="Status" list={list} />
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={7} onRetry={list.reload}
                       emptyText="No Stores match these filters. Clear them to see everything in this account's Scope." />
            {!list.loading && !list.error && list.items.map((row) => (
              <tr key={row.id} data-testid={`receiver-card-${row.store_code}`}
                  className="border-b border-line even:bg-surface-alt">
                <td className="px-3 py-2 font-mono text-xs">{row.store_code}</td>
                <td className="px-3 py-2 font-medium">{row.store_name}</td>
                <td className="px-3 py-2">{row.city}</td>
                <td className="px-3 py-2">{row.region}</td>
                <td className="px-3 py-2 text-xs text-body">{row.device_count}</td>
                <td className="px-3 py-2 text-xs">
                  {row.has_primary
                    ? <span className="text-green-800">Assigned</span>
                    : <span className="text-amber-700">None</span>}
                </td>
                <td className="px-3 py-2"><StatusBadge status={row.status} /></td>
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
