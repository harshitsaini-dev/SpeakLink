import React from "react";
import { Volume2, VolumeX, Search, RefreshCw, X } from "lucide-react";
import { api } from "@/lib/api";

/**
 * The estate's Windows master volumes, independent of any broadcast.
 *
 * WHY THE CONTROLS ARE NEVER DISABLED
 *
 * They used to grey out whenever a Store was offline, which answered a
 * question the operator had not asked. Choosing what a shop should be set to
 * and that shop being reachable this second are different things, and letting
 * the second decide the first meant a manager could not say "Rajouri should be
 * at 30%" until somebody switched a PC on.
 *
 * So the slider and the mute button always work. What changes when a Store is
 * offline is only what the screen CLAIMS - never what the operator may do.
 *
 * THE THREE FACTS, WHICH ARE NEVER MERGED
 *
 *   TARGET     what HQ wants        - the slider position, always settable
 *   ACTUAL      what Windows reports - only ever from the Receiver's readback
 *   CONNECTION  can it be applied now - ONLINE or OFFLINE
 *
 * "Applied 70%" would be TARGET wearing ACTUAL's clothes, so it is never
 * said. A number is only ever called Current when a Receiver read it back.
 */

const REFRESH_MS = 3000;

const CONNECTION_TEXT = {
  ONLINE: { label: "ONLINE", tone: "ok" },
  OFFLINE: { label: "OFFLINE", tone: "off" },
  NEEDS_OUTPUT_SELECTION: { label: "Re-select the Store audio output", tone: "warn" },
  OUTPUT_UNAVAILABLE: { label: "Store audio output unavailable", tone: "bad" },
  CONTROLLED_BY_BROADCAST: { label: "Controlled by active broadcast", tone: "warn" },
};

