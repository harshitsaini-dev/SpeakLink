import React from "react";
import { Volume2, VolumeX, Search, RefreshCw, X } from "lucide-react";
import { api } from "@/lib/api";

/**
 * The estate's Windows master volumes, independent of any broadcast.
 *
 * WHY THIS PAGE EXISTS OUTSIDE THE BROADCAST CONSOLE
 *
 * Setting a shop's volume is done before opening, after a complaint, or when
 * somebody notices a Store has been left muted since Friday - almost never
 * with an announcement on air. Reaching a volume slider used to require
 * starting a broadcast, which made the most routine audio task depend on the
 * least routine one.
 *
 * THE THREE THINGS THIS SCREEN MUST NEVER CONFLATE
 *
 *   what a Store IS      - a live reading, and only while it is connected
 *   what it WAS          - a memory, once it goes offline
 *   what we WANT it to be - a pending change that has not happened yet
 *
 * Every piece of wording below exists to keep those apart. An offline Store
 * says "Last known", never "Currently"; a queued change says "Pending on
 * reconnect", never "Applied".
 */

const REFRESH_MS = 3000;

const STATUS_TEXT = {
  ONLINE: { label: "ONLINE", tone: "ok" },
  OFFLINE: { label: "OFFLINE", tone: "off" },
  NEEDS_OUTPUT_SELECTION: { label: "Re-select the Store audio output", tone: "warn" },
  OUTPUT_UNAVAILABLE: { label: "Store audio output unavailable", tone: "bad" },
  CONTROLLED_BY_BROADCAST: { label: "Controlled by active broadcast", tone: "warn" },
};

const TONE_CLASS = {
  ok: "bg-emerald-100 text-emerald-800",
  off: "bg-slate-200 text-slate-700",
  warn: "bg-amber-100 text-amber-800",
  bad: "bg-red-100 text-red-800",
};

const LEVEL_CLASS = {
  low: "text-amber-700",
  normal: "text-slate-700",
  high: "text-red-700",
  unknown: "text-slate-400",
};

function timeAgo(iso) {
  if (!iso) return "never";
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (Number.isNaN(seconds)) return "unknown";
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleString();
}

