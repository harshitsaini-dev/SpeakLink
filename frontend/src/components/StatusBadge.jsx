import React from "react";

const STATUS_STYLES = {
  online: "bg-emerald-100 text-emerald-800 border-emerald-200",
  offline: "bg-slate-100 text-slate-700 border-slate-200",
  playing: "bg-blue-100 text-blue-800 border-blue-200 animate-pulse",
  error: "bg-red-100 text-red-800 border-red-200",
  waiting: "bg-amber-100 text-amber-800 border-amber-200",
  pending: "bg-slate-100 text-slate-700 border-slate-200",
  stopped: "bg-slate-100 text-slate-700 border-slate-200",
  failed: "bg-red-100 text-red-800 border-red-200",
  live: "bg-red-100 text-red-800 border-red-200",
  ended: "bg-slate-100 text-slate-700 border-slate-200",
  emergency_stopped: "bg-red-100 text-red-900 border-red-300",
};

export default function StatusBadge({ status, testid }) {
  const cls = STATUS_STYLES[status] || "bg-slate-100 text-slate-700 border-slate-200";
  const label = String(status || "unknown").replace(/_/g, " ");
  return (
    <span
      data-testid={testid || `status-badge-${status}`}
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-semibold border ${cls}`}
    >
      {status === "playing" && <span className="w-1.5 h-1.5 rounded-full bg-blue-600" />}
      {status === "live" && <span className="w-1.5 h-1.5 rounded-full bg-red-600" />}
      <span className="uppercase tracking-wide">{label}</span>
    </span>
  );
}
