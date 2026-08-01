import React from "react";
import { api } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import { formatIst, parseUtcMs, elapsedSeconds } from "@/lib/time";
import { RefreshCw } from "lucide-react";

const fmt = (s) => formatIst(s);
const dur = (a, b) => {
  if (!a) return "—";
  const endMs = b ? parseUtcMs(b) : Date.now();
  const s = elapsedSeconds(a, endMs ?? Date.now());
  const m = Math.floor(s / 60); const r = s % 60;
  return `${m}m ${String(r).padStart(2, "0")}s`;
};

export default function BroadcastHistory() {
  const [sessions, setSessions] = React.useState([]);
  const [open, setOpen] = React.useState(null); // detail

  const load = async () => {
    const { data } = await api.get("/broadcast/history");
    setSessions(data);
  };
  React.useEffect(() => { load(); }, []);

  const openDetail = async (id) => {
    const { data } = await api.get(`/broadcast/sessions/${id}`);
    setOpen(data);
  };

  return (
    <div className="space-y-4" data-testid="history-page">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">Broadcast History</h1>
        <button data-testid="history-refresh-btn" onClick={load} className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50"><RefreshCw size={14}/> Refresh</button>
      </div>

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2">#</th>
              <th className="px-3 py-2">Campaign</th>
              <th className="px-3 py-2">Mode</th>
              <th className="px-3 py-2">Targets</th>
              <th className="px-3 py-2">Started</th>
              <th className="px-3 py-2">Duration</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.id} data-testid={`history-row-${s.id}`} onClick={() => openDetail(s.id)} className="border-b border-slate-100 even:bg-slate-50/50 cursor-pointer hover:bg-blue-50/50">
                <td className="px-3 py-2 font-mono text-xs text-slate-500">#{s.id}</td>
                <td className="px-3 py-2 font-medium">{s.campaign_name}</td>
                <td className="px-3 py-2 text-xs text-slate-600 uppercase tracking-wider">{s.target_mode}</td>
                <td className="px-3 py-2">{s.selected_store_count} <span className="text-slate-400 text-xs">({s.online_store_count} online)</span></td>
                <td className="px-3 py-2 text-xs">{fmt(s.started_at)}</td>
                <td className="px-3 py-2 font-mono text-xs">{dur(s.started_at, s.ended_at)}</td>
                <td className="px-3 py-2"><StatusBadge status={s.status}/></td>
              </tr>
            ))}
            {sessions.length === 0 && (
              <tr><td colSpan="7" className="px-3 py-6 text-center text-slate-500">No broadcast sessions yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

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
