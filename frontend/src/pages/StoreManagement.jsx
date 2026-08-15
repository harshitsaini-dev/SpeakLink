import React from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { Plus, RefreshCw, KeyRound, MonitorSmartphone, Pencil, Power, Archive, ArchiveRestore, Trash2, ShieldAlert } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager, SortableTh, ExportButton } from "@/components/AdminFilters";

// Archive and Permanent Delete are different things and must stay different.
//
// ARCHIVE retires a Store, keeps its row, and keeps its Store Code reserved,
// because it can be restored.
//
// PERMANENT DELETE removes the row and RELEASES the Store Code. History
// survives because each Broadcast Target, Receiver event and Device keeps a
// snapshot of the code, not because a hidden row is propping it up. A new
// Store that later takes the same code is a different Store with a different
// id, and inherits no Device, credential or history. See
// backend/store_permanent_delete.py.
//
// Restore is deliberately separate from Re-enable, and returns a Store to
// DISABLED rather than ACTIVE: un-retiring a Store is not a small decision, and
// Re-enable is a small button. Somebody has to look at its Devices first.
function lifecycleOf(store) {
  if (store.lifecycle_state) return store.lifecycle_state.toUpperCase();
  return store.is_active ? "ACTIVE" : "DISABLED";
}

function LifecycleBadge({ state }) {
  const styles = {
    ACTIVE: "bg-green-50 text-green-800 border-green-200",
    DISABLED: "bg-amber-50 text-amber-800 border-amber-200",
    ARCHIVED: "bg-surface-muted text-body border-line-strong",
    DELETED: "bg-red-50 text-red-700 border-red-200",
  };
  return (
    <span
      data-testid={`lifecycle-${state}`}
      className={`inline-flex px-2 py-0.5 rounded text-[11px] font-semibold uppercase tracking-wide border ${styles[state] || styles.DISABLED}`}
    >
      {state}
    </span>
  );
}

