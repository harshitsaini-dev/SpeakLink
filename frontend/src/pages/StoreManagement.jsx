import React from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { Plus, RefreshCw, KeyRound, MonitorSmartphone, Pencil, Power, Archive, ArchiveRestore, Trash2 } from "lucide-react";
import StatusBadge from "@/components/StatusBadge";

// A Store is never deleted. It owns Receiver Devices, broadcast sessions,
// targets and events; removing the row would destroy the only record of what
// was announced where. "Archive" retires it and keeps all of that readable.
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
    ARCHIVED: "bg-slate-100 text-slate-600 border-slate-300",
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
  const [stores, setStores] = React.useState([]);
  const [showAdd, setShowAdd] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState("");
  const [deleting, setDeleting] = React.useState(null);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const { data } = await api.get("/stores", {
        params: { include_inactive: true, include_archived: true },
      });
      setStores(Array.isArray(data) ? data : []);
    } catch (e) {
      setError("Stores could not be loaded. Try again.");
    } finally {
      setLoading(false);
    }
  };
  React.useEffect(() => { load(); }, []);

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
  // RECEIVER_ENROLMENT.md.
  return (
    <div className="space-y-4" data-testid="stores-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Store Management</h1>
          <p className="text-sm text-slate-500">Manage stores and rotate Receiver credentials. Credentials are never displayed or copied from this page.</p>
        </div>
        <div className="flex gap-2">
          <button data-testid="stores-refresh-btn" onClick={load} className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50"><RefreshCw size={14}/> Refresh</button>
          {can("stores.create") && (
            <button data-testid="add-store-btn" onClick={() => setShowAdd(true)} className="inline-flex items-center gap-1 px-3 py-2 bg-blue-700 hover:bg-blue-800 text-white rounded-md text-sm font-medium"><Plus size={16}/> Add Store</button>
          )}
        </div>
      </div>

      {error && <div data-testid="stores-error" role="alert" className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{error}</div>}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2">Code</th>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">City</th>
              <th className="px-3 py-2">Zone</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Lifecycle</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={8} data-testid="stores-loading" className="px-3 py-6 text-center text-slate-500">Loading…</td></tr>
            )}
            {!loading && stores.length === 0 && (
              <tr><td colSpan={8} data-testid="stores-empty" className="px-3 py-6 text-center text-slate-500">No Stores yet.</td></tr>
            )}
            {stores.map((s) => {
              const state = lifecycleOf(s);
              const archived = state === "ARCHIVED";
              return (
                <tr key={s.id} data-testid={`store-mgmt-row-${s.store_code}`} className="border-b border-slate-100 even:bg-slate-50/50">
                  <td className="px-3 py-2 font-mono text-xs">{s.store_code}</td>
                  <td className="px-3 py-2 font-medium">{s.store_name}</td>
                  <td className="px-3 py-2">{s.city}</td>
                  <td className="px-3 py-2">{s.region}</td>
                  <td className="px-3 py-2 text-xs text-slate-600">{s.is_online_store ? "Online" : "Physical"}</td>
                  <td className="px-3 py-2"><LifecycleBadge state={state}/></td>
                  <td className="px-3 py-2">{archived ? <span className="text-xs text-slate-400">—</span> : <StatusBadge status={s.status}/>}</td>
                  <td className="px-3 py-2 text-right space-x-1 whitespace-nowrap">
                    {/* The Store id, not a credential. Enrolment lives on that page. */}
                    <Link data-testid={`devices-${s.store_code}`} to={`/stores/${s.id}/devices`} title="Receiver Devices" aria-label={`Receiver Devices for ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-blue-50"><MonitorSmartphone size={12}/></Link>
                    {!archived && can("stores.update") && (
                      <button data-testid={`edit-store-${s.store_code}`} onClick={() => setEditing(s)} title="Edit Store" aria-label={`Edit ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-slate-50"><Pencil size={12}/></button>
                    )}
                    {!archived && can("stores.update") && (
                      <button data-testid={`regen-token-${s.store_code}`} onClick={() => regen(s.id)} disabled={busy === `regen-${s.id}`} title="Regenerate legacy token" aria-label={`Regenerate the legacy Receiver token for ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-amber-50 disabled:opacity-50"><KeyRound size={12}/></button>
                    )}
                    {state === "ACTIVE" && can("stores.archive") && (
                      <button data-testid={`disable-store-${s.store_code}`} onClick={() => disable(s)} disabled={busy === `disable-${s.id}`} title="Disable" aria-label={`Disable ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-amber-200 text-amber-800 rounded hover:bg-amber-50 disabled:opacity-50"><Power size={12}/></button>
                    )}
                    {state === "DISABLED" && can("stores.update") && (
                      <button data-testid={`enable-store-${s.store_code}`} onClick={() => enable(s)} disabled={busy === `enable-${s.id}`} title="Re-enable" aria-label={`Re-enable ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-green-200 text-green-800 rounded hover:bg-green-50 disabled:opacity-50"><Power size={12}/></button>
                    )}
                    {!archived && can("stores.archive") && (
                      <button data-testid={`archive-store-${s.store_code}`} onClick={() => archive(s)} disabled={busy === `archive-${s.id}`} title="Archive" aria-label={`Archive ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-200 text-red-700 rounded hover:bg-red-50 disabled:opacity-50"><Archive size={12}/></button>
                    )}
                    {archived && can("stores.update") && (
                      <button data-testid={`restore-store-${s.store_code}`} onClick={() => restore(s)} disabled={busy === `restore-${s.id}`} title="Restore to disabled" aria-label={`Restore ${s.store_name}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-blue-200 text-blue-800 rounded hover:bg-blue-50 disabled:opacity-50"><ArchiveRestore size={12}/></button>
                    )}
                    {/* Offered on every Store this account may archive, because
                        whether it is actually allowed is decided by the
                        dependency summary the dialog fetches - and again by
                        the server inside the deleting transaction. Hiding it
                        on a guess would hide it from the one never-used Store
                        it exists for. */}
                    {can("stores.archive") && (
                      <button data-testid={`delete-store-${s.store_code}`} onClick={() => setDeleting(s)} title="Delete permanently" aria-label={`Delete ${s.store_name} permanently`} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-300 text-red-700 rounded hover:bg-red-50"><Trash2 size={12}/></button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {deleting && (
        <DeleteStoreModal
          store={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => { setDeleting(null); load(); }}
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
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
         data-testid="delete-store-modal">
      <div className="bg-white rounded-lg w-full max-w-md p-5 space-y-3">
        <h3 className="font-semibold text-red-900">
          Delete {store.store_code} permanently
        </h3>

        {summary === null && !failed && (
          <p className="text-sm text-slate-600" data-testid="delete-store-checking">
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
            <p className="text-sm text-slate-700" data-testid="delete-store-summary">
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
                  className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
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
                  className="flex-1 px-4 py-2 border border-slate-300 rounded-md text-sm">
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
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" data-testid="edit-store-modal">
      <form onSubmit={submit} className="bg-white rounded-md shadow-xl w-full max-w-md p-6 space-y-3">
        <h3 className="text-lg font-semibold">Edit Store</h3>
        <p className="text-xs text-slate-500">
          Details only. Enabling, disabling and archiving have their own actions, and
          Receiver credentials are never editable here.
        </p>
        {["store_code", "store_name", "city", "region"].map((k) => (
          <div key={k}>
            <label htmlFor={`edit-${k}`} className="block text-xs font-bold uppercase tracking-widest text-slate-500 mb-1">{(k === "region" ? "zone" : k).replace("_", " ")}</label>
            <input required id={`edit-${k}`} data-testid={`edit-${k.replace("_", "-")}-input`} value={f[k]}
                   onChange={(e) => setF({ ...f, [k]: e.target.value })}
                   className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          </div>
        ))}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" data-testid="edit-online-checkbox" checked={f.is_online_store} onChange={(e) => setF({ ...f, is_online_store: e.target.checked })}/>
          Online store
        </label>
        {err && <div data-testid="edit-store-error" role="alert" className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{err}</div>}
        <div className="flex gap-2 pt-2">
          <button type="button" data-testid="edit-store-cancel" onClick={onClose} className="flex-1 px-4 py-2 border border-slate-300 rounded-md text-sm">Cancel</button>
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
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" data-testid="add-store-modal">
      <form onSubmit={submit} className="bg-white rounded-md shadow-xl w-full max-w-md p-6 space-y-3">
        <h3 className="text-lg font-semibold">Add Store</h3>
        {["store_code", "store_name", "city", "region"].map((k) => (
          <div key={k}>
            <label className="block text-xs font-bold uppercase tracking-widest text-slate-500 mb-1">{(k === "region" ? "zone" : k).replace("_", " ")}</label>
            <input required data-testid={`add-${k.replace("_", "-")}-input`} value={f[k]}
                   onChange={(e) => setF({ ...f, [k]: e.target.value })}
                   className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          </div>
        ))}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" data-testid="add-online-checkbox" checked={f.is_online_store} onChange={(e) => setF({ ...f, is_online_store: e.target.checked })}/>
          Online store
        </label>
        {err && <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{err}</div>}
        <div className="flex gap-2 pt-2">
          <button type="button" onClick={onClose} className="flex-1 px-4 py-2 border border-slate-300 rounded-md text-sm">Cancel</button>
          <button type="submit" data-testid="add-store-submit-btn" disabled={busy} className="flex-1 px-4 py-2 bg-blue-700 text-white rounded-md text-sm font-medium">{busy ? "Saving…" : "Create"}</button>
        </div>
      </form>
    </div>
  );
}
