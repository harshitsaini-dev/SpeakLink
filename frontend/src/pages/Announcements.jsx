import React from "react";
import { api } from "@/lib/api";
import { Play, Pause, Square, RefreshCw, Volume2, Upload, Trash2, Plus, X } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager, SortableTh, ExportButton } from "@/components/AdminFilters";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Recorded announcements: what is playing in every shop, and the templates
 * that decide what plays where.
 *
 * WHY THE STATUS TABLE IS THE FIRST THING ON THE PAGE
 *
 * The question this page is opened to answer is almost never "what templates
 * exist". It is "why is that shop talking", or "why is it not". So the live
 * state of every Store comes first, searchable, and the templates sit
 * underneath as the thing you set up once and rarely touch again.
 *
 * DUCKED IS SHOWN AS ITSELF, NOT AS PAUSED
 *
 * A Store standing aside for a live broadcast is not the same as one somebody
 * paused, and the difference is the only thing that explains why one comes
 * back on its own and the other does not. Collapsing them into "Paused" would
 * make the console's behaviour look arbitrary.
 */

const STATE_STYLES = {
  PLAYING: "bg-emerald-100 text-emerald-800 border-emerald-200",
  PAUSED: "bg-amber-100 text-amber-800 border-amber-200",
  DUCKED: "bg-sky-100 text-sky-800 border-sky-200",
  STOPPED: "bg-surface-muted text-body border-line",
};

const STATE_EXPLANATION = {
  PLAYING: "playing now",
  PAUSED: "paused by a person - it will NOT come back on its own",
  DUCKED: "standing aside for a live broadcast - it resumes by itself",
  STOPPED: "nothing chosen",
};

/**
 * What this shop is doing - and only what HQ can actually prove.
 *
 * PLAYING is a claim about a speaker in a shop. When no Receiver is connected
 * to that shop, HQ has not observed anything: it sent a play command into a
 * gap. The table used to say "Playing" anyway, which is the one thing it must
 * never do - somebody reads it, believes the promotion is on air, and finds
 * out from a customer that it is not. So an unreachable shop says what is
 * true: HQ asked, and nothing has answered.
 */
function StateBadge({ state, reachable = true, confirmed = true, error = "",
                     supported = true, version = "" }) {
  // A SHOP THAT CANNOT PLAY ANNOUNCEMENTS AT ALL.
  //
  // Announcements need a Receiver new enough to have them; an older one
  // connects, broadcasts, and ignores every announcement command it is sent.
  // That is indistinguishable from silence unless somebody says so - and a
  // whole day went into chasing a shop that was running an eleven-day-old
  // build.
  if (!supported) {
    return (
      <span data-testid="announcement-state-OLD-RECEIVER"
            title={`This Store is running Receiver ${version}, which is older than announcements. Install the current Store Kit on that computer.`}
            className="inline-block px-2 py-0.5 text-xs font-medium rounded-full border bg-amber-100 text-amber-800 border-amber-200">
        Receiver too old ({version})
      </span>
    );
  }
  // THE SHOP SAID IT COULD NOT.
  //
  // Its own words, because "it did not play" without a reason sends somebody
  // to the wrong computer. This is the answer the Receiver has always sent
  // and HQ never read.
  if (error) {
    return (
      <span title={error} data-testid="announcement-state-FAILED"
            className="inline-block max-w-[16rem] truncate px-2 py-0.5 text-xs font-medium rounded-full border bg-rose-100 text-rose-800 border-rose-200">
        Refused: {error}
      </span>
    );
  }
  if (!reachable && (state === "PLAYING" || state === "DUCKED")) {
    return (
      <span title="HQ sent this, but no Receiver is connected to this shop - nothing here confirms it is audible."
            data-testid="announcement-state-UNREACHABLE"
            className="inline-block px-2 py-0.5 text-xs font-medium rounded-full border bg-rose-100 text-rose-800 border-rose-200">
        Asked, no Receiver
      </span>
    );
  }
  return (
    <span title={STATE_EXPLANATION[state] || ""}
          data-testid={`announcement-state-${state}`}
          className={`inline-block px-2 py-0.5 text-xs font-medium rounded-full border ${STATE_STYLES[state] || STATE_STYLES.STOPPED}`}>
      {state === "DUCKED" ? "Broadcast" : state.charAt(0) + state.slice(1).toLowerCase()}
      {/* Sent, and nothing has answered yet. A dot rather than a second badge:
          it is a qualifier on this state, not a state of its own. */}
      {state === "PLAYING" && !confirmed && (
        <span title="HQ has sent this and the Store has not confirmed it yet."
              data-testid="announcement-unconfirmed"> ·&nbsp;unconfirmed</span>
      )}
    </span>
  );
}