export default function StoreManagement() {
  const { can } = useAuth();
  // Server-side, like every other admin screen. The catalog is 44 Stores
  // today and is expected to grow; filtering it in React would mean the
  // narrowing an operator sees and the narrowing the backend enforces are
  // two different things, and a scoped account could be shown a total that
  // includes Stores it may not open.
  //
  // ONE lifecycle control, and it is the only thing deciding which Stores
  // appear. The previous pair of include_* switches could latch one on while
  // the other changed, which is how selecting a lifecycle left the previous
  // one still on screen.
  //
  // 'active' is the default: the first view is the estate that is actually
  // running, and Disabled or Archived are chosen deliberately. A permanently
  // deleted Store has no option at all - its history lives in the deletion
  // records, not in this list.
  const list = useAdminList("/stores/search", {
    sort: "", dir: "asc",
    q: "", region: "", city: "", lifecycle: "active",
  });
  const stores = list.items;
  const [options, setOptions] = React.useState({ regions: [], cities: [] });
  const [showAdd, setShowAdd] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState("");
  const [deleting, setDeleting] = React.useState(null);
  const [tombstoning, setTombstoning] = React.useState(null);

  const load = list.reload;

  React.useEffect(() => {
    // Scoped options from the server, not derived from the visible page: a
    // Zone this account may reach should be offered even when no Store from
    // it happens to be on screen.
    api.get("/stores/filter-options")
      .then(({ data }) => setOptions(data))
      .catch(() => { /* the list's own error state already reports a failure */ });
  }, []);

  const act = async (key, request) => {
    setBusy(key);
    setError("");
    try {
      await request();
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || "That action could not be completed.");
    } finally {
      setBusy("");
    }
  };

  const regen = async (id) => {
    if (!window.confirm("Regenerate this Store's legacy Receiver token?\n\nAny Receiver still using the shared token will stop connecting. The new value is never displayed.")) return;
    return act(`regen-${id}`, () => api.post(`/stores/${id}/regenerate-token`));
  };
  const disable = (store) => {
    if (!window.confirm(`Disable "${store.store_name}"?\n\nIts Receivers will stop connecting and it cannot be a broadcast target. Nothing is deleted and you can re-enable it at any time.`)) return;
    return act(`disable-${store.id}`, () => api.post(`/stores/${store.id}/disable`));
  };
  const enable = (store) => {
    if (!window.confirm(`Re-enable "${store.store_name}"?\n\nIt becomes a broadcast target again. No Receiver Device is promoted and no credential is created.`)) return;
    return act(`enable-${store.id}`, () => api.post(`/stores/${store.id}/enable`));
  };
  const archive = (store) => {
    if (!window.confirm(`Archive "${store.store_name}"?\n\nNothing is deleted: its Receiver Devices, broadcast sessions and event history all remain readable. The Store stops being a broadcast target and cannot be re-enabled from here - it must be restored first.`)) return;
    return act(`archive-${store.id}`, () => api.post(`/stores/${store.id}/archive`));
  };
  const restore = (store) => {
    if (!window.confirm(`Restore "${store.store_name}"?\n\nIt returns to DISABLED, not active. Review its Receiver Devices, then re-enable it explicitly.`)) return;
    return act(`restore-${store.id}`, () => api.post(`/stores/${store.id}/restore`));
  };

  // There is deliberately no "copy the Receiver URL" helper any more. It built
  // ${origin}/receiver?token=${receiver_token}, which put a long-lived Store
  // credential into a URL - and therefore into clipboards, chat messages,
  // browser history and any log that saw the link. A Receiver computer will
  // earn its own credential through one-time enrolment instead; see
  // docs/RECEIVER_ENROLMENT.md.
  return (
    <div className="space-y-4" data-testid="stores-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-strong tracking-tight">Store Management</h1>
          <p className="text-sm text-muted">Manage stores and rotate Receiver credentials. Credentials are never displayed or copied from this page.</p>
        </div>
        <div className="flex gap-2">
          <ExportButton dataset="stores" list={list} testId="stores-export" />
          <button data-testid="stores-refresh-btn" onClick={load} className="inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted"><RefreshCw size={14}/> Refresh</button>
          {can("stores.create") && (
            <button data-testid="add-store-btn" onClick={() => setShowAdd(true)} className="inline-flex items-center gap-1 px-3 py-2 bg-blue-700 hover:bg-blue-800 text-white rounded-md text-sm font-medium"><Plus size={16}/> Add Store</button>
          )}
        </div>
      </div>

      {error && <div data-testid="stores-error" role="alert" className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{error}</div>}

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q} onChange={(v) => list.setFilter("q", v)}
                     placeholder="Search Store code or name…" testId="stores-search" />
        <FilterSelect label="Zone" testId="stores-zone" allLabel="All Zones"
                      value={list.filters.region} options={options.regions}
                      onChange={(v) => list.setFilter("region", v)} />
        <FilterSelect label="City" testId="stores-city" allLabel="All Cities"
                      value={list.filters.city} options={options.cities}
                      onChange={(v) => list.setFilter("city", v)} />
        {/* Multi-value, like every other filter.
            I argued this one should stay single because a Store is in exactly
            one lifecycle state - which is true of a STORE and irrelevant to a
            FILTER. "Active and archived" is a perfectly ordinary question and
            answering it used to mean running the search twice.
            The bug the old exclusive behaviour was protecting against - a
            previous choice staying in effect invisibly - cannot happen with
            checkboxes, where what is chosen is on screen. */}
        <FilterSelect label="Lifecycle" testId="stores-lifecycle" allLabel="All Current"
                      value={list.filters.lifecycle}
                      // No explicit "All Current" option: the panel's clear
                      // entry already says it, and two controls with the same
                      // words doing the same thing is one of them being noise.
                      options={[{ value: "active", label: "Active" },
                                { value: "disabled", label: "Disabled" },
                                { value: "archived", label: "Archived" }]}
                      onChange={(v) => list.setFilter("lifecycle", v || "all_current")} />
      </FilterBar>

      <div className="glass rounded-xl overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-surface-muted text-left text-[11px] uppercase tracking-wider text-muted border-b border-line">
            <tr>
              <SortableTh column="store_code" label="Code" list={list} />
              <SortableTh column="store_name" label="Name" list={list} />
              <SortableTh column="city" label="City" list={list} />
              <SortableTh column="region" label="Zone" list={list} />
              <SortableTh column="type" label="Type" list={list} />
              <SortableTh column="lifecycle" label="Lifecycle" list={list} />
              {/* No Receiver status column. A Store row cannot honestly
                  report whether a Receiver is connected - that is live
                  WebSocket state and belongs to Receiver Status, which
                  derives it from the connection manager rather than from a
                  database column that goes stale the moment HQ stops. */}
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!stores.length} colSpan={7} onRetry={list.reload}
                       emptyText="No Stores match these filters. Clear them to see everything in this account's Scope." />
            {!list.loading && !list.error && stores.map((s) => {
              const state = lifecycleOf(s);
              const archived = state === "ARCHIVED";
              return (
                <tr key={s.id} data-testid={`store-mgmt-row-${s.store_code}`} className="border-b border-line even:bg-surface-alt">
                  <td className="px-3 py-2 font-mono text-xs">{s.store_code}</td>
                  <td className="px-3 py-2 font-medium">{s.store_name}</td>
                  <td className="px-3 py-2">{s.city}</td>
                  <td className="px-3 py-2">{s.region}</td>
                  <td className="px-3 py-2 text-xs text-body">{s.is_online_store ? "Online" : "Physical"}</td>
                  <td className="px-3 py-2"><LifecycleBadge state={state}/></td>
                  <td className="px-3 py-2 text-right space-x-1 whitespace-nowrap">
                    {/* The Store id, not a credential. Enrolment lives on that page. */}
                    <Link data-testid={`devices-${s.store_code}`} to={`/stores/${s.id}/devices`} title="Receiver Devices" aria-label={`Receiver Devices for ${s.store_name}`} className="row-action row-action-info"><MonitorSmartphone size={12}/></Link>
                    {!archived && can("stores.update") && (
                      <button data-testid={`edit-store-${s.store_code}`} onClick={() => setEditing(s)} title="Edit Store" aria-label={`Edit ${s.store_name}`} className="row-action"><Pencil size={12}/></button>
                    )}
                    {!archived && can("stores.update") && (
                      <button data-testid={`regen-token-${s.store_code}`} onClick={() => regen(s.id)} disabled={busy === `regen-${s.id}`} title="Regenerate legacy token" aria-label={`Regenerate the legacy Receiver token for ${s.store_name}`} className="row-action row-action-caution"><KeyRound size={12}/></button>
                    )}
                    {state === "ACTIVE" && can("stores.archive") && (
                      <button data-testid={`disable-store-${s.store_code}`} onClick={() => disable(s)} disabled={busy === `disable-${s.id}`} title="Disable" aria-label={`Disable ${s.store_name}`} className="row-action row-action-caution"><Power size={12}/></button>
                    )}
                    {state === "DISABLED" && can("stores.update") && (
                      <button data-testid={`enable-store-${s.store_code}`} onClick={() => enable(s)} disabled={busy === `enable-${s.id}`} title="Re-enable" aria-label={`Re-enable ${s.store_name}`} className="row-action row-action-ok"><Power size={12}/></button>
                    )}
                    {!archived && can("stores.archive") && (
                      <button data-testid={`archive-store-${s.store_code}`} onClick={() => archive(s)} disabled={busy === `archive-${s.id}`} title="Archive" aria-label={`Archive ${s.store_name}`} className="row-action row-action-danger"><Archive size={12}/></button>
                    )}
                    {archived && can("stores.update") && (
                      <button data-testid={`restore-store-${s.store_code}`} onClick={() => restore(s)} disabled={busy === `restore-${s.id}`} title="Restore to disabled" aria-label={`Restore ${s.store_name}`} className="row-action row-action-info"><ArchiveRestore size={12}/></button>
                    )}
                    {/* Offered on every Store this account may archive, because
                        whether it is actually allowed is decided by the
                        dependency summary the dialog fetches - and again by
                        the server inside the deleting transaction. Hiding it
                        on a guess would hide it from the one never-used Store
                        it exists for. */}
                    {can("stores.archive") && (
                      <button data-testid={`delete-store-${s.store_code}`} onClick={() => setDeleting(s)} title="Delete permanently (if unused)" aria-label={`Delete ${s.store_name} permanently if unused`} className="row-action row-action-destructive"><Trash2 size={12}/></button>
                    )}
                    {/* Distinct from the button above: this one works even
                        when the Store HAS history. SUPER ADMIN only - see
                        stores.delete_permanently in the permission catalog. */}
                    {can("stores.delete_permanently") && (
                      <button data-testid={`tombstone-store-${s.store_code}`} onClick={() => setTombstoning(s)} title="Permanently delete (even with history)" aria-label={`Permanently delete ${s.store_name}, even with history`} className="row-action row-action-destructive"><ShieldAlert size={12}/></button>
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

      {deleting && (
        <DeleteStoreModal
          store={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => { setDeleting(null); load(); }}
        />
      )}

      {tombstoning && (
        <TombstoneStoreModal
          store={tombstoning}
          onClose={() => setTombstoning(null)}
          onDeleted={() => { setTombstoning(null); load(); }}
        />
      )}

      {showAdd && <AddStoreModal onClose={() => setShowAdd(false)} onCreated={() => { setShowAdd(false); load(); }} />}
      {editing && <EditStoreModal store={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}

/**
 * Permanent Store deletion, which is refused far more often than it succeeds.
 *
 * A Store owns Receiver Devices, broadcast targets, sessions and Receiver
 * events. Removing the row would orphan that history or cascade it away, and
 * both destroy the only record of what was announced where. So the dialog asks
 * the backend what still refers to this Store BEFORE offering the button, and
 * points at Archive when anything does.
 *
 * The typed short code is the confirmation that cannot be clicked through by
 * muscle memory. The server checks it again inside the deleting transaction,
 * because a dialog is not a control.
 */
function DeleteStoreModal({ store, onClose, onDeleted }) {
  const [summary, setSummary] = React.useState(null);
  const [failed, setFailed] = React.useState("");
  const [typed, setTyped] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    api.get(`/stores/${store.id}/dependencies`)
      .then((r) => { if (!cancelled) setSummary(r.data); })
      .catch(() => { if (!cancelled) setFailed("The dependency check could not be run."); });
    return () => { cancelled = true; };
  }, [store.id]);

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await api.delete(`/stores/${store.id}/permanently`, { params: { confirm: typed } });
      onDeleted();
    } catch (e) {
      setError(e?.response?.data?.detail || "That Store could not be deleted.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 scrim flex items-center justify-center p-4"
         data-testid="delete-store-modal">
      <div className="glass w-full max-w-md p-5 space-y-3">
        <h3 className="font-semibold text-red-900">
          Delete {store.store_code} permanently
        </h3>

        {summary === null && !failed && (
          <p className="text-sm text-body" data-testid="delete-store-checking">
            Checking what still refers to this Store…
          </p>
        )}

        {failed && (
          <p className="text-sm text-red-800" data-testid="delete-store-check-failed">
            {failed} Nothing has been deleted. Archive this Store instead.
          </p>
        )}

        {summary && (
          <>
            <p className="text-sm text-body" data-testid="delete-store-summary">
              {summary.explanation}
            </p>
            {!summary.deletable && (
              <p className="text-sm text-red-800" data-testid="delete-store-blocked">
                This Store contains operational history or Receiver Devices.
                Archive it instead — the history stays readable and the Store can
                be restored.
              </p>
            )}
            {summary.deletable && (
              <>
                <p className="text-sm text-red-800">
                  This cannot be undone. Type <strong>{store.store_code}</strong> to confirm.
                </p>
                <input
                  className="w-full rounded border border-line-strong px-3 py-2 text-sm"
                  value={typed} onChange={(e) => setTyped(e.target.value)}
                  autoComplete="off" data-testid="delete-store-confirm-input"
                />
              </>
            )}
          </>
        )}

        {error && (
          <div role="alert" data-testid="delete-store-error"
               className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 pt-1">
          <button type="button" data-testid="delete-store-cancel" onClick={onClose}
                  className="flex-1 px-4 py-2 border border-line-strong rounded-md text-sm">
            Cancel
          </button>
          <button
            type="button" data-testid="delete-store-confirm"
            disabled={busy || !summary || !summary.deletable || typed !== store.store_code}
            onClick={remove}
            className="flex-1 px-4 py-2 bg-red-700 text-white rounded-md text-sm disabled:opacity-40"
          >
            Delete permanently
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Permanently delete a Store even when it HAS history - a tombstone, not a
 * row removal. Unlike DeleteStoreModal above, this is never disabled by the
 * dependency count; instead it shows exactly what history exists, requires
 * the exact Store code typed AND a separate "cannot be restored"
 * acknowledgement, and only a SUPER ADMIN ever sees the button that opens it.
 */
function TombstoneStoreModal({ store, onClose, onDeleted }) {
  const [summary, setSummary] = React.useState(null);
  const [typed, setTyped] = React.useState("");
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    api.get(`/stores/${store.id}/dependencies`)
      .then((r) => { if (!cancelled) setSummary(r.data); })
      .catch(() => { if (!cancelled) setSummary({ counts: {}, total: 0 }); });
    return () => { cancelled = true; };
  }, [store.id]);

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await api.post(`/stores/${store.id}/delete-permanently`, { confirm: typed, acknowledged });
      onDeleted();
    } catch (e) {
      setError(e?.response?.data?.detail || "That Store could not be permanently deleted.");
    } finally {
      setBusy(false);
    }
  };

  const counts = summary?.counts || {};
  const historyLines = [
    counts.broadcast_targets ? `${counts.broadcast_targets} Broadcast Targets` : null,
    counts.receiver_devices ? `${counts.receiver_devices} Receiver Device${counts.receiver_devices === 1 ? "" : "s"}` : null,
    counts.receiver_enrollment_codes ? `${counts.receiver_enrollment_codes} enrollment code${counts.receiver_enrollment_codes === 1 ? "" : "s"}` : null,
    counts.receiver_events ? `${counts.receiver_events} Receiver events` : null,
  ].filter(Boolean);

  return (
    <div className="fixed inset-0 z-50 scrim flex items-center justify-center p-4"
         data-testid="tombstone-store-modal">
      <div className="glass w-full max-w-md p-5 space-y-3">
        <h3 className="font-semibold text-red-900">
          Permanently delete {store.store_code}?
        </h3>

        {summary === null && (
          <p className="text-sm text-body" data-testid="tombstone-checking">
            Checking this Store's history…
          </p>
        )}

        {summary !== null && (
          <>
            {historyLines.length > 0 ? (
              <div className="text-sm text-body space-y-1" data-testid="tombstone-history-summary">
                <p>This Store has:</p>
                <ul className="list-disc list-inside">
                  {historyLines.map((line) => <li key={line}>{line}</li>)}
                </ul>
              </div>
            ) : (
              <p className="text-sm text-body" data-testid="tombstone-no-history">
                Nothing currently refers to this Store.
              </p>
            )}
            <p className="text-sm text-red-800" data-testid="tombstone-consequences">
              This permanently removes the Store and cannot be undone. Its Receiver
              Devices are retired and their credentials revoked, so nothing that
              authenticated as this Store can connect again. Historical audit records
              remain readable. The Store Code becomes available for a new Store — which
              will be a different Store, with a different ID and none of this Store's
              Devices or history.
            </p>

            <label htmlFor="tombstone-confirm-input" className="block text-xs font-bold uppercase tracking-widest text-muted">
              Type the Store code to confirm
            </label>
            <input
              id="tombstone-confirm-input"
              className="w-full rounded border border-line-strong px-3 py-2 text-sm font-mono"
              value={typed} onChange={(e) => setTyped(e.target.value)}
              placeholder={store.store_code}
              autoComplete="off" data-testid="tombstone-confirm-input"
            />

            <label className="flex items-start gap-2 text-sm pt-1">
              <input type="checkbox" data-testid="tombstone-acknowledge-checkbox"
                     checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
              <span>I understand this Store cannot be restored, and that its Store
                    Code becomes available for a completely new Store.</span>
            </label>
          </>
        )}

        {error && (
          <div role="alert" data-testid="tombstone-error"
               className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 pt-1">
          <button type="button" data-testid="tombstone-cancel" onClick={onClose}
                  className="flex-1 px-4 py-2 border border-line-strong rounded-md text-sm">
            Cancel
          </button>
          <button
            type="button" data-testid="tombstone-confirm"
            disabled={busy || summary === null || typed !== store.store_code || !acknowledged}
            onClick={remove}
            className="flex-1 px-4 py-2 bg-red-700 text-white rounded-md text-sm disabled:opacity-40"
          >
            Permanently Delete Store
          </button>
        </div>
      </div>
    </div>
  );
}

function EditStoreModal({ store, onClose, onSaved }) {
  // Only the details. A Store's state is a lifecycle transition with rules, and
  // its credentials are not editable from anywhere.
  const [f, setF] = React.useState({
    store_code: store.store_code, store_name: store.store_name,
    city: store.city, region: store.region, is_online_store: !!store.is_online_store,
  });
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api.put(`/stores/${store.id}`, f); onSaved(); }
    catch (e2) { setErr(e2?.response?.data?.detail || "That Store could not be saved."); }
    finally { setBusy(false); }
  };
  return (
    <div className="fixed inset-0 z-50 scrim flex items-center justify-center p-4" data-testid="edit-store-modal">
      <form onSubmit={submit} className="glass shadow-2xl w-full max-w-md p-6 space-y-3">
        <h3 className="text-lg font-semibold">Edit Store</h3>
        <p className="text-xs text-muted">
          Details only. Enabling, disabling and archiving have their own actions, and
          Receiver credentials are never editable here.
        </p>
        {["store_code", "store_name", "city", "region"].map((k) => (
          <div key={k}>
            <label htmlFor={`edit-${k}`} className="block text-xs font-bold uppercase tracking-widest text-muted mb-1">{(k === "region" ? "zone" : k).replace("_", " ")}</label>
            <input required id={`edit-${k}`} data-testid={`edit-${k.replace("_", "-")}-input`} value={f[k]}
                   onChange={(e) => setF({ ...f, [k]: e.target.value })}
                   className="w-full px-3 py-2 border border-line-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          </div>
        ))}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" data-testid="edit-online-checkbox" checked={f.is_online_store} onChange={(e) => setF({ ...f, is_online_store: e.target.checked })}/>
          Online store
        </label>
        {err && <div data-testid="edit-store-error" role="alert" className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{err}</div>}
        <div className="flex gap-2 pt-2">
          <button type="button" data-testid="edit-store-cancel" onClick={onClose} className="flex-1 px-4 py-2 border border-line-strong rounded-md text-sm">Cancel</button>
          <button type="submit" data-testid="edit-store-submit-btn" disabled={busy} className="flex-1 px-4 py-2 bg-blue-700 text-white rounded-md text-sm font-medium">{busy ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </div>
  );
}

function AddStoreModal({ onClose, onCreated }) {
  const [f, setF] = React.useState({ store_code: "", store_name: "", city: "", region: "", is_online_store: false });
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api.post("/stores", f); onCreated(); }
    catch (e2) { setErr(e2?.response?.data?.detail || e2.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="fixed inset-0 z-50 scrim flex items-center justify-center p-4" data-testid="add-store-modal">
      <form onSubmit={submit} className="glass shadow-2xl w-full max-w-md p-6 space-y-3">
        <h3 className="text-lg font-semibold">Add Store</h3>
        {["store_code", "store_name", "city", "region"].map((k) => (
          <div key={k}>
            <label className="block text-xs font-bold uppercase tracking-widest text-muted mb-1">{(k === "region" ? "zone" : k).replace("_", " ")}</label>
            <input required data-testid={`add-${k.replace("_", "-")}-input`} value={f[k]}
                   onChange={(e) => setF({ ...f, [k]: e.target.value })}
                   className="w-full px-3 py-2 border border-line-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          </div>
        ))}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" data-testid="add-online-checkbox" checked={f.is_online_store} onChange={(e) => setF({ ...f, is_online_store: e.target.checked })}/>
          Online store
        </label>
        {err && <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{err}</div>}
        <div className="flex gap-2 pt-2">
          <button type="button" onClick={onClose} className="flex-1 px-4 py-2 border border-line-strong rounded-md text-sm">Cancel</button>
          <button type="submit" data-testid="add-store-submit-btn" disabled={busy} className="flex-1 px-4 py-2 bg-blue-700 text-white rounded-md text-sm font-medium">{busy ? "Saving…" : "Create"}</button>
        </div>
      </form>
    </div>
  );
}
