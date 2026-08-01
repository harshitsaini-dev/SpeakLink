import React from "react";
import { api } from "@/lib/api";
import { formatIst } from "@/lib/time";
import { RefreshCw } from "lucide-react";

const LEVEL_COLOR = { info: "text-slate-700", warn: "text-amber-700", error: "text-red-700" };

export default function SystemLogs() {
  const [logs, setLogs] = React.useState([]);
  const [level, setLevel] = React.useState("");
  const load = React.useCallback(async () => {
    const { data } = await api.get("/logs", { params: level ? { level } : {} });
    setLogs(data);
  }, [level]);
  React.useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-4" data-testid="logs-page">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">System Logs</h1>
        <div className="flex gap-2">
          <select data-testid="log-level-filter" value={level} onChange={(e) => setLevel(e.target.value)}
                  className="px-3 py-2 text-sm border border-slate-300 rounded-md bg-white">
            <option value="">All levels</option>
            <option value="info">Info</option>
            <option value="warn">Warn</option>
            <option value="error">Error</option>
          </select>
          <button data-testid="logs-refresh-btn" onClick={load} className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50"><RefreshCw size={14}/> Refresh</button>
        </div>
      </div>

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2 w-40">Time</th>
              <th className="px-3 py-2 w-24">Level</th>
              <th className="px-3 py-2">Message</th>
            </tr>
          </thead>
          <tbody className="font-mono text-xs">
            {logs.map((l) => (
              <tr key={l.id} className="border-b border-slate-100 even:bg-slate-50/50">
                <td className="px-3 py-1.5 text-slate-500">{formatIst(l.created_at)}</td>
                <td className={`px-3 py-1.5 uppercase font-bold ${LEVEL_COLOR[l.level] || ""}`}>{l.level}</td>
                <td className="px-3 py-1.5 text-slate-800">{l.message}</td>
              </tr>
            ))}
            {logs.length === 0 && <tr><td colSpan="3" className="px-3 py-6 text-center text-slate-500 font-sans">No logs yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
