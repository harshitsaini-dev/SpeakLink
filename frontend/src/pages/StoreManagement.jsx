import React from "react";
import { api } from "@/lib/api";
import { Plus, RefreshCw, KeyRound, Trash2, Copy, Check } from "lucide-react";
import StatusBadge from "@/components/StatusBadge";

export default function StoreManagement() {
  const [stores, setStores] = React.useState([]);
  const [showAdd, setShowAdd] = React.useState(false);
  const [copiedId, setCopiedId] = React.useState(null);
  const [error, setError] = React.useState("");

  const load = async () => {
    const { data } = await api.get("/stores", { params: { include_inactive: true } });
    setStores(data);
  };
  React.useEffect(() => { load(); }, []);

  const regen = async (id) => {
    if (!window.confirm("Regenerate token? The old receiver URL will stop working.")) return;
    try { await api.post(`/stores/${id}/regenerate-token`); load(); } catch (e) { setError(e.message); }
  };
  const disable = async (id) => {
    if (!window.confirm("Disable this store? Its receiver will no longer connect.")) return;
    try { await api.delete(`/stores/${id}`); load(); } catch (e) { setError(e.message); }
  };

  const receiverUrl = (token) => `${window.location.origin}/receiver?token=${token}`;
  const copy = (id, url) => {
    navigator.clipboard.writeText(url).then(() => {
      setCopiedId(id); setTimeout(() => setCopiedId(null), 1500);
    });
  };

  return (
    <div className="space-y-4" data-testid="stores-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Store Management</h1>
          <p className="text-sm text-slate-500">Manage store receivers, regenerate tokens, and copy kiosk URLs.</p>
        </div>
        <div className="flex gap-2">
          <button data-testid="stores-refresh-btn" onClick={load} className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50"><RefreshCw size={14}/> Refresh</button>
          <button data-testid="add-store-btn" onClick={() => setShowAdd(true)} className="inline-flex items-center gap-1 px-3 py-2 bg-blue-700 hover:bg-blue-800 text-white rounded-md text-sm font-medium"><Plus size={16}/> Add Store</button>
        </div>
      </div>

      {error && <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{error}</div>}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2">Code</th>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">City</th>
              <th className="px-3 py-2">Zone</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Receiver URL</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {stores.map((s) => {
              const url = receiverUrl(s.receiver_token);
              return (
                <tr key={s.id} data-testid={`store-mgmt-row-${s.store_code}`} className="border-b border-slate-100 even:bg-slate-50/50">
                  <td className="px-3 py-2 font-mono text-xs">{s.store_code}</td>
                  <td className="px-3 py-2 font-medium">{s.store_name} {!s.is_active && <span className="text-[10px] uppercase text-slate-400 ml-1">(disabled)</span>}</td>
                  <td className="px-3 py-2">{s.city}</td>
                  <td className="px-3 py-2">{s.region}</td>
                  <td className="px-3 py-2 text-xs text-slate-600">{s.is_online_store ? "Online" : "Physical"}</td>
                  <td className="px-3 py-2"><StatusBadge status={s.status}/></td>
                  <td className="px-3 py-2">
                    <button data-testid={`copy-url-${s.store_code}`} onClick={() => copy(s.id, url)} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-slate-50">
                      {copiedId === s.id ? <><Check size={12} className="text-emerald-600"/> Copied</> : <><Copy size={12}/> Copy</>}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-right space-x-1">
                    <button data-testid={`regen-token-${s.store_code}`} onClick={() => regen(s.id)} title="Regenerate token" className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-amber-50"><KeyRound size={12}/></button>
                    {s.is_active && (
                      <button data-testid={`disable-store-${s.store_code}`} onClick={() => disable(s.id)} title="Disable" className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-200 text-red-700 rounded hover:bg-red-50"><Trash2 size={12}/></button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {showAdd && <AddStoreModal onClose={() => setShowAdd(false)} onCreated={() => { setShowAdd(false); load(); }} />}
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
