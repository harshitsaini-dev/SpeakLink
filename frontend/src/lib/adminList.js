/**
 * Shared state for the server-side-filtered admin lists.
 *
 * Six screens (Receiver Status, System Logs, Broadcast History, User
 * Management, Receiver Devices, Rights) all need the same shape: a filter
 * object, a page, a request that reflects both, and honest loading/error/
 * empty states. Writing that six times guarantees six subtly different
 * versions - one of which forgets to reset the page when a filter changes
 * and silently shows "no results" on page 4 of a 1-page result.
 *
 * TWO RULES THIS ENCODES
 *
 * 1. Changing a filter resets to page 1. Otherwise the operator narrows a
 *    search while on page 3 and sees an empty screen that looks like "no
 *    matches" rather than "you are past the end".
 *
 * 2. Select All Filtered is a MODE, never a materialised id list. The
 *    backend resolves the filter itself (see admin_search.py), so the UI
 *    holds the intent - "everything matching what you can see" - and never
 *    pages through thousands of rows to enumerate ids it would then send
 *    straight back.
 */
import React from "react";
import { api } from "@/lib/api";

export const DEFAULT_PAGE_SIZE = 50;

/** Strip empties so a cleared control does not send `?q=` and match nothing. */
export function activeFilters(filters) {
  const out = {};
  Object.entries(filters || {}).forEach(([key, value]) => {
    if (value === "" || value === null || value === undefined) return;
    if (value === false) return;
    out[key] = value;
  });
  return out;
}

/**
 * A detail that is safe to put on screen.
 *
 * A validation failure answers with a LIST of objects, not a sentence. Handed
 * to React as a child that threw, and the whole page went white - so the one
 * thing that was meant to explain the problem became a worse problem than the
 * one it was explaining. Anything that is not already a string is turned into
 * a sentence here, once, rather than at each of the twenty places that show
 * an error.
 */
export function asSentence(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((entry) => asSentence(entry)).filter(Boolean);
    return parts.join(" ");
  }
  if (typeof detail === "object") {
    return String(detail.msg || detail.message || detail.detail
                  || JSON.stringify(detail));
  }
  return String(detail);
}


export function countActiveFilters(filters, { ignore = [] } = {}) {
  return Object.keys(activeFilters(filters)).filter((k) => !ignore.includes(k)).length;
}

/**
 * One paginated, server-filtered list.
 *
 * `path` is a /search endpoint. `initialFilters` defines the screen's own
 * controls. Everything else is identical across screens on purpose.
 */
