/**
 * Active Broadcasts - the supervision page.
 *
 * Broadcast Console is where somebody speaks. This is where somebody watches,
 * and the two have different shapes: the Console shows one broadcast (yours)
 * and must stay the same height whatever else is happening, while this page
 * shows everybody's and has to stay usable at fifty.
 *
 * PERMISSIONS ARE NOT DECIDED HERE
 *
 * Every capability comes from `meta` on the list response, which the backend
 * derives from the same resolver it enforces with. This component never reads
 * `user.role`, and it never hides a field the server sent - because a field
 * the server sent has already been disclosed. Where a column is missing, the
 * data is missing from the response too.
 *
 * The one thing rendered from local knowledge is `is_mine`, which the backend
 * marks: you are always allowed to know which broadcast is your own.
 */
import React from "react";
import { Radio, Search, RefreshCcw, Square, X } from "lucide-react";
import { api } from "@/lib/api";
import SupervisedWebAudience from "@/components/SupervisedWebAudience";
import { useAdminList } from "@/lib/adminList";

const PAGE_SIZES = [20, 50];

const OWNER_FILTERS = [
  { value: "all", label: "All" },
  { value: "mine", label: "Mine" },
  { value: "others", label: "Others" },
];

export default function ActiveBroadcasts() {
  // Declared before the hook so the control actually drives the request.
  // Holding a separate pageSize beside useAdminList would page in React over
  // whatever the server already sent, which is the client-side pagination
  // this page exists to avoid.
  const [pageSize, setPageSize] = React.useState(20);
  const list = useAdminList(
    "/broadcast/active-management",
    { q: "", owner: "all", sort: "newest" },
    { pageSize },
  );
  const { filters, setFilter, page, setPage, items, total, pages, loading, error, meta, reload } = list;

  const mayViewOwnership = Boolean(meta?.may_view_ownership);
  const mayViewTargets = Boolean(meta?.may_view_targets);
  const mayStopAny = Boolean(meta?.may_stop_any);
  // Managing another operator's audience is its own permission. Reading who is
  // broadcasting does not confer it, and neither does being able to stop them.
  const mayManageWebAudience = Boolean(meta?.may_manage_web_audience);
  const [audienceFor, setAudienceFor] = React.useState(null);

  // Built from the CURRENT origin, so a LAN pilot produces a LAN link and the
  // HQ machine's own hostname never leaks into somebody else's browser.
  const copyText = (value) => {
    try { navigator.clipboard.writeText(value); } catch (ignored) { /* manual copy */ }
  };
  const copyListenerLink = (code) =>
    copyText(`${window.location.origin}/listen/${code}`);

  const [detail, setDetail] = React.useState(null);      // { session, stores }
  const [detailError, setDetailError] = React.useState("");
  const [confirmStop, setConfirmStop] = React.useState(null);
  const [stopping, setStopping] = React.useState(false);
  const [stopError, setStopError] = React.useState("");
  const [notice, setNotice] = React.useState("");

  // Bounded polling, deliberately not a second WebSocket. Search, filters,
  // sort and page live in useAdminList, so a refresh re-issues the CURRENT
  // query rather than resetting to page 1 - and useAdminList's request
  // counter means a slow earlier response cannot overwrite a newer one.
  React.useEffect(() => {
    const timer = setInterval(() => { reload(); }, 10_000);
    return () => clearInterval(timer);
  }, [reload]);

  const openStores = async (row) => {
    setDetailError("");
    try {
      const { data } = await api.get(`/broadcast/active-management/${row.session_id}/stores`);
      setDetail(data);
    } catch (failure) {
      setDetailError(
        failure?.response?.status === 403
          ? "You do not have permission to view the Stores of a broadcast."
          : "Those Stores could not be loaded.",
      );
    }
  };

  const doStop = async () => {
    if (!confirmStop) return;
    setStopping(true);
    setStopError("");
    try {
      await api.post(`/broadcast/active-management/${confirmStop.session_id}/stop`);
      setConfirmStop(null);
      setNotice(`Broadcast #${confirmStop.session_id} was stopped.`);
      // Refresh from the server rather than removing the row locally. A row
      // deleted client-side would look stopped whatever actually happened;
      // the list has to come back from the one active-truth source.
      reload();
    } catch (failure) {
      // Never close the dialog on failure and never report success. If
      // cleanup failed the broadcast may still be live, and an operator who
      // believes a Store is silent when it is not is worse off than one who
      // sees an error.
      const detailBody = failure?.response?.data?.detail;
      setStopError(
        typeof detailBody === "string"
          ? detailBody
          : detailBody?.message || "This broadcast could not be stopped. It may still be live.",
      );
      reload();
    } finally {
      setStopping(false);
    }
  };

  const changePageSize = (size) => { setPageSize(size); setPage(1); };

  return (
    <div className="space-y-4" data-testid="active-broadcasts-page">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <Radio className="text-red-600" size={20} />
          <h1 className="text-xl font-bold text-slate-900">Active Broadcasts</h1>
          <span data-testid="active-total"
                className="text-xs font-medium bg-slate-200 text-slate-700 rounded-full px-2 py-0.5">
            {total}
          </span>
        </div>
        <button data-testid="active-refresh" onClick={reload}
                className="flex items-center gap-2 text-sm px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-100">
          <RefreshCcw size={14} /> Refresh
        </button>
      </div>

      {/* ---- search + filters -------------------------------------------- */}
      <div className="bg-white border border-slate-200 rounded-md p-3 flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[220px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            data-testid="active-search"
            value={filters.q}
            onChange={(e) => setFilter("q", e.target.value)}
            placeholder={
              // The placeholder itself respects permissions: naming Stores or
              // broadcasters as searchable would tell somebody who cannot see
              // them that they exist as a dimension.
              mayViewTargets && mayViewOwnership
                ? "Search broadcast, broadcaster or Store…"
                : mayViewTargets
                  ? "Search broadcast or Store…"
                  : mayViewOwnership
                    ? "Search broadcast or broadcaster…"
                    : "Search broadcast…"
            }
            className="w-full pl-9 pr-3 py-1.5 text-sm border border-slate-300 rounded-md"
          />
        </div>

        <div className="flex items-center gap-1" data-testid="active-owner-filter">
          {OWNER_FILTERS.map((option) => (
            <button
              key={option.value}
              data-testid={`active-owner-${option.value}`}
              onClick={() => setFilter("owner", option.value)}
              className={`text-sm px-3 py-1.5 rounded-md border ${
                filters.owner === option.value
                  ? "bg-blue-700 text-white border-blue-700"
                  : "border-slate-300 text-slate-700 hover:bg-slate-100"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>

        <select
          data-testid="active-sort"
          value={filters.sort}
          onChange={(e) => setFilter("sort", e.target.value)}
          className="text-sm border border-slate-300 rounded-md px-2 py-1.5"
        >
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>

        <select
          data-testid="active-page-size"
          value={pageSize}
          onChange={(e) => changePageSize(Number(e.target.value))}
          className="text-sm border border-slate-300 rounded-md px-2 py-1.5"
        >
          {/* One text child, deliberately. React splits `{size} / page` into
              several children, and an <option> may contain only text. */}
          {PAGE_SIZES.map((size) => (
            <option key={size} value={size}>{`${size} / page`}</option>
          ))}
        </select>
      </div>

      {error && (
        <div role="alert" data-testid="active-error"
             className="border border-red-300 bg-red-50 text-red-800 text-sm rounded-md p-3">
          {error}
        </div>
      )}
      {notice && (
        <div role="status" data-testid="active-notice"
             className="border border-green-300 bg-green-50 text-green-900 text-sm rounded-md p-3">
          {notice}
        </div>
      )}

      {/* ---- the table ---------------------------------------------------- */}
      <div className="bg-white border border-slate-200 rounded-md overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="px-3 py-2">Status</th>
              {/* Absent, not hidden: without view_ownership the response
                  carries no owner field for another operator's broadcast. */}
              {mayViewOwnership && <th className="px-3 py-2" data-testid="col-broadcaster">Broadcaster</th>}
              <th className="px-3 py-2">Broadcast</th>
              <th className="px-3 py-2">Started</th>
              <th className="px-3 py-2">Stores</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && items.length === 0 && (
              <tr><td colSpan={6} className="px-3 py-6 text-center text-slate-500" data-testid="active-loading">
                Loading…
              </td></tr>
            )}
            {!loading && items.length === 0 && (
              <tr><td colSpan={6} className="px-3 py-6 text-center text-slate-500" data-testid="active-empty">
                No active broadcasts match this view.
              </td></tr>
            )}
            {items.map((row) => (
              <tr key={row.session_id} data-testid={`active-row-${row.session_id}`}
                  className="border-b border-slate-100 hover:bg-slate-50">
                <td className="px-3 py-2">
                  <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-red-700">
                    <span className="w-2 h-2 rounded-full bg-red-600 animate-pulse" />
                    LIVE
                  </span>
                </td>
                {mayViewOwnership && (
                  <td className="px-3 py-2" data-testid={`active-owner-cell-${row.session_id}`}>
                    {row.owner_display_name || row.owner_username || "—"}
                    {row.is_mine && <span className="ml-2 text-[10px] uppercase tracking-wide text-blue-700">you</span>}
                  </td>
                )}
                <td className="px-3 py-2 font-medium text-slate-900"
                    data-testid={`active-campaign-${row.session_id}`}>
                  {row.campaign_name || "—"}
                  {!mayViewOwnership && row.is_mine && (
                    <span className="ml-2 text-[10px] uppercase tracking-wide text-blue-700">you</span>
                  )}
                  {/* The room summary arrives ONLY when the backend decided
                      this caller may have it - the public code is a credential,
                      so it follows the broadcaster's identity. There is no
                      client-side condition here on purpose: if the key is
                      present it was authorised, and if it is absent there is
                      nothing to hide. */}
                  {row.web_room && (
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-600"
                         data-testid={`active-web-room-${row.session_id}`}>
                      <span className="font-mono font-semibold text-slate-800">
                        {row.web_room.public_code}
                      </span>
                      <button
                        data-testid={`active-copy-link-${row.session_id}`}
                        onClick={() => copyListenerLink(row.web_room.public_code)}
                        className="rounded border border-slate-300 px-1.5 py-0.5 hover:bg-slate-100">
                        Copy Link
                      </button>
                      {row.web_room.password_available ? (
                        <button
                          data-testid={`active-copy-password-${row.session_id}`}
                          onClick={() => copyText(row.web_room.password)}
                          className="rounded border border-slate-300 px-1.5 py-0.5 hover:bg-slate-100">
                          Copy Password
                        </button>
                      ) : (
                        // Only a hash exists. Asterisks would imply SpeakLink
                        // knows a value it is merely hiding.
                        <span data-testid={`active-password-state-${row.session_id}`}>
                          Password configured
                        </span>
                      )}
                      <span className="text-slate-500">
                        {row.web_room.waiting_count} waiting ·{" "}
                        {row.web_room.connected_count} connected ·{" "}
                        {row.web_room.listening_count} listening
                      </span>
                    </div>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-slate-600">{row.started_at || "—"}</td>
                <td className="px-3 py-2 text-xs" data-testid={`active-store-count-${row.session_id}`}>
                  {row.target_store_count}
                </td>
                <td className="px-3 py-2 text-right whitespace-nowrap">
                  {mayViewTargets && (
                    <button
                      data-testid={`active-view-stores-${row.session_id}`}
                      onClick={() => openStores(row)}
                      className="text-xs px-2.5 py-1 rounded-md border border-slate-300 hover:bg-slate-100"
                    >
                      View Stores
                    </button>
                  )}
                  {/* Beside View Stores, in the same compact action system.
                      Offered for your OWN broadcast too - it is your audience -
                      and otherwise only with the explicit manage permission.
                      A Link Only broadcast has no Stores, so this must not
                      depend on view_targets. */}
                  {(row.is_mine || mayManageWebAudience) && (
                    <button
                      data-testid={`active-web-audience-${row.session_id}`}
                      onClick={() => setAudienceFor(row)}
                      className="ml-2 text-xs px-2.5 py-1 rounded-md border border-slate-300 hover:bg-slate-100"
                    >
                      Web Audience
                    </button>
                  )}
                  {/* Own broadcasts are stopped from the Console, which owns
                      the microphone teardown. Offering a second Stop here
                      would leave the Console holding a live recorder. */}
                  {!row.is_mine && mayStopAny && (
                    <button
                      data-testid={`active-stop-${row.session_id}`}
                      onClick={() => { setStopError(""); setConfirmStop(row); }}
                      className="ml-2 text-xs px-2.5 py-1 rounded-md border border-red-300 text-red-700 hover:bg-red-50 inline-flex items-center gap-1"
                    >
                      <Square size={11} /> Stop
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {audienceFor && (
        <SupervisedWebAudience
          sessionId={audienceFor.session_id}
          campaignName={audienceFor.campaign_name}
          onClose={() => setAudienceFor(null)}
        />
      )}

      {/* ---- pagination ---------------------------------------------------- */}
      <div className="flex items-center justify-between text-sm">
        <div className="text-slate-600" data-testid="active-page-info">
          Page {page} of {pages || 1} · {total} broadcast{total === 1 ? "" : "s"}
        </div>
        <div className="flex gap-2">
          <button data-testid="active-prev" disabled={page <= 1}
                  onClick={() => setPage(page - 1)}
                  className="px-3 py-1.5 rounded-md border border-slate-300 disabled:opacity-40 hover:bg-slate-100">
            Previous
          </button>
          <button data-testid="active-next" disabled={page >= (pages || 1)}
                  onClick={() => setPage(page + 1)}
                  className="px-3 py-1.5 rounded-md border border-slate-300 disabled:opacity-40 hover:bg-slate-100">
            Next
          </button>
        </div>
      </div>

      {/* ---- Stores drawer -------------------------------------------------- */}
      {detailError && (
        <div role="alert" data-testid="active-detail-error"
             className="border border-red-300 bg-red-50 text-red-800 text-sm rounded-md p-3">
          {detailError}
        </div>
      )}
      {detail && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4"
             onClick={() => setDetail(null)}>
          <div data-testid="active-stores-modal"
               className="bg-white rounded-md shadow-lg max-w-md w-full p-5"
               onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <div>
                <div className="text-xs uppercase tracking-[0.15em] text-slate-500">Broadcast Stores</div>
                <div className="text-lg font-semibold text-slate-900 mt-1">
                  {detail.campaign_name || "—"}
                </div>
                {detail.owner_username && (
                  <div className="text-sm text-slate-600" data-testid="active-stores-owner">
                    {detail.owner_display_name || detail.owner_username}
                  </div>
                )}
              </div>
              <button onClick={() => setDetail(null)} data-testid="active-stores-close"
                      className="p-1 rounded hover:bg-slate-100"><X size={16} /></button>
            </div>
            <ul className="mt-4 space-y-1" data-testid="active-stores-list">
              {detail.stores.map((store) => (
                <li key={store.store_id} data-testid={`active-store-${store.store_id}`}
                    className="flex items-center gap-3 text-sm border-b border-slate-100 py-1.5">
                  <span className="font-mono text-xs font-semibold text-slate-500 w-12">
                    {store.store_code}
                  </span>
                  <span className="text-slate-900">{store.store_name}</span>
                </li>
              ))}
            </ul>
            {detail.stores.length === 0 && (
              <p className="mt-3 text-sm text-slate-500" data-testid="active-stores-empty">
                No Stores in your Store Scope are targeted by this broadcast.
              </p>
            )}
          </div>
        </div>
      )}

      {/* ---- Stop confirmation ----------------------------------------------
          Says STOP THIS BROADCAST, never "Emergency". Emergency Stop All is a
          different control with a different permission and a different blast
          radius, and an operator who confuses them silences the estate. Each
          field appears only if the caller was permitted to read it, which is
          why a supervisor with stop_any but no view_targets sees a count and
          no Store names. */}
      {confirmStop && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div data-testid="active-stop-modal" className="bg-white rounded-md shadow-lg max-w-md w-full p-5">
            <h2 className="text-lg font-bold text-slate-900">Stop this broadcast?</h2>
            <p className="mt-1 text-sm text-slate-600">
              This ends only this one broadcast. Every other live broadcast keeps running.
            </p>
            <dl className="mt-4 text-sm space-y-1">
              {mayViewOwnership && (
                <div className="flex gap-2" data-testid="stop-modal-owner">
                  <dt className="text-slate-500 w-28">Broadcaster</dt>
                  <dd className="font-medium">{confirmStop.owner_display_name || confirmStop.owner_username}</dd>
                </div>
              )}
              <div className="flex gap-2">
                <dt className="text-slate-500 w-28">Broadcast</dt>
                <dd className="font-medium">{confirmStop.campaign_name || "—"}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-slate-500 w-28">Started</dt>
                <dd>{confirmStop.started_at || "—"}</dd>
              </div>
              <div className="flex gap-2" data-testid="stop-modal-store-count">
                <dt className="text-slate-500 w-28">Stores</dt>
                <dd>{confirmStop.target_store_count}</dd>
              </div>
            </dl>
            {stopError && (
              <div role="alert" data-testid="active-stop-error"
                   className="mt-3 border border-red-300 bg-red-50 text-red-800 text-sm rounded-md p-2">
                {stopError}
              </div>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button data-testid="active-stop-cancel" onClick={() => setConfirmStop(null)}
                      className="px-3 py-1.5 text-sm rounded-md border border-slate-300 hover:bg-slate-100">
                Cancel
              </button>
              <button data-testid="active-stop-confirm" onClick={doStop} disabled={stopping}
                      className="px-3 py-1.5 text-sm rounded-md bg-red-600 hover:bg-red-700 disabled:bg-red-300 text-white font-semibold">
                {stopping ? "Stopping…" : "Stop this broadcast"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
