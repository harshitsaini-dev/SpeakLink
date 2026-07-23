import React from "react";
import { api } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import { RefreshCw } from "lucide-react";

export default function ReceiverStatus() {
  const [stores, setStores] = React.useState([]);
  const load = async () => {
    const { data } = await api.get("/stores");
    setStores(data);
  };
  React.useEffect(() => { load(); const id = setInterval(load, 4000); return () => clearInterval(id); }, []);

  const groups = React.useMemo(() => {
    const g = {};
    stores.forEach((s) => { (g[s.region] = g[s.region] || []).push(s); });
    return g;
  }, [stores]);

  return (
    <div className="space-y-6" data-testid="receivers-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Receiver Status</h1>
          <p className="text-sm text-slate-500">Live connection status of every store receiver.</p>
        </div>
        <button data-testid="receivers-refresh-btn" onClick={load} className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50"><RefreshCw size={14}/> Refresh</button>
      </div>

      {Object.entries(groups).map(([region, list]) => (
        <div key={region}>
          <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500 mb-2">{region} ({list.length})</div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {list.map((s) => (
              <div key={s.id} data-testid={`receiver-card-${s.store_code}`} className="border border-slate-200 rounded-md bg-white p-3 flex items-center justify-between">
                <div>
                  <div className="font-mono text-xs text-slate-500">{s.store_code}</div>
                  <div className="font-medium text-slate-900">{s.store_name}</div>
                  <div className="text-xs text-slate-500">{s.city}</div>
                </div>
                <StatusBadge status={s.status}/>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