const SYNC_TEXT = {
  SYNCED: { label: "Synced", tone: "ok" },
  APPLYING: { label: "Applying…", tone: "warn" },
  OUT_OF_SYNC: { label: "Changed at the Store", tone: "warn" },
  WAITING_FOR_SYNC: { label: "Waiting for Receiver sync", tone: "off" },
  SYNC_FAILED: { label: "Last sync attempt failed", tone: "bad" },
  // SpeakLink tried, repeatedly, and something keeps moving the mixer
  // back. Saying so beats arguing with a shop for ever in silence.
  ENFORCEMENT_SUSPENDED: {
    label: "Not holding — changed at the Store repeatedly", tone: "bad" },
  NO_TARGET_STATE: { label: "No Target set", tone: "off" },
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

/** What the ACTUAL line says. Never "Current" unless a Receiver read it. */
function actualLine(row) {
  const known = row.volume_percent !== null && row.volume_percent !== undefined;
  if (!known) return "Current Windows state: Unknown";
  if (row.online) return `Current: ${row.volume_percent}%`;
  return `Last reported: ${row.volume_percent}%`;
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
  const [sync, setSync] = React.useState("all");

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
   * An operator gesture is the ONLY thing that produces a command. The refresh
   * above updates what is drawn and nothing else - if displaying a reading
   * could issue a command, HQ would answer its own telemetry for ever, and a
   * member of staff adjusting a shop's volume would find HQ fighting them.
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
        || `Could not set ${store.store_code}.`);
    } finally {
      setBusy((current) => ({ ...current, [store.store_id]: false }));
    }
  };

  const clearDesired = async (store) => {
    try {
      const response = await api.delete(
        `/store-audio/master/${store.store_id}/pending`);
      apply(response.data);
    } catch (failure) {
      setError(failure?.response?.data?.detail || "Could not clear.");
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
    if (sync !== "all" && row.sync_state !== sync) return false;
    return true;
  });

  return (
    <div className="p-6 space-y-4" data-testid="master-volume-page">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Master Volume</h1>
          <p className="text-sm text-slate-500">
            Choose the Windows output level for any Store with an installed
            Receiver, online or not. No broadcast required.
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
          <span className="sr-only">Sync state</span>
          <select
            value={sync}
            onChange={(event) => setSync(event.target.value)}
            data-testid="master-volume-sync"
            className="px-2 py-2 border border-slate-300 rounded-md text-sm"
          >
            <option value="all">Any sync state</option>
            <option value="SYNCED">Synced</option>
            <option value="WAITING_FOR_SYNC">Waiting for sync</option>
            <option value="OUT_OF_SYNC">Changed at the Store</option>
            <option value="APPLYING">Applying</option>
            <option value="SYNC_FAILED">Sync failed</option>
            <option value="NO_TARGET_STATE">No Target set</option>
            <option value="ENFORCEMENT_SUSPENDED">Not holding</option>
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
          const connection = CONNECTION_TEXT[row.control_status]
            || { label: row.control_status, tone: "off" };
          const syncState = SYNC_TEXT[row.sync_state]
            || { label: row.sync_state, tone: "off" };
          const working = Boolean(busy[row.store_id]);
          const hasTarget = row.target_volume_percent !== null
            && row.target_volume_percent !== undefined;
          const targetMuted = Boolean(row.target_muted);

          // The slider shows what HQ WANTS. Falling back to the last reported
          // reading only when nobody has expressed an intention yet, and to a
          // neutral position when even that is unknown.
          const sliderValue = hasTarget
            ? row.target_volume_percent
            : (row.volume_percent ?? 50);

          // Nothing about the CONNECTION disables these controls. A Store an
          // active broadcast owns is refused by the server with a message the
          // operator can read, which is a genuine conflict rather than a
          // reason to grey out the control before they have even tried.
          // Only an in-flight request of this operator's own disables it, to
          // stop a drag producing a burst of overlapping writes.

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
                  className={`text-xs px-2 py-1 rounded ${TONE_CLASS[connection.tone]}`}
                >
                  {connection.label}
                </span>
              </header>

              {/* ---- ACTUAL: only ever what a Receiver read back ---- */}
              <div className="flex items-baseline gap-2 flex-wrap">
                <span
                  data-testid={`master-volume-actual-${row.store_code}`}
                  className={`text-sm ${LEVEL_CLASS[row.level_class] || ""}`}
                >
                  {actualLine(row)}
                </span>
                {row.muted === true && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-white">
                    Muted
                  </span>
                )}
              </div>

              {/* ---- DESIRED: what HQ wants, always settable ---- */}
              <div className="flex items-baseline gap-2 flex-wrap">
                <span
                  data-testid={`master-volume-target-${row.store_code}`}
                  className="text-2xl font-semibold text-slate-900"
                >
                  {hasTarget ? `Target Volume: ${row.target_volume_percent}%`
                              : "Target Volume: not set"}
                </span>
                {targetMuted && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-white">
                    Target Mute: ON
                  </span>
                )}
              </div>

              <div className="flex items-center gap-2">
                <span
                  data-testid={`master-volume-sync-${row.store_code}`}
                  className={`text-xs px-2 py-0.5 rounded ${TONE_CLASS[syncState.tone]}`}
                >
                  {syncState.label}
                </span>
                {row.sync_error && (
                  <span data-testid={`master-volume-sync-error-${row.store_code}`}
                        className="text-xs text-red-700">{row.sync_error}</span>
                )}
              </div>

              <input
                type="range"
                min="0"
                max="100"
                value={sliderValue}
                disabled={working}
                data-testid={`master-volume-slider-${row.store_code}`}
                aria-label={`Desired master volume for ${row.store_name}`}
                onChange={(event) =>
                  send(row, { volume_percent: Number(event.target.value) })}
                className="w-full"
              />

              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  disabled={working}
                  data-testid={`master-volume-mute-${row.store_code}`}
                  aria-pressed={targetMuted}
                  onClick={() => send(row, { muted: !targetMuted })}
                  className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md border border-slate-300 text-sm disabled:opacity-50"
                >
                  {targetMuted ? <VolumeX size={15} /> : <Volume2 size={15} />}
                  {targetMuted ? "Unmute" : "Mute"}
                </button>
                <span className="text-xs text-slate-400">
                  {row.online
                    ? `Updated ${timeAgo(row.updated_at)}`
                    : `Last seen ${timeAgo(row.last_seen_at)}`}
                </span>
              </div>

              {!row.online && (
                // Explains, rather than excusing a control that has been taken
                // away. The setting is real and it will be applied.
                <p className="text-xs text-slate-500"
                   data-testid={`master-volume-offline-note-${row.store_code}`}>
                  Receiver offline — automatically syncs when the Receiver
                  reconnects.
                </p>
              )}

              {hasTarget && (
                <button
                  type="button"
                  onClick={() => clearDesired(row)}
                  data-testid={`master-volume-clear-${row.store_code}`}
                  className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-800"
                >
                  <X size={13} /> Clear Target
                </button>
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}