export function useAdminList(path, initialFilters = {},
                             { pageSize = DEFAULT_PAGE_SIZE,
                               refreshSeconds = 0 } = {}) {
  const [filters, setFiltersRaw] = React.useState(initialFilters);
  const [page, setPage] = React.useState(1);
  const [data, setData] = React.useState({ items: [], total: 0, pages: 0, has_more: false });
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [meta, setMeta] = React.useState(null);

  // Which request is the current one. Changing a filter starts a new request
  // without cancelling the old one, and responses can arrive out of order - a
  // heavier query started first can land last. Without this, choosing
  // Archived and then Active can leave Archived rows on screen under an
  // Active filter: the list and the control disagree, which reads exactly
  // like the filter being broken.
  //
  // A counter rather than AbortController on purpose: the request is already
  // in flight and the server has already done the work, so there is nothing
  // useful to cancel. What matters is only that its answer is ignored.
  const requestId = React.useRef(0);

  const load = React.useCallback(async () => {
    const mine = ++requestId.current;
    setLoading(true);
    setError("");
    try {
      const { data: body } = await api.get(path, {
        params: { ...activeFilters(filters), page, page_size: pageSize },
      });
      if (mine !== requestId.current) return;   // a newer request has taken over
      setData(body);
      setMeta(body.meta || null);
    } catch (failure) {
      if (mine !== requestId.current) return;
      setError(
        failure?.response?.status === 403
          ? "You do not have permission to view this."
          : asSentence(failure?.response?.data?.detail)
            || "This list could not be loaded. Try again."
      );
      setData({ items: [], total: 0, pages: 0, has_more: false });
    } finally {
      if (mine === requestId.current) setLoading(false);
    }
  }, [path, filters, page, pageSize]);

  React.useEffect(() => { load(); }, [load]);

  // A LIST THAT DESCRIBES THE PRESENT HAS TO KEEP LOOKING.
  //
  // Opt-in per page, because most of these lists describe records - a Store,
  // a user, an entry in the history - and re-fetching those on a timer is
  // work nobody asked for. The Announcements live status is the opposite: it
  // says what every shop is doing RIGHT NOW, including a volume somebody just
  // turned down at the till, and a page that only updates when a button is
  // pressed reports the past with a straight face.
  //
  // The tab being hidden stops it. A console left open overnight on a back
  // office screen should not poll until morning.
  React.useEffect(() => {
    if (!refreshSeconds) return undefined;
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      load();
    };
    const timer = setInterval(tick, refreshSeconds * 1000);
    return () => clearInterval(timer);
  }, [refreshSeconds, load]);

  // Any filter change returns to page 1 - see rule 1 above.
  const setFilters = React.useCallback((next) => {
    setFiltersRaw(typeof next === "function" ? next : () => next);
    setPage(1);
  }, []);

  const setFilter = React.useCallback((key, value) => {
    setFiltersRaw((current) => ({ ...current, [key]: value }));
    setPage(1);
  }, []);

  const clearFilters = React.useCallback(() => {
    setFiltersRaw(initialFilters);
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    filters, setFilters, setFilter, clearFilters,
    activeCount: countActiveFilters(filters),
    page, setPage, pageSize,
    items: data.items || [],
    total: data.total || 0,
    pages: data.pages || 0,
    hasMore: !!data.has_more,
    loading, error, meta,
    reload: load,
  };
}

/**
 * Row selection with the three modes a bulk table needs.
 *
 * "none"     - nothing selected.
 * "rows"     - an explicit set of ids (individual ticks, or Select Page).
 * "filtered" - EVERY server-side match of the current filter. Deliberately
 *              not expanded into ids: the count comes from the response's
 *              `total`, and the action sends the filter for the backend to
 *              resolve inside the caller's own scope.
 */
export function useBulkSelection({ items, total, filters }) {
  const [mode, setMode] = React.useState("none");
  const [ids, setIds] = React.useState(() => new Set());

  // A filter change invalidates a selection made under the previous one -
  // otherwise "select all filtered" could act on a filter the operator has
  // since edited, which is not what they agreed to.
  const filterKey = JSON.stringify(activeFilters(filters));
  React.useEffect(() => {
    setMode("none");
    setIds(new Set());
  }, [filterKey]);

  const toggleRow = React.useCallback((id) => {
    setMode("rows");
    setIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const selectPage = React.useCallback(() => {
    setMode("rows");
    setIds(new Set(items.map((row) => row.id ?? row.public_id)));
  }, [items]);

  const selectAllFiltered = React.useCallback(() => {
    setMode("filtered");
    setIds(new Set());
  }, []);

  const clear = React.useCallback(() => {
    setMode("none");
    setIds(new Set());
  }, []);

  const selectedCount = mode === "filtered" ? total : ids.size;

  /** The request body every bulk endpoint accepts. */
  const toRequest = React.useCallback(() => (
    mode === "filtered"
      ? { mode: "filtered", filters: activeFilters(filters) }
      : { mode: "ids", ids: Array.from(ids) }
  ), [mode, ids, filters]);

  return {
    mode, ids, selectedCount,
    isSelected: (id) => mode === "filtered" || ids.has(id),
    toggleRow, selectPage, selectAllFiltered, clear, toRequest,
    hasSelection: selectedCount > 0,
  };
}