export default function MasterVolume() {
  const [rows, setRows] = React.useState([]);
  const [zones, setZones] = React.useState([]);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState({});

  const [search, setSearch] = React.useState("");
  const [zone, setZone] = React.useState("all");
  const [presence, setPresence] = React.useState("all");
  const [endpoint, setEndpoint] = React.useState("all");

  const apply = React.useCallback((data) => {
    setRows(data.stores || []);
    setZones(data.zones || []);
  }, []);

  const refresh = React.useCallback(async () => {
    try {
      const response = await api.get("/store-audio/master");
      apply(response.data);
      setError(null);
    } catch (failure) {
      setError(failure?.response?.data?.detail || "Could not load Store audio.");
    } finally {
      setLoading(false);
    }
  }, [apply]);

  React.useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  /**
   * Sending is the ONLY thing that produces a command. Incoming state - the
   * poll above, or a pushed update - updates what is drawn and nothing else.
   * If displaying a reading could issue a command, HQ would answer its own
   * telemetry, hear the result, and never stop.
   */
  const send = async (store, body) => {
    setBusy((current) => ({ ...current, [store.store_id]: true }));
    try {
      const response = await api.post(
        `/store-audio/master/${store.store_id}`, body);
      apply(response.data);
      setError(null);
    } catch (failure) {
      setError(failure?.response?.data?.detail
        || `Could not change ${store.store_code}.`);
    } finally {
      setBusy((current) => ({ ...current, [store.store_id]: false }));
    }
  };

  const cancelPending = async (store) => {
    try {
      const response = await api.delete(
        `/store-audio/master/${store.store_id}/pending`);
      apply(response.data);
    } catch (failure) {
      setError(failure?.response?.data?.detail || "Could not cancel.");
    }
  };

  const visible = rows.filter((row) => {
    const term = search.trim().toLowerCase();
    if (term && !`${row.store_code} ${row.store_name}`.toLowerCase().includes(term)) {
      return false;
    }
    if (zone !== "all" && row.zone !== zone) return false;
    if (presence === "online" && !row.online) return false;
    if (presence === "offline" && row.online) return false;
    if (endpoint !== "all" && row.endpoint_status !== endpoint) return false;
    return true;
  });

  return (
    <div className="p-6 space-y-4" data-testid="master-volume-page">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Master Volume</h1>
          <p className="text-sm text-slate-500">
            Windows output level for every Store with an installed Receiver.
            No broadcast required.
          </p>
        </div>
        <button
          type="button"
          onClick={refresh}
          data-testid="master-volume-refresh"
          className="inline-flex items-center gap-2 px-3 py-2 rounded-md border border-slate-300 text-sm hover:bg-slate-50"
        >
          <RefreshCw size={15} /> Refresh
        </button>
      </div>

      {error && (
        <div role="alert" data-testid="master-volume-error"
             className="rounded-md bg-red-50 text-red-800 px-3 py-2 text-sm">
          {error}
        </div>
      )}

      {/* ---- filters ---- */}
      <div className="flex flex-wrap gap-3 items-center">
        <label className="relative">
          <span className="sr-only">Search Store</span>
          <Search size={15} className="absolute left-2 top-2.5 text-slate-400" />
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search Store"
            data-testid="master-volume-search"
            className="pl-8 pr-3 py-2 border border-slate-300 rounded-md text-sm w-56"
          />
        </label>

        <label className="text-sm text-slate-600">
          <span className="sr-only">Zone</span>
          <select
            value={zone}
            onChange={(event) => setZone(event.target.value)}
            data-testid="master-volume-zone"
            className="px-2 py-2 border border-slate-300 rounded-md text-sm"
          >
            <option value="all">All zones</option>
            {zones.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>

        <label className="text-sm text-slate-600">
          <span className="sr-only">Receiver presence</span>
          <select
            value={presence}
            onChange={(event) => setPresence(event.target.value)}
            data-testid="master-volume-presence"
            className="px-2 py-2 border border-slate-300 rounded-md text-sm"
          >
            <option value="all">Online and offline</option>
            <option value="online">Online only</option>
            <option value="offline">Offline only</option>
          </select>
        </label>

        <label className="text-sm text-slate-600">
          <span className="sr-only">Audio output state</span>
          <select
            value={endpoint}
            onChange={(event) => setEndpoint(event.target.value)}
            data-testid="master-volume-endpoint"
            className="px-2 py-2 border border-slate-300 rounded-md text-sm"
          >
            <option value="all">Any output state</option>
            <option value="ready">Endpoint ready</option>
            <option value="needs_output_selection">Needs selection</option>
            <option value="unavailable">Unavailable</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>

        <span className="text-sm text-slate-500" data-testid="master-volume-count">
          {visible.length} of {rows.length} Stores
        </span>
      </div>

      {loading && <p className="text-sm text-slate-500">Loading Store audio…</p>}

      {!loading && visible.length === 0 && (
        <p className="text-sm text-slate-500" data-testid="master-volume-empty">
          No Store matches these filters.
        </p>
      )}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {visible.map((row) => {
          const status = STATUS_TEXT[row.control_status]
            || { label: row.control_status, tone: "off" };
          const controllable = row.control_status === "ONLINE"
            || row.control_status === "CONTROLLED_BY_BROADCAST";
          const working = Boolean(busy[row.store_id]);
          const known = row.volume_percent !== null && row.volume_percent !== undefined;

          return (
            <article
              key={row.store_id}
              data-testid={`master-volume-card-${row.store_code}`}
              className="rounded-lg border border-slate-200 bg-white p-4 space-y-3"
            >
              <header className="flex items-start justify-between gap-2">
                <div>
                  <p className="font-semibold text-slate-900">
                    {row.store_code} <span className="font-normal text-slate-500">|</span>{" "}
                    {row.store_name}
                  </p>
                  {row.zone && (
                    <p className="text-xs uppercase tracking-wide text-slate-400">
                      {row.zone}
                    </p>
                  )}
                </div>
                <span
                  data-testid={`master-volume-status-${row.store_code}`}
                  className={`text-xs px-2 py-1 rounded ${TONE_CLASS[status.tone]}`}
                >
                  {status.label}
                </span>
              </header>

              {/* The number, always. The class beside it is a scanning aid and
                  never a replacement - the percentage is what is true. */}
              <div className="flex items-baseline gap-2">
                <span
                  data-testid={`master-volume-value-${row.store_code}`}
                  className={`text-2xl font-semibold ${LEVEL_CLASS[row.level_class] || ""}`}
                >
                  {known ? `${row.volume_percent}%` : "—"}
                </span>
                {row.muted && (
                  <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-white">
                    Muted
                  </span>
                )}
                <span
                  data-testid={`master-volume-freshness-${row.store_code}`}
                  className="text-xs text-slate-500"
                >
                  {/* The single most important word on this screen. */}
                  {row.stale
                    ? (known ? "Last known" : "Never reported")
                    : `Currently ${known ? `${row.volume_percent}%` : "unknown"}`}
                </span>
              </div>

              <input
                type="range"
                min="0"
                max="100"
                value={known ? row.volume_percent : 0}
                disabled={!controllable || working}
                data-testid={`master-volume-slider-${row.store_code}`}
                aria-label={`Master volume for ${row.store_name}`}
                onChange={(event) =>
                  send(row, { volume_percent: Number(event.target.value) })}
                className="w-full"
              />

              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  disabled={!controllable || working}
                  data-testid={`master-volume-mute-${row.store_code}`}
                  aria-pressed={Boolean(row.muted)}
                  onClick={() => send(row, { muted: !row.muted })}
                  className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md border border-slate-300 text-sm disabled:opacity-50"
                >
                  {row.muted ? <VolumeX size={15} /> : <Volume2 size={15} />}
                  {row.muted ? "Unmute" : "Mute"}
                </button>
                <span className="text-xs text-slate-400">
                  {row.online
                    ? `Updated ${timeAgo(row.updated_at)}`
                    : `Last seen ${timeAgo(row.last_seen_at)}`}
                </span>
              </div>

              {!row.online && (
                <p className="text-xs text-slate-500"
                   data-testid={`master-volume-offline-note-${row.store_code}`}>
                  Immediate control unavailable.
                </p>
              )}

              {row.pending_status && (
                <div
                  data-testid={`master-volume-pending-${row.store_code}`}
                  className="rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-900 space-y-1"
                >
                  {/* Never "Applied", and never "Currently". Nothing has
                      happened to this shop yet. */}
                  <p className="font-medium">
                    {row.pending_status === "failed"
                      ? "Pending — last attempt failed"
                      : "Pending — will apply when Receiver reconnects"}
                  </p>
                  <p>
                    {row.pending_volume_percent !== null
                      && row.pending_volume_percent !== undefined
                      && `${row.pending_volume_percent}%`}
                    {row.pending_muted !== null && row.pending_muted !== undefined
                      && ` ${row.pending_muted ? "Muted" : "Unmuted"}`}
                  </p>
                  {row.pending_error && <p>{row.pending_error}</p>}
                  <button
                    type="button"
                    onClick={() => cancelPending(row)}
                    data-testid={`master-volume-cancel-${row.store_code}`}
                    className="inline-flex items-center gap-1 mt-1 px-2 py-1 rounded border border-amber-300 hover:bg-amber-100"
                  >
                    <X size={13} /> Cancel Pending Change
                  </button>
                </div>
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}
