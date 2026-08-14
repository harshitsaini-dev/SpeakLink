/**
 * Every Receiver Device, across every Store this account may see.
 *
 * The per-Store page (/stores/:storeId/devices) stays where it is: enrolment,
 * credential rotation and promotion are all things you do while looking at one
 * Store. This page answers the other question - "where is that Device?" - which
 * cannot be asked one Store at a time when there are dozens.
 *
 * ARCHIVED AND DELETED ARE NOT SHADES OF THE SAME THING and this page never
 * lets them look alike. Archived is reversible and shows a Restore path.
 * Deleted is a tombstone kept only so credential and broadcast history stay
 * readable; it is red, it says so in words, and there is no Restore anywhere
 * near it.
 */
import React from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { RefreshCw, Star, Trash2, ExternalLink } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import {
  FilterBar, SearchInput, FilterSelect, ListState, Pager, SortableTh,
  ExportButton, DestructiveModal,
} from "@/components/AdminFilters";
import StoreKitDownload from "@/components/StoreKitDownload";

const LIFECYCLE_STYLE = {
  active: "bg-emerald-100 text-emerald-800 border-emerald-200",
  archived: "bg-amber-100 text-amber-900 border-amber-300",
  deleted: "bg-red-100 text-red-800 border-red-300",
};

const LIFECYCLE_LABEL = {
  active: "Active",
  archived: "Archived",
  deleted: "Permanently deleted",
};

const shortId = (id) =>
  typeof id === "string" && id.length > 8 ? `${id.slice(0, 8)}…` : id;