export default function Announcements() {
  const { can } = useAuth();
  const status = useAdminList("/announcements/status",
                { q: "", zone: "", store_id: "", state: "", sort: "", dir: "asc" },
                // This table is the answer to "what is every shop doing right
                // now" - including a level somebody just changed at the till.
                // Five seconds is quicker than anybody walks back to a
                // counter, and the request is one small query.
                { refreshSeconds: 5 });
  const [busy, setBusy] = React.useState("");
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");

  const mayControl = can("announcements.control");
  const mayControlAll = can("announcements.control_all");


  // Refreshed periodically, but never while a filter is being typed: a table
  // that reorders under the cursor is worse than a slightly stale one.
  React.useEffect(() => {
    const id = setInterval(() => { if (!status.loading) status.reload(); }, 8000);
    return () => clearInterval(id);
  }, [status]);

  async function act(label, request) {
    setBusy(label);
    setError("");
    setMessage("");
    try {
      const { data } = await request();
      setMessage(typeof data?.note === "string" ? data.note : `${label} - done.`);
      status.reload();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || `${label} could not be completed. Try again.`);
    } finally {
      setBusy("");
    }
  }

  // The Zones on offer come from the ESTATE, not from the rows on screen.
  // Deriving them from the current page made the filter single-select in
  // practice: choosing one Zone narrowed the rows to that Zone, which was the
  // only option left in the list, so a second one could never be ticked.
  const [zones, setZones] = React.useState([]);
  const [storeOptions, setStoreOptions] = React.useState([]);
  React.useEffect(() => {
    api.get("/receivers/filter-options")
      .then(({ data }) => {
        setZones(data.regions || []);
        setStoreOptions((data.stores || []).map((store) => ({
          value: String(store.id),
          label: `${store.store_name} (${store.store_code})` })));
      })
      .catch(() => { setZones([]); setStoreOptions([]); });  // a filter with nothing to offer, not a broken page
  }, []);

  return (
    <div className="space-y-6" data-testid="announcements-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-strong">Announcements</h1>
          <p className="text-sm text-muted">
            Recorded promotions, and what every shop is playing right now.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button data-testid="announcements-refresh" onClick={status.reload}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted">
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
          {mayControlAll && (
            <>
              <button data-testid="announcements-play-all"
                      disabled={busy !== ""}
                      onClick={() => act("Play All",
                        () => api.post("/announcements/play-all"))}
                      className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50">
                <Play className="w-4 h-4" /> Play All
              </button>
              <button data-testid="announcements-pause-all"
                      disabled={busy !== ""}
                      onClick={() => act("Pause All",
                        () => api.post("/announcements/pause-all"))}
                      className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-amber-600 hover:bg-amber-700 disabled:opacity-50">
                <Pause className="w-4 h-4" /> Pause All
              </button>
              {/* Stop is not Pause, and the two sit apart for that reason.
                  Pause All leaves every shop holding its campaign, ready to
                  carry on. Stop All lets go of the choice everywhere: after
                  it nothing is selected anywhere, and Play has nothing to
                  resume. Confirmed, because it cannot be undone by pressing
                  the other button. */}
              <button data-testid="announcements-stop-all"
                      disabled={busy !== ""}
                      onClick={() => {
                        if (!window.confirm(
                          "Stop announcements in every shop?\n\nThis is not a "
                          + "pause: each shop lets go of what it was told to "
                          + "play, and nothing is selected afterwards.")) return;
                        act("Stop All", () => api.post("/announcements/stop-all"));
                      }}
                      className="inline-flex items-center gap-1 px-3 py-2 rounded-md text-sm text-white bg-rose-700 hover:bg-rose-800 disabled:opacity-50">
                <Square className="w-4 h-4" /> Stop All
              </button>
            </>
          )}
        </div>
      </div>

      {message && (
        <div data-testid="announcements-message"
             className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {message}
        </div>
      )}
      {error && (
        <div data-testid="announcements-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}

      {/* ---- What is playing right now ---- */}
      <section className="bg-surface rounded-lg border border-line">
        <div className="px-4 py-3 border-b border-line">
          <h2 className="font-semibold text-strong">Live status</h2>
          <p className="text-xs text-muted">
            Every active Store appears here, including shops that have never
            played anything - those are the ones somebody is usually looking for.
          </p>
        </div>

        <FilterBar onClear={status.clearFilters} activeCount={status.activeCount}
                   total={status.total} loading={status.loading}>
          <SearchInput value={status.filters.q}
                       onChange={(value) => status.setFilter("q", value)}
                       placeholder="Store, template or recording…"
                       testId="announcements-search" />
          <FilterSelect label="Zone" testId="announcements-zone" allLabel="All Zones"
                        value={status.filters.zone}
                        onChange={(value) => status.setFilter("zone", value)}
                        options={zones.map((zone) => ({ value: zone, label: zone }))} />
          {/* By shop, not only by Zone. "Why is this one shop silent" is the
              question this page exists for, and answering it by scrolling
              forty rows is not answering it. */}
          <FilterSelect label="Store" testId="announcements-store" allLabel="All Stores"
                        value={status.filters.store_id}
                        onChange={(value) => status.setFilter("store_id", value)}
                        options={storeOptions} />
          <FilterSelect label="State" testId="announcements-state" allLabel="Any state"
                        value={status.filters.state}
                        onChange={(value) => status.setFilter("state", value)}
                        options={[
                          { value: "PLAYING", label: "Playing" },
                          { value: "PAUSED", label: "Paused by a person" },
                          { value: "DUCKED", label: "Standing aside for a broadcast" },
                          { value: "STOPPED", label: "Nothing chosen" },
                        ]} />
        </FilterBar>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-surface-muted text-body">
              <tr>
                <SortableTh column="store_name" label="Store" list={status} />
                <SortableTh column="zone" label="Zone" list={status} />
                <SortableTh column="state" label="State" list={status} />
                <SortableTh column="audio_title" label="Playing" list={status} />
                <SortableTh column="volume_percent" label="Volume" list={status} />
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              <ListState loading={status.loading} error={status.error}
                         empty={!status.items.length} colSpan={6}
                         onRetry={status.reload}
                         emptyText="No Store matches these filters." />
              {!status.loading && !status.error && status.items.map((row) => (
                <tr key={row.store_id} className="border-t border-line"
                    data-testid={`announcement-row-${row.store_id}`}>
                  <td className="px-4 py-2">
                    <div className="font-medium text-strong">{row.store_name}</div>
                    <div className="text-xs text-muted">{row.store_code}</div>
                  </td>
                  <td className="px-4 py-2 text-body">{row.zone}</td>
                  <td className="px-4 py-2"><StateBadge state={row.state} reachable={row.reachable !== false}
                                confirmed={row.confirmed !== false}
                                error={row.confirm_error || ""}
                                supported={row.announcements_supported !== false}
                                version={row.receiver_version || ""} /></td>
                  <td className="px-4 py-2 text-body">
                    {row.template_name
                      ? <>
                          <div>{row.audio_title || "-"}</div>
                          <div className="text-xs text-faint">{row.template_name}</div>
                        </>
                      : <span className="text-faint">nothing chosen</span>}
                  </td>
                  <td className="px-4 py-2">
                    <VolumeControl row={row} disabled={!can("announcements.volume")}
                                   onDone={status.reload} />
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex items-center justify-end gap-1">
                      {mayControl && row.state !== "PLAYING" && (
                        <button data-testid={`announcement-play-${row.store_id}`}
                                disabled={busy !== ""}
                                title={row.state === "DUCKED"
                                  ? "A broadcast is playing here; the announcement resumes by itself"
                                  : "Resume this Store"}
                                onClick={() => act(`Play ${row.store_code}`,
                                  () => api.post(`/announcements/stores/${row.store_id}/play`))}
                                className="p-1.5 rounded border border-line-strong hover:bg-surface-muted disabled:opacity-50">
                          <Play className="w-4 h-4" />
                        </button>
                      )}
                      {mayControl && row.state !== "STOPPED" && row.state !== "PAUSED" && (
                        <button data-testid={`announcement-pause-${row.store_id}`}
                                disabled={busy !== ""}
                                onClick={() => act(`Pause ${row.store_code}`,
                                  () => api.post(`/announcements/stores/${row.store_id}/pause`))}
                                className="p-1.5 rounded border border-line-strong hover:bg-surface-muted disabled:opacity-50">
                          <Pause className="w-4 h-4" />
                        </button>
                      )}
                      {/* Offered wherever there is something to let go of -
                          including a paused shop, which is still holding a
                          campaign it would resume. */}
                      {mayControl && row.template_id && (
                        <button data-testid={`announcement-stop-${row.store_id}`}
                                disabled={busy !== ""}
                                title="Stop this Store and let go of what it was told to play"
                                onClick={() => act(`Stop ${row.store_code}`,
                                  () => api.post(`/announcements/stores/${row.store_id}/stop`))}
                                className="p-1.5 rounded border border-rose-300 text-rose-700 hover:bg-rose-50 disabled:opacity-50">
                          <Square className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Pager page={status.page} pages={status.pages} total={status.total}
               hasMore={status.hasMore} onPage={status.setPage} />
      </section>

    </div>
  );
}

/**
 * The per-Store level.
 *
 * Committed on release rather than on every pixel of movement: dragging a
 * slider would otherwise send fifty requests, and the shop would hear the
 * level stepping rather than moving.
 */
function VolumeControl({ row, disabled, onDone }) {
  // THE SHOP'S OWN LEVEL WINS, when the Store has reported one.
  //
  // `volume_percent` is what HQ last SET. `store_volume_percent` is what the
  // speaker is actually on - including a change somebody made at the till,
  // which HQ could not see at all, so this slider sat on a number nobody had
  // touched in hours and pretended it was the shop's.
  const shopLevel = row.store_volume_percent;
  const known = shopLevel !== null && shopLevel !== undefined;
  const [value, setValue] = React.useState(
    known ? shopLevel : (row.volume_percent ?? 80));
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    if (saving) return;              // do not fight the hand on the slider
    setValue(known ? shopLevel : (row.volume_percent ?? 80));
  }, [shopLevel, known, row.volume_percent, saving]);

  async function commit() {
    setSaving(true);
    try {
      await api.post(`/announcements/stores/${row.store_id}/volume`,
                     { volume_percent: value });
      onDone?.();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <Volume2 className="w-4 h-4 text-faint" />
      <input type="range" min="0" max="100" value={value} disabled={disabled || saving}
             data-testid={`announcement-volume-${row.store_id}`}
             onChange={(event) => setValue(Number(event.target.value))}
             onMouseUp={commit} onTouchEnd={commit} onKeyUp={commit}
             className="w-24" />
      {/* WHOSE NUMBER THIS IS.
          A reading from the shop and a setting from HQ look identical on a
          slider, and only one of them is what anybody can hear. */}
      <span className={`text-xs w-8 tabular-nums ${known ? "text-strong" : "text-muted"}`}
            title={known
              ? "This is the shop's own master volume, as the Store reported it."
              : "This Store has not reported its own volume - this is what HQ last set. "
                + "An older Receiver does not report it at all."}>
        {value}%
      </span>
      {/* Said out loud when this is a reading rather than a setting: the two
          look identical on a slider, and only one of them is what the shop
          can hear. */}
      {row.store_muted && (
        <span className="text-[10px] uppercase tracking-wider text-amber-700"
              data-testid={`announcement-muted-${row.store_id}`}>
          muted at the shop
        </span>
      )}
    </div>
  );
}

