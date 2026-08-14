import React from "react";
import { api } from "@/lib/api";
import { RefreshCw, Trash2 } from "lucide-react";
import { useAdminList } from "@/lib/adminList";
import { FilterBar, SearchInput, FilterSelect, ListState, Pager } from "@/components/AdminFilters";
import { useAuth } from "@/contexts/AuthContext";
import { formatIst } from "@/lib/time";

/**
 * What played, where, and why it stopped.
 *
 * The same architecture as Broadcast History - server-side search, filters and
 * pagination through useAdminList - because it answers the same shape of
 * question and an operator should not have to learn a second one.
 *
 * WHY "WHY IT STOPPED" IS A COLUMN
 *
 * Paused by a person and ducked by a live broadcast look identical afterwards:
 * both are a shop that went quiet. They are not the same event, and "it went
 * quiet at 4pm" is only answerable because the reason was written down at the
 * time.
 */

const REASON_LABEL = {
  paused: "paused by a person",
  broadcast: "a broadcast interrupted it",
  stopped: "stopped",
  superseded: "replaced by another announcement",
};

export default function AnnouncementHistory() {
  const { can } = useAuth();
  const list = useAdminList("/announcements/history", {
    q: "", zone: "", reason: "", since: "", until: "",
  });
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState("");
  const [deleting, setDeleting] = React.useState(null);
  const [word, setWord] = React.useState("");

  const mayTidy = can("announcements.templates.manage");
  const mayDelete = can("announcements.delete_permanently");

  const zones = Array.from(new Set((list.items || [])
    .map((row) => row.zone).filter(Boolean)));

  async function act(label, request) {
    setBusy(label);
    setError("");
    try {
      await request();
      list.reload();
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || `${label} could not be completed.`);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4" data-testid="announcement-history-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">
            Announcement History
          </h1>
          <p className="text-sm text-slate-500">
            What each shop played, and why it stopped.
          </p>
        </div>
        <button data-testid="announcement-history-refresh" onClick={list.reload}
                className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {error && (
        <div data-testid="announcement-history-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}

      <FilterBar onClear={list.clearFilters} activeCount={list.activeCount}
                 total={list.total} loading={list.loading}>
        <SearchInput value={list.filters.q}
                     onChange={(value) => list.setFilter("q", value)}
                     placeholder="Store, template or recording…"
                     testId="announcement-history-search" />
        <FilterSelect label="Zone" testId="announcement-history-zone"
                      allLabel="All Zones" value={list.filters.zone}
                      onChange={(value) => list.setFilter("zone", value)}
                      options={zones.map((zone) => ({ value: zone, label: zone }))} />
        <FilterSelect label="Ended because" testId="announcement-history-reason"
                      allLabel="Any" value={list.filters.reason}
                      onChange={(value) => list.setFilter("reason", value)}
                      options={[
                        // The absence of an end, not a reason - named here
                        // rather than left to somebody discovering that an
                        // empty filter means something different.
                        { value: "open", label: "Still playing" },
                        { value: "paused", label: "Paused by a person" },
                        { value: "broadcast", label: "A broadcast interrupted it" },
                        { value: "stopped", label: "Stopped" },
                        { value: "superseded", label: "Replaced by another" },
                      ]} />
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">From</span>
          <input type="date" value={list.filters.since}
                 data-testid="announcement-history-since"
                 onChange={(event) => list.setFilter("since", event.target.value)}
                 className="px-2 py-1.5 border border-slate-300 rounded-md text-sm" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">To</span>
          <input type="date" value={list.filters.until}
                 data-testid="announcement-history-until"
                 onChange={(event) => list.setFilter("until", event.target.value)}
                 className="px-2 py-1.5 border border-slate-300 rounded-md text-sm" />
        </label>
      </FilterBar>

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th className="px-3 py-2">Store</th>
              <th className="px-3 py-2">Zone</th>
              <th className="px-3 py-2">Played</th>
              <th className="px-3 py-2">Started</th>
              <th className="px-3 py-2">Ended</th>
              <th className="px-3 py-2">Because</th>
              {(mayTidy || mayDelete) && <th className="px-3 py-2"></th>}
            </tr>
          </thead>
          <tbody>
            <ListState loading={list.loading} error={list.error}
                       empty={!list.items.length} colSpan={7}
                       onRetry={list.reload}
                       emptyText="Nothing has played in the period these filters cover." />
            {!list.loading && !list.error && list.items.map((row) => (
              <tr key={row.id} data-testid={`announcement-history-${row.id}`}
                  className="border-b border-slate-100 even:bg-slate-50/50">
                <td className="px-3 py-2">
                  <div className="font-medium">{row.store_name}</div>
                  <div className="text-xs text-slate-500 font-mono">{row.store_code}</div>
                </td>
                <td className="px-3 py-2">{row.zone}</td>
                <td className="px-3 py-2">
                  <div>{row.audio_title || "-"}</div>
                  <div className="text-xs text-slate-400">{row.template_name}</div>
                </td>
                <td className="px-3 py-2 text-xs">{formatIst(row.started_at)}</td>
                <td className="px-3 py-2 text-xs">
                  {row.ended_at
                    ? formatIst(row.ended_at)
                    : <span className="text-emerald-700">still playing</span>}
                </td>
                <td className="px-3 py-2 text-xs text-slate-600">
                  {row.ended_reason ? (REASON_LABEL[row.ended_reason]
                                       || row.ended_reason) : "-"}
                </td>
                {(mayTidy || mayDelete) && (
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    {mayTidy && (
                      <button data-testid={`announcement-history-archive-${row.id}`}
                              disabled={busy !== ""}
                              onClick={() => act("Archive", () =>
                                api.post(`/announcements/history/${row.id}/archive`))}
                              className="px-2 py-1 rounded border border-slate-300 text-xs hover:bg-slate-50 mr-1">
                        Archive
                      </button>
                    )}
                    {mayDelete && (
                      <button data-testid={`announcement-history-delete-${row.id}`}
                              onClick={() => { setDeleting(row); setWord(""); }}
                              className="px-2 py-1 rounded border border-rose-300 text-xs text-rose-700 hover:bg-rose-50">
                        <Trash2 className="w-3.5 h-3.5 inline" />
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        <Pager page={list.page} pages={list.pages} total={list.total}
               hasMore={list.hasMore} onPage={list.setPage} />
      </div>

      {deleting && (
        <div className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 space-y-2"
             data-testid="announcement-history-delete-confirm">
          <p className="text-sm text-rose-900">
            Delete the record of <strong>{deleting.audio_title}</strong> in{" "}
            <strong>{deleting.store_name}</strong>? Unlike a recording there is
            nothing to re-upload - this destroys the answer to "what was that
            shop playing" for a moment that has already passed.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-rose-900">Type DELETE to confirm:</label>
            <input value={word} onChange={(event) => setWord(event.target.value)}
                   data-testid="announcement-history-delete-word"
                   className="px-3 py-2 border border-rose-300 rounded-md text-sm w-32" />
            <button data-testid="announcement-history-delete-confirm-btn"
                    disabled={word.trim().toUpperCase() !== "DELETE"}
                    onClick={() => act("Delete", async () => {
                      await api.post(
                        `/announcements/history/${deleting.id}/delete-permanently`,
                        { confirmation: word });
                      setDeleting(null);
                    })}
                    className="px-3 py-2 rounded-md text-sm text-white bg-rose-700 hover:bg-rose-800 disabled:opacity-40">
              Delete permanently
            </button>
            <button onClick={() => setDeleting(null)}
                    data-testid="announcement-history-delete-cancel"
                    className="px-3 py-2 rounded-md text-sm border border-slate-300 hover:bg-white">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