export default function ReceiverDeviceFleet() {
  const { can } = useAuth();
  const list = useAdminList("/receiver-devices/search", {
    sort: "", dir: "asc",
    q: "", region: "", city: "", store_id: "", status: "", is_primary: "",
    // One control, one source of truth. include_deleted is never sent: this
    // screen is operational, and a permanently deleted Device is not.
    lifecycle: "all_current",
  });
  const [options, setOptions] = React.useState({ regions: [], cities: [], stores: [] });
  const [purging, setPurging] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [purgeError, setPurgeError] = React.useState("");

  React.useEffect(() => {
    api.get("/receivers/filter-options")
      .then(({ data }) => setOptions({ regions: data.regions || [], cities: data.cities || [], stores: data.stores || [] }))
      .catch(() => { /* the list's own error state already reports a failure */ });
  }, []);

  return (
    <div className="space-y-4" data-testid="device-fleet-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Receiver Devices</h1>
          <p className="text-sm text-slate-500">
            Every enrolled Device across the Stores this account may see. Open a Store to
            enrol, rotate or promote.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ExportButton dataset="receiver-devices" list={list}
                        testId="fleet-export" />
          <button data-testid="fleet-refresh-btn" onClick={list.reload}
                className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
          <RefreshCw size={14} /> Refresh
        </button>
        </div>
      </div>

      {/* The kit lives on this page because this is where somebody stands when
          they are dealing with a Store's software - enrolling one, repairing
          one, or looking at why one is offline. */}
      <StoreKitDownload />

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q} onChange={(v) => list.setFilter("q", v)}
                     placeholder="Device name, id, or Store…" testId="fleet-search" />
        <FilterSelect label="Zone" testId="fleet-zone" allLabel="All Zones"
                      value={list.filters.region} options={options.regions}
                      onChange={(v) => list.setFilter("region", v)} />
        <FilterSelect label="City" testId="fleet-city" allLabel="All Cities"
                      value={list.filters.city} options={options.cities}
                      onChange={(v) => list.setFilter("city", v)} />
        <FilterSelect label="Store" testId="fleet-store" allLabel="All Stores"
                      value={list.filters.store_id}
                      options={options.stores.map((s) => ({
                        value: String(s.id), label: `${s.store_name} (${s.store_code})` }))}
                      onChange={(v) => list.setFilter("store_id", v)} />
        <FilterSelect label="Status" testId="fleet-status" allLabel="Any status"
                      value={list.filters.status}
                      options={[{ value: "active", label: "Active" },
                                { value: "disabled", label: "Disabled" },
                                { value: "retired", label: "Retired" }]}
                      onChange={(v) => list.setFilter("status", v)} />
        <FilterSelect label="Primary" testId="fleet-primary" allLabel="Any"
                      value={list.filters.is_primary}
                      options={[{ value: "true", label: "Primary only" },
                                { value: "false", label: "Standby only" }]}
                      onChange={(v) => list.setFilter("is_primary", v)} />
        {/* ONE control. There used to be two - a Lifecycle dropdown and an
            Include dropdown - both deciding the same thing, and the Include
            flags only ever latched ON. Choosing "Permanently deleted" and
            then "Active only" therefore showed both, because the deleted
            latch was never cleared.

            "Permanently deleted" is gone entirely: a deleted Device is
            operationally gone, and its history is in the deletion-event
            records. There is no Restore, so offering the state here would
            only invite one. */}
        <FilterSelect label="Lifecycle" testId="fleet-lifecycle" allLabel={null}
                      value={list.filters.lifecycle}
                      options={[{ value: "all_current", label: "All Current" },
                                { value: "active", label: "Active" },
                                { value: "archived", label: "Archived" }]}
                      onChange={(v) => list.setFilter("lifecycle", v || "all_current")} />
      </FilterBar>

      {purgeError && !purging && (
        <div role="alert" data-testid="fleet-action-error"
             className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
          {purgeError}
        </div>
      )}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Receiver Devices across every visible Store</caption>
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <SortableTh column="display_name" label="Device" list={list} />
              <SortableTh column="public_id" label="Identifier" list={list} />
              <SortableTh column="store_name" label="Store" list={list} />
              <SortableTh column="city" label="City" list={list} />
              <SortableTh column="region" label="Zone" list={list} />
              <SortableTh column="role" label="Role" list={list} />
              <SortableTh column="status" label="Status" list={list} />
              <SortableTh column="lifecycle" label="Lifecycle" list={list} />
              <th scope="col" className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={9} onRetry={list.reload}
                       emptyText="No Receiver Devices match these filters." />
            {!list.loading && !list.error && list.items.map((device) => {
              const deleted = device.lifecycle === "deleted";
              return (
                <tr key={device.public_id} data-testid={`fleet-row-${device.public_id}`}
                    className={`border-b border-slate-100 even:bg-slate-50/50 ${
                      deleted ? "bg-red-50/40" : device.lifecycle === "archived" ? "opacity-75" : ""}`}>
                  <td className="px-3 py-2 font-medium">{device.display_name}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-500"
                      title={device.public_id}>{shortId(device.public_id)}</td>
                  <td className="px-3 py-2">
                    <Link to={`/stores/${device.store_id}/devices`}
                          data-testid={`fleet-open-store-${device.public_id}`}
                          className="inline-flex items-center gap-1 text-blue-800 hover:underline">
                      {device.store_code} <ExternalLink size={11} />
                    </Link>
                    <div className="text-xs text-slate-500">{device.store_name}</div>
                  </td>
                  <td className="px-3 py-2 text-xs">{device.city}</td>
                  <td className="px-3 py-2 text-xs">{device.region}</td>
                  <td className="px-3 py-2 text-xs">
                    {device.is_primary
                      ? <span className="inline-flex items-center gap-1 text-blue-800 font-semibold">
                          <Star size={11} /> Primary
                        </span>
                      : <span className="text-slate-500">Standby</span>}
                  </td>
                  <td className="px-3 py-2 text-xs uppercase tracking-wide text-slate-600"
                      data-testid={`fleet-status-${device.public_id}`}>
                    {device.status}
                  </td>
                  <td className="px-3 py-2">
                    <span data-testid={`fleet-lifecycle-${device.public_id}`}
                          className={`inline-block rounded border px-2 py-0.5 text-[11px] font-medium ${
                            LIFECYCLE_STYLE[device.lifecycle] || LIFECYCLE_STYLE.active}`}>
                      {LIFECYCLE_LABEL[device.lifecycle] || device.lifecycle}
                    </span>
                    {deleted && (
                      <div className="mt-0.5 text-[11px] text-slate-500"
                           data-testid={`fleet-deleted-note-${device.public_id}`}>
                        Kept only so history stays readable. This cannot be restored.
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    {!deleted && can("devices.delete_permanently") && (
                      <button type="button" data-testid={`fleet-purge-${device.public_id}`}
                              onClick={() => { setPurgeError(""); setPurging(device); }}
                              className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-300 text-red-700 rounded hover:bg-red-50">
                        <Trash2 size={12} /> Delete Permanently
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <Pager page={list.page} pages={list.pages} total={list.total}
               hasMore={list.hasMore} onPage={list.setPage} />
      </div>

      {purging && (
        <DestructiveModal
          testIdPrefix="fleet-purge"
          title={`Permanently delete ${purging.display_name}?`}
          count={1} countNoun="Device"
          /* The same word Store, User, history and log deletion ask for. A
             36-character id is not extra safety - it is long enough that people
             paste it, and a paste is not a decision. The Device is named in the
             title and the acknowledgement is still separate. */
          confirmWord="DELETE"
          warning={`Its credentials are revoked immediately and it can never reconnect or be restored. If it is this Store's primary, ${purging.store_code} will have no primary until another Device is promoted. Its enrolment and broadcast history stay readable.`}
          busy={busy} error={purgeError}
          onCancel={() => { setPurging(null); setPurgeError(""); }}
          onConfirm={async ({ typed, acknowledged }) => {
            setBusy(true);
            setPurgeError("");
            try {
              await api.post(`/receiver-devices/${purging.public_id}/delete-permanently`,
                             { confirm: typed, acknowledged });
              setPurging(null);
              await list.reload();
            } catch (failure) {
              setPurgeError(failure?.response?.data?.detail
                            || "That Device could not be deleted.");
            } finally {
              setBusy(false);
            }
          }}
        />
      )}
    </div>
  );
}
